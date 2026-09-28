"""MCP 配置与最小 Client 封装（mcp执行确认.md §8/§9）。

职责边界：
- 读取 config/mcp.yaml；
- 为 RealProvider 提供 OpenAI-compatible 的 tool schema；
- 按名称调用 MCP tool 并返回文本结果；
- **同一 MCP Server 只启动一个实例，所有模型共享**（避免 EADDRINUSE 等冲突）。

第一版只实现：connect、tools/list、call_tool、disconnect。
不实现 Tool Router / Ranking / Planner / Cache / Supervisor。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anyio import ClosedResourceError

from . import mcp_runtime_log as runtime_log


_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# 单个 MCP tool_result 最大字符数；超过则截断，防止一次性撑爆模型上下文窗口。
# 按英文约 1 token / 4 chars、中文约 1 token / 1 char 估算，12000 字符对
# 196608 tokens 的窗口是安全的，且保留足够供 LLM 决策的信息。
TOOL_RESULT_MAX_CHARS = 12000

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent


@dataclass(frozen=True)
class MCPServerConfig:
    """单个 MCP Server 的连接配置。"""

    name: str
    enabled: bool = True
    transport: str = "stdio"  # "stdio" | "sse"
    command: str = ""
    args: tuple[str, ...] = field(default_factory=tuple)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    # SSE / HTTP 远程服务所需的请求头（如 Authorization: Bearer <token>）。
    # 支持从环境变量读取："${ENV_NAME}" 会被替换为对应环境变量值。
    headers: dict[str, str] = field(default_factory=dict)
    # 该 Server 暴露给 LLM 的实际工具名。
    # 对 Open-WebSearch 而言通常是 "search"；发现多个工具时以这里为准。
    tool_name: str = ""


@dataclass(frozen=True)
class MCPTool:
    """CrossLLM 内部使用的工具定义。"""

    name: str  # CrossLLM 侧工具名，与 models.yaml 中的 tools 列表对应
    server: str  # 所属 MCP Server 名
    mcp_tool_name: str  # Server 侧实际工具名
    description: str
    parameters: dict[str, Any]


def _log(message: str) -> None:
    print(f"[crossllm] {message}", file=sys.stderr)


def _resolve_env_placeholders(value: str) -> str:
    """替换字符串中的 ${ENV_NAME} 为对应环境变量值（未定义则替换为空字符串）。"""

    def _repl(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1), "")

    return _ENV_PLACEHOLDER.sub(_repl, value)


class MCPRegistry:
    """MCP Server 配置表与最小 Client。

    **关键设计：每个 Server 只维护一个持久 session 实例**。
    所有模型（minimax_m2_7, glm_5_2 等）共享同一个 openwebsearch 连接，
    不会为每个模型单独启动 Server 进程。
    """

    def __init__(self, servers: dict[str, MCPServerConfig]):
        self._servers = servers
        # 工具 schema 缓存（首次 connect 后填充）
        self._schemas: dict[str, MCPTool] | None = None
        # 单例 session 缓存：server_name -> (session, cm)
        # cm 是 transport 层的上下文管理器，用于 cleanup
        self._sessions: dict[str, tuple[ClientSession, Any]] = {}
        # 保护并发初始化的锁
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------

    @classmethod
    def from_yaml(cls, path: str | Path) -> MCPRegistry:
        import yaml  # 局部导入，与 model_manager 保持一致

        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"MCP 配置不存在: {p}")

        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        raw_servers = data.get("servers") or {}
        servers: dict[str, MCPServerConfig] = {}

        for name, raw in raw_servers.items():
            if not isinstance(raw, dict):
                raise ValueError(f"MCP Server {name!r} 配置必须是字典")

            enabled = bool(raw.get("enabled", True))
            transport = str(raw.get("transport", "stdio")).lower()
            if transport not in ("stdio", "sse"):
                raise ValueError(
                    f"MCP Server {name!r} 的 transport 必须是 stdio 或 sse"
                )

            command = str(raw.get("command", "")).strip()
            if transport == "stdio" and not command:
                raise ValueError(
                    f"MCP Server {name!r} 使用 stdio 时必须提供 command"
                )

            args = tuple(str(a) for a in raw.get("args") or [])
            raw_env = {str(k): str(v) for k, v in (raw.get("env") or {}).items()}
            env = {k: _resolve_env_placeholders(v) for k, v in raw_env.items()}
            url = str(raw.get("url", "")).strip()
            raw_headers = {str(k): str(v) for k, v in (raw.get("headers") or {}).items()}
            resolved_headers = {k: _resolve_env_placeholders(v) for k, v in raw_headers.items()}

            tool_name = str(raw.get("tool_name", "")).strip()

            servers[name] = MCPServerConfig(
                name=name,
                enabled=enabled,
                transport=transport,
                command=command,
                args=args,
                env=env,
                url=url,
                headers=resolved_headers,
                tool_name=tool_name,
            )

        return cls(servers)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def servers(self) -> dict[str, MCPServerConfig]:
        return dict(self._servers)

    def enabled_servers(self) -> dict[str, MCPServerConfig]:
        return {k: v for k, v in self._servers.items() if v.enabled}

    async def tool_schemas(self, names: list[str]) -> list[dict[str, Any]]:
        """返回指定名称列表的 OpenAI-compatible function 定义。"""
        schemas = await self._ensure_schemas()
        result: list[dict[str, Any]] = []
        for name in names:
            tool = schemas.get(name)
            if tool is None:
                continue
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
            )
        return result

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """调用指定 MCP tool，返回文本结果。复用已有 session。

        Runtime 调试日志（core/mcp_runtime_log.py）以**旁路**方式在此记录：
        - 日志写入失败不会影响调用结果；
        - 无 runtime 上下文（健康检查 / 单测）时不落盘；
        - 调用行为、返回值、异常传播均与加入日志前完全一致。
        """
        schemas = await self._ensure_schemas()
        tool = schemas.get(name)
        if tool is None:
            raise KeyError(f"未知 MCP tool: {name!r}")

        server = self._servers.get(tool.server)
        if server is None:
            raise RuntimeError(f"MCP tool {name!r} 所属 Server 不存在")

        session = await self._get_session(server)
        _log(f"[mcp] call_tool({name!r}, arguments={arguments})")

        started = time.monotonic()
        started_at = runtime_log.now_iso()
        ok = True
        error_type: str | None = None
        error_message: str | None = None
        text = ""
        try:
            try:
                result = await session.call_tool(tool.mcp_tool_name, arguments)
            except ClosedResourceError as exc:
                # session 已失效（典型：远程 SSE 服务端断开连接后，缓存的
                # ClientSession 底层流已关闭）。最小修复：清理失效 session、
                # 重建、**最多自动重试一次**；重试仍失败则原样向外抛。
                reason = type(exc).__name__
                _log(
                    f"[mcp] ⚠ {server.name} session 已失效 ({reason})，"
                    "清理缓存并重连（最多重试 1 次）"
                )
                await self._drop_session(server)
                try:
                    retry_session = await self._get_session(server)
                except Exception:
                    self._record_reconnect(
                        server.name, tool.mcp_tool_name, reason, attempt=1, ok=False
                    )
                    raise
                self._record_reconnect(
                    server.name, tool.mcp_tool_name, reason, attempt=1, ok=True
                )
                result = await retry_session.call_tool(tool.mcp_tool_name, arguments)
            text = self._truncate_text(self._extract_text(result.content))
            _log(f"[mcp] call_tool({name!r}) returned {len(text)} chars (after truncation)")
            return text
        except Exception as exc:  # noqa: BLE001 - 原样向上传播，仅用于记录日志
            ok = False
            error_type = type(exc).__name__
            error_message = str(exc)
            raise
        finally:
            self._record_runtime_log(
                tool=tool,
                arguments=arguments,
                started_at=started_at,
                ok=ok,
                text=text,
                error_type=error_type,
                error_message=error_message,
                returned_at=runtime_log.now_iso(),
                duration_ms=int((time.monotonic() - started) * 1000),
            )

    def _record_runtime_log(
        self,
        *,
        tool: MCPTool,
        arguments: dict[str, Any],
        started_at: str,
        ok: bool,
        text: str,
        error_type: str | None,
        error_message: str | None,
        returned_at: str,
        duration_ms: int,
    ) -> None:
        """旁路写一条 runtime JSONL 记录；无上下文或写失败都静默跳过。"""
        ctx = runtime_log.get_runtime_context()
        if ctx is None:
            return
        try:
            self._write_runtime_record(
                ctx=ctx,
                tool=tool,
                arguments=arguments,
                started_at=started_at,
                ok=ok,
                text=text,
                error_type=error_type,
                error_message=error_message,
                returned_at=returned_at,
                duration_ms=duration_ms,
            )
        except Exception:  # noqa: BLE001 - 日志绝不能影响 MCP 调用
            pass

    @staticmethod
    def _write_runtime_record(
        *,
        ctx: Any,
        tool: MCPTool,
        arguments: dict[str, Any],
        started_at: str,
        ok: bool,
        text: str,
        error_type: str | None,
        error_message: str | None,
        returned_at: str,
        duration_ms: int,
    ) -> None:
        safe_args = runtime_log.scrub_value(arguments)
        record = runtime_log.build_record(
            ctx=ctx,
            tool_call={"name": tool.name, "arguments": safe_args},
            mcp_name=tool.server,
            tool_name=tool.mcp_tool_name,
            arguments=safe_args,
            started_at=started_at,
            ok=ok,
            result_chars=len(text),
            result_preview=runtime_log.scrub_text(
                text[: runtime_log.RESULT_PREVIEW_CHARS]
            ),
            error_type=error_type,
            error_message=(
                runtime_log.scrub_text(error_message) if error_message else None
            ),
            returned_at=returned_at,
            duration_ms=duration_ms,
        )
        runtime_log.append_record(ctx.directory, record)

    @staticmethod
    def _record_reconnect(
        mcp_name: str, tool_name: str, reason: str, *, attempt: int, ok: bool
    ) -> None:
        """旁路写一条 reconnect 事件；无上下文或写失败都静默跳过。"""
        try:
            ctx = runtime_log.get_runtime_context()
            if ctx is None:
                return
            record = runtime_log.build_reconnect_record(
                ctx=ctx,
                mcp_name=mcp_name,
                tool_name=tool_name,
                reason=reason,
                attempt=attempt,
                ok=ok,
            )
            runtime_log.append_record(ctx.directory, record)
        except Exception:  # noqa: BLE001 - 日志绝不能影响 MCP 调用
            pass

    async def _drop_session(self, server: MCPServerConfig) -> None:
        """从缓存中移除并关闭一个（已失效的）session。"""
        entry = self._sessions.pop(server.name, None)
        if entry is None:
            return
        session, cm = entry
        await _safe_aexit(session)
        await _safe_aexit(cm)

    async def close_all(self) -> None:
        """关闭所有已连接的 Server session。应用退出时调用。"""
        for name, (session, cm) in self._sessions.items():
            try:
                await session.__aexit__(None, None, None)
                if hasattr(cm, "__aexit__"):
                    await cm.__aexit__(None, None, None)
            except Exception as exc:
                _log(f"[mcp] 关闭 {name} 时出错: {exc}")
        self._sessions.clear()

    # ------------------------------------------------------------------
    # 内部实现 —— 单实例 session 管理
    # ------------------------------------------------------------------

    async def _ensure_schemas(self) -> dict[str, MCPTool]:
        if self._schemas is not None:
            return self._schemas

        _log("[mcp] 正在发现 MCP 工具...")
        discovered: dict[str, MCPTool] = {}
        for server in self.enabled_servers().values():
            try:
                session = await self._get_session(server)
                result = await session.list_tools()
            except Exception as exc:
                _log(
                    f"[mcp] ⚠ 获取 Server {server.name!r} 的工具定义失败: {type(exc).__name__}: {exc}"
                )
                continue

            target_name = server.tool_name
            for t in result.tools:
                if target_name and t.name != target_name:
                    continue
                cross_name = server.name
                discovered[cross_name] = MCPTool(
                    name=cross_name,
                    server=server.name,
                    mcp_tool_name=t.name,
                    description=t.description or "",
                    parameters=dict(t.inputSchema) if t.inputSchema else {"type": "object"},
                )

        self._schemas = discovered
        if discovered:
            _log(f"[mcp] 发现 {len(discovered)} 个工具: {list(discovered.keys())}")
        else:
            _log("[mcp] ⚠ 未发现任何 MCP 工具")
        return discovered

    async def _get_session(self, server: MCPServerConfig) -> ClientSession:
        """获取或创建 Server 的单例 session。线程安全。"""
        if server.name in self._sessions:
            return self._sessions[server.name][0]

        # 并发保护：防止多个模型同时触发同一个 Server 的初始化
        lock = self._locks.setdefault(server.name, asyncio.Lock())
        async with lock:
            # double-check：等锁期间可能已被其他协程初始化
            if server.name in self._sessions:
                return self._sessions[server.name][0]

            _log(f"[mcp] 正在连接 MCP Server: {server.name} (transport={server.transport})")
            if server.transport == "sse":
                if not server.url:
                    raise ValueError(f"MCP Server {server.name!r} 使用 sse 时必须提供 url")
                # 过滤空值 header，避免发送 "Bearer " 这类非法/无效鉴权头
                headers = {k: v for k, v in server.headers.items() if v and v.strip()}
                cm, read, write = await sse_server_cm(server.url, headers=headers)
            else:
                # stdio：只启动一次，后续复用
                env = os.environ.copy()
                env.update(server.env)
                params = StdioServerParameters(
                    command=server.command,
                    args=list(server.args),
                    env=env,
                )
                cm, read, write = await stdio_server_cm(params)

            session = ClientSession(read, write)
            try:
                await session.__aenter__()
                await session.initialize()
            except Exception:
                await _safe_aexit(session)
                await _safe_aexit(cm)
                raise

            self._sessions[server.name] = (session, cm)
            _log(f"[mcp] 已连接 MCP Server: {server.name}")
            return session

    @staticmethod
    def _extract_text(content: list[Any]) -> str:
        """把 MCP content 列表拼接成文本。"""
        parts: list[str] = []
        for item in content:
            if isinstance(item, TextContent):
                parts.append(item.text)
            else:
                parts.append(str(item))
        return "\n".join(parts)

    @staticmethod
    def _truncate_text(text: str, max_chars: int = TOOL_RESULT_MAX_CHARS) -> str:
        """对超长 tool_result 做安全截断，保留语义边界并附加提示。"""
        if len(text) <= max_chars:
            return text
        # 优先在 max_chars 前最近的一个空行处截断，保留语义边界
        boundary = text.rfind("\n\n", max_chars - 2000, max_chars)
        if boundary == -1:
            boundary = text.rfind("\n", max_chars - 500, max_chars)
        if boundary == -1:
            boundary = max_chars
        return (
            text[:boundary]
            + f"\n\n[Tool result truncated: {len(text)} chars total, "
            f"showing first {boundary} chars]"
        )


# ------------------------------------------------------------------
# Transport 层上下文管理器（可复用的，不绑定到 async with 生命周期）
# ------------------------------------------------------------------


async def _safe_aexit(obj: Any) -> None:
    """安全调用对象的 __aexit__，忽略异常。"""
    if obj is None:
        return
    try:
        await obj.__aexit__(None, None, None)
    except Exception:
        pass


async def stdio_server_cm(params: StdioServerParameters):
    """stdio transport 的可复用上下文管理器。"""
    cm = stdio_client(params)
    read, write = await cm.__aenter__()
    # 返回 cm 以便后续 cleanup，但不自动 close
    return cm, read, write


async def sse_server_cm(url: str, headers: dict[str, str] | None = None):
    """SSE transport 的可复用上下文管理器。"""
    cm = sse_client(url, headers=headers or {})
    read, write = await cm.__aenter__()
    return cm, read, write
