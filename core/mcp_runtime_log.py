"""MCP Runtime 调试日志（最小实现，不引入第三方框架）。

只做一件事：把每次 MCP tool_call 的关键信息以 **JSONL** 追加写入
「当前对话轮次目录」，便于点对点追踪：

    sessions/<session_id>/round_<NNN>/mcp_runtime.jsonl

设计约束（本次需求明确要求）：
- **不改变 MCP 调用行为**：写日志失败会被静默吞掉，绝不影响 call_tool 的返回值。
- **不引入 logging / observability 框架**：只用标准库 json / pathlib / datetime。
- **与 health check 完全分开**：健康检查脚本（scripts/manual/run_mcp_health.py）不依赖本模块；
  本模块在无 runtime 上下文时（如健康检查、单测）直接不落盘。
- **敏感信息必须脱敏**：Token / Authorization / Cookie / API Key 等一律替换。

Runtime 上下文通过 ``contextvars`` 传递——``round_manager`` 用
``asyncio.gather`` 并发跑多个模型，每个协程是独立 Task，Task 会复制当前 context，
因此在 ``_run_one`` 里设置 contextvar 天然做到「同轮多模型互不串扰」。
"""

from __future__ import annotations

import json
import re
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# 轮次目录内的日志文件名。与 health check 的 .mcp_health_report.json 完全分开。
RUNTIME_LOG_NAME = "mcp_runtime.jsonl"

# 返回内容预览长度（需求第 8 项：返回内容前 N 个字符）。
RESULT_PREVIEW_CHARS = 200

# 触发「整值替换」的敏感字段名（小写比对，含子串即命中）。
SENSITIVE_KEYS = (
    "authorization",
    "auth",
    "token",
    "api_key",
    "apikey",
    "api-key",
    "access_key",
    "secret",
    "client_secret",
    "password",
    "passwd",
    "cookie",
    "set-cookie",
    "bearer",
    "credential",
    "credentials",
    "private_key",
    "session_token",
)

REDACTED = "***REDACTED***"

# 字符串值内已知的凭证形态（无法按 key 判断时的兜底）。
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{8,}=*"),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}"),
    re.compile(r"\bsk-or-v1-[A-Za-z0-9]{16,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)\bauthorization\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(?:set-)?cookie\s*[:=]\s*\S+"),
)


# ----------------------------------------------------------------------
# Runtime 上下文（contextvars，天然隔离并发）
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeContext:
    """一次模型调用期间的 runtime 日志上下文。"""

    session_id: str
    round_id: int
    model_id: str
    directory: Path  # 轮次目录，例如 sessions/<sid>/round_001


_ctx: ContextVar[RuntimeContext | None] = ContextVar("mcp_runtime_ctx", default=None)


def set_runtime_context(ctx: RuntimeContext) -> Token:
    """设置当前协程的 runtime 上下文，返回用于 reset 的 Token。"""
    return _ctx.set(ctx)


def reset_runtime_context(token: Token) -> None:
    """还原 runtime 上下文（必须在 finally 中调用）。"""
    _ctx.reset(token)


def get_runtime_context() -> RuntimeContext | None:
    """取当前协程的 runtime 上下文；无上下文（健康检查/单测）时返回 None。"""
    return _ctx.get()


# ----------------------------------------------------------------------
# 脱敏
# ----------------------------------------------------------------------


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(s in lowered for s in SENSITIVE_KEYS)


def scrub_text(text: str) -> str:
    """对字符串做凭证脱敏（兜底：不依赖 key 名的模式匹配）。"""
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out


def scrub_value(value: Any) -> Any:
    """递归脱敏任意结构：dict 按 key 判定，str 按模式兜底。"""
    if isinstance(value, dict):
        return {
            k: (REDACTED if _is_sensitive_key(str(k)) else scrub_value(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [scrub_value(v) for v in value]
    if isinstance(value, tuple):
        return [scrub_value(v) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


# ----------------------------------------------------------------------
# 落盘
# ----------------------------------------------------------------------


def now_iso() -> str:
    """当前本地时间 ISO 8601（毫秒精度）。"""
    return datetime.now().isoformat(timespec="milliseconds")


def build_record(
    *,
    ctx: RuntimeContext,
    tool_call: dict[str, Any],
    mcp_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    started_at: str,
    ok: bool,
    result_chars: int,
    result_preview: str,
    error_type: str | None,
    error_message: str | None,
    returned_at: str,
    duration_ms: int,
) -> dict[str, Any]:
    """构造一条 JSONL 记录（字段顺序即需求 1–11 的顺序）。"""
    return {
        "ts": now_iso(),  # 写日志时刻
        "session_id": ctx.session_id,
        "round_id": ctx.round_id,
        "model_id": ctx.model_id,
        "tool_call": tool_call,  # 1. LLM 发出的原始 tool_call
        "mcp": mcp_name,  # 2. MCP 名称
        "tool": tool_name,  # 3. tool 名称
        "arguments": arguments,  # 4. arguments（脱敏）
        "started_at": started_at,  # 5. call 开始时间
        "ok": ok,  # 6. 返回成功/失败
        "result_chars": result_chars,  # 7. 返回字符数
        "result_preview": result_preview,  # 8. 返回内容前 N 个字符（脱敏）
        "error_type": error_type,  # 9. 异常类型
        "error_message": error_message,  # 9. 异常信息
        "returned_at": returned_at,  # 10. tool_result 回传给 LLM 的时间
        "duration_ms": duration_ms,
        "log_path": str(ctx.directory / RUNTIME_LOG_NAME),  # 11. 日志路径
    }


def append_record(directory: Path, record: dict[str, Any]) -> bool:
    """追加一行 JSONL。任何失败都返回 False，**绝不抛异常**（不影响调用行为）。"""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / RUNTIME_LOG_NAME
        line = json.dumps(record, ensure_ascii=False, default=str)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except Exception:  # noqa: BLE001 - 日志失败不能影响 MCP 调用
        return False


def build_reconnect_record(
    *,
    ctx: RuntimeContext,
    mcp_name: str,
    tool_name: str,
    reason: str,
    attempt: int,
    ok: bool,
) -> dict[str, Any]:
    """构造一条 reconnect 事件记录（session 失效 → 清理 → 重建）。"""
    return {
        "ts": now_iso(),
        "event": "reconnect",
        "session_id": ctx.session_id,
        "round_id": ctx.round_id,
        "model_id": ctx.model_id,
        "mcp": mcp_name,
        "tool": tool_name,
        "reason": reason,  # 触发重连的异常类型，如 ClosedResourceError
        "attempt": attempt,  # 第几次重试（当前实现最多 1）
        "ok": ok,  # session 重建是否成功
        "log_path": str(ctx.directory / RUNTIME_LOG_NAME),
    }


def read_records(directory: Path) -> list[dict[str, Any]]:
    """读取轮次目录下的全部 runtime 记录（供测试与人工排查使用）。"""
    path = directory / RUNTIME_LOG_NAME
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
