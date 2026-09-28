"""CrossLLM Phase 2 测试（phase2_real_llm.md §26）。

覆盖：
  Provider   请求建立 / streaming 转 chunk / API Error / timeout / 流中断 / 响应异常
  并发       多模型并发 / 一个慢不阻塞 / 一个失败不取消
  Round      Round 1 独立 / Round 2 只读上一轮 / CLOSED 无新请求
  匿名       Peer Context 无真实身份 / 标签轮内一致 / 正文命中只告警不阻断
  持久化     落盘 / 历史不覆盖 / Session 恢复
  密钥       不落盘 / 不进响应 / 不进错误消息

手法说明：
  真实 API **不进自动化测试**（要花钱、不稳定、不可重复）。
  全部用 httpx.MockTransport 伪造 OpenAI-compatible SSE 响应，
  从而确定性地覆盖正常流、HTTP 错误、非法 JSON、流中断、慢模型、超时。
  真实 API 只用于 phase2 §27 的人工验收。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from types import SimpleNamespace

import httpx
import pytest

import app as app_module
from core.anonymizer import IdentityLeakError, assign_candidate_ids
from core.model_manager import (
    ModelRegistry,
    ProviderConfig,
    ProviderSettings,
    load_dotenv,
)
from core import round_manager as rm
from core.prompt_builder import SYSTEM_INSTRUCTION, build_round_n_prompt
from core.round_manager import RoundManager
from core.session_manager import SessionManager
from core.state import ModelParticipant
from core.storage import Storage
from providers.base import LLMProvider, ProviderError
from providers.mock_provider import MockProvider, content_for
from providers.real_provider import RealProvider

from _capability_stub import StubCapabilityResolver

API_KEY = "sk-crossllm-test-secret-000"
QUESTION = "这个 OCR 架构是否合理？"
INSTRUCTION_R2 = "请评价其他参与者的方案，并说明哪些内容值得吸收。"

# 每个"模型"返回的中立正文（不含任何身份词），便于验证注入
MODEL_TEXT = {
    "model-qwen_main": "先量化再优化，缓存应当按变更频率分层。",
    "model-deepseek_main": "先补齐观测再谈优化，瓶颈可能在数据搬运。",
    "model-glm_main": "长尾延迟比平均值更值得关注，灰度需要保留回滚能力。",
}


# --------------------------------------------------------------------------
# 假传输
# --------------------------------------------------------------------------


def sse_body(pieces: list[str]) -> str:
    lines = [f"data: {json.dumps({'choices': [{'delta': {'content': p}}]})}" for p in pieces]
    lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


def split_chunks(text: str, size: int = 6) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def fake_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def default_handler(request: httpx.Request) -> httpx.Response:
    """按 model 名区分行为，模拟真实 Provider 之间的差异。"""
    body = json.loads(request.content)
    model = body["model"]
    text = MODEL_TEXT.get(model, "默认回答。")

    if "glm" in model:
        return httpx.Response(500, json={"error": {"message": "upstream unavailable"}})
    if "deepseek" in model:
        # 慢模型：chunk 之间明显停顿
        async def slow():
            for piece in split_chunks(text):
                await asyncio.sleep(0.05)
                yield (f"data: {json.dumps({'choices': [{'delta': {'content': piece}}]})}\n\n").encode()
            yield b"data: [DONE]\n\n"

        return httpx.Response(200, content=slow())
    return httpx.Response(200, text=sse_body(split_chunks(text)))


# Provider 级设置：端点与凭据位置。所有模型共用同一份（Key 不按模型绑定）
FAKE_SETTINGS = ProviderSettings(
    name="fake",
    type="openai_compatible",
    base_url="http://fake.local/v1",
    api_key_env="CROSSLLM_API_KEY_X",
)


def make_config(model_id: str, **overrides) -> ProviderConfig:
    """模型级配置：只有身份与 slug，不含端点或凭据。"""
    kwargs = {
        "model_id": model_id,
        "display_name": model_id,
        "model": f"model-{model_id}",
    }
    kwargs.update(overrides)
    return ProviderConfig(**kwargs)


def make_provider(
    model_id: str = "qwen_main",
    handler=default_handler,
    settings: ProviderSettings = FAKE_SETTINGS,
    **cfg_overrides,
):
    return RealProvider(
        settings, make_config(model_id, **cfg_overrides), API_KEY, client=fake_client(handler)
    )


async def collect(provider) -> str:
    return "".join([c async for c in provider.generate(round_id=1, prompt="p")])


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------


def test_provider_implements_unified_interface():
    assert isinstance(make_provider(), LLMProvider)
    assert isinstance(MockProvider("qwen"), LLMProvider)


def test_streaming_is_converted_to_chunks():
    text = asyncio.run(collect(make_provider()))
    assert text == MODEL_TEXT["model-qwen_main"]


def test_chunks_are_actually_incremental():
    """验证是流式产出，而不是一次性返回整段。"""
    provider = make_provider()
    chunks = asyncio.run(
        _collect_list(provider)
    )
    assert len(chunks) > 1


async def _collect_list(provider) -> list[str]:
    return [c async for c in provider.generate(round_id=1, prompt="p")]


def test_http_error_becomes_provider_error():
    async def handler(request):
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    with pytest.raises(ProviderError) as exc:
        asyncio.run(collect(make_provider(handler=handler)))
    assert "429" in str(exc.value)
    assert "rate limited" in str(exc.value)


def test_error_message_never_contains_api_key():
    """phase2 §8：错误消息会进日志和 error 事件，绝不能带密钥。"""

    async def handler(request):
        auth = request.headers.get("authorization", "")
        return httpx.Response(401, json={"error": {"message": f"bad auth header: {auth}"}})

    with pytest.raises(ProviderError) as exc:
        asyncio.run(collect(make_provider(handler=handler)))
    assert API_KEY not in str(exc.value)
    assert "***" in str(exc.value)


def test_malformed_json_lines_are_tolerated():
    """厂商偶尔插入非 JSON 保活行，不应打断整条流。"""

    async def handler(request):
        good = f"data: {json.dumps({'choices': [{'delta': {'content': '正常内容'}}]})}"
        body = f"data: not-json-at-all\n\n{good}\n\ndata: [DONE]\n\n"
        return httpx.Response(200, text=body)

    assert asyncio.run(collect(make_provider(handler=handler))) == "正常内容"


def test_empty_response_raises():
    async def handler(request):
        return httpx.Response(200, text="data: [DONE]\n\n")

    with pytest.raises(ProviderError):
        asyncio.run(collect(make_provider(handler=handler)))


def test_stream_disconnect_raises_provider_error():
    async def handler(request):
        raise httpx.RemoteProtocolError("连接中断")

    with pytest.raises(ProviderError):
        asyncio.run(collect(make_provider(handler=handler)))


def test_total_timeout_is_enforced():
    """总时长上限：慢慢吐、一直不断的情形。"""

    async def handler(request):
        async def slow():
            yield b"data: " + json.dumps({"choices": [{"delta": {"content": "甲"}}]}).encode() + b"\n\n"
            await asyncio.sleep(0.4)
            yield b"data: " + json.dumps({"choices": [{"delta": {"content": "乙"}}]}).encode() + b"\n\n"
            yield b"data: [DONE]\n\n"

        return httpx.Response(200, content=slow())

    with pytest.raises(ProviderError) as exc:
        asyncio.run(collect(make_provider(handler=handler, total_timeout=0.1)))
    assert "总时长" in str(exc.value)


# --------------------------------------------------------------------------
# 并发与隔离
# --------------------------------------------------------------------------


def test_slow_model_does_not_block_others():
    async def main():
        slow = make_provider("deepseek_main")
        fast = make_provider("qwen_main")
        order: list[str] = []

        async def run(name, provider):
            text = "".join([c async for c in provider.generate(round_id=1, prompt="p")])
            order.append(name)
            return text

        results = await asyncio.gather(run("slow", slow), run("fast", fast))
        return dict(zip(order, results, strict=False)), order

    _, order = asyncio.run(main())
    assert order == ["fast", "slow"], "慢模型不得阻塞快模型完成"


def test_one_failure_does_not_cancel_others():
    async def main():
        bad = make_provider("glm_main")
        good = make_provider("qwen_main")
        outcomes = {}

        async def run(name, provider):
            try:
                await collect(provider)
                outcomes[name] = "complete"
            except ProviderError:
                outcomes[name] = "error"

        await asyncio.gather(run("bad", bad), run("good", good))
        return outcomes

    outcomes = asyncio.run(main())
    assert outcomes == {"bad": "error", "good": "complete"}


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

REAL_YAML = """
provider:
  name: fake
  type: openai_compatible
  base_url: http://fake.local/v1
  api_key_env: CROSSLLM_API_KEY_FAKE

models:
  - id: qwen_main
    display_name: Qwen
    model: model-qwen_main
    enabled: true
    timeout: { connect: 5, read: 30, total: 120 }
    params: { temperature: 0.5 }

  - id: deepseek_main
    display_name: DeepSeek
    model: model-deepseek_main
    enabled: true

  - id: glm_main
    display_name: GLM
    model: model-glm_main
    enabled: false
"""


@pytest.fixture
def real_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CROSSLLM_API_KEY_FAKE", API_KEY)
    path = tmp_path / "models.yaml"
    path.write_text(REAL_YAML, encoding="utf-8")
    return ModelRegistry.from_yaml(path)


def test_config_loads_models(real_registry):
    assert real_registry.ids() == ["qwen_main", "deepseek_main"]  # glm_main 已 disabled
    cfg = real_registry.config_for("qwen_main")
    assert cfg.model == "model-qwen_main"
    assert cfg.read_timeout == 30
    assert cfg.params == {"temperature": 0.5}


def test_credential_belongs_to_provider_not_model(real_registry):
    """解耦核心：凭据在 Provider 上，Model 只保存身份与 slug。"""
    settings = real_registry.provider_settings
    assert settings.name == "fake"
    assert settings.type == "openai_compatible"
    assert settings.base_url == "http://fake.local/v1"
    assert settings.api_key_env == "CROSSLLM_API_KEY_FAKE"

    cfg = real_registry.config_for("qwen_main")
    # ProviderConfig 上不存在任何凭据或端点字段
    assert not hasattr(cfg, "api_key_env")
    assert not hasattr(cfg, "base_url")


def test_all_models_share_one_credential(real_registry):
    """同一个 key 服务所有模型，而不是每个模型各配一个。"""
    assert real_registry.api_key() == API_KEY
    assert real_registry.api_key() == real_registry.api_key()


def test_config_never_holds_api_key(real_registry):
    """phase2 §6/§8：配置对象里只有环境变量名，不能有密钥本身。"""
    settings = real_registry.provider_settings
    cfg = real_registry.config_for("qwen_main")
    assert API_KEY not in repr(settings)
    assert API_KEY not in str(vars(settings))
    assert API_KEY not in repr(cfg)
    assert API_KEY not in str(vars(cfg))
    # key 只在真正调用时从环境取出
    assert os.environ["CROSSLLM_API_KEY_FAKE"] == API_KEY


def test_missing_credential_is_lazy_not_startup_failure(tmp_path, monkeypatch):
    """凭据缺失不该在导入期就炸掉整个进程（否则 test_phase1 会全挂）。

    配置结构仍然正常加载，只在真正构造 Provider 取 key 时才失败。
    """
    monkeypatch.delenv("CROSSLLM_API_KEY_FAKE", raising=False)
    path = tmp_path / "models.yaml"
    path.write_text(REAL_YAML, encoding="utf-8")

    registry = ModelRegistry.from_yaml(path)  # 不应抛异常
    with pytest.raises(ValueError, match="CROSSLLM_API_KEY_FAKE"):
        registry.api_key()


def test_duplicate_id_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("CROSSLLM_API_KEY_FAKE", API_KEY)
    path = tmp_path / "models.yaml"
    path.write_text(
        """
provider:
  name: fake
  type: openai_compatible
  base_url: http://x/v1
  api_key_env: CROSSLLM_API_KEY_FAKE
models:
  - id: qwen_main
    model: a
  - id: qwen_main
    model: b
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="重复"):
        ModelRegistry.from_yaml(path)


def test_unknown_provider_is_rejected(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(
        "provider:\n  name: x\n  type: gemini_native\nmodels:\n  - id: a\n    model: b\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="provider.type 未知"):
        ModelRegistry.from_yaml(path)


def test_missing_provider_block_is_rejected(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text("models:\n  - id: a\n    model: b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="缺少 provider 配置块"):
        ModelRegistry.from_yaml(path)


def test_default_registry_stays_mock():
    """无配置时回落到 Phase 1 的 Mock 池，保证 test_phase1 不受影响。"""
    registry = ModelRegistry()
    assert registry.ids() == ["qwen", "deepseek", "gpt"]
    assert registry.provider_settings.type == "mock"
    assert registry.config_for("qwen").model == "mock-qwen"


def test_load_dotenv_prefers_real_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("# 注释\nEXISTING=from_file\nNEW_KEY=abc\n", encoding="utf-8")
    monkeypatch.setenv("EXISTING", "from_env")
    load_dotenv(env_file)

    assert os.environ["EXISTING"] == "from_env", "系统环境变量优先"
    assert os.environ["NEW_KEY"] == "abc"


# --------------------------------------------------------------------------
# 匿名化：软 / 硬分离（D2）
# --------------------------------------------------------------------------


def test_peer_body_hit_only_warns(real_registry):
    """真实模型自我介绍 → 只告警，不阻断整轮（phase2 §28 问题 3）。"""
    from core.anonymizer import build_anonymous_context

    cids = assign_candidate_ids(["qwen_main", "deepseek_main"])
    entries = build_anonymous_context(
        self_model_id="qwen_main",
        candidate_ids=cids,
        # 模拟真实模型在回答里自称
        previous_answers={"qwen_main": "我是 Qwen，我的看法是…", "deepseek_main": "我认为…"},
    )
    text, warnings = build_round_n_prompt(
        original_question=QUESTION,
        user_instruction=INSTRUCTION_R2,
        rounds_entries=[(1, entries)],
        participants=real_registry.participants,
    )
    assert warnings, "应当产生告警"
    assert "Qwen" in warnings[0]
    assert "我是 Qwen" in text, "不自动清洗内容，原文照常注入"


def test_system_generated_identity_still_fails_hard(real_registry):
    """系统自己生成的身份信息出错 = bug，必须阻断。"""
    from core.anonymizer import build_anonymous_context

    cids = assign_candidate_ids(["qwen_main", "deepseek_main"])
    entries = build_anonymous_context(
        self_model_id="qwen_main",
        candidate_ids=cids,
        previous_answers={"qwen_main": "甲", "deepseek_main": "乙"},
    )
    original = SYSTEM_INSTRUCTION
    try:
        import core.prompt_builder as pb

        pb.SYSTEM_INSTRUCTION = "你是 Qwen，请回答。"  # 模拟系统指令被写错
        with pytest.raises(IdentityLeakError):
            build_round_n_prompt(
                original_question=QUESTION,
                user_instruction=INSTRUCTION_R2,
                rounds_entries=[(1, entries)],
                participants=real_registry.participants,
            )
    finally:
        import core.prompt_builder as pb

        pb.SYSTEM_INSTRUCTION = original
    assert original is not None


# --------------------------------------------------------------------------
# 端到端（真实 Provider + 假传输）
# --------------------------------------------------------------------------


@pytest.fixture
def real_env(tmp_path, monkeypatch, real_registry):
    storage = Storage(tmp_path / "sessions")

    def factory(model_id: str):
        return RealProvider(
            real_registry.provider_settings,
            real_registry.config_for(model_id),
            real_registry.api_key(),
            client=fake_client(default_handler),
        )

    return SimpleNamespaceLike(
        storage=storage,
        registry=real_registry,
        sessions=SessionManager(storage, real_registry),
        rounds=RoundManager(
            storage, real_registry, factory,
            capability_resolver=StubCapabilityResolver(),
        ),
        root=tmp_path / "sessions",
    )


class SimpleNamespaceLike:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def run_round(env, session, instruction: str):
    round_state, bus = env.rounds.prepare_round(session, instruction)
    asyncio.run(env.rounds.execute_round(session.session_id, round_state, bus))
    return round_state, bus


def test_real_round1_independent_and_persisted(real_env):
    session = real_env.sessions.create(QUESTION)
    run_round(real_env, session, QUESTION)

    round_dir = real_env.storage.round_dir(session.session_id, 1)
    for model_id in real_env.registry.ids():
        assert (round_dir / f"{model_id}.md").exists()
        prompt = real_env.storage.load_prompt(session.session_id, 1, model_id)
        # Round 1 不得包含其他模型的回答
        for other in real_env.registry.ids():
            if other != model_id:
                assert MODEL_TEXT[f"model-{other}"] not in prompt


def test_real_error_isolation_and_partial_persistence(real_env, monkeypatch):
    """glm 在假传输里返回 500 —— 它失败，其他模型继续。"""
    # 临时启用 glm，让它进入本轮
    session = real_env.sessions.create(QUESTION)
    session.active_models = real_env.registry.ids() + ["glm_main"]
    real_env.storage.save_state(session)

    round_state, bus = run_round(real_env, session, QUESTION)
    assert round_state.status["glm_main"] == "error"
    assert round_state.status["qwen_main"] == "complete"
    assert round_state.status["deepseek_main"] == "complete"

    errors = [e for e in bus.events if e["type"] == "error"]
    assert [e["model"] for e in errors] == ["glm_main"]
    # 错误消息不得带密钥
    assert API_KEY not in json.dumps(bus.events, ensure_ascii=False)


def test_real_round2_anonymous_injection(real_env):
    session = real_env.sessions.create(QUESTION)
    run_round(real_env, session, QUESTION)
    session = real_env.sessions.set_selection(session.session_id, ["qwen_main"])
    round2, _ = run_round(real_env, session, INSTRUCTION_R2)

    assert round2.participants == ["qwen_main"]
    prompt = real_env.storage.load_prompt(session.session_id, 2, "qwen_main")
    # 自己的上一轮在
    assert MODEL_TEXT["model-qwen_main"] in prompt
    # 已 CLOSED 的 deepseek 不在
    assert MODEL_TEXT["model-deepseek_main"] not in prompt
    # 无真实身份字段
    for token in ("qwen_main", "deepseek_main", "model-qwen_main"):
        assert token not in prompt


def test_no_api_key_anywhere_on_disk(real_env):
    session = real_env.sessions.create(QUESTION)
    run_round(real_env, session, QUESTION)
    run_round(real_env, session, INSTRUCTION_R2)

    session_dir = real_env.storage.session_dir(session.session_id)
    files = list(session_dir.rglob("*"))
    assert files
    for path in files:
        if path.is_file():
            assert API_KEY not in path.read_text(encoding="utf-8")
    # state.json 里也不得有密钥字段
    state_text = (session_dir / "state.json").read_text(encoding="utf-8")
    assert "api_key" not in state_text.lower()


def test_no_api_key_in_api_response(tmp_path, monkeypatch, real_registry):
    storage = Storage(tmp_path / "sessions")

    def factory(model_id: str):
        return RealProvider(
            real_registry.provider_settings,
            real_registry.config_for(model_id),
            real_registry.api_key(),
            client=fake_client(default_handler),
        )

    monkeypatch.setattr(app_module, "storage", storage)
    monkeypatch.setattr(app_module, "sessions", SessionManager(storage, real_registry))
    monkeypatch.setattr(
        app_module,
        "rounds",
        RoundManager(
            storage, real_registry, factory,
            capability_resolver=StubCapabilityResolver(),
        ),
    )
    monkeypatch.setattr(app_module, "registry", real_registry)

    from fastapi.testclient import TestClient

    with TestClient(app_module.app) as client:
        sid = client.post("/api/session", json={"question": QUESTION}).json()["session_id"]
        rid = client.post(f"/api/session/{sid}/round", json={"instruction": ""}).json()["round_id"]

        deadline = time.time() + 15
        while time.time() < deadline:
            body = client.get(f"/api/session/{sid}").json()
            r = next((x for x in body["rounds"] if x["round_id"] == rid), None)
            if r and all(s in ("complete", "error") for s in r["status"].values()):
                break
            time.sleep(0.02)

        assert API_KEY not in json.dumps(body, ensure_ascii=False)


def test_session_recovery_with_real_provider(real_env):
    session = real_env.sessions.create(QUESTION)
    run_round(real_env, session, QUESTION)
    run_round(real_env, session, INSTRUCTION_R2)

    fresh = SessionManager(Storage(real_env.root), real_registry)
    restored = fresh.get(session.session_id)
    assert restored.current_round == 2
    assert [r.round_id for r in restored.rounds] == [1, 2]
    assert set(real_env.storage.load_responses(session.session_id, 1)) == set(
        real_env.registry.ids()
    )


# --------------------------------------------------------------------------
# phase1_1 补充：发送选择 / 互评开关 / 历史窗口 / 预算保护 / usage
#
# 这些用 Mock Provider 跑，不消耗任何真实额度。
# --------------------------------------------------------------------------


@pytest.fixture
def mock_env(tmp_path, monkeypatch):
    """Mock 版环境，专测编排语义（不涉及真实 HTTP）。"""
    monkeypatch.setenv("CROSSLLM_MOCK_DELAY_SCALE", "0")
    storage = Storage(tmp_path / "sessions")
    registry = ModelRegistry()
    return SimpleNamespace(
        storage=storage,
        registry=registry,
        sessions=SessionManager(storage, registry),
        rounds=RoundManager(
            storage, registry, lambda m: MockProvider(m, delay_scale=0.0),
            capability_resolver=StubCapabilityResolver(),
        ),
    )


def run_with(env, session, instruction, **kwargs):
    round_state, bus = env.rounds.prepare_round(session, instruction, **kwargs)
    asyncio.run(env.rounds.execute_round(session.session_id, round_state, bus))
    return round_state, bus


def test_send_subset_only_calls_selected(mock_env):
    """phase1_1 §2：本次发送只发给选中的 ACTIVE 模型。"""
    session = mock_env.sessions.create(QUESTION)
    round_state, bus = run_with(mock_env, session, QUESTION, selected_models=["qwen", "gpt"])

    assert round_state.participants == ["qwen", "gpt"]
    assert "deepseek" not in round_state.status
    assert {e["model"] for e in bus.events} == {"qwen", "gpt"}


def test_zero_selected_is_rejected(mock_env):
    """phase1_1 §2.3：选择 0 个模型不允许发送。"""
    session = mock_env.sessions.create(QUESTION)
    with pytest.raises(ValueError, match="未选中任何模型"):
        mock_env.rounds.prepare_round(session, QUESTION, selected_models=[])


def test_closed_model_cannot_be_selected(mock_env):
    """phase1_1 §2.2：CLOSED 模型不得被本次发送选中。"""
    session = mock_env.sessions.create(QUESTION)
    session = mock_env.sessions.set_selection(session.session_id, ["qwen", "gpt"])
    with pytest.raises(ValueError, match="CLOSED"):
        mock_env.rounds.prepare_round(session, QUESTION, selected_models=["qwen", "deepseek"])


def test_unknown_model_in_selection_is_rejected(mock_env):
    session = mock_env.sessions.create(QUESTION)
    with pytest.raises(KeyError):
        mock_env.rounds.prepare_round(session, QUESTION, selected_models=["nope"])


def test_peer_review_off_keeps_own_history_without_peers(mock_env):
    """phase1_1 §3.1：互评 OFF = 正常多轮对话，只是不注入其他模型的回答。"""
    session = mock_env.sessions.create(QUESTION)
    run_with(mock_env, session, QUESTION)
    session = mock_env.sessions.get(session.session_id)
    round2, _ = run_with(mock_env, session, INSTRUCTION_R2, peer_review=False)

    assert round2.peer_review is False
    assert round2.candidate_ids == {}, "互评关闭时不产生 Candidate"

    prompt = mock_env.storage.load_prompt(session.session_id, 2, "qwen")
    assert content_for("qwen", 1) in prompt, "自己的历史必须保留"
    assert content_for("gpt", 1) not in prompt, "不得注入其他模型的回答"
    assert "Candidate" not in prompt


def test_peer_review_on_injects_peers(mock_env):
    """phase1_1 §3.2：互评 ON = 自己的历史 + 其他模型的匿名历史。"""
    session = mock_env.sessions.create(QUESTION)
    run_with(mock_env, session, QUESTION)
    session = mock_env.sessions.get(session.session_id)
    round2, _ = run_with(mock_env, session, INSTRUCTION_R2, peer_review=True)

    assert round2.peer_review is True
    assert round2.candidate_ids
    prompt = mock_env.storage.load_prompt(session.session_id, 2, "qwen")
    assert content_for("qwen", 1) in prompt
    assert content_for("gpt", 1) in prompt


def test_single_model_peer_review_equals_off(mock_env):
    """phase1_1 §3.3：单模型时即使打开互评也没有 peer context。"""
    session = mock_env.sessions.create(QUESTION)
    run_with(mock_env, session, QUESTION, selected_models=["qwen"])
    session = mock_env.sessions.get(session.session_id)
    round2, _ = run_with(
        mock_env, session, INSTRUCTION_R2, selected_models=["qwen"], peer_review=True
    )

    assert round2.peer_review is False, "应自动降级为关闭"
    assert round2.candidate_ids == {}, "不产生无意义的 Candidate"
    prompt = mock_env.storage.load_prompt(session.session_id, 2, "qwen")
    assert "Candidate" not in prompt


# --------------------------------------------------------------------------
# 输入 / 输出 token 限制分离
# --------------------------------------------------------------------------


def test_input_budget_equals_context_limit():
    """输入预算 = context_limit 本身，无折扣、无扣除。

    旧设计（已废弃）：
      - context_limit * 0.95（经验系数，无依据）
      - context_limit - max_output_tokens（固定扣除）

    新设计：预算就是模型的硬上限，真正的约束
    (input + output <= context_limit) 由 Provider 每次请求前保证。
    """
    cfg = ProviderConfig(
        model_id="m", display_name="M", model="m", context_limit=100000
    )
    budget = rm.input_budget_for(cfg)
    # 预算等于 context_limit：不乘系数、不扣任何输出预留
    assert budget == cfg.context_limit
    assert budget == 100000


def test_config_has_no_model_capability_field():
    """§三 / §九：ProviderConfig 不得再持有任何"模型输出能力"字段。

    模型能力上限（model_max_output）只能来自 Provider 元数据
    （core/model_capability.py），不能由 YAML / 静态配置提供。
    """
    cfg = ProviderConfig(model_id="m", display_name="M", model="m")
    assert not hasattr(cfg, "max_output_tokens")


def test_input_budget_reflects_small_context_limit():
    """小 context_limit 也如实反映（不做人为下限兜底）。

    预算 = context_limit 本身。若窗口确实很小，应如实体现，
    由 Provider 请求前的 input+output<=context_limit 约束来暴露问题，
    而不是用一个拍脑袋的 MIN_INPUT_BUDGET 掩盖。
    """
    cfg = ProviderConfig(
        model_id="m", display_name="M", model="m", context_limit=1000
    )
    assert rm.input_budget_for(cfg) == 1000


def test_config_parses_context_limit():
    cfg = make_config("glm", context_limit=128000)
    assert cfg.context_limit == 128000


def test_context_limit_may_be_absent_for_provider_lookup(tmp_path):
    """§一：context_limit 允许留空 —— 能力由 Provider 元数据自动获取。"""
    path = tmp_path / "models.yaml"
    path.write_text(
        """
provider:
  name: fake
  type: openai_compatible
  base_url: http://x/v1
  api_key_env: CROSSLLM_API_KEY_FAKE
models:
  - id: a
    model: m1
""",
        encoding="utf-8",
    )
    registry = ModelRegistry.from_yaml(path)
    assert registry.config_for("a").context_limit is None


def test_deprecated_max_output_tokens_is_rejected(tmp_path):
    """§二 / §九：YAML 里出现 max_output_tokens 必须直接报错。

    该字段是"静态值伪装成模型能力"的来源，已废弃且不再被读取；
    留一个显式报错比静默忽略更能防止它悄悄回归。
    """
    path = tmp_path / "models.yaml"
    path.write_text(
        """
provider:
  name: fake
  type: openai_compatible
  base_url: http://x/v1
  api_key_env: CROSSLLM_API_KEY_FAKE
models:
  - id: bad
    model: m
    context_limit: 1000
    max_output_tokens: 5000
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="已废弃的 max_output_tokens"):
        ModelRegistry.from_yaml(path)


def test_name_and_display_name_are_both_accepted(tmp_path, monkeypatch):
    """文档示例里写的是 name，配置解析器两种写法都要认。"""
    monkeypatch.setenv("CROSSLLM_API_KEY_FAKE", API_KEY)
    path = tmp_path / "models.yaml"
    path.write_text(
        """
provider:
  name: fake
  type: openai_compatible
  base_url: http://x/v1
  api_key_env: CROSSLLM_API_KEY_FAKE
models:
  - id: a
    name: 用 name 写
    model: m1
  - id: b
    display_name: 用 display_name 写
    model: m2
""",
        encoding="utf-8",
    )
    registry = ModelRegistry.from_yaml(path)
    assert registry.config_for("a").display_name == "用 name 写"
    assert registry.config_for("b").display_name == "用 display_name 写"


def test_max_tokens_is_sent_to_provider():
    """预算层给出的有效输出上限要真正下发，并满足 input + output <= context_limit。"""
    seen = {}

    async def handler(request):
        seen.update(json.loads(request.content))
        body = f"data: {json.dumps({'choices': [{'delta': {'content': '好'}}]})}\n\n"
        return httpx.Response(200, text=body + "data: [DONE]\n\n")

    context_limit = 100000
    provider = make_provider(handler=handler, context_limit=context_limit)

    async def _collect():
        # §四：上限由预算层显式给出（= ContextBudget.reserved_output_tokens）
        return "".join([
            c async for c in provider.generate(
                round_id=1, prompt="p", max_output_tokens=4321,
            )
        ])

    asyncio.run(_collect())

    assert "max_tokens" in seen, "max_tokens 必须存在于请求体"
    assert seen["max_tokens"] > 0

    # 短 prompt，input 远小于窗口 → output 应被预算层上限 4321 封顶
    assert seen["max_tokens"] == 4321
    assert seen["stream_options"] == {"include_usage": True}


def test_max_tokens_respects_remaining_context():
    """核心约束：input + output <= context_limit。

    当 messages 占用较多时，预算层给的输出上限必须与"剩余空间"求交，
    而不是无脑采用；预算层给不出上限时则**不下发** max_tokens
    （不把 context_limit 的剩余空间当成输出上限）。
    """
    seen = {}

    async def handler(request):
        seen.update(json.loads(request.content))
        body = f"data: {json.dumps({'choices': [{'delta': {'content': '好'}}]})}\n\n"
        return httpx.Response(200, text=body + "data: [DONE]\n\n")

    # 小窗口：context_limit=1000（模型能力上限由 Provider 元数据提供，此处无关）
    provider = make_provider(handler=handler, context_limit=1000)

    # 构造一个约 300 token 的 prompt（900 chars / 3）
    long_prompt = "x" * 900

    async def _collect(max_output_tokens):
        return "".join([
            c async for c in provider.generate(
                round_id=1, prompt=long_prompt,
                max_output_tokens=max_output_tokens,
            )
        ])

    # ① 预算层给了远超剩余空间的上限 → 求交后 = 剩余空间
    #    input ≈ 300，剩余空间 = 1000 - 300 = 700
    asyncio.run(_collect(100000))
    assert seen["max_tokens"] == 700, (
        f"output 应被剩余空间限制为 700，实际: {seen['max_tokens']}"
    )
    assert 300 + seen["max_tokens"] <= 1000

    # ② 预算层没给上限 → 不下发 max_tokens（不虚构，也不拿剩余空间兜）
    seen.clear()
    asyncio.run(_collect(None))
    assert "max_tokens" not in seen, (
        f"reserved 未知时不得下发 max_tokens，实际: {seen.get('max_tokens')}"
    )



def test_real_usage_is_captured():
    """phase1_1 §6.2：优先使用 Provider 返回的真实 usage。"""

    async def handler(request):
        body = f"data: {json.dumps({'choices': [{'delta': {'content': '你好'}}]})}\n\n"
        usage = (
            "data: "
            + json.dumps(
                {
                    "choices": [],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                }
            )
            + "\n\n"
        )
        return httpx.Response(200, text=body + usage + "data: [DONE]\n\n")

    provider = make_provider(handler=handler)
    text = asyncio.run(collect(provider))
    assert text == "你好"
    assert provider.usage == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
    }


def test_mock_provider_has_no_usage():
    """Mock 没有真实 token 数据，保持同名属性恒为 None。"""
    assert MockProvider("qwen").usage is None


def test_usage_is_persisted_per_model(monkeypatch):
    """usage 会被 round_manager 记进 RoundState 并落盘。"""

    async def handler(request):
        body = f"data: {json.dumps({'choices': [{'delta': {'content': '甲'}}]})}\n\n"
        usage = (
            "data: "
            + json.dumps({"choices": [], "usage": {"total_tokens": 42}})
            + "\n\n"
        )
        return httpx.Response(200, text=body + usage + "data: [DONE]\n\n")

    storage = Storage(tempfile.mkdtemp())
    registry = ModelRegistry()
    rounds = RoundManager(
        storage,
        registry,
        lambda m: RealProvider(
            FAKE_SETTINGS, make_config(m), API_KEY, client=fake_client(handler)
        ),
        capability_resolver=StubCapabilityResolver(),
    )
    sessions = SessionManager(storage, registry)
    session = sessions.create(QUESTION)
    round_state, bus = rounds.prepare_round(session, QUESTION, selected_models=["qwen"])
    asyncio.run(rounds.execute_round(session.session_id, round_state, bus))

    saved = storage.load_state(session.session_id).round(1)
    assert saved.usage["qwen"]["total_tokens"] == 42
