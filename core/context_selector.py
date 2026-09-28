"""S3.5：ContextSelector（PHASE4_REFACTOR_PLAN §4.3 / §11 / §13 / §13.3）

严格范围（不前置 S3.6+）：
  - HistoricalContextItem（§4.3）：user_question + answers 不可分
  - ContextSelector.build():
      1. Pin 优先占用 lossless_budget              §13.2
      2. Recent Raw 倒序 while 完整装载 break 超预算 §11.2 / §29
      3. dropped（旧 Round 未入 Lossless）           §59
      4. overflow 状态（pin>lossless, total>limit）  §27 / §34
  - 回填 ContextBudget.pinned_tokens / recent_raw_tokens / compacted_tokens 占位
  - 落 context_policy.jsonl: context_budget / history_selection / context_budget_warning

明确不在本模块范围：
  - Compaction 触发 / Compacted Block 实现           —— S3.8（待）
  - Summary 注入流程                                  —— S3.6
  - Peer Context                                      —— S3.7
  - Project Memory                                    —— Phase 4.6
  - 修改 round_NNN/ 任何文件                            —— 严格禁止

开工三检（2026-09-03 S3.5）：
  1. 算法消费 ContextBudget（S3.3 已定义），不重新实现预算
     → 全部 budget 字段从 compute_budget() 输入；ContextSelector **不**自己
       算 context_limit / k / reserved_output
  2. 删除 HISTORY_WINDOW=3 硬限制
     → 由 budget_lossless 驱动 Recent Raw 装载；零硬裁常量
  3. Pin 自动行为红线（§13.4 三条铁律）
     → "不自动压缩 / 不静默删除 / 不自动解除" — 编译器级 + 测试级双守门
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

# S3.10（MD §12）：事件 timestamp 复用 S3.1 的 ISO8601 helper
from core.model_capability import _iso_now
from core.context_budget import (
    ContextBudget,
    BudgetOverflowError,
    FixedInputTokens,
    compute_budget,
    make_fixed_input_tokens,
    validate_hard_constraint,
)
from core.model_capability import TokenEstimate, ProviderModelMetadata
from core.pin_store import PinState, compute_pinned_tokens
from core.valid_round import ValidRound

if TYPE_CHECKING:
    from core.storage import Storage


# ─────────────────────────────────────────────────────────────────────────────
# §4.3 HistoricalContextItem
# ─────────────────────────────────────────────────────────────────────────────

ContextSource = Literal["pinned", "recent_raw", "compacted"]


@dataclass(frozen=True)
class HistoricalContextItem:
    """§4.3 注入 Prompt 的最小渲染单元。Q 与 As 永不分离（§31 逐字禁止）。"""

    round_id: int
    user_question: str
    # [(展示名, 原文)] —— 展示名 = "你的回答" 或匿名 Candidate-X
    answers: list[tuple[str, str]]
    source: ContextSource
    token_estimate: TokenEstimate


# ─────────────────────────────────────────────────────────────────────────────
# §11 / §13 ContextSelectionResult
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ContextSelectionResult:
    """§11.3 完成一次 Context Selection 的最终结果。

    字段：
      pinned        ：lossless 优先占用的 Pin Rounds（§13.2）
      recent_raw    ：倒序 while 装入的 Recent Valid Rounds（§11.2）
      compacted     ：Compacted Block 占位（S3.5 永远为空，S3.8 才填）
      dropped       ：装不下的旧 Round（§59 不代表删除）
      overflow      ：溢出警告字典：pinned > lossless / total > context_limit
      budget        ：§4.4 完整 ContextBudget，已回填 pinned/recent_raw/compacted
                      三值（具体语义见 to_log()）
      all_items     ：按 pinned → recent_raw → compacted 顺序拼接的最终 Prompt
                      渲染序列（RoundManager 直接消费）
    """

    pinned: list[HistoricalContextItem] = field(default_factory=list)
    recent_raw: list[HistoricalContextItem] = field(default_factory=list)
    compacted: list[HistoricalContextItem] = field(default_factory=list)
    dropped: list[int] = field(default_factory=list)
    overflow: dict = field(default_factory=dict)
    budget: ContextBudget | None = None

    @property
    def all_items(self) -> list[HistoricalContextItem]:
        """按 pinned → recent_raw → compacted 顺序拼接（§13.2）。"""
        return list(self.pinned) + list(self.recent_raw) + list(self.compacted)

    def to_log(self) -> dict:
        """§57 / §59 / §61 落日志字段映射。"""
        pinned_total = self._sum_token(self.pinned)
        recent_total = self._sum_token(self.recent_raw)
        compacted_total = self._sum_token(self.compacted)
        return {
            "event": "history_selection",
            "pinned_round_ids": [it.round_id for it in self.pinned],
            "recent_raw_round_ids": [it.round_id for it in self.recent_raw],
            "compacted_round_ids": [it.round_id for it in self.compacted],
            "dropped_round_ids": list(self.dropped),
            "pinned_tokens": _token_to_log_dict(pinned_total),
            "recent_raw_tokens": _token_to_log_dict(recent_total),
            "compacted_tokens": _token_to_log_dict(compacted_total),
            "overflow": dict(self.overflow),
            "context_budget": self.budget.to_log() if self.budget else None,
        }

    @staticmethod
    def _sum_token(items: list[HistoricalContextItem]) -> TokenEstimate:
        if not items:
            return TokenEstimate(value=0, source="crossllm_estimator", accuracy_mode="estimated")
        # 估算聚合：取最低精度（§57 诚实标记）；任一 unknown → 整体 unknown
        if any(it.token_estimate.value is None for it in items):
            return TokenEstimate(value=None, source="provider_unavailable", accuracy_mode="unknown")
        any_exact = any(it.token_estimate.accuracy_mode == "exact" for it in items)
        return TokenEstimate(
            value=sum(int(it.token_estimate.value) for it in items),  # type: ignore[arg-type]
            source=("openrouter_metadata" if any_exact else "crossllm_estimator"),
            accuracy_mode=("exact" if any_exact else "estimated"),
        )


# ─────────────────────────────────────────────────────────────────────────────
# §11.2 / §13 ContextSelector
# ─────────────────────────────────────────────────────────────────────────────


# 避免循环导入；raw text → token 估算内联
def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return -(-len(text) // 3)  # ceil(len/3)


def _vr_token_to_estimate(vr: ValidRound) -> TokenEstimate:
    return vr.token_estimate


def _make_item(vr: ValidRound, *, source: ContextSource) -> HistoricalContextItem:
    """默认不区分 self / peer：全部用真实 model_id 当展示名。

    完整匿名化由 RoundManager 二次转换（anonymizer 路径）。
    本步保留 source 即可，RoundManager 把 answers 中 model_id 作为内容。
    """
    answers = [(mid, ans) for mid, ans in vr.successful_answers.items()]
    return HistoricalContextItem(
        round_id=vr.round_id,
        user_question=vr.user_question,
        answers=answers,
        source=source,
        token_estimate=vr.token_estimate,
    )


@dataclass
class ContextSelector:
    """S3.5 主类。

    §11.3 顺序：
      ValidRoundPool
       ↓ compute_budget()            §21
       ↓ compute_pinned_tokens()     §13.2
       ↓ _fill_recent_raw()         §11.2 (倒序 while break)
       ↓ overflow 警告                §27 §34
       ↓ 回填 Budget 三值             §4.4
       ↓ 落 context_policy.jsonl      §61

    不变量：
      - 不修改 valid_pool（只读聚合）
      - 不修改 pin_state（只读）
      - 不修改 raw archive（round_NNN/）
    """

    def build(
        self,
        *,
        metadata: ProviderModelMetadata | None,
        request_max_output: TokenEstimate,
        valid_pool: list[ValidRound],
        pin_state: PinState,
        fixed: FixedInputTokens | None = None,
        k: float = 0.5,
        storage: "Storage | None" = None,
        session_id: str | None = None,
        # S3.8：Compaction 产物（CompactedBlock → HistoricalContextItem）。
        # 默认 None = S3.5 行为（compacted 恒空），既有算法与测试零影响。
        compacted_items: "list[HistoricalContextItem] | None" = None,
        # S3.10（MD §57）：context_budget / history_selection 事件必须带
        # round_id —— 由 RoundManager 传入，让日志能回答"这一轮为什么
        # 保留这些历史"。None = 旧调用方（测试），行为不变。
        round_id: int | None = None,
    ) -> ContextSelectionResult:
        """§11 / §13 主入口。

        返回 ContextSelectionResult，包含 pinned/recent_raw/compacted
        三个 HistoricalContextItem 列表，以及 dropped 与 overflow。

        计费 token 三值来源：
          pinned_tokens   ← compute_pinned_tokens(pin_state, valid_pool)
          recent_raw_tokens ← _fill_recent_raw 算法聚合
          compacted_tokens ← S3.5 占位 0（S3.8 才填真实摘要）
        """
        # 1. ContextBudget（S3.3 既有，不重算）
        if fixed is None:
            fixed = make_fixed_input_tokens()
        budget = compute_budget(
            metadata=metadata,
            request_max_output=request_max_output,
            fixed=fixed,
            k=k,
        )

        # 2. Pin 优先 — 取 Pinned Rounds（valid_pool ∩ pin_state）
        pinned_vrs = [vr for vr in valid_pool if pin_state.is_pinned(vr.round_id)]
        pinned_items = [_make_item(vr, source="pinned") for vr in pinned_vrs]
        pinned_te = compute_pinned_tokens(pin_state, valid_pool)

        # 3. Remaining lossless = lossless_budget - pinned_tokens
        lossless_v = budget.lossless_budget.value
        pinned_v = pinned_te.value
        overflow: dict = {}

        if lossless_v is None or pinned_v is None:
            # 任一 unknown：保守降级 — recent_raw 整体 unknown 占用
            recent_raw_items: list[HistoricalContextItem] = []
            dropped: list[int] = []
            if lossless_v is None or pinned_v is None:
                overflow["unknown_budget_drops_recent_raw"] = True
        else:
            overflow_pinned_over_lossless = pinned_v > lossless_v
            if overflow_pinned_over_lossless:
                # §27 / §13.2：Pin 超 Lossless → 不解除 / 不压缩 / 记 warning
                overflow["pinned_history_exceeds_budget"] = True
                overflow["pinned_tokens"] = pinned_v
                overflow["lossless_budget"] = lossless_v
            remaining_lossless = max(0, lossless_v - pinned_v)

            # 4. Recent Raw 倒序 while break
            recent_raw_items, dropped = _fill_recent_raw(
                valid_pool=valid_pool,
                pinned_ids=set(pin_state.pinned_round_ids),
                remaining_lossless=remaining_lossless,
            )

            # 5. §27 第 3 步：若 total_input + reserved > limit，recent_raw 清空
            total_input = (
                (budget.fixed_input_tokens.value or 0)
                + pinned_v
                + _sum_recent(recent_raw_items)
            )
            if (
                total_input + (budget.reserved_output_tokens.value or 0)
                > (budget.context_limit.value or 0)
                and recent_raw_items
            ):
                # 不解除 Pin；recent_raw 整体让位
                dropped.extend(it.round_id for it in recent_raw_items)
                recent_raw_items = []
                overflow["recent_raw_dropped_for_pin"] = True

            # 6. §34 / §24：仍超限 → context_overflow_cannot_fit warning
            ctx_v = budget.context_limit.value
            res_v = budget.reserved_output_tokens.value
            if (
                ctx_v is not None and res_v is not None
                and (budget.fixed_input_tokens.value or 0) + pinned_v
                + _sum_recent(recent_raw_items) + res_v > ctx_v
            ):
                overflow["context_overflow_cannot_fit"] = True

        # 6.5 S3.8：消费 compaction_budget（**不重算**预算，§11.2）
        #     Compaction 产物由调用方（RoundManager / CompactionService）提供，
        #     本方法只负责按 compaction_budget 校验与裁剪。
        compacted_final: list[HistoricalContextItem] = list(compacted_items or [])
        if compacted_final:
            cb_v = budget.compaction_budget.value
            used_v = ContextSelectionResult._sum_token(compacted_final).value
            if cb_v is not None and used_v is not None and used_v > cb_v:
                overflow["compacted_exceeds_budget"] = True
                overflow["compacted_tokens"] = used_v
                overflow["compaction_budget"] = cb_v
                # 裁剪：保留较新的压缩块，超出部分让位（不静默丢弃，已记 overflow）
                kept: list[HistoricalContextItem] = []
                acc = 0
                for it in compacted_final:
                    tv = it.token_estimate.value or 0
                    if acc + tv <= cb_v:
                        kept.append(it)
                        acc += tv
                    else:
                        break
                compacted_final = kept
        compacted_te = ContextSelectionResult._sum_token(compacted_final)

        # 7. 回填 Budget 三值（pinned/recent_raw/compacted）
        budget = _with_occupancy(
            budget,
            pinned_te=pinned_te,
            recent_raw_te=ContextSelectionResult._sum_token(recent_raw_items),
            compacted_te=compacted_te,
        )

        result = ContextSelectionResult(
            pinned=pinned_items,
            recent_raw=recent_raw_items,
            compacted=compacted_final,
            dropped=dropped,
            overflow=overflow,
            budget=budget,
        )

        # 8. 落 context_policy.jsonl（§61：context_budget + history_selection）
        if storage is not None and session_id is not None:
            self._log_selection(storage, session_id, result, round_id)

        return result

    # ── 日志 ──

    @staticmethod
    def _log_selection(
        storage: "Storage",
        session_id: str,
        result: ContextSelectionResult,
        round_id: int | None = None,
    ) -> None:
        """§57 / §59 / §61：每次 build_context 必须落 context_policy.jsonl。

        单次 build 落两条事件：
          1) context_budget（§57 / §4.4 三元组全部 + Pin / Recent Raw / Compacted 三值）
          2) history_selection（§59）
        若 overflow 非空，再追一条 context_budget_warning（§34）。
        """
        session_dir = storage.session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        policy_path = session_dir / "context_policy.jsonl"

        events: list[dict] = []
        # S3.10（MD §57 §12）：预算与选择事件必须带 session_id / round_id /
        # timestamp，与 context_policy 事件一起构成可回放的核心决策链。
        ident = {
            "session_id": session_id,
            "round_id": round_id,
            "timestamp": _iso_now(),
        }
        events.append(
            {"event": "context_budget", **ident,
             **(result.budget.to_log() if result.budget else {})}
        )
        events.append({"event": "history_selection", **ident,
                       **{k: v for k, v in result.to_log().items()
                          if k != "event"}})
        if result.overflow:
            events.append({
                "event": "context_budget_warning",
                **ident,
                "overflow": dict(result.overflow),
                "pinned_round_ids": [it.round_id for it in result.pinned],
                "recent_raw_round_ids": [it.round_id for it in result.recent_raw],
            })

        _atomic_append_jsonl(policy_path, events)


# ─────────────────────────────────────────────────────────────────────────────
# 内部算法 helper
# ─────────────────────────────────────────────────────────────────────────────


def _fill_recent_raw(
    *,
    valid_pool: list[ValidRound],
    pinned_ids: set[int],
    remaining_lossless: int,
) -> tuple[list[HistoricalContextItem], list[int]]:
    """§11.2 / §29 倒序 while break 装载完整 Q+A Round。

    决策（§11.2 Custom Rule C6）：
      倒序 for，命中 pin_id 跳过；若 vr.token_estimate.value > remaining_lossless → break
      （不丢 / 不拆 / 不压缩；§59 dropped 不代表删除）。
    """
    items: list[HistoricalContextItem] = []
    dropped: list[int] = []

    for vr in reversed(valid_pool):
        if vr.round_id in pinned_ids:
            continue  # Pin 已在上一步计入
        v = vr.token_estimate.value
        if v is None:
            # 任一 unknown → 保守：直接 break（避免基于 unknown 累加错误结果）
            dropped.append(vr.round_id)
            break
        if v <= remaining_lossless:
            items.append(_make_item(vr, source="recent_raw"))
            remaining_lossless -= v
        else:
            dropped.append(vr.round_id)
            break  # §11.2 决策：break（不再继续）

    return items, dropped


def _sum_recent(items: list[HistoricalContextItem]) -> int:
    """recent_raw items 求和（token_estimate.value 部分）。unknown → 0。"""
    total = 0
    for it in items:
        if it.token_estimate.value is not None:
            total += int(it.token_estimate.value)
    return total


def _with_occupancy(
    budget: ContextBudget,
    *,
    pinned_te: TokenEstimate,
    recent_raw_te: TokenEstimate,
    compacted_te: TokenEstimate,
) -> ContextBudget:
    """回填 ContextBudget.pinned_tokens / recent_raw_tokens / compacted_tokens。

    frozen dataclass → 用 dataclasses.replace。
    """
    from dataclasses import replace
    return replace(
        budget,
        pinned_tokens=pinned_te,
        recent_raw_tokens=recent_raw_te,
        compacted_tokens=compacted_te,
    )


def _token_to_log_dict(te: TokenEstimate) -> dict:
    """§57 三元组日志字段。"""
    return {
        "value": te.value,
        "token_source": te.source,
        "token_exactness": te.accuracy_mode,
    }


def _atomic_append_jsonl(path: Path, events: list[dict]) -> None:
    """追加 events 到 JSONL 文件（同目录原子替换）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    # 读取已有内容（如果有）
    existing_lines: list[str] = []
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
            existing_lines = [ln for ln in raw.splitlines() if ln]
        except OSError:
            existing_lines = []
    # 追加
    new_lines = existing_lines + [json.dumps(e, ensure_ascii=False) for e in events]
    # 原子写：mkstemp + replace
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".tmp-", suffix=".part"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(new_lines) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
