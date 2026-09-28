"""兼容垫片：mcp-server-reddit 0.2.1 代码引用了 mcp.shared.exceptions.McpError，
而本项目使用的 mcp 2.1.1 仍叫 MCPError。

本脚本在子进程启动时注入补丁，让 mcp-server-reddit 能正常 import。
"""

from __future__ import annotations

import asyncio

import mcp.shared.exceptions

if not hasattr(mcp.shared.exceptions, "McpError"):
    mcp.shared.exceptions.McpError = mcp.shared.exceptions.MCPError

from mcp_server_reddit import server  # noqa: E402

asyncio.run(server.serve())
