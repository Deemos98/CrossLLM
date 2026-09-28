"""Phase 4 核心单元测试（合并文件）。

本文件由下列 4 个碎片化测试文件合并而成（源码段原样搬迁，未改写任何断言）：

  1) 原 test_phase4_budget.py             —— ContextBudget 预算与硬约束
  2) 原 test_phase4_context_selection.py  —— Context Selection（k / Pin / 降序填充）
  3) 原 test_valid_round.py               —— ValidRound + ValidRoundPoolBuilder
  4) 原 test_model_capability.py          —— ModelCapabilityResolver + TokenEstimate

合并只做物理归并：不新增测试、不删测试、不改断言；各段仍按原文件中的
`Test*` 类组织。两个原模块级 autouse fixture（`_reset_response_seed`、
`_restore_async_client`）合并后对本文件所有用例生效——两者都只做
「清理自己模块的测试用注册表 / 还原 httpx.AsyncClient」，不改变其他段的语义。
"""

from __future__ import annotations

from __future__ import annotations
import ast
import asyncio
import json
from pathlib import Path
import time
from typing import Any
import httpx
import pytest
from core.context_budget import (
    K_DEFAULT,
    K_MAX,
    K_MIN,
    BudgetOverflowError,
    ContextBudget,
    FixedInputTokens,
    compute_budget,
    make_fixed_input_tokens,
    validate_hard_constraint,
)
from core.model_capability import ProviderModelMetadata, TokenEstimate
from core.context_budget import (
    FixedInputTokens,
    make_fixed_input_tokens,
    _token,
)
from core.context_selector import (
    ContextSelectionResult,
    ContextSelector,
    HistoricalContextItem,
)
from core.pin_store import PinStore
from core.prompt_builder import (
    build_round_n_prompt_v2,
)
from core.state import RoundState, SessionState
from core.storage import Storage
from core.valid_round import ValidRoundPoolBuilder
from core.model_capability import TokenEstimate
from core.valid_round import ValidRound, ValidRoundPoolBuilder
from core.model_capability import (
    ModelCapabilityResolver,
    ProviderModelMetadata,
    TokenEstimate,
)

# ══════════════════════════════════════════════════════════════════════════
# 1) ContextBudget：预算公式 / 三重不变量 / 硬约束 / 日志序列化 （原 test_phase4_budget.py）
# ══════════════════════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────────────────────
# Helper：构造 ProviderModelMetadata（精简版，避开 S3.1 网络/缓存）
# ─────────────────────────────────────────────────────────────────────────────


def _exact(v: int, source: str = "openrouter_metadata") -> TokenEstimate:
    """精度刚好 exact + 来源可信（Provider 报告或 tokenizer）。"""
    return TokenEstimate(v, source, "exact")


def _estim(v: int, source: str = "crossllm_estimator") -> TokenEstimate:
    """精度 estimated（粗略估算）。"""
    return TokenEstimate(v, source, "estimated")


def _unknown() -> TokenEstimate:
    return TokenEstimate(None, "provider_unavailable", "unknown")


def _metadata(
    *,
    ctx: int | None = 131072,
    provider_max: int | None = 32768,
) -> ProviderModelMetadata:
    """构造 ProviderModelMetadata。None → unknown。"""
    return ProviderModelMetadata(
        model_slug="fake/model",
        context_limit=(
            _exact(ctx) if ctx is not None else _unknown()
        ),
        model_max_output=(
            _exact(provider_max) if provider_max is not None else _unknown()
        ),
        fetched_at="2026-09-03T00:00:00Z",
        fetch_status="ok",
    )


def _budget_with_unknown_ctx() -> ProviderModelMetadata:
    """metadata 为 None 的场景（取不到任何 Provider 元数据）。"""
    return None  # type: ignore[return-value]


def _fixed(
    *,
    sys: int = 1000,
    tool: int = 4000,
    user: int = 500,
    peer: int = 1500,
) -> FixedInputTokens:
    """fixed_input_tokens 四分量；估算精度。"""
    return FixedInputTokens(
        system=_estim(sys),
        tool=_estim(tool),
        current_user=_estim(user),
        peer_context_reserve=_estim(peer),
    )


# ─────────────────────────────────────────────────────────────────────────────
# §4.4 ContextBudget 三元组不变量（K1-K4）
# ─────────────────────────────────────────────────────────────────────────────


class TestContextBudgetTripleInvariants:
    """§4.4 / §9.2 三值分离语义：provider / request / reserved 必须分开。"""

    def test_K1_provider_request_reserved_three_distinct_fields(self):
        """K1：provider 与 request 必须分开存在，不允许合并成一个 'max_output'。"""
        meta = _metadata(ctx=131072, provider_max=32768)
        # request 来自 models.yaml（S3.1 #3 不变量：config_static 必须配 estimated）
        req = _estim(16384, source="config_static")
        fx = _fixed()
        budget = compute_budget(
            metadata=meta, request_max_output=req, fixed=fx, k=0.5,
        )
        # 三个独立字段，且均存在
        assert budget.model_max_output is not None
        assert budget.request_max_output is not None
        assert budget.reserved_output_tokens is not None
        # 命名一致性（修订 #2 不退化为旧 model_max_output 命名）
        assert hasattr(budget, "model_max_output")
        assert hasattr(budget, "request_max_output")
        assert hasattr(budget, "reserved_output_tokens")

    def test_K2_reserved_le_provider_and_request(self):
        """K2：reserved ≤ provider 且 reserved ≤ request（取 min）。"""
        meta = _metadata(ctx=131072, provider_max=32768)
        req = _estim(16384, source="config_static")
        fx = _fixed()
        budget = compute_budget(
            metadata=meta, request_max_output=req, fixed=fx, k=0.5,
        )
        p = budget.model_max_output.value
        r = budget.request_max_output.value
        v = budget.reserved_output_tokens.value
        assert v == min(p, r) == 16384
        assert v <= p
        assert v <= r

    def test_K3_reserved_min_not_hardcoded_16000(self):
        """K3（修订 #2）：reserved 来自 min(provider, request)，**不可**是固定 16000。

        T21 反固定值：构造 provider=32768 / request=8000 与 provider=8000 /
        request=32768 两个组合，断言两个 reserved 都等于 8000（min）。
        """
        fx = _fixed()

        # 组合 A：provider 远大，request 较小 → reserved = request = 8000
        meta_a = _metadata(ctx=131072, provider_max=32768)
        req_a = _estim(8000, source="config_static")
        budget_a = compute_budget(
            metadata=meta_a, request_max_output=req_a, fixed=fx, k=0.5,
        )

        # 组合 B：provider 较小，request 远大 → reserved = provider = 8000
        meta_b = _metadata(ctx=131072, provider_max=8000)
        req_b = _estim(32768, source="config_static")
        budget_b = compute_budget(
            metadata=meta_b, request_max_output=req_b, fixed=fx, k=0.5,
        )

        # 两个 budget 的 reserved 相等且均 = 8000（min），绝非默认 16000
        assert budget_a.reserved_output_tokens.value == 8000
        assert budget_b.reserved_output_tokens.value == 8000
        assert (
            budget_a.reserved_output_tokens.value
            == budget_b.reserved_output_tokens.value
        )
        # 显式拒绝 16000：若代码退回固定 16000，下面这个断言会失败
        assert budget_a.reserved_output_tokens.value != 16000
        assert budget_b.reserved_output_tokens.value != 16000

    def test_K4_lossless_plus_compaction_equals_history(self):
        """K4：lossless + compaction == history_budget（§21 数学恒等）。"""
        meta = _metadata(ctx=131072, provider_max=32768)
        req = _estim(16384, source="config_static")
        fx = _fixed()
        # k = 0.5（默认）
        budget = compute_budget(
            metadata=meta, request_max_output=req, fixed=fx, k=K_DEFAULT,
        )
        hb = budget.history_budget.value
        lb = budget.lossless_budget.value
        cb = budget.compaction_budget.value
        assert hb is not None and lb is not None and cb is not None
        assert lb + cb == hb, (
            f"§21 违反：lossless({lb}) + compaction({cb}) ≠ "
            f"history_budget({hb})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# §21 ContextBudget 公式（history / lossless / compaction）
# ─────────────────────────────────────────────────────────────────────────────


class TestComputeBudgetFormula:
    """§21 公式逐条验证：compute_budget() 是纯函数。"""

    def test_history_budget_subtracts_fixed_and_reserved(self):
        """§21：history_budget = context_limit − fixed_input_tokens − reserved_output_tokens。"""
        meta = _metadata(ctx=131072, provider_max=32768)
        req = _estim(16384, source="config_static")
        fx = _fixed(sys=2000, tool=5000, user=1000, peer=2000)
        budget = compute_budget(
            metadata=meta, request_max_output=req, fixed=fx, k=0.5,
        )

        # 期望：131072 − (2000+5000+1000+2000) − min(32768, 16384) = 131072 − 10000 − 16384
        expected = 131072 - 10000 - 16384
        assert budget.fixed_input_tokens.value == 10000
        assert budget.reserved_output_tokens.value == 16384
        assert budget.history_budget.value == expected

    def test_T8_k_three_tiers_lossless_compaction(self):
        """T8：k = 0.2 / 0.5 / 0.9 三档，lossless 与 compaction 都正确分配。"""
        meta = _metadata(ctx=131072, provider_max=32768)
        req = _estim(16384, source="config_static")
        fx = _fixed()
        # history_budget = 131072 - 7000 - 16384 = 107688
        expected_history = 107688

        for k_val in (0.2, 0.5, 0.9):
            budget = compute_budget(
                metadata=meta, request_max_output=req, fixed=fx, k=k_val,
            )
            assert budget.history_budget.value == expected_history
            assert budget.lossless_budget.value == int(expected_history * k_val)
            assert budget.compaction_budget.value == (
                expected_history - int(expected_history * k_val)
            )
            # §21 数学恒等
            assert (
                budget.lossless_budget.value
                + budget.compaction_budget.value
                == expected_history
            )

    def test_T9_provider_metadata_drives_reserved_basic(self):
        """T9：provider 元数据参与 reserved 计算（基本路径）。"""
        meta = _metadata(ctx=200000, provider_max=128000)
        req = _estim(16384, source="config_static")
        fx = _fixed(sys=500, tool=500, user=100, peer=100)
        budget = compute_budget(
            metadata=meta, request_max_output=req, fixed=fx, k=0.5,
        )
        # request(16384) < provider(128000) → reserved = 16384
        assert budget.reserved_output_tokens.value == 16384
        # context_limit 透传
        assert budget.context_limit.value == 200000
        # provider 也被保留（§22 三值分离）
        assert budget.model_max_output.value == 128000
        assert budget.request_max_output.value == 16384

    def test_provider_unavailable_fallback_to_request(self):
        """§9.2 分支 1：provider 元数据 unknown → reserved 用 request 兜底（estimated）。"""
        meta = _metadata(ctx=131072, provider_max=None)  # provider 完全取不到
        req = _estim(8192, source="config_static")
        fx = _fixed()
        budget = compute_budget(
            metadata=meta, request_max_output=req, fixed=fx, k=0.5,
        )
        # reserved 退化为 request 值
        assert budget.reserved_output_tokens.value == 8192
        # accuracy 标 estimated（不是 exact，因为 provider 不可信）
        assert budget.reserved_output_tokens.accuracy_mode == "estimated"
        # provider 本身保持 unknown
        assert budget.model_max_output.value is None
        assert budget.model_max_output.accuracy_mode == "unknown"

    def test_request_unavailable_uses_provider(self):
        """§9.2 分支 2：request unknown → reserved 透传 provider。"""
        meta = _metadata(ctx=131072, provider_max=8192)
        req = _unknown()  # 本次请求未设置
        fx = _fixed()
        budget = compute_budget(
            metadata=meta, request_max_output=req, fixed=fx, k=0.5,
        )
        # reserved = provider 完全透传
        assert budget.reserved_output_tokens.value == 8192
        assert budget.reserved_output_tokens.accuracy_mode == "exact"
        # request 本身保持 unknown
        assert budget.request_max_output.value is None

    def test_both_unavailable_makes_reserved_unknown(self):
        """§9.2 分支 4：双侧 unknown → reserved 全 unknown（§22 降级链最末态）。"""
        meta = _metadata(ctx=131072, provider_max=None)
        req = _unknown()
        fx = _fixed()
        budget = compute_budget(
            metadata=meta, request_max_output=req, fixed=fx, k=0.5,
        )
        # 双 unknown → reserved 也是 unknown（§S3.3 不装作精确）
        assert budget.reserved_output_tokens.value is None
        assert budget.reserved_output_tokens.accuracy_mode == "unknown"
        # TokenEstimate 三不变量 #1：value=None ⟺ unknown
        assert budget.reserved_output_tokens.source == "provider_unavailable"
        # history_budget / lossless / compaction 也都是 unknown
        assert budget.history_budget.value is None
        assert budget.lossless_budget.value is None
        assert budget.compaction_budget.value is None

    def test_metadata_none_equivalent_to_unknown_both(self):
        """metadata=None 等价于 provider+context 双 unknown。"""
        req = _unknown()
        fx = _fixed()
        budget = compute_budget(
            metadata=None, request_max_output=req, fixed=fx, k=0.5,
        )
        assert budget.context_limit.value is None
        assert budget.model_max_output.value is None
        assert budget.reserved_output_tokens.value is None
        assert budget.history_budget.value is None

    def test_k_out_of_range_rejected_by_K3(self):
        """K3（§20 §55）：k 不在 [K_MIN, K_MAX] → ContextBudget __post_init__ 拒收。"""
        meta = _metadata()
        req = _estim(8192, source="config_static")
        fx = _fixed()
        with pytest.raises(ValueError, match="k.*不在"):
            compute_budget(metadata=meta, request_max_output=req,
                           fixed=fx, k=0.005)
        with pytest.raises(ValueError, match="k.*不在"):
            compute_budget(metadata=meta, request_max_output=req,
                           fixed=fx, k=0.95)

    def test_history_budget_can_be_negative_when_inputs_overflow(self):
        """§24 触线时 history_budget 可为负——compute_budget 不静默 clamp，记录真实值。"""
        meta = _metadata(ctx=4096, provider_max=8192)        # context 极小
        req = _estim(2048, source="config_static")
        # fixed 撑爆
        fx = _fixed(sys=1000, tool=1000, user=1000, peer=1000)
        budget = compute_budget(
            metadata=meta, request_max_output=req, fixed=fx, k=0.5,
        )
        # history_budget = 4096 - 4000 - 2048 = -1952
        assert budget.history_budget.value == 4096 - 4000 - 2048
        assert budget.history_budget.value < 0
        # S3.5 ContextSelector 会捕获 BudgetOverflowError；这里只验证准确传递


# ─────────────────────────────────────────────────────────────────────────────
# §22 TokenEstimate 精度传导
# ─────────────────────────────────────────────────────────────────────────────


class TestAccuracyPropagation:
    """§22 / §57：accuracy 必须如实传导，且不准把估算伪装为精确。"""

    def test_accuracy_lower_of_provider_and_request(self):
        """Provider 元数据（exact）↔ Request 声明（estimated）准确传导。

        S3.3 边界：受 S3.1 TokenEstimate 不变量 #3 约束 —— ``config_static``
        源必须配 ``estimated``（Config 不是 Provider 报告，不许伪装精确）。
        Plan §9.2 的"body 提供 max_output_tokens 标 openrouter_metadata"
        分支才是 exact 来源。
        """
        fx = _fixed()

        # Case A：provider(exact) + request(exact via "openrouter_metadata"
        # 假装来自 cross-llm body) → reserved = exact（最低 = exact）
        meta_exact = _metadata(ctx=131072, provider_max=32768)
        # 在 Plan §9.2 中当 body 提供 max_output_tokens 时 source 标
        # openrouter_metadata —— 用 _exact 模拟此场景
        req_body_exact = _exact(16384, source="openrouter_metadata")
        b1 = compute_budget(metadata=meta_exact,
                            request_max_output=req_body_exact,
                            fixed=fx, k=0.5)
        assert b1.reserved_output_tokens.accuracy_mode == "exact"

        # Case B：provider(exact) + request(models.yaml 配置, estimated)
        # → reserved 退到 estimated（精度取两者最低）
        req_cfg_est = _estim(16384, source="config_static")
        b2 = compute_budget(metadata=meta_exact,
                            request_max_output=req_cfg_est,
                            fixed=fx, k=0.5)
        assert b2.reserved_output_tokens.accuracy_mode == "estimated"

        # Case C：provider 完全 unknown → 用 request 兜底，accuracy 强制 estimated
        meta_no_provider = _metadata(ctx=131072, provider_max=None)
        b3 = compute_budget(metadata=meta_no_provider,
                            request_max_output=req_cfg_est,
                            fixed=fx, k=0.5)
        assert b3.reserved_output_tokens.accuracy_mode == "estimated"

    def test_unknown_does_not_pretend_to_be_zero(self):
        """§S3.3：unknown 永不准填 0（TokenEstimate 三不变量 #1）。"""
        meta = _metadata(ctx=131072, provider_max=None)
        req = _unknown()
        fx = _fixed(sys=0, tool=0, user=0, peer=0)
        budget = compute_budget(metadata=meta, request_max_output=req,
                                fixed=fx, k=0.5)
        # 任一 unknown → 整条链 unknown
        assert budget.reserved_output_tokens.value is None
        assert budget.fixed_input_tokens.value == 0  # 0+0+0+0=0 不算 unknown
        assert budget.history_budget.value is None  # 但 ctx 无值 → unknown


# ─────────────────────────────────────────────────────────────────────────────
# §24 硬约束
# ─────────────────────────────────────────────────────────────────────────────


class TestHardConstraint:
    """§24：B1 ``fixed + used + reserved ≤ context_limit`` 校验。"""

    def test_validate_hard_constraint_pass(self):
        meta = _metadata(ctx=131072, provider_max=32768)
        req = _estim(8192, source="config_static")
        fx = _fixed()
        budget = compute_budget(metadata=meta, request_max_output=req,
                                fixed=fx, k=0.5)
        # 还没用任何 history（recent_raw=0，pinned=0，compacted=0）
        # 总和 = fixed(7000) + used(0) + reserved(8192) = 15192 < context_limit
        validate_hard_constraint(budget)  # 不抛

    def test_validate_hard_constraint_overflow_raises(self):
        """§24 违反 → BudgetOverflowError（S3.5 ContextSelector 捕获后压缩）。"""
        meta = _metadata(ctx=4096, provider_max=32768)
        req = _estim(8192, source="config_static")
        fx = _fixed()
        budget = compute_budget(metadata=meta, request_max_output=req,
                                fixed=fx, k=0.5)
        # fixed(7000) + reserved(8192) = 15192 > 4096 → 抛
        with pytest.raises(BudgetOverflowError, match="§24 硬约束违反"):
            validate_hard_constraint(budget)

    def test_validate_hard_constraint_skipped_when_any_unknown(self):
        """任何关键值 unknown → 不校验（unknown 路径不装作精确）。"""
        meta = _metadata(ctx=None, provider_max=None)
        req = _unknown()
        fx = _fixed()
        budget = compute_budget(metadata=meta, request_max_output=req,
                                fixed=fx, k=0.5)
        # 全部 unknown → 静默通过（无错可报）
        validate_hard_constraint(budget)


# ─────────────────────────────────────────────────────────────────────────────
# §57 占位（pinned / recent_raw / compacted 起始均为 0）
# ─────────────────────────────────────────────────────────────────────────────


class TestPlaceholderOccupancy:
    """§57 字段：pinned/recent_raw/compacted 在 S3.3 阶段尚未填真实值，初始均为 0。"""

    def test_occupancy_placeholders_start_at_zero(self):
        meta = _metadata(ctx=131072, provider_max=32768)
        req = _estim(8192, source="config_static")
        fx = _fixed()
        budget = compute_budget(metadata=meta, request_max_output=req,
                                fixed=fx, k=0.5)
        assert budget.pinned_tokens.value == 0
        assert budget.recent_raw_tokens.value == 0
        assert budget.compacted_tokens.value == 0
        # S3.3 边界：value=0 + source=crossllm_estimator → accuracy=estimated
        # （S3.1 TokenEstimate 不变量 #3：exact 源必须 ∈ {provider_tokenizer, openrouter_metadata}）
        for occ in (budget.pinned_tokens, budget.recent_raw_tokens,
                    budget.compacted_tokens):
            assert occ.source == "crossllm_estimator"
            assert occ.accuracy_mode == "estimated"


# ─────────────────────────────────────────────────────────────────────────────
# §57 to_log() 序列化
# ─────────────────────────────────────────────────────────────────────────────


class TestLogSerialization:
    """§57 context_policy.jsonl 字段全量映射。"""

    def test_to_log_contains_all_required_fields(self):
        meta = _metadata(ctx=131072, provider_max=32768)
        req = _estim(8192, source="config_static")
        fx = _fixed()
        budget = compute_budget(metadata=meta, request_max_output=req,
                                fixed=fx, k=0.5)
        log = budget.to_log()
        required = {
            # 模型能力（含来源 / 拉取状态）
            "context_limit", "context_limit_source", "context_limit_fetch_status",
            "model_max_output", "model_max_output_source",
            # 请求侧
            "request_max_output", "request_max_output_source",
            "reserved_output_tokens",
            # 状态
            "budget_status",
            # fixed_input 四分量 + 总
            "system_tokens", "tool_tokens", "current_user_tokens",
            "peer_context_reserve", "fixed_input_tokens",
            # history
            "history_budget",
            # k
            "k", "lossless_budget", "compaction_budget",
            # occupancy
            "pinned_tokens", "recent_raw_tokens", "compacted_tokens",
        }
        missing = required - set(log.keys())
        assert not missing, f"§八 字段缺失：{missing}"
        # k 是数字（Python float）
        assert log["k"] == 0.5

    def test_to_log_is_flat_not_nested(self):
        """§八：日志字段必须平铺 —— 值直接是数字 / null，不得是嵌套三元组。"""
        meta = _metadata(ctx=131072, provider_max=32768)
        budget = compute_budget(
            metadata=meta,
            request_max_output=_estim(8192, source="config_static"),
            fixed=_fixed(), k=0.5,
        )
        log = budget.to_log()
        nested = {k: v for k, v in log.items() if isinstance(v, dict)}
        assert not nested, f"§八 要求平铺字段，出现嵌套：{sorted(nested)}"
        # 旧的三元组形态键不得再出现
        for legacy in (
            # 旧名（2026-09-25 统一后已废弃）：带 _tokens 后缀的嵌套键
            "request_max_output" + "_tokens",
            "context_limit_estimate",
            "model_max_output_estimate",
            "reserved_output_estimate",
        ):
            assert legacy not in log, f"旧嵌套键 {legacy!r} 不应存在"
        # 来源字段是字符串，值字段是数字
        assert log["context_limit"] == 131072
        assert isinstance(log["context_limit_source"], str)
        assert log["context_limit_fetch_status"] == "ok"
        assert log["budget_status"] in ("complete", "degraded")


# ─────────────────────────────────────────────────────────────────────────────
# §10 make_fixed_input_tokens helper
# ─────────────────────────────────────────────────────────────────────────────


class TestMakeFixedInputTokensHelper:
    """make_fixed_input_tokens 是纯文本估算便利构造。"""

    def test_estimates_using_chars_per_token_3(self):
        # CHARS_PER_TOKEN=3 向上取整
        fx = make_fixed_input_tokens(
            system_text="a" * 30,        # 10
            tool_text="b" * 60,          # 20
            current_user_text="c" * 9,   # 3
            peer_context_reserve_text="d" * 12,  # 4
        )
        assert fx.system.value == 10
        assert fx.tool.value == 20
        assert fx.current_user.value == 3
        assert fx.peer_context_reserve.value == 4
        # 所有估算值标记 estimated（不假装 exact）
        for part in (fx.system, fx.tool, fx.current_user, fx.peer_context_reserve):
            assert part.source == "crossllm_estimator"
            assert part.accuracy_mode == "estimated"

    def test_empty_strings_yield_zero_each_part(self):
        fx = make_fixed_input_tokens()
        assert fx.system.value == 0
        assert fx.tool.value == 0
        assert fx.current_user.value == 0
        assert fx.peer_context_reserve.value == 0


# ─────────────────────────────────────────────────────────────────────────────
# AST 守门：S3.3 不前置 S3.4+，不恢复旧 ratio 触发
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Cross-references：旧 test_context_budget.py 已废弃
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# K 默认值／区间常量一致性（§20 §55）
# ─────────────────────────────────────────────────────────────────────────────


def test_K_constants_within_valid_range():
    """§20 §55：K_MIN=0.01, K_MAX=0.90, K_DEFAULT=0.5。"""
    assert K_MIN == 0.01
    assert K_MAX == 0.90
    assert K_DEFAULT == 0.5
    assert K_MIN < K_DEFAULT < K_MAX


# ══════════════════════════════════════════════════════════════════════════
# 2) Context Selection：k 分配 / Pin 优先 / 降序填充 / Raw Archive 不变 （原 test_phase4_context_selection.py）
# ══════════════════════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────────────────────
# Helper：构造 N 个全 Valid Round（每个 round 所有模型都成功）
# ─────────────────────────────────────────────────────────────────────────────

MODELS = ("a", "b", "c")
# 每轮字符数 → token 估算（len//3 向上取整）
ROUND_CHARS = 300


def seed_valid_rounds(
    storage: Storage,
    session_id: str,
    n_rounds: int,
    *,
    chars: int = ROUND_CHARS,
) -> SessionState:
    """造 n_rounds 个 Round，每个 Round 3 个模型全部 complete（全 Valid）。"""
    storage.ensure_session_dir(session_id)
    storage.save_question(session_id, "原始问题?")
    rounds = []
    for rid in range(1, n_rounds + 1):
        for m in MODELS:
            storage.save_response(
                session_id, rid, m, f"R{rid}-{m} " + ("x" * chars)
            )
        rounds.append(
            RoundState(
                round_id=rid,
                user_instruction=f"第{rid}轮指令 " + ("q" * chars),
                participants=list(MODELS),
                status={m: "complete" for m in MODELS},
                timestamp="2026-09-03T00:00:00Z",
            )
        )
    session = SessionState(
        session_id=session_id,
        original_question="原始问题?",
        current_round=n_rounds,
        active_models=list(MODELS),
        closed_models=[],
        rounds=rounds,
    )
    storage.save_state(session)
    return session


def make_metadata(context_limit: int) -> ProviderModelMetadata:
    """构造 ProviderModelMetadata；model_max_output=2048（estimated）。

    注意 ``fetch_status`` 与 ``context_limit_source`` 是**两条独立的诚实轴**：
    这里 fetch_status="ok"（查询成功）但 source="config_static"（值取自配置），
    两者并不矛盾，也不得被合并成一条。
    """
    return ProviderModelMetadata(
        model_slug="fake/model",
        context_limit=TokenEstimate(context_limit, "config_static", "estimated"),
        model_max_output=TokenEstimate(2048, "openrouter_metadata", "exact"),
        fetched_at="2026-09-03T00:00:00Z",
        fetch_status="ok",
    )


def select(
    storage: Storage,
    session_id: str,
    *,
    context_limit: int = 100_000,
    k: float = 0.5,
    pin_ids: list[int] | None = None,
    with_log: bool = True,
) -> ContextSelectionResult:
    """跑一次完整 ContextSelector.build()。"""
    session = storage.load_state(session_id)
    pool = ValidRoundPoolBuilder(storage).build(session)

    pin_store = PinStore(storage)
    for pid in pin_ids or []:
        pin_store.pin(session_id, pid)
    pin_state = pin_store.load(session_id)

    return ContextSelector().build(
        metadata=make_metadata(context_limit),
        request_max_output=_token(2048, "config_static", "estimated"),
        valid_pool=pool,
        pin_state=pin_state,
        fixed=make_fixed_input_tokens(system_text="sys", current_user_text="inst"),
        k=k,
        storage=storage if with_log else None,
        session_id=session_id if with_log else None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# T1：10 个 Valid Round，预算充足 → 全部进入，不因 3 轮而停止
# ─────────────────────────────────────────────────────────────────────────────


class TestT1NoHardThreeRoundWindow:
    """§54 / §29：不得因"最多 N 轮"硬限制删除历史。"""

    def test_t1_ten_valid_rounds_all_selected(self, tmp_path):
        """10 个 Round 全 Valid + budget 充足 → 10 个全部进 recent_raw。"""
        storage = Storage(tmp_path / "sessions")
        seed_valid_rounds(storage, "s-t1", 10)

        result = select(storage, "s-t1", context_limit=100_000, k=0.5)

        selected = [it.round_id for it in result.recent_raw]
        # 关键断言：不是 3 个，是 10 个
        assert selected == [10, 9, 8, 7, 6, 5, 4, 3, 2, 1], (
            f"§54 违反：10 个 Valid Round 应全部进入 Context，实际 {selected}"
        )
        assert len(selected) == 10
        assert result.dropped == []
        # 倒序（由新到旧）
        assert selected == sorted(selected, reverse=True)

    def test_t1_twelve_rounds_still_all_selected(self, tmp_path):
        """12 个 Round 同样全部进入（进一步证伪"最多 N 轮"）。"""
        storage = Storage(tmp_path / "sessions")
        seed_valid_rounds(storage, "s-t1b", 12)
        result = select(storage, "s-t1b", context_limit=200_000, k=0.5)
        assert len(result.recent_raw) == 12
        assert [it.round_id for it in result.recent_raw][0] == 12

    def test_t1_no_history_window_constant_in_modules(self):
        """AST 扫描：core/ 下产品模块不得再定义 HISTORY_WINDOW 类硬裁常量。"""
        core_dir = Path(__file__).resolve().parent.parent / "core"
        forbidden = {
            "HISTORY_WINDOW", "MAX_HISTORY", "RECENT_RAW_TARGET",
            "RECENT_ROUNDS", "MAX_RECENT",
        }
        leaked: list[str] = []
        for py in core_dir.glob("*.py"):
            if py.name == "__init__.py":
                continue
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in tree.body:  # 只看顶层常量
                if isinstance(node, ast.Assign):
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name) and tgt.id in forbidden:
                            leaked.append(f"{py.name}:{tgt.id}")
        assert not leaked, f"§54 违反：core/ 仍有硬裁常量 {leaked}"


# ─────────────────────────────────────────────────────────────────────────────
# T2：明确预算 → 倒序加入，首次超预算 break
# ─────────────────────────────────────────────────────────────────────────────


class TestT2BudgetDrivenDescendingFill:
    """§11.2：由新→旧倒序，装得下就加，装不下立即 break（Custom Rule C6）。"""

    def test_t2_stops_at_first_overflow_round(self, tmp_path):
        """lossless=1300，每 Round 408 tokens → 装 R10/R9/R8，R7 首次超 → 停。"""
        storage = Storage(tmp_path / "sessions")
        seed_valid_rounds(storage, "s-t2", 10)

        # ctx=4651 → fixed(system 'sys'=1 + user 'inst'=2) + reserved 2048
        # history = 4651 - 3 - 2048 = 2600; lossless = 2600 * 0.5 = 1300
        result = select(storage, "s-t2", context_limit=4651, k=0.5)

        assert result.budget.history_budget.value == 2600
        assert result.budget.lossless_budget.value == 1300

        selected = [it.round_id for it in result.recent_raw]
        assert selected == [10, 9, 8], (
            f"§11.2 违反：应装 R10/R9/R8 后于 R7 首次超预算停止，实际 {selected}"
        )
        # dropped 记录第一个装不下的 Round（§59 不代表删除）
        assert result.dropped == [7]
        # 已装入 token 之和 ≤ lossless
        assert result.budget.recent_raw_tokens.value == 1225  # 408*3 + 1
        assert result.budget.recent_raw_tokens.value <= result.budget.lossless_budget.value

    def test_t2_k_changes_lossless_and_selection_width(self, tmp_path):
        """k 越大 lossless 越大 → 装入的 Round 越多（k 只做分配，非触发）。"""
        storage = Storage(tmp_path / "sessions")
        seed_valid_rounds(storage, "s-t2k", 10)

        narrow = select(storage, "s-t2k", context_limit=4651, k=0.2, with_log=False)
        wide = select(storage, "s-t2k", context_limit=4651, k=0.9, with_log=False)

        # k=0.2 → lossless = 520 → 只能装 1 个（408）
        assert narrow.budget.lossless_budget.value == 520
        assert len(narrow.recent_raw) == 1
        # k=0.9 → lossless = 2340 → 能装 5 个（408*5=2040）
        assert wide.budget.lossless_budget.value == 2340
        assert len(wide.recent_raw) == 5
        # k 只改变分配宽度，不改变"倒序 break"的语义
        assert [it.round_id for it in wide.recent_raw] == [10, 9, 8, 7, 6]

    def test_t2_drop_is_not_delete(self, tmp_path):
        """§59：dropped 的 Round 仍存在于 Raw Archive（不是删除）。"""
        storage = Storage(tmp_path / "sessions")
        seed_valid_rounds(storage, "s-t2d", 10)
        result = select(storage, "s-t2d", context_limit=4651, k=0.5)

        for rid in result.dropped:
            # Round 文件仍在
            for m in MODELS:
                p = storage.response_path("s-t2d", rid, m)
                assert p.exists(), f"§59 违反：dropped 的 Round {rid} 文件被删除"
            # 且仍能构造出 ValidRound（状态未变）
        pool = ValidRoundPoolBuilder(storage).build(
            storage.load_state("s-t2d")
        )
        assert {vr.round_id for vr in pool} >= set(result.dropped)


# ─────────────────────────────────────────────────────────────────────────────
# §19.1 迁移（S3.12）：旧 test_budget_trim_drops_oldest_first /
#   test_truncate_when_single_round_still_too_long 的新语义替代
# ─────────────────────────────────────────────────────────────────────────────


def test_oversized_round_goes_to_compaction(tmp_path):
    """§29 / §59：Context 不足 → 超出 lossless 的旧 Round 进 Compaction 候选区
    （dropped 列表），recent_raw 保留装得下的；不截断、不删除。

    旧语义（已废弃，本测试反向证伪）：
      - ``_trim_to_budget`` 从最旧一轮开始静默丢弃，无任何记录
      - ``_truncate_entries`` 单轮超限时在 prompt 中写入"已截断"
    """
    storage = Storage(tmp_path / "sessions")
    seed_valid_rounds(storage, "s-t3c", 10)

    # 同 T2 场景：lossless=1300 → 只装 R10/R9/R8，R7..R1 超限
    result = select(storage, "s-t3c", context_limit=4651, k=0.5)

    # 1. 首次超限即 break（§11.2）：dropped 记录首个装不下的 Round；
    #    该 Round 及所有更旧 Round 一并构成 Compaction 候选区，非静默丢失
    assert result.dropped == [7]
    assert [it.round_id for it in result.recent_raw] == [10, 9, 8]
    compaction_candidates = set(range(1, result.dropped[0] + 1)) - {
        it.round_id for it in result.recent_raw
    }
    assert compaction_candidates == {7, 6, 5, 4, 3, 2, 1}

    # 2. 预算三区恒等（§21）：lossless + compaction = history，Compaction 区有预算
    b = result.budget
    assert b.compaction_budget.value == (
        b.history_budget.value - b.lossless_budget.value
    )
    assert b.compaction_budget.value > 0

    # 3. Compaction 候选 Round 的 Raw Archive 完整保留（§59 不是删除，交 Compaction 处理）
    pool_ids = {
        vr.round_id
        for vr in ValidRoundPoolBuilder(storage).build(storage.load_state("s-t3c"))
    }
    assert compaction_candidates <= pool_ids
    for rid in sorted(compaction_candidates):
        for m in MODELS:
            assert storage.response_path("s-t3c", rid, m).exists(), (
                f"§59 违反：dropped 的 Round {rid} 原始文件被删除"
            )

    # 4. 新语义不产生"已截断"文本（旧 _truncate_entries 已删除）
    for it in result.recent_raw:
        assert "已截断" not in it.user_question
        for _, ans in it.answers:
            assert "已截断" not in ans


# ─────────────────────────────────────────────────────────────────────────────
# T3：Pin 优先占用 lossless，Recent Raw 用剩余
# ─────────────────────────────────────────────────────────────────────────────


class TestT3PinConsumesLosslessFirst:
    """§13.2 / §26：Pin 优先占用 lossless_budget；普通历史只能用剩余。"""

    def test_t3_pin_reduces_recent_raw_capacity(self, tmp_path):
        """同样 ctx=4651：无 Pin 装 3 个；Pin R1（408）后只能装 2 个。"""
        storage = Storage(tmp_path / "sessions")
        seed_valid_rounds(storage, "s-t3", 10)

        no_pin = select(storage, "s-t3", context_limit=4651, k=0.5, with_log=False)
        assert len(no_pin.recent_raw) == 3

        storage2 = Storage(tmp_path / "sessions2")
        seed_valid_rounds(storage2, "s-t3b", 10)
        with_pin = select(
            storage2, "s-t3b", context_limit=4651, k=0.5, pin_ids=[1], with_log=False
        )

        # Pin 优先占用
        assert [it.round_id for it in with_pin.pinned] == [1]
        assert with_pin.budget.pinned_tokens.value == 408
        # 剩余 lossless = 1300 - 408 = 892 → 只能装 R10/R9（816），R8 首次超 → 停
        assert [it.round_id for it in with_pin.recent_raw] == [10, 9]
        assert with_pin.dropped == [8]
        # Pin 占用的确减少了 recent_raw 容量（这就是"优先占用"的可观测证据）
        assert len(with_pin.recent_raw) < len(no_pin.recent_raw)

    def test_t3_pinned_round_not_counted_twice(self, tmp_path):
        """Pin 的 Round 不会重复出现在 recent_raw（不重复计预算）。"""
        storage = Storage(tmp_path / "sessions")
        seed_valid_rounds(storage, "s-t3c", 10)
        result = select(storage, "s-t3c", context_limit=100_000, k=0.5, pin_ids=[3, 7])

        pinned_ids = {it.round_id for it in result.pinned}
        recent_ids = {it.round_id for it in result.recent_raw}
        assert pinned_ids == {3, 7}
        assert not (pinned_ids & recent_ids), "Pin 的 Round 不得重复计入 recent_raw"

    def test_t3_budget_triple_all_backfilled(self, tmp_path):
        """§57：pinned / recent_raw / compacted 三值全部回填到 ContextBudget。"""
        storage = Storage(tmp_path / "sessions")
        seed_valid_rounds(storage, "s-t3d", 10)
        result = select(storage, "s-t3d", context_limit=100_000, k=0.5, pin_ids=[1, 2])

        assert result.budget.pinned_tokens.value == 816          # R1(408) + R2(408)
        # R3..R10 共 8 个：7 个 408 + R10 的 409（末尾多 1 字符 → 向上取整）
        assert result.budget.recent_raw_tokens.value == 3265
        # S3.5：compacted 仍为占位 0（S3.8 才填真实摘要）
        assert result.budget.compacted_tokens.value == 0
        assert result.budget.compacted_tokens.accuracy_mode == "estimated"


# ─────────────────────────────────────────────────────────────────────────────
# T4：Pin 超 Lossless → 不解除 / 不压缩 / 不删除，产生 overflow 状态
# ─────────────────────────────────────────────────────────────────────────────


class TestT4PinOverflowBehaviour:
    """§27：Pin 过多时只 warning，绝不自动解除 / 压缩 / 静默删除。"""

    def test_t4_pin_exceeds_lossless_keeps_all_pins(self, tmp_path):
        """Pin R1-R4（1632）> lossless（274）→ 全部 Pin 仍在，overflow 被标记。"""
        storage = Storage(tmp_path / "sessions")
        seed_valid_rounds(storage, "s-t4", 10)

        # ctx=2600 → history = 2600-3-2048 = 549; lossless = 274
        result = select(storage, "s-t4", context_limit=2600, k=0.5, pin_ids=[1, 2, 3, 4])

        assert result.budget.lossless_budget.value == 274
        assert result.budget.pinned_tokens.value == 1632
        assert result.budget.pinned_tokens.value > result.budget.lossless_budget.value

        # ① 不解除：4 个 Pin 一个不少
        assert [it.round_id for it in result.pinned] == [1, 2, 3, 4]
        # ② 不压缩：Pin 内容仍是完整原文（未被截断/摘要）
        for it in result.pinned:
            assert it.answers, f"Pin 的 Round {it.round_id} 内容被清空"
            for _mid, content in it.answers:
                assert content.startswith(f"R{it.round_id}-"), (
                    f"Pin 的 Round {it.round_id} 内容疑似被压缩/改写"
                )
        # ③ 不静默删除：不是把 pin 挪去 dropped
        assert 1 not in result.dropped
        # ④ 明确 overflow 状态
        assert result.overflow.get("pinned_history_exceeds_budget") is True
        assert result.overflow["pinned_tokens"] == 1632
        assert result.overflow["lossless_budget"] == 274

    def test_t4_overflow_recent_raw_may_be_empty(self, tmp_path):
        """Pin 挤占后 recent_raw 可能为空（§27 第 3 步：普通历史让位）。"""
        storage = Storage(tmp_path / "sessions")
        seed_valid_rounds(storage, "s-t4b", 10)
        result = select(storage, "s-t4b", context_limit=2600, k=0.5, pin_ids=[1, 2, 3, 4])
        # 剩余 lossless = max(0, 274-1632) = 0 → recent_raw 空
        assert result.recent_raw == []
        # 但 Pin 仍在 Context
        assert len(result.pinned) == 4

    def test_t4_pin_never_compacted_invariant(self):
        """编译期守门：ContextSelectionResult / ContextSelector 无"自动压缩 Pin"接口。"""
        public = set()
        for cls in (ContextSelector, ContextSelectionResult):
            public |= {n for n in dir(cls) if not n.startswith("_")}
        forbidden = {
            "compact_pinned", "auto_compact_pin", "shrink_pinned",
            "drop_overflow_pins", "auto_unpin", "truncate_pinned",
        }
        leaked = forbidden & public
        assert not leaked, f"§27 违反：暴露了自动压缩/解除 Pin 的接口 {leaked}"


# ─────────────────────────────────────────────────────────────────────────────
# T5：真实 Prompt —— ContextSelector → PromptBuilder → 完整 Q&A
# ─────────────────────────────────────────────────────────────────────────────


class TestT5RealPromptContainsFullQA:
    """§31：历史单位必须是 User Question + 该 Round 所有成功 Final Answers。"""

    def test_t5_prompt_contains_question_and_all_answers(self, tmp_path):
        """每个被选中的 Round：prompt 里既有问题也有 3 个模型的答案。"""
        storage = Storage(tmp_path / "sessions")
        session = seed_valid_rounds(storage, "s-t5", 10)
        result = select(storage, "s-t5", context_limit=100_000, k=0.5)

        historical_items = [(it.round_id, it) for it in result.all_items]
        # candidate_ids 由轮内全局共享分配（本测试直接用 A/B/C）
        cand = {"a": "Candidate A", "b": "Candidate B", "c": "Candidate C"}

        prompt, _warns = build_round_n_prompt_v2(
            original_question=session.original_question,
            user_instruction="本轮指令",
            historical_items=historical_items,
            self_model_id="a",
            candidate_ids=cand,
            peer_review=True,
            participants=[],  # 空 participants → 不触发身份硬校验（本测只验内容）
        )

        # 10 个 Round 全部出现在 prompt
        for rid in range(1, 11):
            assert f"## 第 {rid} 轮" in prompt, f"§31 违反：prompt 缺第 {rid} 轮"
            # User Question 在
            assert f"第{rid}轮指令" in prompt, f"§31 违反：第 {rid} 轮的 User Question 缺失"
            # 该 Round 全部成功 Answers 在
            for m in MODELS:
                assert f"R{rid}-{m}" in prompt, (
                    f"§31 违反：第 {rid} 轮模型 {m} 的 Final Answer 缺失"
                )

    def test_t5_not_answer_only_history(self, tmp_path):
        """反向断言：不存在"只有 Answer、没有 Question"的历史块。"""
        storage = Storage(tmp_path / "sessions")
        session = seed_valid_rounds(storage, "s-t5b", 5)
        result = select(storage, "s-t5b", context_limit=100_000, k=0.5, with_log=False)

        historical_items = [(it.round_id, it) for it in result.all_items]
        cand = {"a": "Candidate A", "b": "Candidate B", "c": "Candidate C"}
        prompt, _ = build_round_n_prompt_v2(
            original_question=session.original_question,
            user_instruction="本轮指令",
            historical_items=historical_items,
            self_model_id="a",
            candidate_ids=cand,
            peer_review=True,
            participants=[],
        )

        # 每个 "## 第 N 轮" 块内部必须紧跟"用户问题："
        import re
        blocks = re.split(r"\n(?=## 第 \d+ 轮)", prompt)
        history_blocks = [b for b in blocks if b.startswith("## 第 ")]
        assert len(history_blocks) == 5
        for b in history_blocks:
            assert "用户问题：" in b, (
                "§31 违反：出现 Answer-only 历史块（缺 User Question）"
            )

    def test_t5_historical_item_is_frozen_pair(self, tmp_path):
        """数据结构层：HistoricalContextItem 强制带 user_question（§4.3）。"""
        storage = Storage(tmp_path / "sessions")
        seed_valid_rounds(storage, "s-t5c", 3)
        result = select(storage, "s-t5c", context_limit=100_000, k=0.5, with_log=False)

        for it in result.all_items:
            assert isinstance(it, HistoricalContextItem)
            assert it.user_question, "§4.3 违反：HistoricalContextItem 缺 user_question"
            assert it.answers, "§4.3 违反：HistoricalContextItem 缺 answers"
            assert it.source in {"pinned", "recent_raw", "compacted"}


# ─────────────────────────────────────────────────────────────────────────────
# T6：Context Selection 不修改 Raw Archive
# ─────────────────────────────────────────────────────────────────────────────


class TestT6RawArchiveUntouched:
    """§1-1：任何 Context Selection 都不得改动 round_NNN/ 下的文件。"""

    def test_t6_bytes_identical_after_selection(self, tmp_path):
        storage = Storage(tmp_path / "sessions")
        seed_valid_rounds(storage, "s-t6", 10)

        before = {}
        for rid in range(1, 11):
            for m in MODELS:
                p = storage.response_path("s-t6", rid, m)
                before[str(p)] = p.read_bytes()

        select(storage, "s-t6", context_limit=4651, k=0.5, pin_ids=[1])

        for path_str, data in before.items():
            assert Path(path_str).read_bytes() == data, (
                f"§1-1 违反：Raw Archive 被改动 {path_str}"
            )

    def test_t6_state_json_untouched(self, tmp_path):
        """state.json 也不被 ContextSelector 改动（Pin 走 pins.jsonl）。"""
        storage = Storage(tmp_path / "sessions")
        seed_valid_rounds(storage, "s-t6b", 5)
        before = storage.state_path("s-t6b").read_bytes()

        select(storage, "s-t6b", context_limit=100_000, k=0.5, pin_ids=[2])

        assert storage.state_path("s-t6b").read_bytes() == before


# ─────────────────────────────────────────────────────────────────────────────
# 日志：context_policy.jsonl
# ─────────────────────────────────────────────────────────────────────────────


class TestContextPolicyLogging:
    """§57 / §59 / §61：每次 build 都落 context_budget + history_selection。"""

    def test_logs_context_budget_and_history_selection(self, tmp_path):
        storage = Storage(tmp_path / "sessions")
        seed_valid_rounds(storage, "s-log", 10)
        select(storage, "s-log", context_limit=4651, k=0.5, pin_ids=[1])

        policy = storage.session_dir("s-log") / "context_policy.jsonl"
        assert policy.exists()
        events = [json.loads(ln) for ln in policy.read_text(encoding="utf-8").splitlines() if ln]
        kinds = [e["event"] for e in events]

        assert "context_budget" in kinds, "§57 违反：缺 context_budget 事件"
        assert "history_selection" in kinds, "§59 违反：缺 history_selection 事件"

    def test_history_selection_explains_pin_and_dropped(self, tmp_path):
        """日志能回答：Pin 占多少 / Recent Raw 选了哪些 / 谁没进 Context。"""
        storage = Storage(tmp_path / "sessions")
        seed_valid_rounds(storage, "s-log2", 10)
        select(storage, "s-log2", context_limit=4651, k=0.5, pin_ids=[1])

        policy = storage.session_dir("s-log2") / "context_policy.jsonl"
        events = [json.loads(ln) for ln in policy.read_text(encoding="utf-8").splitlines() if ln]
        sel = next(e for e in events if e["event"] == "history_selection")

        assert sel["pinned_round_ids"] == [1]
        assert sel["recent_raw_round_ids"] == [10, 9]
        assert sel["dropped_round_ids"] == [8]
        # 三元组完整（value + source + accuracy）
        assert sel["pinned_tokens"]["value"] == 408
        assert "token_source" in sel["pinned_tokens"]
        assert "token_exactness" in sel["pinned_tokens"]

    def test_overflow_emits_context_budget_warning(self, tmp_path):
        """§34：Pin 超 lossless → 额外落 context_budget_warning 事件。"""
        storage = Storage(tmp_path / "sessions")
        seed_valid_rounds(storage, "s-log3", 10)
        select(storage, "s-log3", context_limit=2600, k=0.5, pin_ids=[1, 2, 3, 4])

        policy = storage.session_dir("s-log3") / "context_policy.jsonl"
        events = [json.loads(ln) for ln in policy.read_text(encoding="utf-8").splitlines() if ln]
        warn = [e for e in events if e["event"] == "context_budget_warning"]
        assert warn, "§34 违反：overflow 未落 context_budget_warning"
        assert warn[0]["overflow"]["pinned_history_exceeds_budget"] is True

    def test_pins_jsonl_also_written(self, tmp_path):
        """S3.4 的 pins.jsonl 与 S3.5 的 context_policy.jsonl 并存。"""
        storage = Storage(tmp_path / "sessions")
        seed_valid_rounds(storage, "s-log4", 5)
        select(storage, "s-log4", context_limit=100_000, k=0.5, pin_ids=[2])

        assert (storage.session_dir("s-log4") / "pins.jsonl").exists()
        assert (storage.session_dir("s-log4") / "context_policy.jsonl").exists()


# ─────────────────────────────────────────────────────────────────────────────
# AST 守门：S3.5 不前置 S3.6+
# ─────────────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════
# 3) ValidRound + PoolBuilder：有效性判定 / 部分失败 / token 估算 （原 test_valid_round.py）
# ══════════════════════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────────────────────
# 工具：构造测试 Session
# ─────────────────────────────────────────────────────────────────────────────


def _make_round(
    *,
    round_id: int,
    user_instruction: str,
    participants: list[str],
    status: dict[str, str] | None = None,
) -> RoundState:
    return RoundState(
        round_id=round_id,
        user_instruction=user_instruction,
        participants=participants,
        status=dict(status or {}),
    )


def _write_session(
    storage: Storage,
    *,
    session_id: str,
    rounds: list[RoundState],
    active_models: list[str] | None = None,
) -> None:
    """构造 SessionState + 写出 responses 文件 + 写出 state.json。

    response_by_round_model[(round_id, model_id)] = 答案内容（不写 → 模型失败）。
    """
    if active_models is None:
        # 默认取所有 participants
        active_models = sorted({m for r in rounds for m in r.participants})

    storage.ensure_session_dir(session_id)
    session = SessionState(
        session_id=session_id,
        original_question=rounds[0].user_instruction if rounds else "",
        current_round=rounds[-1].round_id if rounds else 0,
        active_models=active_models,
        closed_models=[],
        rounds=list(rounds),
    )
    storage.save_state(session)

    # 写出 responses：只有 status=complete 的模型写文件
    for r in rounds:
        for mid, ans in _RESPONSES_BY_ROUND.get(r.round_id, {}).items():
            storage.save_response(session_id, r.round_id, mid, ans)


# 全局辅助：测试用 answers 注册表（在 _write_session 时消费）
_RESPONSES_BY_ROUND: dict[int, dict[str, str]] = {}


def _seed_response(round_id: int, model_id: str, content: str) -> None:
    _RESPONSES_BY_ROUND.setdefault(round_id, {})[model_id] = content


@pytest.fixture(autouse=True)
def _reset_response_seed():
    _RESPONSES_BY_ROUND.clear()
    yield
    _RESPONSES_BY_ROUND.clear()


# ─────────────────────────────────────────────────────────────────────────────
# §4.2 ValidRound 数据结构 + 不变量
# ─────────────────────────────────────────────────────────────────────────────


class TestValidRound:
    def test_field_shape(self):
        """字段严格按 §4.2（修订 #1–#5 后版）。"""
        te = TokenEstimate(100, "crossllm_estimator", "estimated")
        vr = ValidRound(
            round_id=1,
            user_question="Q",
            successful_answers={"alpha": "A"},
            failed_models=["beta"],
            is_valid=True,
            token_estimate=te,
        )
        assert vr.round_id == 1
        assert vr.user_question == "Q"
        assert vr.successful_answers == {"alpha": "A"}
        assert vr.failed_models == ["beta"]
        assert vr.is_valid is True
        assert vr.token_estimate is te
        assert vr.is_pinned is False  # 默认 False，S3.4 才置 True

    def test_invariant_i1_is_valid_false_with_successful_raises(self):
        """I1：is_valid=True 必须有 ≥1 个 successful。"""
        te = TokenEstimate(0, "crossllm_estimator", "estimated")
        with pytest.raises(ValueError, match="I1"):
            ValidRound(
                round_id=1, user_question="Q",
                successful_answers={}, failed_models=["alpha"],
                is_valid=True,  # 矛盾
                token_estimate=te,
            )

    def test_invariant_i1_is_valid_true_with_no_successful_raises(self):
        """I1：is_valid=False 必须没有 successful。"""
        te = TokenEstimate(0, "crossllm_estimator", "estimated")
        with pytest.raises(ValueError, match="I1"):
            ValidRound(
                round_id=1, user_question="Q",
                successful_answers={"alpha": "A"}, failed_models=[],
                is_valid=False,  # 矛盾
                token_estimate=te,
            )

    def test_invariant_i2_overlap_raises(self):
        """I2：失败模型不应出现在 successful。"""
        te = TokenEstimate(0, "crossllm_estimator", "estimated")
        with pytest.raises(ValueError, match="I2"):
            ValidRound(
                round_id=1, user_question="Q",
                successful_answers={"alpha": "A"},
                failed_models=["alpha"],   # ← 重叠
                is_valid=True,
                token_estimate=te,
            )

    def test_frozen(self):
        te = TokenEstimate(100, "crossllm_estimator", "estimated")
        vr = ValidRound(
            round_id=1, user_question="Q",
            successful_answers={"alpha": "A"},
            failed_models=[],
            is_valid=True, token_estimate=te,
        )
        with pytest.raises(Exception):
            vr.user_question = "changed"  # type: ignore[misc]

    def test_is_valid_round_alias_per_plan(self):
        """is_valid_round 是 §2.1 的术语别名：本类即其实现。"""
        assert ValidRound.__doc__ is not None
        assert "§2.1" in ValidRound.__doc__


# ─────────────────────────────────────────────────────────────────────────────
# ValidRoundPoolBuilder：构造行为
# ─────────────────────────────────────────────────────────────────────────────


class TestPoolBuilderAllSuccess:
    def test_single_round_all_success(self, tmp_path):
        storage = Storage(tmp_path / "sessions")
        sid = "s-all-ok"
        r1 = _make_round(
            round_id=1, user_instruction="Q1",
            participants=["alpha", "beta"],
            status={"alpha": "complete", "beta": "complete"},
        )
        _seed_response(1, "alpha", "A-alpha")
        _seed_response(1, "beta", "A-beta")
        _write_session(storage, session_id=sid, rounds=[r1])

        pool = ValidRoundPoolBuilder(storage).build(storage.load_state(sid))
        assert len(pool) == 1
        vr = pool[0]
        assert vr.round_id == 1
        assert vr.user_question == "Q1"
        assert vr.successful_answers == {"alpha": "A-alpha", "beta": "A-beta"}
        assert vr.failed_models == []
        assert vr.is_valid is True
        assert vr.token_estimate.source == "crossllm_estimator"
        assert vr.token_estimate.accuracy_mode == "estimated"


class TestPoolBuilderPartialFailure:
    def test_partial_failure_round_stays_valid(self, tmp_path):
        """A success / B timeout / C success → Round Valid，含 A、C，不含 B。"""
        storage = Storage(tmp_path / "sessions")
        sid = "s-partial"
        r1 = _make_round(
            round_id=1, user_instruction="Q",
            participants=["alpha", "beta", "gamma"],
            status={"alpha": "complete", "beta": "error", "gamma": "complete"},
        )
        _seed_response(1, "alpha", "A-success")
        # 故意把 beta 的"失败输出"也写一个文件 —— 验证 PoolBuilder 不读
        _seed_response(1, "beta", "[ERROR] timeout after 30s")
        _seed_response(1, "gamma", "C-success")
        _write_session(storage, session_id=sid, rounds=[r1])

        pool = ValidRoundPoolBuilder(storage).build(storage.load_state(sid))
        assert len(pool) == 1
        vr = pool[0]
        assert vr.is_valid is True
        assert set(vr.successful_answers.keys()) == {"alpha", "gamma"}
        assert "A-success" in vr.successful_answers["alpha"]
        assert "C-success" in vr.successful_answers["gamma"]
        assert vr.failed_models == ["beta"]
        # beta 的错误文本**绝不**进入 successful
        for ans in vr.successful_answers.values():
            assert "ERROR" not in ans
            assert "timeout" not in ans

    def test_status_complete_but_no_response_file_treated_as_failed(self, tmp_path):
        """status=complete 但 .md 文件缺失 → 视为 failed（防御性）。"""
        storage = Storage(tmp_path / "sessions")
        sid = "s-orphan"
        r1 = _make_round(
            round_id=1, user_instruction="Q",
            participants=["alpha"],
            status={"alpha": "complete"},
        )
        # 不写 alpha.md —— 模拟文件丢失
        _write_session(storage, session_id=sid, rounds=[r1])

        pool = ValidRoundPoolBuilder(storage).build(storage.load_state(sid))
        assert pool == []  # 没有 successful → Invalid → 不进 Pool


class TestPoolBuilderAllFailure:
    def test_all_failure_round_excluded(self, tmp_path):
        """全部失败 → 不进 Pool。"""
        storage = Storage(tmp_path / "sessions")
        sid = "s-all-fail"
        r1 = _make_round(
            round_id=1, user_instruction="Q",
            participants=["alpha", "beta"],
            status={"alpha": "error", "beta": "error"},
        )
        # 失败模型可能/可能没有文件 —— 即使有也不该读
        _seed_response(1, "alpha", "[ERR] 429 rate limit")
        _seed_response(1, "beta", "[ERR] timeout")
        _write_session(storage, session_id=sid, rounds=[r1])

        pool = ValidRoundPoolBuilder(storage).build(storage.load_state(sid))
        assert pool == []

    def test_all_failure_does_not_delete_raw_archive(self, tmp_path):
        """全失败 Round：PoolBuilder 不删 Raw Archive（§3.1）。"""
        storage = Storage(tmp_path / "sessions")
        sid = "s-preserve"
        r1 = _make_round(
            round_id=1, user_instruction="Q",
            participants=["alpha"],
            status={"alpha": "error"},
        )
        _seed_response(1, "alpha", "[ERR] something")
        _write_session(storage, session_id=sid, rounds=[r1])

        before_files = set(p.name for p in (tmp_path / "sessions" / sid / "round_001").iterdir())
        ValidRoundPoolBuilder(storage).build(storage.load_state(sid))
        after_files = set(p.name for p in (tmp_path / "sessions" / sid / "round_001").iterdir())
        assert before_files == after_files, "Raw Archive 必须字节级保留"


class TestPoolBuilderMultiRound:
    def test_mixed_validity_across_rounds(self, tmp_path):
        """R1 有效，R2 全失败，R3 部分失败 → 池里只有 R1 + R3。"""
        storage = Storage(tmp_path / "sessions")
        sid = "s-multi"
        r1 = _make_round(
            round_id=1, user_instruction="Q1",
            participants=["alpha"], status={"alpha": "complete"},
        )
        r2 = _make_round(
            round_id=2, user_instruction="Q2",
            participants=["alpha"], status={"alpha": "error"},
        )
        r3 = _make_round(
            round_id=3, user_instruction="Q3",
            participants=["alpha", "beta"], status={"alpha": "complete", "beta": "error"},
        )
        _seed_response(1, "alpha", "R1-A")
        _seed_response(2, "alpha", "[ERR] 502")
        _seed_response(3, "alpha", "R3-A")
        _seed_response(3, "beta", "[ERR] 502")
        _write_session(storage, session_id=sid, rounds=[r1, r2, r3])

        pool = ValidRoundPoolBuilder(storage).build(storage.load_state(sid))
        assert [vr.round_id for vr in pool] == [1, 3]
        assert pool[0].user_question == "Q1"
        assert pool[1].user_question == "Q3"
        assert pool[1].failed_models == ["beta"]

    def test_empty_session_returns_empty_pool(self, tmp_path):
        storage = Storage(tmp_path / "sessions")
        sid = "s-empty"
        # 仅创建会话目录，不加 rounds
        storage.ensure_session_dir(sid)
        # 写一个空 SessionState
        empty = SessionState(
            session_id=sid, original_question="",
            current_round=0, active_models=[], closed_models=[], rounds=[],
        )
        storage.save_state(empty)

        pool = ValidRoundPoolBuilder(storage).build(storage.load_state(sid))
        assert pool == []

    def test_round_with_empty_participants_excluded(self, tmp_path):
        """空 participants → 不会产生 successful → Invalid → 排除。"""
        storage = Storage(tmp_path / "sessions")
        sid = "s-no-participants"
        r1 = _make_round(
            round_id=1, user_instruction="Q", participants=[],
            status={},
        )
        _write_session(storage, session_id=sid, rounds=[r1])

        pool = ValidRoundPoolBuilder(storage).build(storage.load_state(sid))
        assert pool == []


class TestPoolBuilderTokenEstimate:
    def test_token_estimate_includes_question_and_all_answers(self, tmp_path):
        """token = ceil(len(Q)/3) + Σ ceil(len(A)/3)。"""
        storage = Storage(tmp_path / "sessions")
        sid = "s-tok"
        q = "x" * 30          # ceil(30/3) = 10
        a1 = "y" * 60         # ceil(60/3) = 20
        a2 = "z" * 9        # ceil(9/3) = 3
        r1 = _make_round(
            round_id=1, user_instruction=q,
            participants=["a", "b"], status={"a": "complete", "b": "complete"},
        )
        _seed_response(1, "a", a1)
        _seed_response(1, "b", a2)
        _write_session(storage, session_id=sid, rounds=[r1])

        pool = ValidRoundPoolBuilder(storage).build(storage.load_state(sid))
        assert pool[0].token_estimate.value == 10 + 20 + 3
        assert pool[0].token_estimate.source == "crossllm_estimator"
        assert pool[0].token_estimate.accuracy_mode == "estimated"

    def test_token_estimate_excludes_failed_models_text(self, tmp_path):
        """失败模型的内容**不计**入 token 估算（不进 successful_answers）。"""
        storage = Storage(tmp_path / "sessions")
        sid = "s-tok-excl"
        q = "x" * 30          # 10
        a_ok = "y" * 30       # 10
        # 即使 beta 的 .md 内容很长，也不该计入
        _seed_response(1, "a", a_ok)
        _seed_response(1, "b", "z" * 9999)
        r1 = _make_round(
            round_id=1, user_instruction=q,
            participants=["a", "b"], status={"a": "complete", "b": "error"},
        )
        _write_session(storage, session_id=sid, rounds=[r1])

        pool = ValidRoundPoolBuilder(storage).build(storage.load_state(sid))
        assert pool[0].token_estimate.value == 20  # 10 (Q) + 10 (a) + 0 (b excluded)


# ─────────────────────────────────────────────────────────────────────────────
# 守门测试：S3.2 严格不前置 S3.3+
# ─────────────────────────────────────────────────────────────────────────────


def test_module_exports_only_s32_surface():
    """S3.2 公共 API 严格限定。"""
    from core import valid_round as vr_mod
    public = {n for n in dir(vr_mod) if not n.startswith("_")}
    # 不严格限制辅助函数（_partition 是实现细节），但 dataclass 必须有
    assert "ValidRound" in public
    assert "ValidRoundPoolBuilder" in public


# ─────────────────────────────────────────────────────────────────────────────
# AST 级守门（防止 docstring 内的提及被源码扫描漏过）
# ─────────────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════
# 4) ModelCapability + TokenEstimate：缓存 / 降级 / 磁盘原子写 （原 test_model_capability.py）
# ══════════════════════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────────────────────
# 工具：httpx MockTransport 注入到 Resolver 内部 new 的 AsyncClient
# ─────────────────────────────────────────────────────────────────────────────


def _patch_async_client_with_transport(handler) -> None:
    """把 ``httpx.AsyncClient`` 替换成自带 MockTransport 的子类。

    ModelCapabilityResolver 每次 ``fetch_online`` 都 `` `` httpx.AsyncClient(...)``，
    没有传 ``transport``，所以这里通过 monkey-patch 类的方式注入。
    测试结束需 ``_unpatch_async_client()`` 复原。
    """
    import core.model_capability as mc
    mc._ORIGINAL_ASYNC_CLIENT = mc.httpx.AsyncClient  # type: ignore[attr-defined]

    class _Patched(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            super().__init__(*args, transport=httpx.MockTransport(handler), **kwargs)

    mc.httpx.AsyncClient = _Patched  # type: ignore[misc]


def _unpatch_async_client() -> None:
    import core.model_capability as mc
    if hasattr(mc, "_ORIGINAL_ASYNC_CLIENT"):
        mc.httpx.AsyncClient = mc._ORIGINAL_ASYNC_CLIENT  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _restore_async_client():
    """每个测试后还原 httpx.AsyncClient，避免污染其他测试。"""
    yield
    _unpatch_async_client()


# ─────────────────────────────────────────────────────────────────────────────
# TokenEstimate（§4.1）
# ─────────────────────────────────────────────────────────────────────────────


class TestTokenEstimate:
    def test_exact_with_value_ok(self):
        te = TokenEstimate(176947, "openrouter_metadata", "exact")
        assert te.value == 176947
        assert te.source == "openrouter_metadata"
        assert te.accuracy_mode == "exact"

    def test_unknown_value_must_be_none(self):
        # value=None ⟺ unknown
        te = TokenEstimate(None, "provider_unavailable", "unknown")
        assert te.value is None

    def test_invariant_violation_value_none_with_estimated_raises(self):
        with pytest.raises(ValueError, match="value is None"):
            TokenEstimate(None, "config_static", "estimated")

    def test_invariant_violation_value_int_with_unknown_raises(self):
        with pytest.raises(ValueError, match="value is None"):
            TokenEstimate(100, "provider_unavailable", "unknown")

    def test_invariant_violation_unknown_source_must_be_unavailable(self):
        with pytest.raises(ValueError, match="provider_unavailable"):
            TokenEstimate(None, "openrouter_metadata", "unknown")

    def test_invariant_violation_exact_source_must_be_trusted(self):
        # exact + 不可信源（crossllm_estimator）应拒
        with pytest.raises(ValueError, match="provider_tokenizer"):
            TokenEstimate(100, "crossllm_estimator", "exact")

    def test_exact_with_provider_tokenizer_ok(self):
        # exact + provider_tokenizer 也是合法
        te = TokenEstimate(100, "provider_tokenizer", "exact")
        assert te.accuracy_mode == "exact"

    def test_frozen(self):
        te = TokenEstimate(100, "openrouter_metadata", "exact")
        with pytest.raises(Exception):
            te.value = 200  # type: ignore[misc]

    def test_to_log_dict(self):
        te = TokenEstimate(176947, "openrouter_metadata", "exact")
        d = te.to_log()
        assert d == {
            "value": 176947,
            "token_source": "openrouter_metadata",
            "token_exactness": "exact",
        }

    def test_to_log_with_unknown(self):
        te = TokenEstimate(None, "provider_unavailable", "unknown")
        d = te.to_log()
        assert d == {
            "value": None,
            "token_source": "provider_unavailable",
            "token_exactness": "unknown",
        }


# ─────────────────────────────────────────────────────────────────────────────
# ProviderModelMetadata（§4.6）
# ─────────────────────────────────────────────────────────────────────────────


class TestProviderModelMetadata:
    def test_field_names_match_revised_2(self):
        """修订 #2：字段名是 model_max_output，不是 provider_max_output_tokens。"""
        meta = ProviderModelMetadata(
            model_slug="x/y",
            context_limit=TokenEstimate(100, "openrouter_metadata", "exact"),
            model_max_output=TokenEstimate(50, "openrouter_metadata", "exact"),
            fetched_at="2026-09-03T00:00:00Z",
            fetch_status="ok",
        )
        assert hasattr(meta, "model_max_output")
        assert not hasattr(meta, "provider_max_output_tokens")

    def test_frozen(self):
        meta = ProviderModelMetadata(
            model_slug="x/y",
            context_limit=TokenEstimate(100, "openrouter_metadata", "exact"),
            model_max_output=TokenEstimate(50, "openrouter_metadata", "exact"),
            fetched_at="t",
            fetch_status="ok",
        )
        with pytest.raises(Exception):
            meta.fetch_status = "failed"  # type: ignore[misc]


# ─────────────────────────────────────────────────────────────────────────────
# ModelCapabilityResolver（§9）—— 测试用 OpenRouter payload 构造器
# ─────────────────────────────────────────────────────────────────────────────


def _openrouter_payload(models: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """构造与 OpenRouter /api/v1/models 一致的响应结构。"""
    return {"data": models or []}


def _model_record(
    *,
    model_id: str,
    context_length: int | None = 196608,
    top_provider_ctx: int | None | str = "SAME",
    max_completion: int | None = 176947,
) -> dict[str, Any]:
    tp_ctx: int | None
    if top_provider_ctx == "SAME":
        tp_ctx = context_length
    else:
        tp_ctx = top_provider_ctx
    top_provider: dict[str, Any] = {"is_moderated": False}
    if tp_ctx is not None:
        top_provider["context_length"] = tp_ctx
    if max_completion is not None:
        top_provider["max_completion_tokens"] = max_completion
    return {
        "id": model_id,
        "context_length": context_length,
        "top_provider": top_provider,
    }


class TestModelCapabilityResolver:
    def test_network_success_returns_metadata(self, tmp_path):
        payload = _openrouter_payload(
            models=[_model_record(model_id="x/y:free", context_length=100000, max_completion=8000)]
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        _patch_async_client_with_transport(handler)
        resolver = ModelCapabilityResolver(disk_cache_path=tmp_path / ".cache.json")
        meta = asyncio.run(resolver.resolve("x/y:free"))
        assert meta.fetch_status == "ok"
        assert meta.model_slug == "x/y:free"
        assert meta.context_limit.value == 100000
        assert meta.context_limit.source == "openrouter_metadata"
        assert meta.context_limit.accuracy_mode == "exact"
        assert meta.model_max_output.value == 8000
        assert meta.model_max_output.accuracy_mode == "exact"
        assert meta.raw is not None and meta.raw["id"] == "x/y:free"

    def test_context_limit_uses_min_of_two_levels(self, tmp_path):
        """C9：minimax-m2.5 实测顶层 204800 / top_provider 200000 → 取 200000。"""
        payload = _openrouter_payload(
            models=[_model_record(
                model_id="m/m2.5",
                context_length=204800,
                top_provider_ctx=200000,
                max_completion=128000,
            )]
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        _patch_async_client_with_transport(handler)
        resolver = ModelCapabilityResolver(disk_cache_path=tmp_path / ".cache.json")
        meta = asyncio.run(resolver.resolve("m/m2.5"))
        assert meta.context_limit.value == 200000  # min(204800, 200000)
        assert meta.model_max_output.value == 128000

    def test_context_limit_handles_missing_top_provider(self, tmp_path):
        """top_provider 缺失时回退到顶层 context_length；max_output 按不变量记 unknown。"""
        payload = {
            "data": [{
                "id": "m/no-top",
                "context_length": 32768,
                # 故意没有 top_provider
            }]
        }

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        _patch_async_client_with_transport(handler)
        resolver = ModelCapabilityResolver(disk_cache_path=tmp_path / ".cache.json")
        meta = asyncio.run(resolver.resolve("m/no-top"))
        assert meta.context_limit.value == 32768
        assert meta.context_limit.source == "openrouter_metadata"
        # provider_max_output 拿不到 → source 严格为 provider_unavailable
        assert meta.model_max_output.value is None
        assert meta.model_max_output.source == "provider_unavailable"
        assert meta.model_max_output.accuracy_mode == "unknown"

    def test_model_not_in_catalog_returns_failed(self, tmp_path):
        payload = _openrouter_payload(models=[])  # 空目录

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        _patch_async_client_with_transport(handler)
        resolver = ModelCapabilityResolver(disk_cache_path=tmp_path / ".cache.json")
        meta = asyncio.run(resolver.resolve("nonexistent/model"))
        assert meta.fetch_status == "failed"
        assert meta.context_limit.accuracy_mode == "unknown"
        assert meta.model_max_output.accuracy_mode == "unknown"
        assert meta.context_limit.value is None
        assert meta.model_max_output.value is None

    def test_network_failure_with_disk_cache_returns_stale(self, tmp_path):
        cache = tmp_path / ".cache.json"
        # 预先写一份"2 小时前"的记录（未过期，ttl=24h）
        seed = {
            "x/y:free": {
                "context_limit": {
                    "value": 100000,
                    "token_source": "openrouter_metadata",
                    "token_exactness": "exact",
                },
                "model_max_output": {
                    "value": 8000,
                    "token_source": "openrouter_metadata",
                    "token_exactness": "exact",
                },
                "_fetched_at_unix": time.time() - 7200,
            }
        }
        cache.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")

        def boom(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        _patch_async_client_with_transport(boom)
        resolver = ModelCapabilityResolver(disk_cache_path=cache, ttl_seconds=86400)
        meta = asyncio.run(resolver.resolve("x/y:free"))
        assert meta.fetch_status == "cached"
        assert meta.context_limit.value == 100000
        assert meta.context_limit.accuracy_mode == "exact"

    def test_network_failure_no_cache_returns_unknown(self, tmp_path):
        def boom(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("network down")

        _patch_async_client_with_transport(boom)
        resolver = ModelCapabilityResolver(disk_cache_path=tmp_path / ".cache.json")
        meta = asyncio.run(resolver.resolve("x/y:free"))
        assert meta.fetch_status == "failed"
        assert meta.context_limit.value is None
        assert meta.model_max_output.value is None
        assert meta.context_limit.source == "provider_unavailable"
        # unknown 严格来源校验
        assert meta.context_limit.accuracy_mode == "unknown"
        assert meta.model_max_output.accuracy_mode == "unknown"

    def test_in_memory_cache_hits_second_call(self, tmp_path):
        calls = {"n": 0}
        payload = _openrouter_payload(
            models=[_model_record(model_id="x/y:free")]
        )

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json=payload)

        _patch_async_client_with_transport(handler)
        resolver = ModelCapabilityResolver(disk_cache_path=tmp_path / ".cache.json")
        a = asyncio.run(resolver.resolve("x/y:free"))
        b = asyncio.run(resolver.resolve("x/y:free"))
        assert calls["n"] == 1, "第二次调用应走 in-memory cache"
        assert a.fetch_status == "ok"
        assert b.fetch_status == "ok"
        assert b.context_limit.value == a.context_limit.value

    def test_disk_cache_written_atomically(self, tmp_path):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_openrouter_payload(models=[_model_record(model_id="x/y:free")]),
            )

        _patch_async_client_with_transport(handler)
        resolver = ModelCapabilityResolver(disk_cache_path=tmp_path / ".cache.json")
        asyncio.run(resolver.resolve("x/y:free"))
        cache = tmp_path / ".cache.json"
        assert cache.exists()
        assert not (tmp_path / ".cache.json.tmp").exists(), "tmp 已被 replace"
        data = json.loads(cache.read_text(encoding="utf-8"))
        assert "x/y:free" in data
        assert data["x/y:free"]["context_limit"]["value"] == 196608

    def test_stale_disk_falls_through_to_network(self, tmp_path):
        """TTL 过期：disk miss → 网络（值会被刷新）。"""
        cache = tmp_path / ".cache.json"
        seed = {
            "x/y:free": {
                "context_limit": {"value": 1, "token_source": "openrouter_metadata", "token_exactness": "exact"},
                "model_max_output": {"value": 1, "token_source": "openrouter_metadata", "token_exactness": "exact"},
                "_fetched_at_unix": time.time() - 86400 * 7,  # 7 天前
            }
        }
        cache.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_openrouter_payload(
                    models=[_model_record(model_id="x/y:free", max_completion=99999)]
                ),
            )

        _patch_async_client_with_transport(handler)
        resolver = ModelCapabilityResolver(disk_cache_path=cache, ttl_seconds=86400)
        meta = asyncio.run(resolver.resolve("x/y:free"))
        assert meta.fetch_status == "ok"
        assert meta.model_max_output.value == 99999, "网络值刷新磁盘过期值"

    def test_no_disk_cache_path_skips_disk_io(self, tmp_path):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_openrouter_payload(models=[_model_record(model_id="x/y:free")]),
            )

        _patch_async_client_with_transport(handler)
        resolver = ModelCapabilityResolver(disk_cache_path=None)
        meta = asyncio.run(resolver.resolve("x/y:free"))
        assert meta.fetch_status == "ok"
        assert list(tmp_path.iterdir()) == [], "无 disk_cache_path 时不写任何文件"

    def test_invalidate_clears_memory_cache(self, tmp_path):
        resolver = ModelCapabilityResolver(disk_cache_path=tmp_path / ".cache.json")
        meta = ProviderModelMetadata(
            model_slug="x/y:free",
            context_limit=TokenEstimate(100, "openrouter_metadata", "exact"),
            model_max_output=TokenEstimate(50, "openrouter_metadata", "exact"),
            fetched_at="t",
            fetch_status="ok",
        )
        resolver._memory_cache["x/y:free"] = meta
        resolver.invalidate()
        assert resolver._memory_cache == {}


# ─────────────────────────────────────────────────────────────────────────────
# S3.1 边界守门：confirm 本模块不产生/修改其他业务代码
# ─────────────────────────────────────────────────────────────────────────────


def test_module_exports_only_s31_surface():
    """S3.1 范围守门：模块公共 API 严格限定。"""
    from core import model_capability as mc
    public = {n for n in dir(mc) if not n.startswith("_")}
    expected = {
        "TokenEstimate",
        "TokenSource",
        "AccuracyMode",
        "ProviderModelMetadata",
        "FetchStatus",
        "ModelCapabilityResolver",
    }
    missing = expected - public
    assert not missing, f"S3.1 公共 API 缺失：{missing}"
