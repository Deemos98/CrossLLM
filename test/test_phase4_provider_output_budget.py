"""S3.11 Provider Output Budget 接线测试（MD §22 §23 §24）。

链路：

    ContextBudget.reserved_output_tokens（预算层；R1 与 R2+ 都要建）
        ↓ RoundManager（写入 RoundState.reserved_output_tokens）
        ↓ _run_one → provider.generate(max_output_tokens=…)
        ↓ real_provider._dynamic_max_tokens(messages, tools, reserved)
        ↓ reserved 已知 → body["max_tokens"] = min(reserved, context_limit - actual_input)
          reserved 未知 → **不下发** max_tokens（不虚构上限）

四个上限严格区分：

    model_max_output       —— 模型自身能力（**只**来自 Provider 元数据）
    request_max_output     —— 本次请求显式上限
    reserved_output_tokens —— min(model_max_output, request_max_output)，预算层预留
    actual_output_limit    —— min(reserved, remaining)，真正发给 Provider

验收项：
 1. model_max > request → reserved = min(model_max, request)
 2. model_max < request → reserved = min(model_max, request)
 3. model_max unknown  → 按既定降级策略（reserved = request）
 4. request unknown    → 按既定降级策略（reserved = model_max）
 5. Provider 实际使用 reserved，不重算另一套预算
 6. Input + actual output limit <= context_limit
 7. MockProvider / Protocol 向后兼容
 8. 日志可对账（model_max_output / request_max_output / reserved）
 9. R1 也建立请求预算（reserved 写回 + context_budget 事件），且**不**把
    context_limit 的剩余空间当作百万级 max_tokens
10. state.json 保留本轮真实的 context_limit / reserved_output_tokens

禁止：固定 16000 / 80% ratio / HISTORY_WINDOW / should_compact 不得恢复。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import tempfile
from pathlib import Path

import pytest

from _capability_stub import StubCapabilityResolver


def _val(tok):
    """兼容三种形态：TokenEstimate 对象 / 旧嵌套日志 dict / §八 平铺数字。"""
    if tok is None or isinstance(tok, (int, float)):
        return tok
    if isinstance(tok, dict):
        return tok.get("value")
    return getattr(tok, "value", None)


# ── 1~4. 四种 provider / request 组合 → reserved（消费既有 compute_budget）──


def _meta(provider_max, source="config_static"):
    from core.model_capability import ProviderModelMetadata, TokenEstimate

    unknown = provider_max is None
    # TokenEstimate 不变量：accuracy='unknown' ⟺ value is None，且此时
    # source 必须为 'provider_unavailable'
    src = "provider_unavailable" if unknown else source
    return ProviderModelMetadata(
        model_slug="fake/m",
        context_limit=TokenEstimate(32768, "config_static", "estimated"),
        model_max_output=TokenEstimate(
            provider_max, src, "unknown" if unknown else "estimated",
        ),
        fetched_at="2026-09-05T00:00:00Z",
        fetch_status="ok",
    )


def _req(v):
    from core.model_capability import TokenEstimate

    if v is None:
        return TokenEstimate(None, "provider_unavailable", "unknown")
    return TokenEstimate(v, "config_static", "estimated")


def _budget(provider_max, request_max, k=0.5):
    from core.context_budget import compute_budget, make_fixed_input_tokens

    fixed = make_fixed_input_tokens(system_text="sys", current_user_text="u")
    return compute_budget(
        metadata=_meta(provider_max),
        request_max_output=_req(request_max),
        fixed=fixed,
        k=k,
    )


def test_1_provider_gt_request():
    """provider(8192) > request(4096) → reserved = 4096 = request。"""
    b = _budget(8192, 4096)
    assert _val(b.reserved_output_tokens) == 4096


def test_2_provider_lt_request():
    """provider(2048) < request(4096) → reserved = 2048 = provider。"""
    b = _budget(2048, 4096)
    assert _val(b.reserved_output_tokens) == 2048


def test_3_provider_unknown_falls_back_to_request():
    """provider unknown + request(4096) → reserved = 4096（§9.2 分支②）。"""
    b = _budget(None, 4096)
    assert _val(b.reserved_output_tokens) == 4096


def test_4_request_unknown_falls_back_to_provider():
    """provider(2048) + request unknown → reserved = 2048（§9.2 分支①）。"""
    b = _budget(2048, None)
    assert _val(b.reserved_output_tokens) == 2048


def test_reserved_never_exceeds_either_cap():
    """reserved <= min(provider, request) 恒成立。"""
    for p, r in ((8192, 4096), (2048, 4096), (4096, 4096), (1000, 5000)):
        assert _val(_budget(p, r).reserved_output_tokens) <= min(p, r)


# ── 5. Provider 实际使用 reserved（不重算）───────────────────────────


class _CfgStub:
    """最小 config 替身（RealProvider 只用到这些字段）。"""

    def __init__(self, context_limit=8192, max_output_tokens=2048):
        self.context_limit = context_limit
        self.max_output_tokens = max_output_tokens
        self.model = "fake/m"
        self.params = {}
        self.connect_timeout = 5
        self.read_timeout = 10


def _make_provider(context_limit=8192, max_output_tokens=2048,
                   injected_context_limit=None):
    """构造最小 RealProvider（绕过 __init__ 的网络/设置依赖）。

    injected_context_limit = RoundState 注入的本轮 context_limit（§一）：
    None → 回退 _cfg.context_limit（YAML fallback）。
    """
    from providers.real_provider import RealProvider

    p = RealProvider.__new__(RealProvider)  # 绕过 __init__ 的网络/设置依赖
    p._cfg = _CfgStub(context_limit, max_output_tokens)
    p._reserved_output_tokens = None
    p._context_limit = injected_context_limit
    return p


def test_5_provider_uses_reserved_not_own_calc():
    """Provider 必须采用传入的 reserved，而不是自己发明另一套上限。

    cfg.max_output_tokens 已删除（模型能力只来自 Provider 元数据）：
    reserved=1000 → 输出 1000；reserved 缺失 → **不下发** max_tokens，
    既不虚构固定值，也**不**把 context_limit 的剩余空间当成输出上限。
    """
    p = _make_provider(context_limit=8192, max_output_tokens=2048)
    msgs = [{"role": "user", "content": "x" * 90}]  # 输入很小，remaining 充足

    # 未指定 reserved → 不下发（剩余空间不是"输出上限"的合法来源）
    assert p._dynamic_max_tokens(msgs, None, None) is None
    # 指定 reserved → 采用预算层的值
    assert p._dynamic_max_tokens(msgs, None, 1000) == 1000


def test_5b_reserved_greater_than_provider_cap_still_clamped_by_remaining():
    """reserved 大于 remaining 时，硬约束优先（input+output<=limit）。"""
    p = _make_provider(context_limit=1000, max_output_tokens=8192)
    # 输入约占据大部分窗口 → remaining 很小
    msgs = [{"role": "user", "content": "x" * 2900}]  # ≈ 967 tokens
    out = p._dynamic_max_tokens(msgs, None, 8000)
    assert out <= 1000 - 967 + 1  # 允许估算误差
    assert out > 0


def test_6_input_plus_output_within_context_limit():
    """MD §24 硬约束：input + actual_output <= context_limit。"""
    p = _make_provider(context_limit=8192, max_output_tokens=4096)
    for n_chars in (90, 900, 9000, 20000):
        msgs = [{"role": "user", "content": "x" * n_chars}]
        actual_input = p._estimate_messages_tokens(msgs, None)
        actual_output = p._dynamic_max_tokens(msgs, None, 4096)
        assert actual_input + actual_output <= 8192, (
            f"input={actual_input} + output={actual_output} > 8192"
        )


def test_input_exceeds_limit_raises():
    """输入占满窗口 → 明确报错（不猜测值）。"""
    from providers.base import ProviderError

    p = _make_provider(context_limit=100, max_output_tokens=2048)
    msgs = [{"role": "user", "content": "x" * 5000}]
    with pytest.raises(ProviderError):
        p._dynamic_max_tokens(msgs, None, 2048)


def test_invalid_reserved_falls_back_safely():
    """reserved <= 0 视为"预算层给不出" → 不下发 max_tokens（不产生非法值）。

    **不**回退成"剩余空间"：那等于把 context_limit 当输出上限（§五 / §六）。
    """
    p = _make_provider(context_limit=8192, max_output_tokens=2048)
    msgs = [{"role": "user", "content": "x" * 90}]
    assert p._dynamic_max_tokens(msgs, None, 0) is None
    assert p._dynamic_max_tokens(msgs, None, -1) is None
    # 有效 reserved 不受影响
    assert p._dynamic_max_tokens(msgs, None, 2048) == 2048


# ── 7. MockProvider / Protocol 向后兼容 ─────────────────────────────────


def test_7_protocol_and_mock_accept_new_param():
    from providers.base import LLMProvider
    from providers.mock_provider import MockProvider
    from providers.real_provider import RealProvider

    for cls in (MockProvider, RealProvider):
        params = inspect.signature(cls.generate).parameters
        assert "max_output_tokens" in params
        # 必须可选（不传 = 旧调用方行为不变）
        assert params["max_output_tokens"].default is None
    # 仍满足 Protocol
    assert isinstance(MockProvider("qwen", delay_scale=0.0), LLMProvider)


def test_7b_mock_ignores_param_and_still_streams():
    from providers.mock_provider import MockProvider

    async def _run():
        p = MockProvider("qwen", delay_scale=0.0)
        buf = []
        async for chunk in p.generate(round_id=1, prompt="hi",
                                      max_output_tokens=16):
            buf.append(chunk)
        return "".join(buf)

    out = asyncio.run(_run())
    assert out  # Mock 正常产出，不受 max_output_tokens 影响


def test_7c_old_callers_without_param_still_work():
    from providers.mock_provider import MockProvider

    async def _run():
        p = MockProvider("qwen", delay_scale=0.0)
        return [c async for c in p.generate(round_id=1, prompt="hi")]

    assert asyncio.run(_run())


# ── 9. §一：Provider 侧**实际**使用的 context_limit 来源 ────────────────
#
# 要求：本轮经 Provider Metadata Resolver 得到的有效 context_limit 必须成为
# Provider 输出预算计算实际使用的值；models.yaml.context_limit 只在 Provider
# 元数据失败/不可用时作 fallback。Provider Protocol / generate() 签名不变。


def test_9_provider_uses_resolved_context_limit_over_yaml():
    """§一：Provider 解析值 > YAML —— 实际计算用的是 Provider 值。"""
    p = _make_provider(context_limit=8192, injected_context_limit=100000)
    msgs = [{"role": "user", "content": "x" * 90}]  # ≈30 tokens

    assert p._effective_context_limit() == 100000
    # 未给 reserved → 不下发 max_tokens（**不**把剩余空间当输出上限）
    assert p._dynamic_max_tokens(msgs, None, None) is None
    # reserved 可以超过 YAML 值 —— 证明 YAML 不再是上限
    assert p._dynamic_max_tokens(msgs, None, 50000) == 50000
    # §24：input + output ≤ Provider 给出的 context_limit
    actual_input = p._estimate_messages_tokens(msgs, None)
    assert actual_input + p._dynamic_max_tokens(msgs, None, 50000) <= 100000


def test_9b_yaml_context_limit_is_fallback_only():
    """§一：只有 Provider 值缺失时才用 models.yaml（fallback，不伪装）。"""
    p = _make_provider(context_limit=8192, injected_context_limit=None)
    msgs = [{"role": "user", "content": "x" * 90}]

    assert p._effective_context_limit() == 8192
    # fallback 值参与 §24 求交：reserved=100000 被 8192-30 截断（证明用的是 YAML 值）
    assert p._dynamic_max_tokens(msgs, None, 100000) == 8192 - 30
    # 无 reserved → 不下发（不拿剩余空间兜）
    assert p._dynamic_max_tokens(msgs, None, None) is None


def test_9c_both_unknown_sends_no_max_tokens():
    """§一 + §五：Provider 值与 YAML 都取不到 → 不下发 max_tokens（语义不变）。"""
    p = _make_provider(context_limit=None, injected_context_limit=None)
    msgs = [{"role": "user", "content": "x" * 90}]

    assert p._effective_context_limit() is None
    # 预算层也没给出 → 不虚构上限，由 Provider 默认语义决定
    assert p._dynamic_max_tokens(msgs, None, None) is None
    # reserved 已知时仍然采用（不受 ctx 未知影响）
    assert p._dynamic_max_tokens(msgs, None, 2048) == 2048


def test_9d_round_manager_injects_resolved_limit_into_provider(tmp_path):
    """§一 全链路：Resolver 值 → RoundState.context_limit → Provider 实际使用。

    YAML 写 16384、Provider 元数据给 131072：最终 Provider 侧必须拿到 131072。
    """
    os.environ.setdefault("CROSSLLM_MOCK_DELAY_SCALE", "0")
    from core.model_manager import ModelRegistry, ProviderConfig
    from core.round_manager import RoundManager
    from core.session_manager import SessionManager
    from core.storage import Storage

    YAML_CTX = 16384
    PROVIDER_CTX = 131072

    injected: dict[str, int | None] = {}

    class _Spy:
        def __init__(self, mid):
            self.mid = mid

        def set_round_context_limit(self, context_limit):
            injected[self.mid] = context_limit

        async def generate(self, round_id, prompt, tools=None,
                           max_output_tokens=None):
            yield f"[{self.mid}] " + ("内容。" * 20)

    def _registry() -> ModelRegistry:
        return ModelRegistry(configs={
            mid: ProviderConfig(
                model_id=mid, display_name=mid.upper(),
                model=f"fake/{mid}", context_limit=YAML_CTX,
            )
            for mid in ("qwen", "deepseek", "gpt")
        })

    storage = Storage(tmp_path / "sessions")
    registry = _registry()
    rm = RoundManager(
        storage, registry, _Spy,
        capability_resolver=StubCapabilityResolver(
            context_limit=PROVIDER_CTX, model_max_output=4096,
        ),
    )
    sm = SessionManager(storage, registry)
    sess = sm.create("Q?")
    sid = sess.session_id

    rs, bus = rm.prepare_round(sess, "R1", peer_review=False, k=0.5)
    asyncio.run(rm.execute_round(sid, rs, bus))

    # ① RoundState 记的是 Provider 值，不是 YAML
    assert rs.context_limit == PROVIDER_CTX
    # ② Provider 实际拿到同一个值（每个参与者一次）
    assert injected, "Provider 应被注入本轮 context_limit"
    assert set(injected.values()) == {PROVIDER_CTX}, (
        f"Provider 侧必须使用 Provider 元数据值，实际={injected}"
    )
    # ③ 与 context_budget 日志同源
    events = [
        json.loads(l)
        for l in (storage.session_dir(sid) / "context_policy.jsonl")
        .read_text(encoding="utf-8").splitlines() if l.strip()
    ]
    # ③ 与 context_budget 日志同源：R1 也必须落 context_budget（它真实建了 prompt）
    cb = [e for e in events if e["event"] == "context_budget"
          and e["round_id"] == 1]
    assert cb, "R1 必须落 context_budget 事件（它是真实建立 LLM Prompt 的请求）"
    assert cb[0]["context_limit"] == PROVIDER_CTX
    # 刻意**不**清理 tmp_path：测试产物默认保留，便于人工复查 / 反向证明。


def test_9e_round_manager_falls_back_to_yaml_when_provider_fails(tmp_path):
    """§一：Provider 查询失败 → Provider 侧拿到的才是 YAML fallback 值。"""
    os.environ.setdefault("CROSSLLM_MOCK_DELAY_SCALE", "0")
    from _capability_stub import FailingCapabilityResolver
    from core.model_manager import ModelRegistry, ProviderConfig
    from core.round_manager import RoundManager
    from core.session_manager import SessionManager
    from core.storage import Storage

    YAML_CTX = 16384
    injected: dict[str, int | None] = {}

    class _Spy:
        def __init__(self, mid):
            self.mid = mid

        def set_round_context_limit(self, context_limit):
            injected[self.mid] = context_limit

        async def generate(self, round_id, prompt, tools=None,
                           max_output_tokens=None):
            yield f"[{self.mid}] ok"

    storage = Storage(tmp_path / "sessions")
    registry = ModelRegistry(configs={
        mid: ProviderConfig(
            model_id=mid, display_name=mid.upper(),
            model=f"fake/{mid}", context_limit=YAML_CTX,
        )
        for mid in ("qwen", "deepseek", "gpt")
    })
    rm = RoundManager(
        storage, registry, _Spy,
        capability_resolver=FailingCapabilityResolver(),
    )
    sm = SessionManager(storage, registry)
    sess = sm.create("Q?")
    sid = sess.session_id

    rs, bus = rm.prepare_round(sess, "R1", peer_review=False, k=0.5)
    asyncio.run(rm.execute_round(sid, rs, bus))

    assert rs.context_limit == YAML_CTX
    assert set(injected.values()) == {YAML_CTX}
    # 同样不清理 tmp_path（测试产物默认保留）。


# ── 8. 日志可对账 + 全链路（RoundManager → Provider 同源）──────────────


def test_8_round_state_carries_reserved_from_budget(tmp_path):
    """真链路：ContextBudget.reserved → RoundState.reserved → 日志同源。"""
    os.environ.setdefault("CROSSLLM_MOCK_DELAY_SCALE", "0")
    from core.storage import Storage
    from core.model_manager import ModelRegistry
    from core.round_manager import RoundManager
    from core.session_manager import SessionManager

    captured: dict[int, int | None] = {}

    class _Spy:
        """记录每次 generate 收到的 max_output_tokens。"""

        def __init__(self, mid):
            self.mid = mid

        async def generate(self, round_id, prompt, tools=None,
                           max_output_tokens=None):
            captured[round_id] = max_output_tokens
            yield f"[{self.mid}] " + ("内容。" * 300)

    tmp = Path(tempfile.mkdtemp(prefix="s311-"))
    try:
        storage = Storage(tmp / "sessions")
        registry = ModelRegistry()
        # §一：注入确定性模型能力（生产链路走 Provider 元数据）
        rm = RoundManager(
            storage, registry, _Spy,
            capability_resolver=StubCapabilityResolver(
                context_limit=8192, model_max_output=2048,
            ),
        )
        sm = SessionManager(storage, registry)
        sess = sm.create("Q?")
        sid = sess.session_id

        async def drive(instr, k=0.7, req_max=4096):
            rs, bus = rm.prepare_round(
                sess, instr, peer_review=False, k=k,
                request_max_output=req_max,
            )
            await rm.execute_round(sid, rs, bus)
            return rm, rs

        # R1 走 round1 prompt（无 ContextSelector → reserved 未写入）
        _, r1 = asyncio.run(drive("R1"))
        # R2+ 有 ContextSelector → reserved 写入 round_state 并传给 Provider
        _, r2 = asyncio.run(drive("R2"))

        assert r2.reserved_output_tokens is not None, (
            "R2 应把 ContextBudget.reserved 写入 RoundState"
        )
        # Provider 收到的就是 round_state 的值（同源，未重算）
        assert captured.get(2) == r2.reserved_output_tokens
        # reserved = min(provider_max=2048, request=4096) = 2048
        assert r2.reserved_output_tokens == 2048

        # 日志对账：context_budget 事件的 reserved 与传给 Provider 的值一致
        events = [
            json.loads(l)
            for l in (storage.session_dir(sid) / "context_policy.jsonl")
            .read_text(encoding="utf-8").splitlines() if l.strip()
        ]
        cb = [e for e in events if e["event"] == "context_budget"
              and e["round_id"] == 2]
        assert cb, "R2 应有 context_budget 事件"
        assert _val(cb[0]["reserved_output_tokens"]) == r2.reserved_output_tokens
        assert _val(cb[0]["model_max_output"]) == 2048
        assert _val(cb[0]["request_max_output"]) == 4096
        # 三值可对账：reserved == min(model_max, request)
        assert _val(cb[0]["reserved_output_tokens"]) == min(
            _val(cb[0]["model_max_output"]),
            _val(cb[0]["request_max_output"]),
        )
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_8b_request_max_smaller_than_provider_propagates(tmp_path):
    """request(1000) < provider(2048) → reserved=1000 传到 Provider。"""
    os.environ.setdefault("CROSSLLM_MOCK_DELAY_SCALE", "0")
    from core.storage import Storage
    from core.model_manager import ModelRegistry
    from core.round_manager import RoundManager
    from core.session_manager import SessionManager

    captured: dict[int, int | None] = {}

    class _Spy:
        def __init__(self, mid):
            self.mid = mid

        async def generate(self, round_id, prompt, tools=None,
                           max_output_tokens=None):
            captured[round_id] = max_output_tokens
            yield f"[{self.mid}] " + ("内容。" * 300)

    tmp = Path(tempfile.mkdtemp(prefix="s311b-"))
    try:
        storage = Storage(tmp / "sessions")
        rm = RoundManager(
            storage, ModelRegistry(), _Spy,
            capability_resolver=StubCapabilityResolver(
                context_limit=8192, model_max_output=2048,
            ),
        )
        sm = SessionManager(storage, rm.registry)
        sess = sm.create("Q?")
        sid = sess.session_id

        async def drive(instr, req_max):
            rs, bus = rm.prepare_round(
                sess, instr, peer_review=False, k=0.5,
                request_max_output=req_max,
            )
            await rm.execute_round(sid, rs, bus)
            return rs

        asyncio.run(drive("R1", 1000))
        rs2 = asyncio.run(drive("R2", 1000))
        assert rs2.reserved_output_tokens == 1000
        assert captured.get(2) == 1000
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ── 禁止恢复旧逻辑（AST 守门）─────────────────────────────────────────


def test_four_caps_are_distinct_concepts():
    """四个上限在数据结构中确实可区分（不是同一个值被复用）。"""
    b = _budget(8192, 4096)  # provider > request
    assert _val(b.model_max_output) == 8192
    assert _val(b.request_max_output) == 4096
    assert _val(b.reserved_output_tokens) == 4096
    # provider 与 request 各自保留，不被 reserved 覆盖
    assert _val(b.model_max_output) != _val(b.reserved_output_tokens)
    assert _val(b.request_max_output) == _val(b.reserved_output_tokens)


# ── §四：request_max_output 不由 UI 手动配置 ────────────────────────────

_STATIC = Path(__file__).resolve().parent.parent / "static"


def test_ui_has_no_request_output_limit_control():
    """§四：UI 不提供 request 输出上限输入框（能力来自 Provider，不由用户拍值）。"""
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    js = (_STATIC / "app.js").read_text(encoding="utf-8")

    assert "req-max-output" not in html, "已废弃的输出上限输入框不得回归"
    assert "req-max-output" not in js, "app.js 不得再引用该输入框"
    assert "reqMaxOutput" not in js, "旧元素引用不得残留"


def test_ui_round_policy_only_carries_k():
    """§四 / §六：前端 readRoundPolicy() 只产出 k，不带任何输出上限。"""
    js = (_STATIC / "app.js").read_text(encoding="utf-8")
    start = js.index("function readRoundPolicy()")
    end = js.index("\n}\n", start)
    body = js[start:end]
    # 只看 return 的对象字面量（上面的说明性注释里会合法地提到该字段名）
    ret = body[body.index("return {") + len("return {"):]

    assert "request_max_output" not in ret, (
        "request 输出上限不得由 UI 创造（未显式指定 = 后端按模型能力决定）"
    )
    assert "reqMaxOutput" not in ret
    assert "k:" in ret, "k 仍必须由 UI 提供（§六：k 只影响 lossless/compaction）"


# ── R1 也必须建立请求预算（A / B / D / E / F / G）────────────────────────
#
# 已确认的问题：_build_prompts_async() 原先在 R1 分支**过早 return** ——
# R1 在 ContextSelector（唯一建预算的地方）之前就返回了，于是
#   round_state.reserved_output_tokens 恒为 None
#   → provider.generate(max_output_tokens=None)
#   → RealProvider 只拿得到"剩余空间"，把它当输出上限
#   → 实测一次真实请求发出 max_tokens = 1048351（≈ 整个窗口）。
#
# R1 确实没有历史，但它同样是**真实建立 LLM Prompt 的请求**，因此必须与
# R2+ 一样先建立预算；而预算的输入（模型能力 + 请求上限 + 固定输入）本来就
# 与"有没有历史可选"无关。

R1_PROVIDER_CTX = 1_048_576   # 真实量级：Provider 元数据给出的窗口
R1_YAML_CTX = 16384           # models.yaml fallback（不应被 R1 用到）
R1_MODEL_MAX_OUT = 8192       # 模型自身能力上限（远小于窗口）


def _drive_rounds(
    tmp_path, *, model_max_output=R1_MODEL_MAX_OUT, req_max=None, rounds=1,
):
    """驱动 rounds 个 Round，返回 (storage, sid, [RoundState], captured)。

    captured[round_id] = Provider 实际收到的 max_output_tokens。
    """
    os.environ.setdefault("CROSSLLM_MOCK_DELAY_SCALE", "0")
    from core.model_manager import ModelRegistry, ProviderConfig
    from core.round_manager import RoundManager
    from core.session_manager import SessionManager
    from core.storage import Storage

    captured: dict[int, int | None] = {}

    class _Spy:
        def __init__(self, mid):
            self.mid = mid

        async def generate(self, round_id, prompt, tools=None,
                           max_output_tokens=None):
            captured[round_id] = max_output_tokens
            yield f"[{self.mid}] " + ("内容。" * 20)

    storage = Storage(tmp_path / "sessions")
    registry = ModelRegistry(configs={
        mid: ProviderConfig(
            model_id=mid, display_name=mid.upper(),
            model=f"fake/{mid}", context_limit=R1_YAML_CTX,
        )
        for mid in ("qwen", "deepseek")
    })
    rm = RoundManager(
        storage, registry, _Spy,
        capability_resolver=StubCapabilityResolver(
            context_limit=R1_PROVIDER_CTX,
            model_max_output=model_max_output,
        ),
    )
    sm = SessionManager(storage, registry)
    sess = sm.create("Q?")
    sid = sess.session_id

    async def _drive(instr: str):
        rs, bus = rm.prepare_round(
            sess, instr, peer_review=False, k=0.5,
            request_max_output=req_max,
        )
        await rm.execute_round(sid, rs, bus)
        return rs

    rss = [asyncio.run(_drive(f"R{i}")) for i in range(1, rounds + 1)]
    return storage, sid, rss, captured


def _round_events(storage, sid, round_id=None):
    ev = [
        json.loads(l)
        for l in (storage.session_dir(sid) / "context_policy.jsonl")
        .read_text(encoding="utf-8").splitlines() if l.strip()
    ]
    if round_id is None:
        return ev
    return [e for e in ev if e.get("round_id") == round_id]


def test_r1_establishes_request_budget(tmp_path):
    """A / B / D / E：R1 必须建立完整请求预算，并落 context_budget 事件。

    request_max_output 未给出时，reserved = 已知的 model_max_output（§22 分支①）。
    """
    storage, sid, rss, _ = _drive_rounds(tmp_path, rounds=1)
    rs1 = rss[0]

    # A：R1 有 reserved（不再是 None）
    assert rs1.reserved_output_tokens is not None, (
        "R1 必须建立 reserved_output_tokens（它真实建立了 LLM Prompt）"
    )
    # B：request 未显式给出 → reserved = model_max_output
    assert rs1.reserved_output_tokens == R1_MODEL_MAX_OUT
    # context_limit 仍是 Provider 元数据值（不是 YAML）
    assert rs1.context_limit == R1_PROVIDER_CTX

    # E：R1 必须落 context_budget 事件
    cb = [e for e in _round_events(storage, sid, 1)
          if e["event"] == "context_budget"]
    assert cb, "R1 必须落 context_budget 事件（它是真正建立请求的那一轮）"
    e = cb[0]

    # D：R1 的字段一个都不能缺
    for field in ("context_limit", "model_max_output", "request_max_output",
                  "reserved_output_tokens", "fixed_input_tokens",
                  "history_budget", "k"):
        assert field in e, f"R1 的 context_budget 缺字段 {field}"

    assert e["context_limit"] == R1_PROVIDER_CTX
    assert e["model_max_output"] == R1_MODEL_MAX_OUT
    assert e["request_max_output"] is None      # 未显式指定 = unknown，不造固定值
    assert e["reserved_output_tokens"] == R1_MODEL_MAX_OUT
    assert e["k"] == 0.5
    assert e["budget_status"] == "complete"
    # §21：history_budget = context_limit − fixed − reserved
    assert e["history_budget"] == (
        e["context_limit"] - e["fixed_input_tokens"] - e["reserved_output_tokens"]
    )


def test_r1_never_sends_context_remaining_as_max_tokens(tmp_path):
    """F：R1 下发给 Provider 的 max_tokens 必须是 reserved，不是剩余空间。

    这是本问题的直接回归守卫：修复前 R1 会发出 ≈ 整个窗口的 max_tokens。
    """
    _, _, _, captured = _drive_rounds(tmp_path, rounds=1)

    assert captured.get(1) == R1_MODEL_MAX_OUT, (
        f"R1 的 max_tokens 必须来自预算层 reserved，实际={captured.get(1)}"
    )
    # 明确排除"把 context_limit 剩余空间当输出上限"的形态（应远小于窗口量级）
    assert captured[1] < R1_PROVIDER_CTX // 100, (
        f"R1 不得把 context_limit 剩余空间当输出上限，实际={captured[1]}"
    )


def test_r1_request_max_bounds_reserved(tmp_path):
    """B：request_max_output 有值时 → reserved = min(model_max, request)。"""
    _, _, rss, captured = _drive_rounds(tmp_path, req_max=1000, rounds=1)
    assert rss[0].reserved_output_tokens == 1000
    assert captured.get(1) == 1000


def test_r1_unknown_model_max_is_degraded_not_fabricated(tmp_path):
    """B / §五：model_max_output 取不到 → degraded + 不下发，**不伪造**数值。"""
    storage, sid, rss, captured = _drive_rounds(
        tmp_path, model_max_output=None, rounds=1,
    )

    assert rss[0].reserved_output_tokens is None

    cb = [e for e in _round_events(storage, sid, 1)
          if e["event"] == "context_budget"]
    assert cb, "R1 仍必须落 context_budget 事件（哪怕是 degraded）"
    assert cb[0]["reserved_output_tokens"] is None
    assert cb[0]["budget_status"] == "degraded"
    assert cb[0]["history_budget"] is None, "未知时不得虚构 history_budget"

    # F：没有合法上限 → 不下发 max_tokens（而不是拿剩余空间兜底）
    assert captured.get(1) is None


def test_state_json_keeps_round_budget(tmp_path):
    """G：state.json 必须保留本轮真实的 context_limit / reserved_output_tokens。

    修复前 execute_round() 从磁盘 reload 后只回写 status/injected_rounds/
    usage/timestamp，这两项会显示为 null —— 与真实发送的请求参数不一致。
    """
    storage, sid, rss, _ = _drive_rounds(tmp_path, req_max=1000, rounds=1)

    state = json.loads(
        (storage.session_dir(sid) / "state.json").read_text(encoding="utf-8")
    )
    r1 = next(r for r in state["rounds"] if r["round_id"] == 1)

    assert r1["context_limit"] == R1_PROVIDER_CTX
    assert r1["reserved_output_tokens"] == 1000
    assert r1["request_max_output"] == 1000          # 既有字段不受影响
    # 与内存中的 RoundState 完全一致（不是另一套值）
    assert r1["context_limit"] == rss[0].context_limit
    assert r1["reserved_output_tokens"] == rss[0].reserved_output_tokens


def test_r2_budget_not_regressed(tmp_path):
    """R2+ 原有行为不回归：仍有 context_budget、reserved 同源、并写入 state.json。"""
    storage, sid, rss, captured = _drive_rounds(tmp_path, req_max=1000, rounds=2)

    # 两轮都建立了预算，且值一致（同一次注入的模型能力）
    assert [r.reserved_output_tokens for r in rss] == [1000, 1000]
    assert captured.get(1) == 1000 and captured.get(2) == 1000

    # R2 仍走 ContextSelector 路径 → context_budget 与 history_selection 都在
    kinds = {e["event"] for e in _round_events(storage, sid, 2)}
    assert "context_budget" in kinds
    assert "history_selection" in kinds, "R2+ 的历史选择事件不得消失"

    cb2 = [e for e in _round_events(storage, sid, 2)
           if e["event"] == "context_budget"][0]
    assert cb2["reserved_output_tokens"] == 1000
    assert cb2["budget_status"] == "complete"

    state = json.loads(
        (storage.session_dir(sid) / "state.json").read_text(encoding="utf-8")
    )
    r2 = next(r for r in state["rounds"] if r["round_id"] == 2)
    assert r2["reserved_output_tokens"] == 1000
    assert r2["context_limit"] == R1_PROVIDER_CTX
