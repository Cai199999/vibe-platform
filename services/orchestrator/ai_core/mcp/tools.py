"""把 MCP tools/list 声明的远端工具适配为 LangChain Tool。

适配策略：
  - 连接只在发现工具时短暂启动，拿到 tools/list 后立即关闭；
  - 每次实际调用再启动一次独立进程，避免长连接在 FastAPI 多线程 /
    Worker 重启后变成僵尸；
  - 远端 inputSchema 转成 Pydantic 模型，交给 StructuredTool 做参数校验；
  - 工具名统一前缀 ``mcp_<server>_<tool>``，避免与本地 search_web /
    shell_tool 冲突；
  - allowed_tools 白名单只允许暴露服务端显式批准的工具。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import Field, create_model

from ai_core.config import Settings
from ai_core.mcp.client import McpError, McpServerConfig, McpStdioClient
from ai_core.tools.base import ToolDescriptor, ToolRegistry

logger = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_]+")
_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _safe_name(value: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("_", value or "").strip("_")
    return cleaned or "tool"


def _argument_model(local_name: str, definition: dict[str, Any]) -> type:
    """把 MCP inputSchema.properties 转成 Pydantic 参数模型。"""
    schema = definition.get("inputSchema") or definition.get("input_schema") or {}
    if not isinstance(schema, dict):
        schema = {}
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    fields: dict[str, Any] = {}
    if isinstance(properties, dict):
        for raw_name, raw_spec in properties.items():
            name = str(raw_name)
            if not name.isidentifier():
                # Pydantic / Python 不支持的关键字交给远端服务器自校验，
                # 这里跳过，避免生成非法模型。
                continue
            spec = raw_spec if isinstance(raw_spec, dict) else {}
            python_type = _TYPE_MAP.get(str(spec.get("type")), Any)
            description = str(spec.get("description") or "")
            if name in required:
                default = Field(..., description=description)
            else:
                default = Field(spec.get("default"), description=description)
            fields[name] = (python_type, default)
    model_name = f"McpArgs_{_safe_name(local_name)}"
    return create_model(model_name, **fields)


def _build_langchain_tool(
    config: McpServerConfig,
    definition: dict[str, Any],
    local_name: str,
) -> BaseTool:
    remote_name = str(definition.get("name") or "").strip()
    description = str(definition.get("description") or "").strip() or remote_name
    args_schema = _argument_model(local_name, definition)

    def call(**kwargs: Any) -> str:
        client = McpStdioClient(config)
        try:
            client.start()
            result = client.call_tool(remote_name, kwargs)
        except McpError as exc:
            logger.warning("MCP tool %s failed: %s", local_name, exc)
            return f"MCP 工具调用失败：{exc}"
        finally:
            client.close()
        if result.get("isError"):
            return f"MCP 工具返回错误：{result.get('text')}"
        return str(result.get("text") or "")

    return StructuredTool.from_function(
        func=call,
        name=local_name,
        description=f"[MCP/{config.name}] {description}",
        args_schema=args_schema,
    )


def _discover(config: McpServerConfig) -> list[dict[str, Any]]:
    client = McpStdioClient(config)
    try:
        client.start()
        return client.list_tools()
    finally:
        client.close()


def register_mcp_tools(registry: ToolRegistry, settings: Settings) -> int:
    """发现并注册所有配置的 MCP 工具，返回注册数量。

    单个 Server 失败只记录 warning，不影响其它 Server 和本地 Tool。
    """
    registered = 0
    for raw in settings.mcp_servers:
        if not isinstance(raw, dict):
            logger.warning("skip invalid MCP config: %r", raw)
            continue
        if raw.get("enabled", True) is False:
            continue
        transport = str(raw.get("transport") or "stdio")
        if transport != "stdio":
            logger.warning(
                "skip MCP server %s: unsupported transport %s",
                raw.get("name"),
                transport,
            )
            continue
        try:
            config = McpServerConfig.from_dict(
                raw,
                default_timeout=settings.mcp_call_timeout_seconds,
            )
            definitions = _discover(config)
        except Exception as exc:  # noqa: BLE001 - 外部进程故障隔离
            logger.warning(
                "skip MCP server %s: %s",
                raw.get("name") or "unknown",
                exc,
            )
            continue
        for definition in definitions:
            remote_name = str(definition.get("name") or "").strip()
            if not remote_name:
                continue
            if config.allowed_tools and remote_name not in config.allowed_tools:
                logger.info(
                    "skip MCP tool %s: not in allowed_tools",
                    remote_name,
                )
                continue
            local_name = f"mcp_{_safe_name(config.name)}_{_safe_name(remote_name)}"
            try:
                tool = _build_langchain_tool(config, definition, local_name)
                registry.register(
                    ToolDescriptor(
                        name=local_name,
                        description=str(definition.get("description") or remote_name),
                        danger_level="external",
                        metadata={
                            "mcp": True,
                            "server": config.name,
                            "remoteTool": remote_name,
                            "transport": "stdio",
                            "inputSchema": definition.get("inputSchema") or {},
                        },
                    ),
                    tool,
                )
            except Exception as exc:  # noqa: BLE001 - 单个工具失败隔离
                logger.warning(
                    "skip MCP tool %s/%s: %s",
                    config.name,
                    remote_name,
                    exc,
                )
                continue
            registered += 1
    return registered
