"""S3.3：ContextBudget + compute_budget()（phase4_memory.md §21–§26 / PHASE4_REFACTOR_PLAN §4.4 / §10）

严格范围：
  - ContextBudget 数据结构（frozen dataclass）
  - compute_budget() 纯函数：按 §21 公式构造 ContextBudget
  - §22 三值分离（model_max_output / request_max_output / reserved_output）
  - §八 平铺日志字段（含 source / fetch_status / budget_status）

明确不在本模块范围：
  - Pin / is_pinned 接线                       —— S3.4
  - ContextSelector / 选择算法                  —— S3.5
  - Summary / Compaction / Peer Context          —— S3.6+
  - 能力获取（Provider 元数据 → YAML fallback）   —— core/model_capability.py
  - real_provider max_output_tokens 接线         —— S3.11

铁律：
  1. k **只做预算分配**，不做触发阈值
     → k 只允许出现在 ``lossless_budget = history_budget × k`` 一行
  2. reserved_output_tokens **不得** 由任何经验系数得出
     → 由 ``min(model_max_output, request_max_output)`` 计算（§22）
     → 禁止 ``context_limit * 0.9`` / 80% / 固定 16000 / 固定 8192
  3. reserved_output 未知时 **不得伪造数值**
     → 记 unknown，ContextBudget.budget_status 变为 "degraded"（§五 / §七）
  4. TokenEstimate 语义保持（S3.1 不变量）
     → 三元组 + accuracy_mode 三态，不把估算当精确

已**删除**（相对 V2）：
  - ``should_compact`` （80% 阈值触发压缩）
  - ``DEFAULT_COMPACTION_RATIO = 0.8`` （触发阈值常量）
  - ``estimate_prompt_tokens`` / ``PromptTokenBreakdown``
触发压缩的判断不在本模块范围，由 S3.5 ContextSelector 在 history 装不下时接管（§38）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.model_capability import FetchStatus, TokenEstimate

if TYPE_CHECKING:
    from core.model_capability import ProviderModelMetadata


# ─────────────────────────────────────────────────────────────────────────────
# 内联常量与工具（刻意避免循环导入：不从 round_manager 引入）
# ─────────────────────────────────────────────────────────────────────────────

# 字符 → Token 粗略换算（CHARS_PER_TOKEN=3，向上取整）。
# 与 round_manager.estimate_tokens 完全一致；刻意保留两份以避免循环导入。
CHARS_PER_TOKEN = 3


def _estimate_tokens(text: str) -> int:
    """向上取整的 len // CHARS_PER_TOKEN（V2 风格延续）。空文本 = 0。"""
    if not text:
        return 0
    return -(-len(text) // CHARS_PER_TOKEN)


# k 用户可调区间（§20 / §55）。
K_MIN = 0.01
K_MAX = 0.90
K_DEFAULT = 0.5


# budget_status 取值（§七 / §八）：
#   complete —— context_limit / fixed / reserved 三值全部已知，
#               ``input + reserved ≤ context_limit`` 可被完整证明
#   degraded —— 任一为 unknown，硬约束无法证明（**不得**用假设值补齐）
BUDGET_COMPLETE = "complete"
BUDGET_DEGRADED = "degraded"


# ─────────────────────────────────────────────────────────────────────────────
# §4.4 ContextBudget
# ─────────────────────────────────────────────────────────────────────────────


# 仅内部使用：fixed_input_tokens 的分量明细。
# S3.5 ContextSelector._estimate_fixed_input() 会构造该对象后传入 compute_budget()。
@dataclass(frozen=True)
class FixedInputTokens:
    """fixed_input_tokens 的四分量（§21）。每个分量必须能各自回答"从哪来"。"""

    system: TokenEstimate
    tool: TokenEstimate
    current_user: TokenEstimate
    peer_context_reserve: TokenEstimate

    @property
    def total_value(self) -> int | None:
        """四个 TokenEstimate 值求和。

        任一为 unknown（None）则结果为 None —— 不装作精确。
        不变量：fixed_input_tokens.value is None ⟺ 任一分量 unknown。
        """
        values = [self.system.value, self.tool.value,
                  self.current_user.value, self.peer_context_reserve.value]
        if any(v is None for v in values):
            return None
        return sum(values)  # type: ignore[arg-type]

    def to_log(self) -> dict:
        return {
            "system": self.system.to_log(),
            "tool": self.tool.to_log(),
            "current_user": self.current_user.to_log(),
            "peer_context_reserve": self.peer_context_reserve.to_log(),
        }


# 仅内部使用：请求侧 / Provider 侧的实际预留计算结果。
# 接线方按 §9.2 公式预构造；此处只封装三值语义不变量。
@dataclass(frozen=True)
class ReservedOutputTokens:
    """model_max_output / request_max_output / reserved_output 三值（§22 / §9.2）。

    不变量（__post_init__ 强制）：
      R1. reserved.value <= model_max_output.value 与 <= request_max_output.value 同时成立
      R2. 任一为 unknown（None）则 reserved 退化为另一侧已知值（见 §9.2 三个分支）
    """

    model_max_output: TokenEstimate
    request_max_output: TokenEstimate
    reserved_output_tokens: TokenEstimate

    def __post_init__(self) -> None:
        p = self.model_max_output.value
        r = self.request_max_output.value
        v = self.reserved_output_tokens.value

        if v is not None:
            if p is not None and v > p:
                raise ValueError(
                    f"ReservedOutputTokens 不变量 R1 违反：reserved={v} > "
                    f"model_max_output={p}（§22 reserved ≤ model_max_output）"
                )
            if r is not None and v > r:
                raise ValueError(
                    f"ReservedOutputTokens 不变量 R1 违反：reserved={v} > "
                    f"request={r}（§22 reserved ≤ request_max_output）"
                )


# ─────────────────────────────────────────────────────────────────────────────
# §4.4 ContextBudget 主数据结构
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ContextBudget:
    """本轮 Context Budget 完整结果（§4.4 / §21 / §24 / §八）。

    字段分组：
      ── 模型能力（§22 / §三）──
      context_limit                ：模型上下文窗口（Provider 元数据优先，YAML 仅 fallback）
      model_max_output             ：模型**能**输出多少（只来自 Provider 元数据）
      ── 本次请求（§22 / §四）──
      request_max_output    ：本次请求**要求**最多输出多少（未显式指定 = unknown）
      reserved_output_tokens       ：本次预算**实际**预留多少（= min(model_max, request)）
      ── 固定输入（§21）──
      system_tokens / tool_tokens / current_user_tokens / peer_context_reserve
      fixed_input_tokens           ：= system + tool + current_user + peer_reserve
      ── 历史预算（§21）──
      history_budget               ：= context_limit − fixed − reserved
      ── k 分配（§20 / §33）──
      k                            ：0.01 ~ 0.90（**只**影响 lossless / compaction）
      lossless_budget              ：= history_budget × k
      compaction_budget            ：= history_budget × (1 − k)
      ── 实际占用（回填）──
      pinned_tokens / recent_raw_tokens / compacted_tokens

    不变量：
      B1. (fixed + history_used + reserved) ≤ context_limit  —— §24 硬约束
      B2. lossless_budget + compaction_budget == history_budget
      B3. k ∈ [K_MIN, K_MAX]
      B4. budget_status == "complete" ⟹ context_limit / fixed / reserved 三值非 None
    """

    # ── 模型能力 ──
    context_limit: TokenEstimate

    # ── 本次请求 ──
    request_max_output: TokenEstimate

    # ── 实际预留（model_max_output + reserved_output_tokens）──
    model_max_output: TokenEstimate
    reserved_output_tokens: TokenEstimate

    # ── 固定输入 ──
    system_tokens: TokenEstimate
    tool_tokens: TokenEstimate
    current_user_tokens: TokenEstimate
    peer_context_reserve: TokenEstimate
    fixed_input_tokens: TokenEstimate

    # ── 历史预算 ──
    history_budget: TokenEstimate

    # ── k 分配 ──
    k: float
    lossless_budget: TokenEstimate
    compaction_budget: TokenEstimate

    # ── 实际占用 ──
    pinned_tokens: TokenEstimate
    recent_raw_tokens: TokenEstimate
    compacted_tokens: TokenEstimate

    # ── 能力查询状态（§一 / §八）──
    # 由 compute_budget(metadata=...) 从 ProviderModelMetadata.fetch_status 带入
    context_limit_fetch_status: FetchStatus = "not_fetched"

    def __post_init__(self) -> None:
        # B3: k 必须在规定区间
        if not (K_MIN - 1e-9 <= self.k <= K_MAX + 1e-9):
            raise ValueError(
                f"ContextBudget 不变量 B3 违反：k={self.k} 不在 [{K_MIN}, {K_MAX}] 区间"
            )

        # B2: lossless + compaction == history_budget
        hb = self.history_budget.value
        lb = self.lossless_budget.value
        cb = self.compaction_budget.value
        if hb is not None and lb is not None and cb is not None:
            if lb + cb != hb:
                raise ValueError(
                    f"ContextBudget 不变量 B2 违反：lossless({lb}) + "
                    f"compaction({cb}) ≠ history_budget({hb})"
                )

    # ── §七 / §八 硬约束可证明性 ──

    @property
    def budget_status(self) -> str:
        """§七：reserved / context_limit / fixed 全部已知才谈得上"完整硬约束证明"。

        任一为 unknown → "degraded"。**不**用假设值补齐（§五：不得伪造数值）。
        """
        if (
            self.context_limit.value is None
            or self.reserved_output_tokens.value is None
            or self.fixed_input_tokens.value is None
        ):
            return BUDGET_DEGRADED
        return BUDGET_COMPLETE

    # ── §八 日志序列化（平铺字段）──

    def to_log(self) -> dict:
        """§八：至少记录下列平铺字段（值直接是数字 / null，不再是嵌套三元组）。

        context_limit              context_limit_source        context_limit_fetch_status
        model_max_output           model_max_output_source
        request_max_output         request_max_output_source
        reserved_output_tokens     budget_status
        fixed_input_tokens         history_budget             k
        lossless_budget            compaction_budget
        pinned_tokens              recent_raw_tokens          compacted_tokens
        system_tokens / tool_tokens / current_user_tokens / peer_context_reserve
        """
        return {
            # ── 模型能力 ──
            "context_limit": self.context_limit.value,
            "context_limit_source": self.context_limit.source,
            "context_limit_fetch_status": self.context_limit_fetch_status,
            "model_max_output": self.model_max_output.value,
            "model_max_output_source": self.model_max_output.source,
            # ── 本次请求 ──
            "request_max_output": self.request_max_output.value,
            "request_max_output_source": self.request_max_output.source,
            "reserved_output_tokens": self.reserved_output_tokens.value,
            # ── 状态 ──
            "budget_status": self.budget_status,
            # ── 固定输入（§21）──
            "fixed_input_tokens": self.fixed_input_tokens.value,
            "system_tokens": self.system_tokens.value,
            "tool_tokens": self.tool_tokens.value,
            "current_user_tokens": self.current_user_tokens.value,
            "peer_context_reserve": self.peer_context_reserve.value,
            # ── 历史预算 ──
            "history_budget": self.history_budget.value,
            # ── k 分配 ──
            "k": self.k,
            "lossless_budget": self.lossless_budget.value,
            "compaction_budget": self.compaction_budget.value,
            # ── 实际占用 ──
            "pinned_tokens": self.pinned_tokens.value,
            "recent_raw_tokens": self.recent_raw_tokens.value,
            "compacted_tokens": self.compacted_tokens.value,
        }


# ─────────────────────────────────────────────────────────────────────────────
# §10 compute_budget() 纯函数
# ─────────────────────────────────────────────────────────────────────────────


def _token(value: int, source: str, accuracy: str) -> TokenEstimate:
    """helper：快速构造 TokenEstimate。"""
    return TokenEstimate(value=value, source=source, accuracy_mode=accuracy)  # type: ignore[arg-type]


def _unknown() -> TokenEstimate:
    """helper：未知值（value=None, accuracy=unknown）。"""
    return TokenEstimate(value=None, source="provider_unavailable", accuracy_mode="unknown")


def compute_budget(
    *,
    metadata: "ProviderModelMetadata | None",
    request_max_output: TokenEstimate,
    fixed: FixedInputTokens,
    k: float = K_DEFAULT,
) -> ContextBudget:
    """按 §21 / §22 公式构造 ContextBudget（纯函数，无 I/O）。

    公式（§21）：
        history_budget   = context_limit − fixed_input_tokens − reserved_output_tokens
        lossless_budget   = history_budget × k
        compaction_budget = history_budget × (1 − k)

    reserved_output_tokens 由 §9.2 三个分支组装：
        1) model_max unknown + request 已知  → reserved = request（标 estimated）
        2) model_max 已知  + request unknown → reserved = model_max（透传能力上限）
        3) 双侧已知                          → reserved = min(model_max, request)
        4) 双侧 unknown                      → reserved = unknown
           → budget_status = "degraded"，hard constraint 不可证明（§五 / §七）

    入参：
      - metadata: ProviderModelMetadata 或 None（取不到时全 unknown）
      - request_max_output: 本次请求 output 上限的 TokenEstimate
      - fixed: FixedInputTokens 四分量
      - k: 用户可调，K_MIN..K_MAX

    出参：
      ContextBudget，所有 TokenEstimate 三元组 value/source/accuracy_mode 完整。

    不强制：
      - B1 硬约束由调用方校验（S3.5 ContextSelector 在 build_context() 末尾 assert）
      - 不写日志（调用方决定是否落 context_jsonl）
    """
    # ── 1. context_limit / model_max_output（能力侧，只读 metadata）──
    if metadata is not None:
        ctx_limit_te = metadata.context_limit
        model_max_te = metadata.model_max_output
        fetch_status: FetchStatus = metadata.fetch_status
    else:
        ctx_limit_te = _unknown()
        model_max_te = _unknown()
        fetch_status = "not_fetched"

    # ── 2. reserved_output_tokens 组装（§9.2 四分支）──
    p_val = model_max_te.value
    r_val = request_max_output.value

    if p_val is None and r_val is None:
        # 双 unknown → reserved 未知（**不伪造**，§五）
        reserved_te = _unknown()
    elif p_val is None:
        # 仅 model_max unknown → 用 request（本次请求显式上限），标 estimated
        reserved_te = TokenEstimate(
            value=r_val,
            source=request_max_output.source,
            accuracy_mode="estimated",
        )
    elif r_val is None:
        # 仅 request unknown → 本次请求的有效输出上限 = 模型能力上限（透传）
        reserved_te = model_max_te
    else:
        # 双侧已知 → min(model_max, request)
        # accuracy 取两者最低（exact + estimated → estimated）
        if (
            model_max_te.accuracy_mode == "exact"
            and request_max_output.accuracy_mode == "exact"
        ):
            min_acc: str = "exact"
        else:
            min_acc = "estimated"
        reserved_val = min(p_val, r_val)
        reserved_te = TokenEstimate(
            value=reserved_val,
            source="openrouter_metadata" if min_acc == "exact" else "config_static",
            accuracy_mode=min_acc,  # type: ignore[arg-type]
        )

    # ── 3. fixed_input_tokens 组装（sum 四分量）──
    parts = [fixed.system, fixed.tool, fixed.current_user, fixed.peer_context_reserve]
    if any(p.value is None for p in parts):
        fixed_total_te = _unknown()
    else:
        # 全已知：accuracy 取四分量最低（保守）；source 用最低分量源
        min_acc = "exact"
        chosen_source = "openrouter_metadata"
        for p in parts:
            p_acc = p.accuracy_mode
            if p_acc == "estimated":
                min_acc = "estimated"
                chosen_source = p.source
            elif p_acc == "unknown":
                min_acc = "estimated"
                chosen_source = "crossllm_estimator"
        fixed_total_te = TokenEstimate(
            value=sum(p.value for p in parts),  # type: ignore[arg-type]
            source=chosen_source,  # type: ignore[arg-type]
            accuracy_mode=min_acc,  # type: ignore[arg-type]
        )

    # ── 4. history_budget = context_limit − fixed − reserved（§21）──
    ctx_v = ctx_limit_te.value
    if ctx_v is None or fixed_total_te.value is None or reserved_te.value is None:
        # 任一 unknown → history_budget 也是 unknown（不装作精确）
        history_te = _unknown()
    else:
        hist_val = ctx_v - fixed_total_te.value - reserved_te.value
        if hist_val < 0:
            # §24 硬约束违反前置：记录实际算出的值（不静默 clamp 到 0），便于排查
            history_te = TokenEstimate(
                value=hist_val,
                source="crossllm_estimator",
                accuracy_mode="estimated",
            )
        else:
            history_te = TokenEstimate(
                value=hist_val,
                source=ctx_limit_te.source,
                accuracy_mode=ctx_limit_te.accuracy_mode,
            )

    # ── 5. lossless / compaction = history_budget × k / (1−k) ──
    hb_v = history_te.value
    if hb_v is None:
        lossless_te = _unknown()
        compaction_te = _unknown()
    else:
        lossless_val = int(hb_v * k)
        compaction_val = hb_v - lossless_val
        lossless_te = TokenEstimate(
            value=lossless_val,
            source=history_te.source,
            accuracy_mode=history_te.accuracy_mode,
        )
        compaction_te = TokenEstimate(
            value=compaction_val,
            source=history_te.source,
            accuracy_mode=history_te.accuracy_mode,
        )

    # ── 6. 实际占用占位（调用方回填）──
    if history_te.value is None:
        pinned_te: TokenEstimate = _unknown()
        recent_raw_te: TokenEstimate = _unknown()
        compacted_te: TokenEstimate = _unknown()
    else:
        pinned_te = TokenEstimate(value=0, source="crossllm_estimator", accuracy_mode="estimated")
        recent_raw_te = TokenEstimate(value=0, source="crossllm_estimator", accuracy_mode="estimated")
        compacted_te = TokenEstimate(value=0, source="crossllm_estimator", accuracy_mode="estimated")

    return ContextBudget(
        # 模型能力
        context_limit=ctx_limit_te,
        # 请求侧
        request_max_output=request_max_output,
        # 三值（model_max + reserved）
        model_max_output=model_max_te,
        reserved_output_tokens=reserved_te,
        # 固定输入（§21）
        system_tokens=fixed.system,
        tool_tokens=fixed.tool,
        current_user_tokens=fixed.current_user,
        peer_context_reserve=fixed.peer_context_reserve,
        fixed_input_tokens=fixed_total_te,
        # 历史预算
        history_budget=history_te,
        # k 分配
        k=k,
        lossless_budget=lossless_te,
        compaction_budget=compaction_te,
        # 实际占用（空位）
        pinned_tokens=pinned_te,
        recent_raw_tokens=recent_raw_te,
        compacted_tokens=compacted_te,
        # 能力查询状态（§一 / §八）
        context_limit_fetch_status=fetch_status,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 便利构造（供接线方 / 测试用）
# ─────────────────────────────────────────────────────────────────────────────


def make_fixed_input_tokens(
    *,
    system_text: str = "",
    tool_text: str = "",
    current_user_text: str = "",
    peer_context_reserve_text: str = "",
) -> FixedInputTokens:
    """用纯文本构造 FixedInputTokens；所有分量都标 ``crossllm_estimator / estimated``。"""
    def est(text: str) -> TokenEstimate:
        v = _estimate_tokens(text)
        return TokenEstimate(value=v, source="crossllm_estimator", accuracy_mode="estimated")

    return FixedInputTokens(
        system=est(system_text),
        tool=est(tool_text),
        current_user=est(current_user_text),
        peer_context_reserve=est(peer_context_reserve_text),
    )


def validate_hard_constraint(budget: ContextBudget) -> None:
    """§24 硬约束 ``input + history_used + reserved ≤ context_limit``。

    仅当四个 TokenEstimate 全部有值（value 非 None）时校验；缺数据不校验
    （unknown 路径由 ``budget_status == "degraded"`` 暴露，不在此处虚构）。
    失败 → 抛 ``BudgetOverflowError``（ContextSelector 捕获后触发 Recursive Compaction）。
    """
    ctx = budget.context_limit.value
    used = budget.recent_raw_tokens.value
    res = budget.reserved_output_tokens.value
    fx = budget.fixed_input_tokens.value
    if ctx is None or used is None or res is None or fx is None:
        return  # 缺数据不校验（unknown 路径）
    if fx + used + res > ctx:
        raise BudgetOverflowError(
            f"§24 硬约束违反：fixed({fx}) + used({used}) + reserved({res}) "
            f"> context_limit({ctx})"
        )


class BudgetOverflowError(Exception):
    """§24 硬约束违反。ContextSelector 捕获后触发 Recursive Compaction。"""
    pass
