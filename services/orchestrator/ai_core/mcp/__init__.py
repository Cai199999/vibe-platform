"""MCP（Model Context Protocol）stdio 客户端与 Tool 适配。

平台不引入官方 ``mcp`` SDK，只实现本任务需要的子集：
  - JSON-RPC 2.0 / newline-delimited stdio；
  - initialize / notifications/initialized / ping / tools/list / tools/call。

这样可以精确控制超时、进程隔离与工具白名单，也避免为一个 Demo 增加
较重的依赖。若后续需要 resources / prompts / sampling，可平滑替换为
官方 SDK，Tool 适配层保持不变。
"""

from ai_core.mcp.client import (
    McpError,
    McpProtocolError,
    McpServerConfig,
    McpStdioClient,
    McpTimeoutError,
)
from ai_core.mcp.tools import register_mcp_tools

__all__ = [
    "McpError",
    "McpProtocolError",
    "McpServerConfig",
    "McpStdioClient",
    "McpTimeoutError",
    "register_mcp_tools",
]
