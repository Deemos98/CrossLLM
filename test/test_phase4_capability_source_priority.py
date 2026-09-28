"""§一 能力来源优先级链路（Provider 元数据 → models.yaml fallback → unknown）。

本文件只覆盖 **改造后新增** 的语义，不重复：

  - ``test_phase4_core_units.py``              —— ModelCapabilityResolver 本体（联网 / 缓存 / 解析）
  - ``test_phase4_provider_output_budget.py``  —— reserved 四上限 + Provider 侧接线
  - ``test_phase2.py``                         —— models.yaml 拒绝 max_output_tokens

逐条对应任务书 §十 的验收项：

  1. 网络能力 > YAML fallback           → test_provider_capability_overrides_yaml_fallback
  2. 网络失败 → YAML fallback            → test_provider_failure_uses_yaml_fallback
  3. 两者都没有 → unknown                → test_no_source_at_all_is_unknown
  4. YAML max_output_tokens 不再作能力    → test_yaml_never_supplies_model_max_output
  5. 不存在 context_limit * 0.9           → test_no_empirical_ratio_in_capability_chain
  6. model_max_output ≠ request_max_output → test_model_capability_is_not_request_limit
  7. k 只影响 lossless / compaction        → test_k_only_affects_lossless_and_compaction
  8. reserved unknown → degraded           → test_unknown_reserved_marks_budget_degraded
  9. Input + known Reserved ≤ Limit        → test_input_plus_known_reserved_within_limit
 10. 现有行为不变                          → 全量 pytest + scripts/run_phase4_e2e.py

能力来源的取值优先级是**生产代码** ``core/round_manager._resolve_round_capability``
的行为，所以 1/2/3 直接在 RoundManager 层面验证（走同一条合并链路），
不放宽成"只测 helper"。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from _capability_stub import FailingCapabilityResolver, StubCapabilityResolver
from core.context_budget import (
    BudgetOverflowError,
    compute_budget,
    make_fixed_input_tokens,
    validate_hard_constraint,
)
from core.model_capability import (
    UNKNOWN_TOKEN,
    ProviderModelMetadata,
    TokenEstimate,
    apply_context_limit_fallback,
    fallback_only_metadata,
)
from core.model_manager import ModelRegistry, ProviderConfig
from core.round_manager import RoundManager
from core.state import RoundState

# ── YAML fallback 与 Provider 值刻意取不同数字，才能证明"谁覆盖谁" ──
YAML_FALLBACK = 16384          # models.yaml（人工 fallback）
PROVIDER_CTX = 4096            # Provider 元数据（真实能力）

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _registry_with_yaml_fallback(
    ctx: int | None = YAML_FALLBACK,
) -> ModelRegistry:
    """注册表：YAML 侧只提供 context_limit fallback（无 max_output_tokens 字段）。"""
    configs = {
        mid: ProviderConfig(
            model_id=mid,
            display_name=mid.upper(),
            model=f"fake/{mid}",
            context_limit=ctx,
        )
        for mid in ("qwen", "deepseek")
    }
    return ModelRegistry(configs=configs)


def _rm(resolver) -> RoundManager:
    return RoundManager(
        storage=None,             # type: ignore[arg-type]  本文件不落盘
        registry=_registry_with_yaml_fallback(),
        provider_factory=None,    # type: ignore[arg-type]
        capability_resolver=resolver,
    )


def _round_state() -> RoundState:
    return RoundState(
        round_id=2,
        user_instruction="Q?",
        participants=["qwen", "deepseek"],
    )


def _resolve(resolver) -> ProviderModelMetadata:
    return asyncio.run(_rm(resolver)._resolve_round_capability(_round_state()))


# ══════════════════════════════════════════════════════════════════════════
# §十-1 网络能力 > YAML fallback
# ══════════════════════════════════════════════════════════════════════════


def test_provider_capability_overrides_yaml_fallback():
    """Provider 查到 4096 → 必须覆盖 YAML 的 16384，且**不**降级 source。"""
    resolver = StubCapabilityResolver(
        context_limit=PROVIDER_CTX, model_max_output=1024,
    )
    meta = _resolve(resolver)

    assert meta.context_limit.value == PROVIDER_CTX
    assert meta.context_limit.source == "openrouter_metadata", (
        "Provider 查询成功时不得把 source 改写成 config_static"
    )
    assert meta.context_limit.accuracy_mode == "exact"
    assert meta.fetch_status == "ok"
    assert YAML_FALLBACK not in (
        meta.context_limit.value,
    ), "YAML fallback 值不得泄漏进结果"
    # 多模型取 min，两者同值
    assert resolver.calls == ["fake/qwen", "fake/deepseek"]


def test_provider_metadata_carries_model_max_output_from_provider_only():
    """model_max_output 来自 Provider 元数据本身（YAML 无此字段可给）。"""
    meta = _resolve(
        StubCapabilityResolver(context_limit=PROVIDER_CTX, model_max_output=777)
    )
    assert meta.model_max_output.value == 777
    assert meta.model_max_output.source == "openrouter_metadata"


# ══════════════════════════════════════════════════════════════════════════
# §十-2 网络失败 → YAML fallback
# ══════════════════════════════════════════════════════════════════════════


def test_provider_failure_uses_yaml_fallback():
    """Provider 失败 → 用 YAML，且 source 必须诚实标 config_static。"""
    meta = _resolve(FailingCapabilityResolver())

    assert meta.context_limit.value == YAML_FALLBACK
    assert meta.context_limit.source == "config_static", (
        "fallback 就是 fallback，不得伪装成 Provider 能力"
    )
    assert meta.context_limit.accuracy_mode == "estimated"
    assert meta.fetch_status == "failed"


def test_provider_failure_without_yaml_is_unknown():
    """Provider 失败 + YAML 未提供 → unknown（不猜值）。"""
    rm = RoundManager(
        storage=None, registry=_registry_with_yaml_fallback(None),  # type: ignore[arg-type]
        provider_factory=None,                                       # type: ignore[arg-type]
        capability_resolver=FailingCapabilityResolver(),
    )
    meta = asyncio.run(rm._resolve_round_capability(_round_state()))

    assert meta.context_limit.value is None
    assert meta.context_limit.source == "provider_unavailable"
    assert meta.context_limit.accuracy_mode == "unknown"
    assert meta.fetch_status == "failed"


# ══════════════════════════════════════════════════════════════════════════
# §十-3 两者都没有 → unknown
# ══════════════════════════════════════════════════════════════════════════


def test_no_source_at_all_is_unknown():
    """无 resolver（不可查）+ YAML 无值 → 全 unknown，fetch_status=not_fetched。"""
    rm = RoundManager(
        storage=None, registry=_registry_with_yaml_fallback(None),  # type: ignore[arg-type]
        provider_factory=None,                                       # type: ignore[arg-type]
        capability_resolver=None,
    )
    meta = asyncio.run(rm._resolve_round_capability(_round_state()))

    assert meta.context_limit is UNKNOWN_TOKEN
    assert meta.model_max_output is UNKNOWN_TOKEN
    assert meta.fetch_status == "not_fetched"


def test_apply_context_limit_fallback_is_pure_and_idempotent():
    """合成函数本身的三分支（纯函数层），且幂等。"""
    # ① Provider 有值 → 原样返回
    ok = fallback_only_metadata("s", None)
    ok = apply_context_limit_fallback(
        apply_context_limit_fallback(ok, YAML_FALLBACK), YAML_FALLBACK,
    )
    assert ok.context_limit.value == YAML_FALLBACK
    assert ok.context_limit.source == "config_static"

    # ② YAML 也无 → unknown
    assert apply_context_limit_fallback(
        fallback_only_metadata("s", None), None,
    ).context_limit is UNKNOWN_TOKEN

    # ③ Provider 有值 → YAML 被忽略
    provider_meta = ProviderModelMetadata(
        model_slug="s",
        context_limit=TokenEstimate(PROVIDER_CTX, "openrouter_metadata", "exact"),
        model_max_output=UNKNOWN_TOKEN,
        fetched_at="2026-09-25T00:00:00Z",
        fetch_status="ok",
    )
    merged = apply_context_limit_fallback(provider_meta, YAML_FALLBACK)
    assert merged.context_limit.value == PROVIDER_CTX
    assert merged.context_limit.source == "openrouter_metadata"


# ══════════════════════════════════════════════════════════════════════════
# §十-4 YAML max_output_tokens 不再作为模型能力来源
# ══════════════════════════════════════════════════════════════════════════


def test_yaml_never_supplies_model_max_output():
    """ProviderModelMetadata / ProviderConfig 都不再有 YAML 侧输出能力字段。"""
    assert "max_output_tokens" not in ProviderConfig.__dataclass_fields__, (
        "models.yaml 的 max_output_tokens 必须已删除"
    )
    # 字段名不得是旧名 provider_max_output_tokens
    assert "provider_max_output_tokens" not in (
        ProviderModelMetadata.__dataclass_fields__
    )
    # YAML-only 路径下 model_max_output 恒 unknown
    meta = fallback_only_metadata("s", YAML_FALLBACK, fetch_status="failed")
    assert meta.model_max_output is UNKNOWN_TOKEN, (
        "YAML 不是模型能力来源，model_max_output 必须 unknown"
    )
    assert meta.context_limit.value == YAML_FALLBACK  # 只有 ctx 走 fallback

    # 合并链路同样不得把 model_max_output 变成配置侧值：
    # ctx 回退到 YAML（config_static）时，model_max_output 必须原样保持 unknown。
    merged = apply_context_limit_fallback(
        fallback_only_metadata("s", None, fetch_status="failed"), YAML_FALLBACK
    )
    assert merged.context_limit.source == "config_static"
    assert merged.model_max_output.source != "config_static", (
        "model_max_output 只能来自 Provider 元数据，不得被 YAML fallback 顶替"
    )
    assert merged.model_max_output is UNKNOWN_TOKEN


def test_config_does_not_accept_deprecated_field(tmp_path):
    """models.yaml 仍写 max_output_tokens → 加载即硬失败（不静默忽略）。

    与 ``test_phase2.test_deprecated_max_output_tokens_is_rejected`` 同源；
    此处只补"YAML 侧没有**任何**输出能力入口"的结构断言（字段名核对）。
    """
    p = tmp_path / "models.yaml"
    p.write_text(
        """
provider:
  name: fake
  type: openai_compatible
  base_url: http://x/v1
  api_key_env: CROSSLLM_API_KEY_FAKE
models:
  - id: qwen
    model: fake/qwen
    max_output_tokens: 8192
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="max_output_tokens"):
        ModelRegistry.from_yaml(p)


# ══════════════════════════════════════════════════════════════════════════
# §十-5 不存在经验系数（源码守门，只扫真实代码 token）
# ══════════════════════════════════════════════════════════════════════════


def _code_only(path: Path) -> str:
    """剥离注释与字符串字面量后的纯代码。

    docstring 里写"禁止使用 context_limit * 0.9"是**说明**，不是经验系数；
    只按行首 ``#`` 过滤会误报，所以用 tokenize 精确剔除 COMMENT / STRING /
    FSTRING 三类 token。
    """
    import io
    import tokenize

    src = path.read_text(encoding="utf-8")
    # tokenize.FSTRING 只在 Python 3.13+ 存在（3.12 里 f-string 是 STRING）
    skip_types = {tokenize.COMMENT, tokenize.STRING}
    fstring = getattr(tokenize, "FSTRING", None)
    if fstring is not None:
        skip_types.add(fstring)

    pieces: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in skip_types:
            continue
        pieces.append(tok.string)
    return "\n".join(pieces)


@pytest.mark.parametrize("rel", [
    "core/model_capability.py",
    "core/context_budget.py",
    "core/round_manager.py",
    "providers/real_provider.py",
])
def test_no_empirical_ratio_in_capability_chain(rel):
    """能力 / 预算链路里不得再出现 ``context_limit * 0.9`` 之类经验系数。"""
    code = _code_only(_REPO_ROOT / rel)
    forbidden = [
        r"\*\s*0\.9\b", r"\b0\.9\s*\*",
        r"\*\s*0\.95\b", r"\b0\.95\s*\*",
        r"OUTPUT_SAFETY_MARGIN", r"\b16384\b", r"\b16000\b", r"\b8192\b",
    ]
    hits = [p for p in forbidden if re.search(p, code)]
    assert not hits, f"{rel} 仍含经验系数：{hits}"


def test_guard_would_catch_a_real_regression(tmp_path):
    """守门器本身有效：真的写入经验系数必须被命中（防空跑）。"""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "def f(context_limit):\n"
        "    # 注释里提到 0.9 不算\n"
        '    """docstring 里的 0.9 也不算"""\n'
        "    return int(context_limit * 0.9)\n",
        encoding="utf-8",
    )
    code = _code_only(bad)
    assert re.search(r"\*\s*0\.9\b", code), "守门器应命中真实代码中的 0.9 系数"
    # 注释 / docstring 必须已被剔除
    assert "不算" not in code


# ══════════════════════════════════════════════════════════════════════════
# §十-6 / 7 / 8 / 9：预算层语义（用 compute_budget 的真实路径）
# ══════════════════════════════════════════════════════════════════════════


def _meta_pair(ctx: int | None, model_max: int | None) -> ProviderModelMetadata:
    def _te(v: int | None) -> TokenEstimate:
        if v is None:
            return TokenEstimate(None, "provider_unavailable", "unknown")
        return TokenEstimate(v, "openrouter_metadata", "exact")

    return ProviderModelMetadata(
        model_slug="fake/m",
        context_limit=_te(ctx),
        model_max_output=_te(model_max),
        fetched_at="2026-09-25T00:00:00Z",
        fetch_status="ok",
    )


def _budget(
    ctx: int | None = 32768,
    model_max: int | None = 8192,
    request_max: int | None = 4096,
    k: float = 0.5,
):
    req = (
        TokenEstimate(request_max, "config_static", "estimated")
        if request_max is not None else UNKNOWN_TOKEN
    )
    return compute_budget(
        metadata=_meta_pair(ctx, model_max),
        request_max_output=req,
        fixed=make_fixed_input_tokens(system_text="sys", current_user_text="u"),
        k=k,
    )


def test_model_capability_is_not_request_limit():
    """§三 / §四：model_max_output 是能力，request_max_output 是本次请求，两者独立。"""
    # 请求上限 < 能力上限
    b = _budget(model_max=8192, request_max=4096)
    assert b.model_max_output.value == 8192
    assert b.request_max_output.value == 4096
    assert b.reserved_output_tokens.value == 4096     # min

    # 请求上限 > 能力上限 → reserved 被能力上限夹住
    b2 = _budget(model_max=1024, request_max=99999)
    assert b2.model_max_output.value == 1024
    assert b2.request_max_output.value == 99999
    assert b2.reserved_output_tokens.value == 1024
    # 能力未知时**不得**被 request 值污染
    b3 = _budget(model_max=None, request_max=4096)
    assert b3.model_max_output.value is None
    assert b3.model_max_output.source == "provider_unavailable"


def test_k_only_affects_lossless_and_compaction():
    """§六：k 只决定 lossless / compaction 分配，不参与任何其它上限。"""
    budgets = [_budget(k=k) for k in (0.01, 0.3, 0.5, 0.9)]
    invariant = [
        (
            b.context_limit.value,
            b.model_max_output.value,
            b.request_max_output.value,
            b.reserved_output_tokens.value,
            b.fixed_input_tokens.value,
            b.history_budget.value,
        )
        for b in budgets
    ]
    assert len(set(invariant)) == 1, f"k 不得影响这些值：{invariant}"

    # 但必须影响 lossless / compaction
    lossless = [b.lossless_budget.value for b in budgets]
    compaction = [b.compaction_budget.value for b in budgets]
    assert lossless == sorted(lossless)          # 单调增
    assert compaction == sorted(compaction, reverse=True)  # 单调减
    assert max(lossless) > min(lossless)
    # 且两者之和 ≠ 随 k 变化（守住 history_budget 守恒）
    hist = {b.history_budget.value for b in budgets}
    assert len(hist) == 1
    for b in budgets:
        assert b.lossless_budget.value + b.compaction_budget.value == \
            b.history_budget.value


def test_unknown_reserved_marks_budget_degraded():
    """§五 / §七：reserved 未知 → degraded；不得伪造数字。"""
    b = _budget(model_max=None, request_max=None)
    assert b.reserved_output_tokens.value is None
    assert b.reserved_output_tokens.source == "provider_unavailable"
    assert b.budget_status == "degraded"
    # unknown 时不得编出一个 history_budget 数字
    assert b.history_budget.value is None

    # 已知 → complete
    ok = _budget(model_max=8192, request_max=4096)
    assert ok.reserved_output_tokens.value == 4096
    assert ok.budget_status == "complete"
    assert ok.history_budget.value == (
        ok.context_limit.value - ok.fixed_input_tokens.value
        - ok.reserved_output_tokens.value
    )
    # ctx / reserved 任一未知 → degraded
    assert _budget(ctx=None, model_max=8192).budget_status == "degraded"


def test_input_plus_known_reserved_within_limit():
    """§十-9：已知 reserved 时 ``fixed_used + reserved ≤ context_limit`` 恒成立。"""
    for ctx, model_max, request_max in (
        (32768, 8192, 4096), (8192, 2048, 99999), (1000, 600, 600),
        (200000, 128000, 128000),
    ):
        b = _budget(ctx=ctx, model_max=model_max, request_max=request_max)
        fx = b.fixed_input_tokens.value
        res = b.reserved_output_tokens.value
        used = b.recent_raw_tokens.value
        assert fx + res <= ctx, f"fixed({fx}) + reserved({res}) > ctx({ctx})"
        # §24 闸门在已知数据集上不抛错
        validate_hard_constraint(b)
        if used is not None:
            assert fx + used + res <= ctx


def test_hard_constraint_raises_on_known_overflow():
    """§24 闸门在已知数据真越界时必须抛（不是空跑）。"""
    # ctx=1000，固定输入约 134，reserved=900 → fixed + 0 + reserved > 1000
    b = compute_budget(
        metadata=_meta_pair(1000, 900),
        request_max_output=TokenEstimate(900, "config_static", "estimated"),
        fixed=make_fixed_input_tokens(system_text="x" * 400),
        k=0.5,
    )
    assert b.fixed_input_tokens.value + b.reserved_output_tokens.value > 1000
    assert b.budget_status == "complete"          # 三值都已知，越界必须被拦
    assert b.history_budget.value < 0             # 不静默 clamp 到 0

    with pytest.raises(BudgetOverflowError):
        validate_hard_constraint(b)


# ══════════════════════════════════════════════════════════════════════════
# 字段命名统一（收尾问题 2）
#
#   canonical（CrossLLM 内部一律用这三个名字）：
#       model_max_output / request_max_output / reserved_output_tokens
#   deprecated（已废弃，只准出现在"废弃说明 / 防复活断言"里）：
#       provider_max_output_tokens / model_max_output_tokens
#       requests_max_output_tokens  / request_max_output_tokens
#   lib-level（只作底层 Provider / API 参数名，不得当 models.yaml 能力字段）：
#       max_output_tokens / max_completion_tokens
# ══════════════════════════════════════════════════════════════════════════

CANONICAL_FIELDS = ("model_max_output", "request_max_output", "reserved_output_tokens")
DEPRECATED_FIELDS = (
    "provider_max_output_tokens",
    "model_max_output_tokens",
    "requests_max_output_tokens",
    "request_max_output_tokens",
)
# 出现旧名时，该行必须同时含下列词之一，才算"这是废弃说明而非真的在用"
_DEPRECATION_MARKERS = ("废弃", "旧名", "不得", "不是", "禁止", "已改名", "曾经")


def test_canonical_field_names_exist_and_deprecated_ones_do_not():
    """三个 canonical 字段名必须存在；旧名不得作为字段复活。"""
    from core.context_budget import ContextBudget, ReservedOutputTokens
    from core.model_capability import ProviderModelMetadata

    # ProviderModelMetadata 只描述**模型能力元数据**，本来就不含 request 侧字段；
    # ContextBudget / ReservedOutputTokens 才是三个上限齐备的地方。
    expected = {
        ProviderModelMetadata: ("model_max_output",),
        ContextBudget: CANONICAL_FIELDS,
        ReservedOutputTokens: CANONICAL_FIELDS,
    }
    for cls, names in expected.items():
        fields = set(cls.__dataclass_fields__)
        assert fields, f"{cls.__name__}.__dataclass_fields__ 为空，用例空跑"
        for name in names:
            assert name in fields, f"{cls.__name__} 缺 canonical 字段 {name}"
        for old in DEPRECATED_FIELDS:
            assert old not in fields, f"{cls.__name__} 复活了废弃字段 {old}"


@pytest.mark.parametrize("rel", [
    "core/model_capability.py",
    "core/context_budget.py",
    "core/round_manager.py",
    "core/state.py",
    "core/model_manager.py",
    "providers/real_provider.py",
    "providers/base.py",
    "app.py",
    "static/app.js",
])
def test_deprecated_field_names_do_not_appear_in_production_code(rel):
    """生产代码里不得再出现废弃字段名（旧名只允许存在于"废弃说明"注释里）。

    对 ``static/app.js`` 不做 tokenize（不是 Python），改用"行内含废弃标记词则放行"。
    """
    path = _REPO_ROOT / rel
    src = path.read_text(encoding="utf-8")

    offenders = []
    for lineno, line in enumerate(src.splitlines(), 1):
        hit = [n for n in DEPRECATED_FIELDS if re.search(r"\b" + n + r"\b", line)]
        if not hit:
            continue
        if any(m in line for m in _DEPRECATION_MARKERS):
            continue  # 废弃说明，合法
        offenders.append((lineno, line.strip(), hit))

    assert not offenders, f"{rel} 仍在使用废弃字段名：\n" + "\n".join(
        f"  L{n}: {t}  {h}" for n, t, h in offenders
    )


def test_guards_would_catch_a_real_regression(tmp_path):
    """防空跑：把废弃字段名写进一份"假生产文件"，守门规则必须报出来。"""
    fake = tmp_path / "fake.py"
    fake.write_text(
        "x = cfg.provider_max_output_tokens\n"
        "# 旧名 provider_max_output_tokens 已废弃\n",
        encoding="utf-8",
    )
    src = fake.read_text(encoding="utf-8")
    offenders = [
        ln for ln, line in enumerate(src.splitlines(), 1)
        if any(re.search(r"\b" + n + r"\b", line) for n in DEPRECATED_FIELDS)
        and not any(m in line for m in _DEPRECATION_MARKERS)
    ]
    assert offenders == [1], f"守门规则没抓到真实回归（offenders={offenders}）"


def test_max_output_tokens_is_lib_level_param_not_capability_field():
    """``max_output_tokens`` / ``max_completion_tokens`` 只是底层参数名。

    不得作为 **models.yaml 的抽象能力字段** 复活：
      - ``ProviderConfig`` 无该字段（模型能力只走 model_max_output）
      - ``models.yaml`` 写它 → 加载即硬失败
    """
    from core.model_manager import ModelRegistry, ProviderConfig

    for lib_name in ("max_output_tokens", "max_completion_tokens"):
        assert lib_name not in ProviderConfig.__dataclass_fields__, (
            f"ProviderConfig 不得含底层参数名 {lib_name}（那是 Provider/API 参数）"
        )

    cfg_dir = _REPO_ROOT / "config"
    ymls = sorted(cfg_dir.glob("*.yaml"))
    assert ymls, "config/ 下没有任何 yaml，用例空跑"
    for y in ymls:
        text = y.read_text(encoding="utf-8")
        for lib_name in ("max_output_tokens", "max_completion_tokens"):
            for ln, line in enumerate(text.splitlines(), 1):
                if re.search(r"^\s*" + lib_name + r"\s*:", line):
                    # 允许注释掉的示例（守门只拦真正生效的键）
                    assert line.lstrip().startswith("#"), (
                        f"{y.name}:{ln} models.yaml 不得把 {lib_name} 当能力字段"
                    )

    # 仍必须能正常加载（说明上面没有误伤）
    assert ModelRegistry.from_yaml(_REPO_ROOT / "config" / "models.yaml").participants


def test_internal_docs_use_unified_field_names():
    """CrossLLM 内部文档统一使用 canonical 命名。

    允许旧名出现，但**同一行必须带废弃标记**，否则视为文档里还在用旧命名。
    """
    docs = [_REPO_ROOT / "README.md"] + sorted((_REPO_ROOT / "docs").glob("*.md"))
    assert len(docs) >= 5, "文档数量异常，用例可能空跑"

    scanned = 0
    offenders = []
    for d in docs:
        if not d.is_file():
            continue
        for lineno, line in enumerate(d.read_text(encoding="utf-8").splitlines(), 1):
            scanned += 1
            hit = [n for n in DEPRECATED_FIELDS if re.search(r"\b" + n + r"\b", line)]
            if not hit:
                continue
            if any(m in line for m in _DEPRECATION_MARKERS):
                continue
            offenders.append(f"{d.name}:{lineno}  {line.strip()[:90]}")

    assert scanned > 1000, f"只扫到 {scanned} 行，用例空跑"
    assert not offenders, "内部文档仍在用废弃字段名：\n" + "\n".join(offenders)
