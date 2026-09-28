"""Conversation Summary（Plan §4.7 + docs/phase4_memory.md §6-§17）。

本模块严格保持：

    Conversation Summary
    ≠ Session Compaction
    ≠ Project Memory

三件事的隔离由：

    D1  Summary 不参与 Round 编号（不创建 round_NNN/）
    D2  Summary 不增加 Valid Round Count（不写 state.rounds）
    D3  Summary 不产生 Final Answer（不写 round_NNN/responses/<model>.md）

三条派生制品不变量（Plan §4.7.1）强制保证，全部在
``ConversationSummary.__post_init__`` 中 ``assert``。

任何 LLM 调用失败必须降级（status='failed'）而非抛异常中断流程
（Plan §1957 失败降级不阻断 + §37）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Sequence

from .model_capability import TokenEstimate
from .storage import atomic_write_text

# ── 类型 ────────────────────────────────────────────────────────────────

SummaryTrigger = Literal["user", "peer_review", "compaction"]

# ── Derived Artifact：不变量字段（Plan §4.7.1）───────────────────────────

_DERIVED_ARTIFACT_TRUE = True
_CREATES_ROUND_FALSE = False
_CONTRIBUTES_VALID_FALSE = False


@dataclass
class ConversationSummary:
    """S3.6 Conversation Summary（§11 §41 + 修订 #1 #3 #4）。

    ⚠ 必须按 model_id 严格隔离 —— 修订 #1。
       Summary ≠ Session Compaction；Compaction 是 Session 级共享，
       Summary 仅代表该模型自己的工作状态。

    Field：
      summary_id             "summary_001"
      session_id
      model_id
      source_rounds          参与总结的 Round 列表（§41）
      source_valid_round     覆盖到的最新有效 Round id（修订 #3 — 唯一复用依据）
      prompt                 实际使用的 Prompt（用户可编辑 / 系统默认）
      content                模型生成的 Summary 正文
      created_at             ISO8601 UTC
      trigger                "user" / "peer_review" / "compaction"
      status                 "success" / "failed"
      is_derived_artifact    恒 True
      creates_round          恒 False
      contributes_valid_round_count  恒 False
      input_tokens           输入侧 TokenEstimate（estimated）
      output_tokens          输出侧 TokenEstimate（estimated 或 exact）
      file_path              summary_NNN.md 相对路径，可追溯（§60）
      error_message          失败原因；status=='success' 时为 None
      reused_from            被复用时的源 summary_id；新建为 None
      reused_reason          复用 / 重生成原因（§4.7.2 判定表）
    """

    # ── 身份 ──
    summary_id: str
    session_id: str
    model_id: str
    # ── 来源与复用 ──
    source_rounds: list[int]
    source_valid_round: int
    # ── 内容 ──
    prompt: str
    content: str
    created_at: str
    trigger: SummaryTrigger
    status: Literal["success", "failed"]
    # ── 派生 artifact 不变量 ──
    is_derived_artifact: bool = _DERIVED_ARTIFACT_TRUE
    creates_round: bool = _CREATES_ROUND_FALSE
    contributes_valid_round_count: bool = _CONTRIBUTES_VALID_FALSE
    # ── 度量 ──
    input_tokens: TokenEstimate = field(
        default_factory=lambda: TokenEstimate(0, "crossllm_estimator", "estimated")
    )
    output_tokens: TokenEstimate = field(
        default_factory=lambda: TokenEstimate(0, "crossllm_estimator", "estimated")
    )
    file_path: str | None = None
    error_message: str | None = None
    reused_from: str | None = None
    reused_reason: str | None = None

    def __post_init__(self) -> None:
        # D1/D2/D3：Summary **永远不是**新的 Round
        assert self.is_derived_artifact is True, (
            f"ConversationSummary.is_derived_artifact 必须恒为 True；"
            f"got {self.is_derived_artifact!r}"
        )
        assert self.creates_round is False, (
            f"creates_round 必须恒为 False；got {self.creates_round!r}"
        )
        assert self.contributes_valid_round_count is False, (
            f"contributes_valid_round_count 必须恒为 False；"
            f"got {self.contributes_valid_round_count!r}"
        )
        # 必须派生自至少一个 Round
        assert self.source_rounds, (
            "Summary 必须派生自至少一个 Round（source_rounds 不能为空）"
        )
        # source_valid_round 必须是 source_rounds 的最大者
        assert self.source_valid_round == max(self.source_rounds), (
            f"source_valid_round({self.source_valid_round}) "
            f"必须 == max(source_rounds)({max(self.source_rounds)})"
        )

    # ── 序列化（conversation_summary 日志 + API 响应）──────────────────

    def to_dict(self) -> dict:
        return {
            "summary_id": self.summary_id,
            "session_id": self.session_id,
            "model_id": self.model_id,
            "source_rounds": list(self.source_rounds),
            "source_valid_round": self.source_valid_round,
            "is_derived_artifact": self.is_derived_artifact,
            "creates_round": self.creates_round,
            "contributes_valid_round_count": self.contributes_valid_round_count,
            "trigger": self.trigger,
            "status": self.status,
            "created_at": self.created_at,
            "input_tokens": {
                "value": self.input_tokens.value,
                "source": self.input_tokens.source,
                "accuracy_mode": self.input_tokens.accuracy_mode,
            },
            "output_tokens": {
                "value": self.output_tokens.value,
                "source": self.output_tokens.source,
                "accuracy_mode": self.output_tokens.accuracy_mode,
            },
            "reused_from": self.reused_from,
            "reused_reason": self.reused_reason,
            "file_path": self.file_path,
            "error_message": self.error_message,
            "prompt": self.prompt,
            "content": self.content,
        }


# ── SummaryReusePolicy（Plan §4.7.2 判定表 + §13/§14 触发条件）────────

class SummaryReusePolicy:
    """复用判定：避免 Peer Review 每轮重新从头总结。

    触发判定表（Plan §4.7.2 §14）：

      ┌──────────────────────────────────────────┬───────────────┬──────────────────────┐
      │ 场景                                      │ 行为            │ reused_reason         │
      ├──────────────────────────────────────────┼───────────────┼──────────────────────┤
      │ 首次生成（无 latest）                      │ 新建           │ no_existing_summary  │
      │ latest is None                            │ 新建           │ no_existing_summary  │
      │ latest.status == 'failed'                 │ 新建           │ latest_summary_failed│
      │ latest.source_valid_round >= current_latest│ 复用           │ source_valid_round_covered│
      │ current latest valid round is None        │ 新建           │ no_valid_round       │
      │ latest.source_valid_round < current_latest│ 新建（出现新 Round）│ new_valid_round_appeared│
      │ trigger == 'user'                          │ 总是新建       │ user_explicit        │
      └──────────────────────────────────────────┴───────────────┴──────────────────────┘
    """

    def should_reuse(
        self,
        latest_summary: ConversationSummary | None,
        current_latest_valid_round: int | None,
        *,
        trigger: SummaryTrigger,
    ) -> tuple[bool, str]:
        # 用户主动触发 = 总是新建（§8 "我点了总结但没变化"的失望）
        if trigger == "user":
            return False, "user_explicit"
        # peer_review / compaction：均严格按 source_valid_round 判定
        if latest_summary is None:
            return False, "no_existing_summary"
        if latest_summary.status != "success":
            return False, "latest_summary_failed"
        if current_latest_valid_round is None:
            return False, "no_valid_round"
        if latest_summary.source_valid_round >= current_latest_valid_round:
            return True, "source_valid_round_covered"
        return False, "new_valid_round_appeared"


# ── 持久化路径 + 日志 ──────────────────────────────────────────────────

def _summary_id_for_index(idx: int) -> str:
    return f"summary_{idx:03d}"


_SUMMARY_ID_RE = re.compile(r"^summary_(\d{3})\.md$")


def parse_summary_id_from_filename(name: str) -> int | None:
    m = _SUMMARY_ID_RE.match(name)
    if not m:
        return None
    return int(m.group(1))


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _estimate_text_tokens(text: str) -> TokenEstimate:
    """估算文本 token（与 ValidRound.token_estimate 同一口径：estimated）。"""
    return TokenEstimate(
        value=max(1, -(-len(text) // 3)),
        source="crossllm_estimator",
        accuracy_mode="estimated",
    )


# ── SummaryService ─────────────────────────────────────────────────────

@dataclass
class SummaryInput:
    """生成 Summary 时所需的模型自身历史（§13 §1092）。

    Summary **只**消费模型自己的 rounds（user_question + successful_answers）；
    **不**消费 peer context，避免 §16 反馈环。
    """

    round_id: int
    user_question: str
    successful_answers: dict[str, str]  # model_id -> Final Answer


class SummaryService:
    """驱动 Conversation Summary 生命周期（§11 §41 §14）。

    本服务**只写 summaries/<model_id>/** 目录与 conversation_summary 日志；
    绝不创建 round_NNN/，绝不修改 state.rounds，**绝不**触达 responses/
    （不变量 D1/D2/D3）。
    """

    def __init__(
        self,
        storage,  # core.storage.Storage（避免循环导入：Annotation only）
        provider_factory: Callable[[str], object] | None = None,
    ) -> None:
        self.storage = storage
        self._provider_factory = provider_factory
        self._policy = SummaryReusePolicy()

    # ── 持久化路径（D3：summaries/<model>/summary_NNN.md）──────────────

    def summaries_root(self, session_id: str, model_id: str) -> Path:
        d = self.storage.session_dir(session_id) / "summaries" / model_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def summary_path(
        self, session_id: str, model_id: str, summary_id: str
    ) -> Path:
        return self.summaries_root(session_id, model_id) / f"{summary_id}.md"

    def list_summaries_for_model(
        self, session_id: str, model_id: str
    ) -> list[str]:
        root = self.summaries_root(session_id, model_id)
        return sorted(p.name for p in root.glob("summary_*.md"))

    def list_all_summarised_models(self, session_id: str) -> list[str]:
        base = self.storage.session_dir(session_id) / "summaries"
        if not base.exists():
            return []
        return sorted(d.name for d in base.iterdir() if d.is_dir())

    # ── latest 读取 ────────────────────────────────────────────────

    def latest_for(
        self, session_id: str, model_id: str
    ) -> ConversationSummary | None:
        names = self.list_summaries_for_model(session_id, model_id)
        for name in reversed(names):
            sid = name[: -len(".md")]
            full = self.summary_path(session_id, model_id, sid)
            obj = self._read_frontmatter(full)
            if obj is None:
                continue
            if obj.get("status") == "success":
                return self._reconstruct(obj)
        return None

    @staticmethod
    def _read_frontmatter(path: Path) -> dict | None:
        """读 `---` 包裹的 JSON frontmatter（atomic_write 写入格式）。"""
        try:
            raw = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return None
        if not raw.startswith("---\n"):
            return None
        end = raw.find("\n---\n", 4)
        if end < 0:
            return None
        fm = raw[4:end]
        try:
            return json.loads(fm)
        except json.JSONDecodeError:
            return None

    def latest_all_models(
        self, session_id: str
    ) -> dict[str, ConversationSummary | None]:
        return {
            m: self.latest_for(session_id, m)
            for m in self.list_all_summarised_models(session_id)
        }

    # ── 主入口 ───────────────────────────────────────────────────────

    async def generate(
        self,
        *,
        session_id: str,
        model_id: str,
        history: Sequence[SummaryInput],
        trigger: SummaryTrigger,
        prompt: str | None = None,
    ) -> ConversationSummary:
        """生成 / 复用 Summary。

        步骤：
          1. 收集 source_rounds + max(source_rounds) = source_valid_round
          2. 拉 latest_summary，按 SummaryReusePolicy.should_reuse() 判定
          3. 复用 → 直接构造 ConversationSummary(reused_from=latest.summary_id)
          4. 新建 → 调 LLM（async iterator 收集），原子写文件 + 落日志
          5. 失败 → status='failed'，写文件不写
        """
        if not history:
            raise ValueError(
                "SummaryService.generate 要求至少一个 SummaryInput（§41）；"
                f"传入了 0 个"
            )
        # source_rounds 按 round_id 升序
        sorted_inputs = sorted(history, key=lambda x: x.round_id)
        source_rounds = [it.round_id for it in sorted_inputs]
        source_valid_round = source_rounds[-1]
        current_latest_valid_round = source_valid_round

        latest = self.latest_for(session_id, model_id)

        # ── 复用判定 ────────────────────────────────────────────
        reused, reason = self._policy.should_reuse(
            latest_summary=latest,
            current_latest_valid_round=current_latest_valid_round,
            trigger=trigger,
        )
        if reused and latest is not None:
            ts = _iso_now()
            reused_summary = ConversationSummary(
                summary_id=latest.summary_id,
                session_id=session_id,
                model_id=model_id,
                source_rounds=list(latest.source_rounds),
                source_valid_round=latest.source_valid_round,
                prompt=latest.prompt,
                content=latest.content,
                created_at=ts,
                trigger=trigger,
                status="success",
                input_tokens=latest.input_tokens,
                output_tokens=latest.output_tokens,
                file_path=latest.file_path,
                reused_from=latest.summary_id,
                reused_reason=reason,
            )
            self._log_event(
                session_id=session_id,
                summary=reused_summary,
                event="conversation_summary",
                reused=True,
                reused_reason=reason,
            )
            return reused_summary

        # ── 新建 ────────────────────────────────────────────────
        effective_prompt = prompt if prompt is not None else (
            self._default_prompt_for_trigger(trigger, model_id, session_id)
        )
        # 构造 user prompt：Summary Prompt + 该模型历史输入
        # ⚠ 不变量 A3（Phase 4.2）：模型可见 Prompt 中不得出现系统主动注入的
        #   真实 model_id / session_id；真实身份仅用于内部路径 / 日志 / provenance。
        history_block = self._render_history_block(sorted_inputs)
        system_header = (
            "--- 以下是该参与者在本 Session 中截至目前的全部工作记录 ---\n\n"
        )
        # A3 硬校验：只覆盖**系统生成**的部分（系统模板 + 系统头部），
        # 不覆盖用户自定义 Prompt 与用户/模型历史正文 —— 沿用 anonymizer
        # “用户输入除外”的既有校验范围约定（anonymizer.py 模块说明）。
        if prompt is None:
            self._assert_no_identity_leak(effective_prompt + system_header, model_id)
        else:
            self._assert_no_identity_leak(system_header, model_id)
        full_prompt = f"{effective_prompt}\n\n{system_header}{history_block}"

        new_index = self._next_index(session_id, model_id)
        summary_id = _summary_id_for_index(new_index)
        ts = _iso_now()

        # 调用模型；失败降级不阻断
        content, in_tokens, out_tokens, err = await self._invoke_model(
            prompt=full_prompt, model_id=model_id,
        )

        if err is not None:
            failed = ConversationSummary(
                summary_id=summary_id,
                session_id=session_id,
                model_id=model_id,
                source_rounds=list(source_rounds),
                source_valid_round=source_valid_round,
                prompt=effective_prompt,
                content="",
                created_at=ts,
                trigger=trigger,
                status="failed",
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                file_path=None,
                error_message=err,
                reused_reason=reason,
            )
            self._log_event(
                session_id=session_id,
                summary=failed,
                event="conversation_summary",
                reused=False,
                reused_reason=reason,
            )
            return failed

        # 成功：原子写文件（D3：不写 round_NNN/responses/）
        rel_path = (
            f"summaries/{model_id}/{summary_id}.md"
        )
        full_path = self.storage.session_dir(session_id) / rel_path
        meta = ConversationSummary(
            summary_id=summary_id,
            session_id=session_id,
            model_id=model_id,
            source_rounds=list(source_rounds),
            source_valid_round=source_valid_round,
            prompt=effective_prompt,
            content=content,
            created_at=ts,
            trigger=trigger,
            status="success",
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            file_path=rel_path,
            reused_reason=reason,
        )
        # 文件内容 = metadata (json frontmatter) + 正文（保留可读性）
        file_text = (
            "---\n"
            + json.dumps(meta.to_dict(), ensure_ascii=False, indent=2)
            + "\n---\n\n"
            + content
            + "\n"
        )
        atomic_write_text(full_path, file_text)

        self._log_event(
            session_id=session_id,
            summary=meta,
            event="conversation_summary",
            reused=False,
            reused_reason=reason,
        )
        return meta

    # ── Peer Review 专用入口（§13 §14；S3.7 才接 Peer Context）────────

    async def maybe_generate_for_peer_review(
        self,
        *,
        session_id: str,
        peer_model_id: str,
        valid_answer_count: int,
        history: Sequence[SummaryInput],
        trigger: SummaryTrigger = "peer_review",
    ) -> ConversationSummary | None:
        """§13 情况分流：

            valid_answer_count == 1   → 返回 None（直接用 Final Answer）
            valid_answer_count  > 1   → 调 generate()

        本方法在 S3.6 阶段**不**自动接 Peer Context（避免 §16 闭环）；
        S3.7 PeerContextPreparator 会消费本方法的返回（None ⇒ 用 Final Answer）
        """
        if valid_answer_count <= 1:
            return None
        if valid_answer_count > 1 and not history:
            # 数据不一致：声称多个有效回答但没有 history，保守起见返回 None
            return None
        return await self.generate(
            session_id=session_id,
            model_id=peer_model_id,
            history=history,
            trigger=trigger,
        )

    # ── 内部：history 块渲染 ──────────────────────────────────────

    def _render_history_block(self, items: Sequence[SummaryInput]) -> str:
        """渲染该模型自己的历史。

        ⚠ 不变量 A3：**不写真实 model_id** —— 系统输入本就只含该模型自己的
        回答（§15），因此用中性标签渲染即可，避免系统把身份注入模型可见 Prompt。
        """
        out_lines: list[str] = []
        for it in items:
            out_lines.append(f"## 第 {it.round_id} 轮")
            out_lines.append(f"**用户问题**：{it.user_question}")
            out_lines.append("**回答**：")
            answers = list(it.successful_answers.values())
            if len(answers) == 1:
                out_lines.append(answers[0])
            else:
                for i, ans in enumerate(answers, start=1):
                    out_lines.append(f"- 回答 {i}：{ans}")
            out_lines.append("")
        return "\n".join(out_lines)

    @staticmethod
    def _identity_participants(model_id: str) -> list:
        """A3 校验用参与者集合（最小适配）。

        Summary 场景中最小的身份词元集合 = 该参与者自身：
        ``ModelParticipant.identity_tokens() = (id, display_name, model)``。
        这里没有 registry，因此用 model_id 构造一个最小参与者（id/display/model
        同为 model_id），足以拦截系统模板主动注入的真实身份。
        """
        from .state import ModelParticipant

        return [
            ModelParticipant(
                id=model_id,
                display_name=model_id,
                provider="",
                model=model_id,
                enabled=True,
            )
        ]

    def _assert_no_identity_leak(self, system_visible_text: str, model_id: str) -> None:
        """Provider 调用前的 A3 守门（复用 anonymizer，不另造一套）。

        只对**系统生成**文本调用；用户 Prompt、用户问题、模型回答正文不在此列。
        """
        from .anonymizer import assert_no_identity_leak

        assert_no_identity_leak(
            system_visible_text, self._identity_participants(model_id)
        )

    def _default_prompt_for_trigger(
        self, trigger: SummaryTrigger, model_id: str, session_id: str,
    ) -> str:
        """默认 Prompt（系统生成）。

        ⚠ model_id / session_id 仅保留在签名中供内部定位；
        返回文本**不含**真实身份（不变量 A3，见 summary_prompt.py）。
        """
        from .summary_prompt import (
            DEFAULT_SUMMARY_PROMPT,
            render_peer_triggered_prompt,
        )
        if trigger == "user":
            return DEFAULT_SUMMARY_PROMPT
        # peer_review / compaction：中性模板（参数保留但不注入 Prompt）
        return render_peer_triggered_prompt(model_id, session_id)

    def _next_index(self, session_id: str, model_id: str) -> int:
        names = self.list_summaries_for_model(session_id, model_id)
        idx = 0
        for n in names:
            v = parse_summary_id_from_filename(n)
            if v is not None and v > idx:
                idx = v
        return idx + 1

    # ── 模型调用（失败降级）───────────────────────────────────────

    async def _invoke_model(
        self, *, prompt: str, model_id: str,
    ) -> tuple[str, TokenEstimate, TokenEstimate, str | None]:
        """调模型生成 Summary。

        返回 (content, input_tokens, output_tokens, error_message)。
        失败不抛异常：error_message 非 None 即视为 failed。
        """
        input_te = _estimate_text_tokens(prompt)
        if self._provider_factory is None:
            # 无 provider factory：返回 deterministic 默认 Summary
            # 用于单元测试 / Pipeline 调试
            content = (
                f"[default-summary-stub for {model_id}]\n\n"
                f"（未配置 provider_factory；当前 prompt 长度 {len(prompt)} chars。）"
            )
            return content, input_te, _estimate_text_tokens(content), None
        try:
            provider = self._provider_factory(model_id)
            buf: list[str] = []
            # 兼容 sync / async iterator：
            # RealProvider 是 async generator；MockProvider 是 sync generator
            agen = provider.generate(round_id=-1, prompt=prompt)
            if hasattr(agen, "__aiter__"):
                async for chunk in agen:
                    if chunk:
                        buf.append(chunk)
            else:
                for chunk in agen:
                    if chunk:
                        buf.append(chunk)
            content = "".join(buf)
            if not content.strip():
                return (
                    "", input_te, TokenEstimate(0, "crossllm_estimator", "estimated"),
                    "model_returned_empty_content",
                )
            out_te = _estimate_text_tokens(content)
            return content, input_te, out_te, None
        except Exception as exc:  # noqa: BLE001 - 失败降级
            return (
                "", input_te,
                TokenEstimate(0, "crossllm_estimator", "estimated"),
                f"{type(exc).__name__}: {exc}",
            )

    # ── 日志（context_policy.jsonl）────────────────────────────────

    def _log_event(
        self,
        *,
        session_id: str,
        summary: ConversationSummary,
        event: str,
        reused: bool,
        reused_reason: str,
    ) -> None:
        line = {
            "event": event,
            "session_id": session_id,
            "summary_id": summary.summary_id,
            "model_id": summary.model_id,
            "trigger": summary.trigger,
            "status": summary.status,
            "source_rounds": list(summary.source_rounds),
            "source_valid_round": summary.source_valid_round,
            "input_tokens": summary.input_tokens.value,
            "output_tokens": summary.output_tokens.value,
            "is_derived_artifact": summary.is_derived_artifact,
            "creates_round": summary.creates_round,
            "contributes_valid_round_count": (
                summary.contributes_valid_round_count
            ),
            "reused": reused,
            "reused_reason": reused_reason,
            "reused_from": summary.reused_from,
            "created_at": summary.created_at,
            "file_path": summary.file_path,
            "error_message": summary.error_message,
        }
        policy_path = (
            self.storage.session_dir(session_id) / "context_policy.jsonl"
        )
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        with policy_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    # ── 复原（读取文件时用）────────────────────────────────────────

    def _reconstruct(self, obj: dict) -> ConversationSummary:
        in_te = TokenEstimate(
            value=obj["input_tokens"]["value"],
            source=obj["input_tokens"]["source"],
            accuracy_mode=obj["input_tokens"]["accuracy_mode"],
        )
        out_te = TokenEstimate(
            value=obj["output_tokens"]["value"],
            source=obj["output_tokens"]["source"],
            accuracy_mode=obj["output_tokens"]["accuracy_mode"],
        )
        return ConversationSummary(
            summary_id=obj["summary_id"],
            session_id=obj["session_id"],
            model_id=obj["model_id"],
            source_rounds=list(obj["source_rounds"]),
            source_valid_round=obj["source_valid_round"],
            prompt=obj.get("prompt") or "",
            content=obj.get("content") or "",
            created_at=obj["created_at"],
            trigger=obj["trigger"],
            status=obj["status"],
            is_derived_artifact=obj["is_derived_artifact"],
            creates_round=obj["creates_round"],
            contributes_valid_round_count=(
                obj["contributes_valid_round_count"]
            ),
            input_tokens=in_te,
            output_tokens=out_te,
            file_path=obj.get("file_path"),
            error_message=obj.get("error_message"),
            reused_from=obj.get("reused_from"),
            reused_reason=obj.get("reused_reason"),
        )
