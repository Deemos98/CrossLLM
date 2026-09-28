"""MCP Server 批量健康检查（mcp中期部署.md §6/§7）。

职责：
- 读取 config/mcp.yaml 中所有 enabled MCP Server；
- 对每个 Server 执行 connect → tools/list → smoke test → disconnect；
- 输出 READY / DEGRADED / AUTH_REQUIRED / FAILED 分类报告；
- 不依赖真实 LLM 调用，仅验证 MCP Server 本身健康度。

用法：
    python scripts/manual/run_mcp_health.py
    # 或在 venv 中：
    .venv/Scripts/python scripts/manual/run_mcp_health.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _load_dotenv(path: str | Path) -> None:
    """极简 .env 加载器（标准库实现，不引 python-dotenv）。真实环境变量优先。"""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _resolve_env(value: str) -> str:
    """解析字符串中的 ${ENV_NAME} 占位符。"""

    def _repl(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1), "")

    return _ENV_PLACEHOLDER.sub(_repl, value)
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_CONFIG_PATH = PROJECT_ROOT / "config" / "mcp.yaml"

# 健康检查脚本独立运行，需要显式加载 .env，否则 os.environ 中缺少密钥占位符。
_load_dotenv(PROJECT_ROOT / ".env")

# 每个 Server 的只读 smoke test 参数。
# key 必须和 mcp.yaml 中 server 名一致。
SMOKE_TESTS: dict[str, dict[str, Any]] = {
    "openwebsearch": {"query": "faster-whisper VAD", "limit": 3, "engines": ["bing"]},
    "github": {"q": "is:issue repo:SYSTRAN/faster-whisper VAD", "limit": 3},
    "fetch": {"url": "https://example.com", "max_length": 1000},
    "filesystem": {"path": "C:/pythonProject/CrossLLM/docs/mcp.md"},
    "context7": {"library": "faster-whisper", "query": "VAD"},
    "playwright": {"url": "https://example.com"},
    "stackoverflow": {
        "query": "python list index out of range",
        "tags": ["python"],
        "limit": 3,
    },
    "zhihu": {"query": "RAG", "count": 3},
    "trendshub": {"limit": 5},
    "openalex": {"query": "transformer", "max_results": 3},
    "everything": {"query": "*.md", "limit": 3, "path": "C:/pythonProject/CrossLLM"},
    "pdf2md": {"file_path": "C:/pythonProject/CrossLLM/README.md", "max_pages": 1},
    "excel": {"fileAbsolutePath": "C:/pythonProject/CrossLLM/.pytest_tmp/sample.xlsx", "sheetName": "Sheet1"},
    "yt_transcript": {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "lang": "en"},
    "huggingface": {"query": "whisper", "limit": 3},
    "reddit": {"limit": 3},
}

OPERATION_TIMEOUT = 60.0  # 单个阶段超时（秒）


def _load_config(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("servers") or {}


def _extract_text(content: list[Any]) -> str:
    parts: list[str] = []
    for item in content:
        if isinstance(item, TextContent):
            parts.append(item.text)
        else:
            parts.append(str(item))
    return "\n".join(parts)


async def _check_stdio(
    name: str,
    cfg: dict[str, Any],
    tool_name: str,
    smoke_args: dict[str, Any],
) -> dict[str, Any]:
    """stdio  transport 健康检查。"""
    result: dict[str, Any] = {
        "name": name,
        "transport": "stdio",
        "connect": {"ok": False, "error": None},
        "tools_list": {"ok": False, "count": 0, "available": [], "error": None},
        "smoke": {"ok": False, "error": None, "chars": 0},
    }

    command = str(cfg.get("command", ""))
    args = [str(a) for a in cfg.get("args") or []]
    raw_env = cfg.get("env") or {}
    env = os.environ.copy()
    for k, v in raw_env.items():
        env[str(k)] = _resolve_env(str(v))

    params = StdioServerParameters(command=command, args=args, env=env)

    cm = None
    session = None
    try:
        cm = stdio_client(params)
        read, write = await asyncio.wait_for(
            cm.__aenter__(), timeout=OPERATION_TIMEOUT
        )
        result["connect"]["ok"] = True
    except Exception as exc:
        result["connect"]["error"] = f"{type(exc).__name__}: {exc}"
        return result

    try:
        session = ClientSession(read, write)
        await asyncio.wait_for(session.__aenter__(), timeout=OPERATION_TIMEOUT)
        await asyncio.wait_for(session.initialize(), timeout=OPERATION_TIMEOUT)
    except Exception as exc:
        result["connect"]["ok"] = False
        result["connect"]["error"] = f"initialize failed: {type(exc).__name__}: {exc}"
        await _cleanup(session, cm)
        return result

    try:
        tools_result = await asyncio.wait_for(
            session.list_tools(), timeout=OPERATION_TIMEOUT
        )
        result["tools_list"]["ok"] = True
        result["tools_list"]["count"] = len(tools_result.tools)
        result["tools_list"]["available"] = [t.name for t in tools_result.tools]
    except Exception as exc:
        result["tools_list"]["error"] = f"{type(exc).__name__}: {exc}"
        await _cleanup(session, cm)
        return result

    if tool_name and not any(t.name == tool_name for t in tools_result.tools):
        result["smoke"]["error"] = f"tool_name {tool_name!r} not found in tools/list"
        await _cleanup(session, cm)
        return result

    actual_tool = tool_name or (tools_result.tools[0].name if tools_result.tools else "")
    if not actual_tool:
        result["smoke"]["error"] = "no tool available for smoke test"
        await _cleanup(session, cm)
        return result

    try:
        call_result = await asyncio.wait_for(
            session.call_tool(actual_tool, smoke_args), timeout=OPERATION_TIMEOUT
        )
        text = _extract_text(call_result.content)
        result["smoke"]["ok"] = True
        result["smoke"]["chars"] = len(text)
    except Exception as exc:
        result["smoke"]["error"] = f"{type(exc).__name__}: {exc}"

    await _cleanup(session, cm)
    return result


async def _check_sse(
    name: str,
    cfg: dict[str, Any],
    tool_name: str,
    smoke_args: dict[str, Any],
) -> dict[str, Any]:
    """SSE transport 健康检查。"""
    result: dict[str, Any] = {
        "name": name,
        "transport": "sse",
        "connect": {"ok": False, "error": None},
        "tools_list": {"ok": False, "count": 0, "available": [], "error": None},
        "smoke": {"ok": False, "error": None, "chars": 0},
    }

    url = str(cfg.get("url", ""))
    raw_headers = cfg.get("headers") or {}
    headers = {str(k): _resolve_env(str(v)) for k, v in raw_headers.items()}

    # 如果鉴权头已配置但解析后为空，直接标记为 AUTH_REQUIRED，避免发送非法 header
    auth_header = headers.get("Authorization", "")
    if auth_header and ("${" in str(raw_headers.get("Authorization", ""))) and auth_header.strip() in ("", "Bearer"):
        result["connect"]["error"] = "Authorization token missing (AUTH_REQUIRED)"
        result["connect"]["ok"] = False
        return result

    cm = None
    session = None
    try:
        cm = sse_client(url, headers=headers)
        read, write = await asyncio.wait_for(
            cm.__aenter__(), timeout=OPERATION_TIMEOUT
        )
        result["connect"]["ok"] = True
    except Exception as exc:
        result["connect"]["error"] = f"{type(exc).__name__}: {exc}"
        return result

    try:
        session = ClientSession(read, write)
        await asyncio.wait_for(session.__aenter__(), timeout=OPERATION_TIMEOUT)
        await asyncio.wait_for(session.initialize(), timeout=OPERATION_TIMEOUT)
    except Exception as exc:
        result["connect"]["ok"] = False
        result["connect"]["error"] = f"initialize failed: {type(exc).__name__}: {exc}"
        await _cleanup(session, cm)
        return result

    try:
        tools_result = await asyncio.wait_for(
            session.list_tools(), timeout=OPERATION_TIMEOUT
        )
        result["tools_list"]["ok"] = True
        result["tools_list"]["count"] = len(tools_result.tools)
        result["tools_list"]["available"] = [t.name for t in tools_result.tools]
    except Exception as exc:
        result["tools_list"]["error"] = f"{type(exc).__name__}: {exc}"
        await _cleanup(session, cm)
        return result

    if tool_name and not any(t.name == tool_name for t in tools_result.tools):
        result["smoke"]["error"] = f"tool_name {tool_name!r} not found in tools/list"
        await _cleanup(session, cm)
        return result

    actual_tool = tool_name or (tools_result.tools[0].name if tools_result.tools else "")
    if not actual_tool:
        result["smoke"]["error"] = "no tool available for smoke test"
        await _cleanup(session, cm)
        return result

    try:
        call_result = await asyncio.wait_for(
            session.call_tool(actual_tool, smoke_args), timeout=OPERATION_TIMEOUT
        )
        text = _extract_text(call_result.content)
        result["smoke"]["ok"] = True
        result["smoke"]["chars"] = len(text)
    except Exception as exc:
        result["smoke"]["error"] = f"{type(exc).__name__}: {exc}"

    await _cleanup(session, cm)
    return result


async def _cleanup(session: ClientSession | None, cm: Any) -> None:
    """安全关闭 session 和 transport。"""
    if session is not None:
        try:
            await session.__aexit__(None, None, None)
        except Exception:
            pass
    if cm is not None:
        try:
            await cm.__aexit__(None, None, None)
        except Exception:
            pass


def _classify(result: dict[str, Any]) -> str:
    """根据结果分类状态。"""
    if not result["connect"]["ok"]:
        err = str(result["connect"]["error"] or "").lower()
        if "auth" in err or "unauthorized" in err or "forbidden" in err or "401" in err:
            return "AUTH_REQUIRED"
        if "api key" in err or "apikey" in err or "missing token" in err or "auth_required" in err:
            return "AUTH_REQUIRED"
        return "FAILED"
    if not result["tools_list"]["ok"]:
        err = str(result["tools_list"]["error"] or "").lower()
        if "auth" in err or "unauthorized" in err or "forbidden" in err or "401" in err:
            return "AUTH_REQUIRED"
        return "DEGRADED"
    if not result["smoke"]["ok"]:
        err = str(result["smoke"]["error"] or "").lower()
        if "auth" in err or "unauthorized" in err or "forbidden" in err or "401" in err:
            return "AUTH_REQUIRED"
        if "api key" in err or "apikey" in err or "rate limit" in err or "429" in err:
            return "AUTH_REQUIRED"
        return "DEGRADED"
    return "READY"


def _icon(ok: bool) -> str:
    return "✅" if ok else "❌"


async def check_server(name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    if not cfg.get("enabled", True):
        return {
            "name": name,
            "status": "DISABLED",
            "connect": {"ok": True},
            "tools_list": {"ok": True, "count": 0},
            "smoke": {"ok": True, "chars": 0},
        }

    transport = str(cfg.get("transport", "stdio")).lower()
    tool_name = str(cfg.get("tool_name", "")).strip()
    smoke_args = SMOKE_TESTS.get(name, {})

    if transport == "sse":
        result = await _check_sse(name, cfg, tool_name, smoke_args)
    else:
        result = await _check_stdio(name, cfg, tool_name, smoke_args)

    result["status"] = _classify(result)
    return result


async def main() -> int:
    print("MCP HEALTH CHECK")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Config: {MCP_CONFIG_PATH}")
    print("-" * 60)

    servers = _load_config(MCP_CONFIG_PATH)
    if not servers:
        print("No enabled MCP servers found.")
        return 1

    results: list[dict[str, Any]] = []
    for name in sorted(servers.keys()):
        cfg = servers[name]
        print(f"\nChecking {name} ...", file=sys.stderr)
        try:
            result = await check_server(name, cfg)
        except Exception as exc:
            traceback.print_exc()
            result = {
                "name": name,
                "status": "FAILED",
                "connect": {"ok": False, "error": f"unexpected: {exc}"},
                "tools_list": {"ok": False},
                "smoke": {"ok": False},
            }
        results.append(result)

    # 分类汇总
    buckets: dict[str, list[dict[str, Any]]] = {
        "READY": [],
        "DEGRADED": [],
        "AUTH_REQUIRED": [],
        "FAILED": [],
        "DISABLED": [],
    }
    for r in results:
        buckets[r["status"]].append(r)

    print("\n" + "=" * 60)
    print("MCP HEALTH CHECK RESULTS")
    print("=" * 60)

    for r in results:
        print(f"\n{r['name']}  (transport={r.get('transport', 'stdio')})")
        print(f"  connect    {_icon(r['connect']['ok'])} {r['connect'].get('error', '')}")
        print(
            f"  tools/list {_icon(r['tools_list']['ok'])} "
            f"{r['tools_list'].get('count', 0)} tools"
        )
        if r["tools_list"].get("available"):
            available = r["tools_list"]["available"]
            print(f"             available: {', '.join(available[:5])}" + (
                " ..." if len(available) > 5 else ""
            ))
        if r["tools_list"].get("error"):
            print(f"             error: {r['tools_list']['error']}")
        print(
            f"  smoke      {_icon(r['smoke']['ok'])} "
            f"{r['smoke'].get('chars', 0)} chars"
        )
        if r["smoke"].get("error"):
            print(f"             error: {r['smoke']['error']}")
        print(f"  status     {r['status']}")

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for status in ["READY", "DEGRADED", "AUTH_REQUIRED", "FAILED", "DISABLED"]:
        items = buckets[status]
        if not items:
            continue
        print(f"\n{status}: {len(items)}")
        for r in items:
            detail = ""
            if not r["connect"]["ok"]:
                detail = r["connect"].get("error", "connect failed")
            elif not r["tools_list"]["ok"]:
                detail = r["tools_list"].get("error", "tools/list failed")
            elif not r["smoke"]["ok"]:
                detail = r["smoke"].get("error", "smoke test failed")
            print(f"  - {r['name']}" + (f" ({detail})" if detail else ""))

    total = len(results)
    ready = len(buckets["READY"])
    print(f"\nTotal: {total} | READY: {ready}")

    # 输出 JSON 报告方便下游消费
    report_path = PROJECT_ROOT / ".mcp_health_report.json"
    report_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nReport saved to: {report_path}")

    return 0 if not buckets["FAILED"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
