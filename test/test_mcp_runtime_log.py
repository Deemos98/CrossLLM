"""MCP Runtime 调试日志测试。

覆盖点：
- 敏感信息脱敏（key 判定 + 已知凭证形态兜底）
- contextvars 上下文设置 / 复位
- JSONL 落盘到「轮次目录」且可被逐行解析
- 成功 / 失败两条路径都记录，且**不改变 call_tool 的行为**
- 无 runtime 上下文时不落盘（健康检查路径不受影响）
- 日志写入失败不影响 MCP 调用
- session 失效（ClosedResourceError）后自动清理、重连、重试一次
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from anyio import ClosedResourceError
from mcp.types import TextContent

from core import mcp_registry as mcp_registry_mod
from core import mcp_runtime_log as runtime_log
from core.mcp_registry import MCPRegistry, MCPServerConfig, MCPTool


# ---------- helpers ----------


def _make_registry(monkeypatch, *, session_call_tool) -> MCPRegistry:
    """构造一个 registry：schema 预先注入，session 用 fake 替换（不真连 MCP）。"""
    reg = MCPRegistry(
        {
            "github": MCPServerConfig(
                name="github",
                transport="stdio",
                command="echo",
                tool_name="search_issues",
            )
        }
    )
    reg._schemas = {
        "github": MCPTool(
            name="github",
            server="github",
            mcp_tool_name="search_issues",
            description="search issues",
            parameters={"type": "object"},
        )
    }

    class _FakeSession:
        async def call_tool(self, tool_name: str, arguments: dict):
            return await session_call_tool(tool_name, arguments)

    async def _fake_get_session(server):  # noqa: ANN001
        return _FakeSession()

    monkeypatch.setattr(reg, "_get_session", _fake_get_session)
    return reg


def _ok_result(text: str):
    class _R:
        content = [TextContent(type="text", text=text)]

    return _R()


def _set_ctx(directory: Path) -> object:
    return runtime_log.set_runtime_context(
        runtime_log.RuntimeContext(
            session_id="20260901-214335-3bf0a7",
            round_id=1,
            model_id="minimax_m2_7",
            directory=directory,
        )
    )


# ---------- 脱敏 ----------


def test_scrub_masks_sensitive_keys():
    data = {
        "Authorization": "Bearer abc123",
        "api_key": "secret-key",
        "access_token": "tok",
        "cookie": "sid=xyz",
        "password": "p@ss",
        "client_secret": "cs",
        "query": "faster-whisper VAD",
        "limit": 3,
    }
    out = runtime_log.scrub_value(data)
    assert out["Authorization"] == runtime_log.REDACTED
    assert out["api_key"] == runtime_log.REDACTED
    assert out["access_token"] == runtime_log.REDACTED
    assert out["cookie"] == runtime_log.REDACTED
    assert out["password"] == runtime_log.REDACTED
    assert out["client_secret"] == runtime_log.REDACTED
    # 普通字段不受影响
    assert out["query"] == "faster-whisper VAD"
    assert out["limit"] == 3


def test_scrub_masks_known_token_patterns():
    cases = [
        "Authorization: Bearer sk-or-v1-TESTFIXTURE0000000000000000000000000000",
        "header=Authorization: Bearer TOKEN1234567890",
        "key is sk-abcdefghijklmnopqrstuvwxyz123456",
        "pat github_pat_TESTFIXTURE_0000000000000000000000000000000000",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "Cookie: session=abc123; other=1",
    ]
    for text in cases:
        out = runtime_log.scrub_text(text)
        assert runtime_log.REDACTED in out, f"未脱敏: {text}"
        # 原始凭证片段不应残留
        assert "sk-or-v1-TESTFIXTURE" not in out
        assert "github_pat_TESTFIXTURE" not in out


def test_scrub_leaves_ordinary_content_intact():
    text = "# faster-whisper VAD\nIssue #944 mentions threshold=0.5 and min_silence_ms"
    assert runtime_log.scrub_text(text) == text


def test_scrub_handles_nested_structures():
    data = {"a": [{"token": "x", "q": "ok"}], "b": {"nested": {"apikey": "y", "n": 1}}}
    out = runtime_log.scrub_value(data)
    assert out["a"][0]["token"] == runtime_log.REDACTED
    assert out["a"][0]["q"] == "ok"
    assert out["b"]["nested"]["apikey"] == runtime_log.REDACTED
    assert out["b"]["nested"]["n"] == 1


# ---------- contextvars ----------


def test_runtime_context_set_and_reset(tmp_path: Path):
    assert runtime_log.get_runtime_context() is None
    token = _set_ctx(tmp_path)
    ctx = runtime_log.get_runtime_context()
    assert ctx is not None
    assert ctx.session_id == "20260901-214335-3bf0a7"
    assert ctx.round_id == 1
    assert ctx.model_id == "minimax_m2_7"
    runtime_log.reset_runtime_context(token)
    assert runtime_log.get_runtime_context() is None


# ---------- 记录结构 ----------


def test_build_record_contains_all_required_fields(tmp_path: Path):
    ctx = runtime_log.RuntimeContext("sid", 1, "m", tmp_path)
    record = runtime_log.build_record(
        ctx=ctx,
        tool_call={"name": "github", "arguments": {"q": "vad"}},
        mcp_name="github",
        tool_name="search_issues",
        arguments={"q": "vad"},
        started_at="2026-09-02T07:00:00.000",
        ok=True,
        result_chars=10,
        result_preview="0123456789",
        error_type=None,
        error_message=None,
        returned_at="2026-09-02T07:00:01.000",
        duration_ms=1000,
    )
    required = [
        "ts",
        "session_id",
        "round_id",
        "model_id",
        "tool_call",  # 1
        "mcp",  # 2
        "tool",  # 3
        "arguments",  # 4
        "started_at",  # 5
        "ok",  # 6
        "result_chars",  # 7
        "result_preview",  # 8
        "error_type",  # 9
        "error_message",  # 9
        "returned_at",  # 10
        "log_path",  # 11
    ]
    for field in required:
        assert field in record, f"缺少字段 {field}"
    assert record["log_path"].endswith("mcp_runtime.jsonl")
    # 必须可被 JSON 序列化（JSONL 前提）
    json.dumps(record, ensure_ascii=False)


# ---------- 成功路径 ----------


def test_call_tool_success_writes_jsonl(tmp_path: Path, monkeypatch):
    async def _call(tool_name: str, arguments: dict):
        return _ok_result("x" * 500)

    reg = _make_registry(monkeypatch, session_call_tool=_call)

    token = _set_ctx(tmp_path)
    try:
        text = asyncio.run(reg.call_tool("github", {"q": "faster-whisper VAD"}))
    finally:
        runtime_log.reset_runtime_context(token)

    # 行为未变：返回的是截断后的文本
    assert len(text) == 500

    records = runtime_log.read_records(tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["ok"] is True
    assert rec["mcp"] == "github"
    assert rec["tool"] == "search_issues"
    assert rec["arguments"] == {"q": "faster-whisper VAD"}
    assert rec["result_chars"] == 500
    assert rec["result_preview"] == "x" * runtime_log.RESULT_PREVIEW_CHARS
    assert rec["error_type"] is None
    assert rec["error_message"] is None
    assert rec["started_at"] and rec["returned_at"]
    assert rec["duration_ms"] >= 0
    assert rec["session_id"] == "20260901-214335-3bf0a7"
    assert rec["round_id"] == 1
    assert rec["model_id"] == "minimax_m2_7"


def test_result_preview_is_truncated_and_scrubbed(tmp_path: Path, monkeypatch):
    payload = "Bearer sk-or-v1-abcdefghijklmnop1234567890 " + "y" * 1000

    async def _call(tool_name: str, arguments: dict):
        return _ok_result(payload)

    reg = _make_registry(monkeypatch, session_call_tool=_call)

    token = _set_ctx(tmp_path)
    try:
        asyncio.run(reg.call_tool("github", {}))
    finally:
        runtime_log.reset_runtime_context(token)

    rec = runtime_log.read_records(tmp_path)[0]
    assert len(rec["result_preview"]) <= runtime_log.RESULT_PREVIEW_CHARS
    assert "sk-or-v1-abcdefghijklmnop" not in rec["result_preview"]
    assert runtime_log.REDACTED in rec["result_preview"]


# ---------- 失败路径 ----------


def test_call_tool_failure_records_exception(tmp_path: Path, monkeypatch):
    async def _call(tool_name: str, arguments: dict):
        raise RuntimeError("MCP server exploded")

    reg = _make_registry(monkeypatch, session_call_tool=_call)

    token = _set_ctx(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="MCP server exploded"):
            asyncio.run(reg.call_tool("github", {"q": "vad"}))
    finally:
        runtime_log.reset_runtime_context(token)

    records = runtime_log.read_records(tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["ok"] is False
    assert rec["error_type"] == "RuntimeError"
    assert rec["error_message"] == "MCP server exploded"
    assert rec["result_chars"] == 0
    assert rec["result_preview"] == ""


def test_unknown_tool_records_nothing_but_still_raises(tmp_path: Path):
    reg = MCPRegistry({})
    reg._schemas = {}
    token = _set_ctx(tmp_path)
    try:
        with pytest.raises(KeyError):
            asyncio.run(reg.call_tool("nope", {}))
    finally:
        runtime_log.reset_runtime_context(token)
    # 未进入实际调用阶段，不应产生记录
    assert runtime_log.read_records(tmp_path) == []


# ---------- 无上下文 / 日志失败不影响调用 ----------


def test_no_record_without_runtime_context(tmp_path: Path, monkeypatch):
    """健康检查等无轮次上下文的场景：不落盘，行为完全不变。"""

    async def _call(tool_name: str, arguments: dict):
        return _ok_result("hello")

    reg = _make_registry(monkeypatch, session_call_tool=_call)
    assert runtime_log.get_runtime_context() is None

    text = asyncio.run(reg.call_tool("github", {}))
    assert text == "hello"
    assert not (tmp_path / runtime_log.RUNTIME_LOG_NAME).exists()


def test_log_write_failure_does_not_break_call(tmp_path: Path, monkeypatch):
    async def _call(tool_name: str, arguments: dict):
        return _ok_result("hello")

    reg = _make_registry(monkeypatch, session_call_tool=_call)

    def _boom(directory, record):  # noqa: ANN001
        raise OSError("disk full")

    monkeypatch.setattr(runtime_log, "append_record", _boom)

    token = _set_ctx(tmp_path)
    try:
        # 日志写入失败被吞掉，调用结果不受影响
        assert asyncio.run(reg.call_tool("github", {})) == "hello"
    finally:
        runtime_log.reset_runtime_context(token)


def test_append_record_returns_false_on_unwritable_path(monkeypatch):
    # 用一个文件当目录，mkdir 会失败；append_record 必须返回 False 而不抛异常
    blocked = Path(__file__).resolve()
    assert runtime_log.append_record(blocked, {"a": 1}) is False


# ---------- 多行追加 ----------


def test_multiple_calls_append_multiple_lines(tmp_path: Path, monkeypatch):
    async def _call(tool_name: str, arguments: dict):
        return _ok_result("result-body")

    reg = _make_registry(monkeypatch, session_call_tool=_call)
    token = _set_ctx(tmp_path)
    try:
        for i in range(3):
            asyncio.run(reg.call_tool("github", {"q": f"q{i}"}))
    finally:
        runtime_log.reset_runtime_context(token)

    records = runtime_log.read_records(tmp_path)
    assert len(records) == 3
    assert [r["arguments"]["q"] for r in records] == ["q0", "q1", "q2"]

    # 每行都是独立 JSON 对象（JSONL 前提）
    lines = [
        line
        for line in (tmp_path / runtime_log.RUNTIME_LOG_NAME)
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert len(lines) == 3
    for line in lines:
        assert isinstance(json.loads(line), dict)


# ---------- session 失效自动重连（最小修复） ----------


class _FakeClientSession:
    """替代真实 ClientSession：可人为标记 closed 模拟 session 失效。"""

    instances: list["_FakeClientSession"] = []
    fail_all = False

    def __init__(self, read, write):  # noqa: ANN001
        self.closed = False
        type(self).instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return None

    async def initialize(self):
        return None

    async def call_tool(self, tool_name: str, arguments: dict):
        if self.closed or type(self).fail_all:
            raise ClosedResourceError("stream closed")
        return _ok_result(f"from-session-{len(type(self).instances)}")


class _FakeCM:
    async def __aenter__(self):
        return (object(), object())

    async def __aexit__(self, *exc):
        return None


async def _fake_stdio_cm(params):  # noqa: ANN001
    return _FakeCM(), object(), object()


def _make_registry_real_transport() -> MCPRegistry:
    """schema 预注入 + 真实 _get_session（transport / ClientSession 由测试替换）。"""
    reg = MCPRegistry(
        {
            "github": MCPServerConfig(
                name="github",
                transport="stdio",
                command="echo",
                tool_name="search_issues",
            )
        }
    )
    reg._schemas = {
        "github": MCPTool(
            name="github",
            server="github",
            mcp_tool_name="search_issues",
            description="search issues",
            parameters={"type": "object"},
        )
    }
    return reg


def test_reconnect_after_session_closed(tmp_path: Path, monkeypatch):
    """要求：session 第一次调用后人为关闭，第二次调用必须自动重连。"""
    _FakeClientSession.instances = []
    _FakeClientSession.fail_all = False
    monkeypatch.setattr(mcp_registry_mod, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(mcp_registry_mod, "stdio_server_cm", _fake_stdio_cm)

    reg = _make_registry_real_transport()
    token = _set_ctx(tmp_path)
    try:
        first = asyncio.run(reg.call_tool("github", {"q": "first"}))
        assert first == "from-session-1"

        # 人为关闭 cache 中的 session（模拟远程服务端断开后的失效状态）
        dead = reg._sessions["github"][0]
        dead.closed = True

        # 第二次调用：命中失效 session → ClosedResourceError → 清理 → 重连 → 重试成功
        second = asyncio.run(reg.call_tool("github", {"q": "second"}))
        assert second == "from-session-2"
        # cache 中已替换为重建的 session
        assert reg._sessions["github"][0] is not dead
    finally:
        runtime_log.reset_runtime_context(token)

    records = runtime_log.read_records(tmp_path)
    reconnects = [r for r in records if r.get("event") == "reconnect"]
    assert len(reconnects) == 1
    assert reconnects[0]["ok"] is True
    assert reconnects[0]["reason"] == "ClosedResourceError"
    assert reconnects[0]["mcp"] == "github"
    assert reconnects[0]["attempt"] == 1

    # 两条 tool_call 记录（两次 call_tool）都成功
    calls = [r for r in records if "tool_call" in r]
    assert len(calls) == 2
    assert all(r["ok"] for r in calls)


def test_retry_only_once_and_reraise(tmp_path: Path, monkeypatch):
    """重试一次后仍 ClosedResourceError → 向外抛出，且不再第二次重连。"""
    _FakeClientSession.instances = []
    _FakeClientSession.fail_all = True
    monkeypatch.setattr(mcp_registry_mod, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(mcp_registry_mod, "stdio_server_cm", _fake_stdio_cm)

    reg = _make_registry_real_transport()
    token = _set_ctx(tmp_path)
    try:
        with pytest.raises(ClosedResourceError):
            asyncio.run(reg.call_tool("github", {"q": "x"}))
    finally:
        runtime_log.reset_runtime_context(token)

    # 最多重试一次：只创建了 2 个 session（初始 + 重连），没有第三次
    assert len(_FakeClientSession.instances) == 2

    records = runtime_log.read_records(tmp_path)
    reconnects = [r for r in records if r.get("event") == "reconnect"]
    assert len(reconnects) == 1
    assert reconnects[0]["ok"] is True  # 重连（重建 session）本身成功

    calls = [r for r in records if "tool_call" in r]
    assert len(calls) == 1
    assert calls[0]["ok"] is False
    assert calls[0]["error_type"] == "ClosedResourceError"


def test_reconnect_without_runtime_context_still_works(monkeypatch):
    """无轮次上下文（如健康检查）：重连逻辑照常工作，只是不落日志。"""
    _FakeClientSession.instances = []
    _FakeClientSession.fail_all = False
    monkeypatch.setattr(mcp_registry_mod, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(mcp_registry_mod, "stdio_server_cm", _fake_stdio_cm)

    reg = _make_registry_real_transport()
    assert runtime_log.get_runtime_context() is None

    async def _run():
        first = await reg.call_tool("github", {})
        dead = reg._sessions["github"][0]
        dead.closed = True
        second = await reg.call_tool("github", {})
        return first, second

    first, second = asyncio.run(_run())
    assert first == "from-session-1"
    assert second == "from-session-2"
