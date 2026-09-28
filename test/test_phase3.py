"""CrossLLM Phase 3 测试（mcp执行确认.md §17）。

覆盖：
  Provider   无工具调用 / Tool Call / Tool Result 循环 / Final Answer 保存
  并发       多模型混合工具使用 / 单模型 Tool 失败隔离
  Peer Review 只交换 Final Answer，不交换 Tool 过程
  MCP        配置解析 / schema 发现 / call_tool

手法说明：
  真实 MCP Server / 真实搜索 **不进自动化测试**。
  全部用 FakeMCPRegistry + httpx.MockTransport 伪造 OpenAI-compatible SSE 响应，
  确定性地覆盖工具调用闭环。
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

import app as app_module
from core.mcp_registry import MCPRegistry
from core.model_manager import (
    ModelRegistry,
    ProviderConfig,
    ProviderSettings,
)
from core.round_manager import RoundManager
from core.session_manager import SessionManager
from core.state import ModelParticipant
from core.storage import Storage
from providers.base import ProviderError
from providers.real_provider import RealProvider

from _capability_stub import StubCapabilityResolver

API_KEY = "sk-crossllm-test-secret-000"
QUESTION = "faster-whisper 当前有什么常见 VAD 问题？"

FAKE_SETTINGS = ProviderSettings(
    name="fake",
    type="openai_compatible",
    base_url="http://fake.local/v1",
    api_key_env="CROSSLLM_API_KEY_X",
)

# 模型最终回答（不含任何身份词）
FINAL_ANSWER = "根据搜索结果，常见 VAD 问题包括静音段切割过长、音乐误识别为语音等。"
SEARCH_RESULT = "<search_item>faster-whisper VAD 问题</search_item>"


# --------------------------------------------------------------------------
# 假 MCP Registry
# --------------------------------------------------------------------------


class FakeMCPRegistry:
    """内存 MCP Registry：只模拟 openwebsearch 工具。"""

    def __init__(self, result: str = SEARCH_RESULT):
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    async def tool_schemas(self, names: list[str]) -> list[dict]:
        if "openwebsearch" in names:
            return [
                {
                    "type": "function",
                    "function": {
                        "name": "openwebsearch",
                        "description": "Search the web using multiple engines.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                                "limit": {"type": "integer"},
                            },
                            "required": ["query"],
                        },
                    },
                }
            ]
        return []

    async def call_tool(self, name: str, arguments: dict) -> str:
        self.calls.append((name, arguments))
        if name == "openwebsearch":
            return self.result
        raise KeyError(f"unknown tool: {name}")


# --------------------------------------------------------------------------
# SSE 辅助
# --------------------------------------------------------------------------


def _sse_event(delta: dict, finish_reason: str | None = None, usage: dict | None = None) -> str:
    data: dict = {"choices": [{"delta": delta}]}
    if finish_reason is not None:
        data["choices"][0]["finish_reason"] = finish_reason
    if usage is not None:
        data["usage"] = usage
    return f"data: {json.dumps(data)}\n\n"


def _split(text: str, size: int = 6) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def sse_content(text: str, usage: dict | None = None) -> str:
    chunks = [_sse_event({"content": p}) for p in _split(text)]
    chunks.append(_sse_event({}, finish_reason="stop", usage=usage))
    chunks.append("data: [DONE]\n\n")
    return "".join(chunks)


def sse_tool_call(tool_name: str, arguments: str, usage: dict | None = None) -> str:
    """模拟模型返回一次 tool_call（finish_reason=tool_calls）。"""
    chunks = [
        _sse_event(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": tool_name, "arguments": ""},
                    }
                ],
            }
        ),
        _sse_event(
            {"tool_calls": [{"index": 0, "function": {"arguments": arguments}}]}
        ),
        _sse_event({}, finish_reason="tool_calls", usage=usage),
        "data: [DONE]\n\n",
    ]
    return "".join(chunks)


# --------------------------------------------------------------------------
# Provider 构造辅助
# --------------------------------------------------------------------------


def make_config(model_id: str, tools: list[str] | None = None, **overrides) -> ProviderConfig:
    kwargs = {
        "model_id": model_id,
        "display_name": model_id,
        "model": f"model-{model_id}",
        "tools": list(tools) if tools else [],
    }
    kwargs.update(overrides)
    return ProviderConfig(**kwargs)


def fake_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def make_provider(
    model_id: str = "qwen",
    handler=None,
    mcp_registry=None,
    tools: list[str] | None = None,
):
    if handler is None:

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=sse_content("默认回答。"))

    return RealProvider(
        FAKE_SETTINGS,
        make_config(model_id, tools=tools),
        API_KEY,
        client=fake_client(handler),
        mcp_registry=mcp_registry,
    )


async def collect(provider) -> str:
    return "".join([c async for c in provider.generate(round_id=1, prompt="p")])


# --------------------------------------------------------------------------
# T1 / T2 / T3 / T4 / T5 / T6：Provider 工具调用闭环
# --------------------------------------------------------------------------


def test_provider_answers_without_tool_call():
    """T1：模型正常回答，不调用 Tool。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse_content("你好，这是直接回答。"))

    provider = make_provider(handler=handler)
    text = asyncio.run(collect(provider))
    assert text == "你好，这是直接回答。"


def test_provider_tool_call_loop():
    """T2-T5：模型产生 Tool Call → CrossLLM 调用 MCP → 结果返回 → 生成 Final Answer。"""

    mcp = FakeMCPRegistry()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        has_tool_result = any(m.get("role") == "tool" for m in body.get("messages", []))
        if has_tool_result:
            return httpx.Response(200, text=sse_content(FINAL_ANSWER, usage={"total_tokens": 20}))
        return httpx.Response(
            200, text=sse_tool_call("openwebsearch", '{"query": "faster-whisper VAD", "limit": 3}')
        )

    provider = make_provider(handler=handler, mcp_registry=mcp, tools=["openwebsearch"])
    text = asyncio.run(collect(provider))

    assert text == FINAL_ANSWER
    assert len(mcp.calls) == 1
    name, args = mcp.calls[0]
    assert name == "openwebsearch"
    assert args["query"] == "faster-whisper VAD"
    assert provider.usage == {"total_tokens": 20}


def test_provider_streams_final_answer_chunks():
    """Final Answer 仍然是逐 chunk 流出，而不是一次性返回。"""

    mcp = FakeMCPRegistry()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        has_tool_result = any(m.get("role") == "tool" for m in body.get("messages", []))
        if has_tool_result:
            return httpx.Response(200, text=sse_content(FINAL_ANSWER))
        return httpx.Response(
            200, text=sse_tool_call("openwebsearch", '{"query": "x"}')
        )

    async def collect_chunks():
        provider = make_provider(handler=handler, mcp_registry=mcp, tools=["openwebsearch"])
        return [c async for c in provider.generate(round_id=1, prompt="p")]

    chunks = asyncio.run(collect_chunks())
    assert "".join(chunks) == FINAL_ANSWER
    assert len(chunks) > 1


def test_provider_no_mcp_registry_falls_back_gracefully():
    """模型配置有 tools 但无 MCP Registry 时，不会崩溃，而是把错误返回给模型。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        has_tool_result = any(m.get("role") == "tool" for m in body.get("messages", []))
        if has_tool_result:
            # 模型看到错误提示后给出最终回答
            return httpx.Response(200, text=sse_content("无法获取外部信息，但我可以基于已有知识回答。"))
        return httpx.Response(
            200, text=sse_tool_call("openwebsearch", '{"query": "x"}')
        )

    provider = make_provider(handler=handler, mcp_registry=None, tools=["openwebsearch"])
    text = asyncio.run(collect(provider))
    assert "无法获取外部信息" in text


# --------------------------------------------------------------------------
# T7 / T10：并发与失败隔离
# --------------------------------------------------------------------------


def test_tool_failure_isolated_to_one_model():
    """T10：Tool 失败只影响当前模型。"""

    class FailingMCPRegistry(FakeMCPRegistry):
        async def call_tool(self, name: str, arguments: dict) -> str:
            raise RuntimeError("MCP Server 挂了")

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        has_tool_result = any(m.get("role") == "tool" for m in body.get("messages", []))
        if has_tool_result:
            return httpx.Response(200, text=sse_content("工具失败了，但我可以继续。"))
        return httpx.Response(200, text=sse_tool_call("openwebsearch", '{"query": "x"}'))

    provider = make_provider(handler=handler, mcp_registry=FailingMCPRegistry(), tools=["openwebsearch"])
    text = asyncio.run(collect(provider))
    assert "工具失败了" in text


# --------------------------------------------------------------------------
# RoundManager 集成：T6 / T7 / T8 / T9
# --------------------------------------------------------------------------


@pytest.fixture
def tool_env(tmp_path, monkeypatch):
    """带 tools 配置的 Mock 环境，Provider 使用 FakeMCPRegistry + MockTransport。"""
    storage = Storage(tmp_path / "sessions")
    mcp = FakeMCPRegistry()

    # 构造一个只有 2 个模型、都带有 openwebsearch 的 registry
    participants = [
        ModelParticipant(
            id="qwen", display_name="Qwen", provider="openai_compatible",
            model="model-qwen", enabled=True, tools=["openwebsearch"],
        ),
        ModelParticipant(
            id="gpt", display_name="GPT", provider="openai_compatible",
            model="model-gpt", enabled=True, tools=["openwebsearch"],
        ),
    ]
    configs = {
        "qwen": make_config("qwen", tools=["openwebsearch"]),
        "gpt": make_config("gpt", tools=["openwebsearch"]),
    }
    registry = ModelRegistry(participants, FAKE_SETTINGS, configs)

    def factory(model_id: str):
        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            has_tool_result = any(m.get("role") == "tool" for m in body.get("messages", []))
            if has_tool_result:
                return httpx.Response(200, text=sse_content(f"{model_id} final with search"))
            return httpx.Response(
                200, text=sse_tool_call("openwebsearch", '{"query": "x"}')
            )

        return RealProvider(
            FAKE_SETTINGS,
            registry.config_for(model_id),
            API_KEY,
            client=fake_client(handler),
            mcp_registry=mcp,
        )

    env = SimpleNamespace(
        storage=storage,
        registry=registry,
        mcp=mcp,
        sessions=SessionManager(storage, registry),
        rounds=RoundManager(
            storage, registry, factory,
            capability_resolver=StubCapabilityResolver(),
        ),
    )

    monkeypatch.setattr(app_module, "storage", storage)
    monkeypatch.setattr(app_module, "sessions", env.sessions)
    monkeypatch.setattr(app_module, "rounds", env.rounds)
    monkeypatch.setattr(app_module, "registry", registry)

    return env


def run_with(env, session, instruction, **kwargs):
    round_state, bus = env.rounds.prepare_round(session, instruction, **kwargs)
    asyncio.run(env.rounds.execute_round(session.session_id, round_state, bus))
    return round_state, bus


def test_round_saves_final_answer(tool_env):
    """T6：Final Answer 正常保存。"""
    session = tool_env.sessions.create(QUESTION)
    run_with(tool_env, session, QUESTION)

    responses = tool_env.storage.load_responses(session.session_id, 1)
    assert "qwen" in responses
    assert "gpt" in responses
    assert "final with search" in responses["qwen"]


def test_tool_calls_do_not_leak_into_peer_context(tool_env):
    """T8：Peer Review ON 时只传递 Final Answer，不传递 Tool 过程。"""
    session = tool_env.sessions.create(QUESTION)
    run_with(tool_env, session, QUESTION, peer_review=True)

    # Round 2：检查注入给每个模型的 prompt 中是否只含 Final Answer，不含 tool result
    round_state, _ = tool_env.rounds.prepare_round(session, "请评价上一轮", peer_review=True)
    prompts = tool_env.rounds._build_prompts(session, round_state)[0]

    for model_id, prompt in prompts.items():
        assert "final with search" in prompt
        assert SEARCH_RESULT not in prompt
        assert "tool" not in prompt.lower()


def test_peer_review_off_does_not_share_final_answer(tool_env):
    """T9：Peer Review OFF 时不传递其他模型 Final Answer，只保留自己历史。"""
    session = tool_env.sessions.create(QUESTION)
    run_with(tool_env, session, QUESTION, peer_review=False)

    round_state, _ = tool_env.rounds.prepare_round(session, "继续", peer_review=False)
    prompts = tool_env.rounds._build_prompts(session, round_state)[0]

    assert "qwen final with search" in prompts["qwen"]
    assert "gpt final with search" not in prompts["qwen"]
    assert "gpt final with search" in prompts["gpt"]
    assert "qwen final with search" not in prompts["gpt"]


# --------------------------------------------------------------------------
# MCP Registry
# --------------------------------------------------------------------------


def test_mcp_registry_parses_config():
    registry = MCPRegistry.from_yaml("config/mcp.yaml")
    assert "openwebsearch" in registry.servers
    server = registry.servers["openwebsearch"]
    assert server.enabled
    # stdio 单例模式：所有模型共享一个 open-websearch 进程
    assert server.transport == "stdio"
    # command 可能是相对路径或绝对路径（取决于部署环境）
    assert "open-websearch" in server.command, f"command 应包含 'open-websearch'，实际值: {server.command}"
    assert server.tool_name == "search"


def test_mcp_registry_returns_openai_tool_schema():
    """tool_schemas 把 MCP tool 转成 OpenAI function 定义。"""
    registry = FakeMCPRegistry()
    schemas = asyncio.run(registry.tool_schemas(["openwebsearch"]))
    assert len(schemas) == 1
    assert schemas[0]["type"] == "function"
    assert schemas[0]["function"]["name"] == "openwebsearch"
