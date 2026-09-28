"""Peer Context Preparation（docs/phase4_memory.md §12-§17 + Plan §4.8 §7 §8）。

核心语义（§12）：

    Peer Review = 让当前模型看到其他 ACTIVE 模型截至当前阶段的**主要思考状态**，
    而不是简单"拿其他模型上一轮 Final Answer"。

分流规则（§13，写死）：

    valid_answer_count == 0   → 不产生 item
    valid_answer_count == 1   → mode="final_answer"（**不**额外调 LLM）
    valid_answer_count  > 1   → mode="conversation_summary"（复用 / 生成 Summary）

合法来源**只有两种**（§16 / §43）：

    final_answer
    conversation_summary

    ⛔ Peer Review Result **绝不**允许成为 Peer Context 来源。
       这是**数据层**约束（L1/L2/L3 三层防御），不是 Prompt 文案约束：
         L1 存储层 —— Peer Context 不落盘为独立文件，磁盘上不存在可回读实体
         L2 构建层 —— ALLOWED_SOURCE_TYPES 白名单 + 无状态重建
         L3 日志层 —— peer_context_preparation 记录 mode + source_id 可审计

本模块**不持久化**任何 Peer Context（§17：Peer Context 只服务于当前 Round）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

PeerContextMode = Literal["final_answer", "conversation_summary"]


@dataclass(frozen=True)
class PeerContextItem:
    """Plan §4.8：注入给 `target_model_id` 的、来自 `source_model_id` 的一条上下文。

    Field：
      source_model_id      提供该上下文的模型（其他 ACTIVE 模型）
      mode                 "final_answer" / "conversation_summary"
      source_type          与 mode 同值；冗余字段用于日志自证（§42）
      source_id            "round_019" 或 "summary_003"
      content              实际注入的文本
      valid_answer_count   分流依据（§13）
      candidate_id         匿名化后的展示名（不变量 A3/A4）
      created_at           ISO8601 UTC
    """

    source_model_id: str
    mode: PeerContextMode
    source_type: PeerContextMode
    source_id: str
    content: str
    valid_answer_count: int
    candidate_id: str
    created_at: str

    def to_dict(self) -> dict:
        return {
            "source_model_id": self.source_model_id,
            "mode": self.mode,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "content": self.content,
            "valid_answer_count": self.valid_answer_count,
            "candidate_id": self.candidate_id,
            "created_at": self.created_at,
        }


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class PeerContextPreparator:
    """§7.3 分流 + §8 隔离。无状态：每次调用只从 Raw Archive 重建。

    依赖注入：
      storage           core.storage.Storage（读 Raw Archive / 写日志）
      summary_service   core.summary_service.SummaryService（S3.6，按 model 隔离）
      participant_ids_fn  可选：返回本轮参与者列表的函数（便于测试）
    """

    # ── L2 构建层：唯一合法的两种来源。没有第三种。──────────────────
    ALLOWED_SOURCE_TYPES = frozenset({"final_answer", "conversation_summary"})

    def __init__(
        self,
        storage,
        summary_service,
    ) -> None:
        self.storage = storage
        self._summary_service = summary_service

    # ── L2：代码级白名单 ─────────────────────────────────────────────

    def _resolve_source(self, source_type: str, source_id: str) -> str:
        """校验来源合法性。**没有第三种分支** —— 即使数据库里存了
        Peer Review Result，本函数也不提供读取它的路径（§8.2）。"""
        if source_type not in self.ALLOWED_SOURCE_TYPES:
            raise ValueError(
                f"非法 Peer Context 来源: {source_type!r}。"
                f"合法来源: {sorted(self.ALLOWED_SOURCE_TYPES)}。"
                "Peer Review Result 不得作为 Peer Context 来源"
                "（docs/phase4_memory.md §16/§43）。"
            )
        if not source_id:
            raise ValueError("PeerContextItem.source_id 不得为空（§42 可追溯）")
        # source_id 前缀必须是 round_ 或 summary_（L3 可审计约束）
        if not (source_id.startswith("round_") or source_id.startswith("summary_")):
            raise ValueError(
                f"PeerContextItem.source_id={source_id!r} 前缀非法；"
                "必须是 round_NNN 或 summary_NNN（§42 可审计）"
            )
        return source_type

    # ── 主入口 ───────────────────────────────────────────────────────

    async def prepare(
        self,
        *,
        session,
        round_state,
        valid_pool: Sequence,
    ) -> dict[str, list[PeerContextItem]]:
        """返回 `{target_model_id: [其他模型的 PeerContextItem]}`。

        步骤：
          1. peer_review=False → 返回 {}（§3.1）
          2. 单模型 → 返回 {}（§3.3，无互评对象）
          3. 对每个 target，遍历其他 ACTIVE 模型 source：
             a. 统计 source 的 valid_answer_count（从 valid_pool 数）
             b. count==0 → 跳过；==1 → final_answer；>1 → conversation_summary
             c. 构造 PeerContextItem（走 `_resolve_source` 白名单）
             d. 落 peer_context_preparation 日志（L3）
        """
        if not round_state.peer_review:
            return {}
        participants = list(round_state.participants)
        if len(participants) <= 1:
            return {}  # §3.3：单模型无互评对象

        result: dict[str, list[PeerContextItem]] = {}
        for target in participants:
            items: list[PeerContextItem] = []
            for source in participants:
                if source == target:
                    continue
                item = await self._prepare_one(
                    session=session,
                    round_state=round_state,
                    valid_pool=valid_pool,
                    target_model_id=target,
                    source_model_id=source,
                )
                if item is not None:
                    items.append(item)
            result[target] = items
        return result

    # ── 单个 source 的分流 ───────────────────────────────────────────

    async def _prepare_one(
        self,
        *,
        session,
        round_state,
        valid_pool: Sequence,
        target_model_id: str,
        source_model_id: str,
    ) -> PeerContextItem | None:
        # (a) 统计 source 在 valid_pool 中的有效回答（§13）
        hits: list[tuple[int, str]] = []  # [(round_id, answer)]
        for vr in valid_pool:
            ans = vr.successful_answers.get(source_model_id)
            if ans is not None:
                hits.append((vr.round_id, ans))
        valid_answer_count = len(hits)

        candidate_id = (round_state.candidate_ids or {}).get(source_model_id, "")

        # (b) count == 0：无有效信息，不产生 item
        if valid_answer_count == 0:
            self._log(
                session=session, round_state=round_state,
                target_model_id=target_model_id,
                source_model_id=source_model_id,
                valid_answer_count=0, mode=None, source_id=None,
                status="skipped_no_valid_answer",
                summary_reused=None, reused_reason="no_valid_answer",
            )
            return None

        # (c) count == 1：直接用唯一 Final Answer，**不**调 LLM
        if valid_answer_count == 1:
            rid, ans = hits[0]
            source_id = f"round_{rid:03d}"
            self._resolve_source("final_answer", source_id)
            self._log(
                session=session, round_state=round_state,
                target_model_id=target_model_id,
                source_model_id=source_model_id,
                valid_answer_count=1, mode="final_answer", source_id=source_id,
                status="success",
                summary_reused=None, reused_reason="single_valid_answer",
            )
            return PeerContextItem(
                source_model_id=source_model_id,
                mode="final_answer",
                source_type="final_answer",
                source_id=source_id,
                content=ans,
                valid_answer_count=1,
                candidate_id=candidate_id,
                created_at=_iso_now(),
            )

        # (d) count > 1：调 SummaryService（S3.6 内部处理复用 / 生成）
        history = self._build_summary_inputs(
            valid_pool=valid_pool, source_model_id=source_model_id,
        )
        summary = await self._summary_service.maybe_generate_for_peer_review(
            session_id=session.session_id,
            peer_model_id=source_model_id,
            valid_answer_count=valid_answer_count,
            history=history,
            trigger="peer_review",
        )

        if summary is not None and summary.status == "success":
            source_id = summary.summary_id
            self._resolve_source("conversation_summary", source_id)
            reused = summary.reused_from is not None
            self._log(
                session=session, round_state=round_state,
                target_model_id=target_model_id,
                source_model_id=source_model_id,
                valid_answer_count=valid_answer_count,
                mode="conversation_summary", source_id=source_id,
                status="success",
                summary_reused=reused,
                reused_reason=summary.reused_reason,
            )
            return PeerContextItem(
                source_model_id=source_model_id,
                mode="conversation_summary",
                source_type="conversation_summary",
                source_id=source_id,
                content=summary.content,
                valid_answer_count=valid_answer_count,
                candidate_id=candidate_id,
                created_at=_iso_now(),
            )

        # (e) Summary 失败 / 缺失 → 降级到最新一轮 Final Answer（§7.3）
        latest_rid, latest_ans = hits[-1]
        source_id = f"round_{latest_rid:03d}"
        self._resolve_source("final_answer", source_id)
        self._log(
            session=session, round_state=round_state,
            target_model_id=target_model_id,
            source_model_id=source_model_id,
            valid_answer_count=valid_answer_count,
            mode="final_answer", source_id=source_id,
            status="degraded_peer_summary_failed",
            summary_reused=None,
            reused_reason=(
                "peer_summary_failed_degraded_to_final_answer"
                if summary is not None else "no_summary_service"
            ),
        )
        return PeerContextItem(
            source_model_id=source_model_id,
            mode="final_answer",
            source_type="final_answer",
            source_id=source_id,
            content=latest_ans,
            valid_answer_count=valid_answer_count,
            candidate_id=candidate_id,
            created_at=_iso_now(),
        )

    # ── Summary 输入：只含 source 模型自己的历史（§15 / §1092）────────

    def _build_summary_inputs(self, *, valid_pool: Sequence, source_model_id: str):
        """构造 SummaryService 输入 —— **只**含 source_model_id 自己。

        不包含：
          ❌ 其他模型的 Final Answer
          ❌ 其他模型的 Summary
          ❌ 任何 Peer Review Result
        """
        from .summary_service import SummaryInput

        inputs = []
        for vr in valid_pool:
            ans = vr.successful_answers.get(source_model_id)
            if ans is None:
                continue
            inputs.append(SummaryInput(
                round_id=vr.round_id,
                user_question=vr.user_question,
                successful_answers={source_model_id: ans},
            ))
        return inputs

    # ── L3 日志层：peer_context_preparation（§42 / Plan §6 row 6）──────

    def _log(
        self,
        *,
        session,
        round_state,
        target_model_id: str,
        source_model_id: str,
        valid_answer_count: int,
        mode: str | None,
        source_id: str | None,
        status: str,
        summary_reused: bool | None,
        reused_reason: str | None,
    ) -> None:
        line = {
            "event": "peer_context_preparation",
            "session_id": session.session_id,
            "round_id": round_state.round_id,
            "target_model_id": target_model_id,
            "source_model_id": source_model_id,
            "valid_answer_count": valid_answer_count,
            "mode": mode,
            "source_id": source_id,
            "status": status,
            # 修订 #3：复用标记，便于审计"为何这次没调 LLM"
            "summary_reused": summary_reused,
            "reused_reason": reused_reason,
            "timestamp": _iso_now(),
        }
        policy_path = (
            self.storage.session_dir(session.session_id) / "context_policy.jsonl"
        )
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        with policy_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
