#!/usr/bin/env python3
"""最小但标准的 MCP stdio Server 示例。

协议：
  - JSON-RPC 2.0，按行分隔（MCP stdio transport 要求一行一个消息，
    消息内部不得包含换行）；
  - 支持 initialize / notifications/initialized / ping / tools/list /
    tools/call；
  - 暴露一个只读工具 ``get_current_time``，用于验证平台 MCP 接入。

运行方式（由平台 MCP 客户端以子进程启动）：
    python scripts/mcp_server_demo.py

单独调试：
    printf '%s\n' \
      '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
      '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
      '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_current_time","arguments":{"timezone":"Asia/Shanghai"}}}' \
      | python scripts/mcp_server_demo.py

日志只能写 stderr；stdout 是协议通道，任何额外输出都会破坏客户端解析。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
SERVER_NAME = "vibe-mcp-demo"
SERVER_VERSION = "0.1.0"

TOOLS = [
    {
        "name": "get_current_time",
        "description": "返回指定 IANA 时区的当前日期与时间。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "IANA 时区名，例如 Asia/Shanghai、UTC。默认 UTC。",
                    "default": "UTC",
                }
            },
            "required": [],
            "additionalProperties": False,
        },
    }
]


def _log(message: str) -> None:
    print(f"[mcp-demo] {message}", file=sys.stderr, flush=True)


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _tool_get_current_time(arguments: dict[str, Any]) -> dict[str, Any]:
    timezone_name = str(arguments.get("timezone") or "UTC").strip() or "UTC"
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"未知时区: {timezone_name}；请使用 IANA 时区名，例如 Asia/Shanghai。",
                }
            ],
            "isError": True,
        }

    now = datetime.now(zone)
    payload = {
        "timezone": timezone_name,
        "iso": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "utcOffset": now.strftime("%z"),
        "unix": int(now.timestamp()),
    }
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False),
            }
        ],
        "structuredContent": payload,
        "isError": False,
    }


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "get_current_time":
        return _tool_get_current_time(arguments)
    return {
        "content": [{"type": "text", "text": f"未知工具: {name}"}],
        "isError": True,
    }


def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    """处理单条 JSON-RPC 消息；notification 返回 None。"""
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    # 没有 id 的是 notification（如 notifications/initialized），不回包。
    if request_id is None:
        if method == "notifications/initialized":
            _log("client initialized")
        return None

    if method == "initialize":
        requested = str(params.get("protocolVersion") or PROTOCOL_VERSION)
        version = (
            requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        )
        return _result(
            request_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not name:
            return _error(request_id, -32602, "tools/call 缺少 name")
        if not isinstance(arguments, dict):
            return _error(request_id, -32602, "arguments 必须是对象")
        return _result(request_id, _call_tool(name, arguments))
    if method == "resources/list":
        return _result(request_id, {"resources": []})
    if method == "prompts/list":
        return _result(request_id, {"prompts": []})
    if method == "logging/setLevel":
        return _result(request_id, {})
    return _error(request_id, -32601, f"Method not found: {method}")


def main() -> int:
    _log(f"server started pid={__import__('os').getpid()} protocol={PROTOCOL_VERSION}")
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = _error(None, -32700, f"Parse error: {exc.msg}")
            print(json.dumps(response, ensure_ascii=False), flush=True)
            continue
        if not isinstance(request, dict):
            response = _error(None, -32600, "Invalid Request")
            print(json.dumps(response, ensure_ascii=False), flush=True)
            continue
        try:
            response = handle_request(request)
        except Exception as exc:  # noqa: BLE001 - 单条请求失败不能退出服务
            _log(f"request failed: {exc}")
            response = _error(request.get("id"), -32603, f"Internal error: {exc}")
        if response is None:
            continue
        # compact JSON + 换行；ensure_ascii=False 保留中文可读性。
        print(
            json.dumps(response, ensure_ascii=False, separators=(",", ":")),
            flush=True,
        )
    _log("stdin closed, server exit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
