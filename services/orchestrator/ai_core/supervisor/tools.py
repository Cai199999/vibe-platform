"""Supervisor 级 Tool 网关。

Supervisor 本身不写代码，但需要知道“当前平台有哪些可调用能力”，并提供
一个受控入口验证本地 Tool（search_web / shell_tool）与 MCP 远端 Tool 是否
真的可用。该网关只做三件事：

  1. 列出 ToolRegistry 中的工具描述（不暴露可调用对象 / 密钥）；
  2. 按名称调用某个工具，并返回耗时与结果；
  3. 把任何外部故障隔离成领域异常，避免 500 泄漏内部细节。

安全边界：
  - ToolRegistry 来自服务端 Settings，绝不接受客户端传入 command / env；
  - shell_tool 默认禁用，MCP Server 只允许 settings.mcp_servers 白名单；
  - 该 API 仅供内部网络调用（与 /supervisor/* 相同边界）。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ai_core.config import Settings, get_settings
from ai_core.tools import ToolRegistry, build_default_toolbox

logger = logging.getLogger(__name__)

MAX_TOOL_RESULT_CHARS = 20_000


class SupervisorToolError(ValueError):
    """Tool 网关领域异常。"""

    code = "SUPERVISOR_TOOL_ERROR"
    status_code = 400


class SupervisorToolNotFoundError(SupervisorToolError):
    code = "TOOL_NOT_FOUND"
    status_code = 404


class SupervisorToolInvocationError(SupervisorToolError):
    code = "TOOL_INVOCATION_FAILED"
    status_code = 502


class SupervisorToolService:
    """构建工具箱并执行受控调用。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _toolbox(self) -> ToolRegistry:
        # 每次请求重新构建：MCP Server 可能刚重启或配置刚 reload。
        # 本地 Tool 注册开销可忽略；MCP 发现失败会被 build_default_toolbox
        # 隔离成 warning，不会阻断列表 / 调用。
        return build_default_toolbox(self.settings)

    def list_tools(self) -> tuple[list[dict[str, Any]], list[str]]:
        registry = self._toolbox()
        servers = sorted(
            {
                str(item.get("name"))
                for item in self.settings.mcp_servers
                if isinstance(item, dict)
                and item.get("name")
                and item.get("enabled", True) is not False
            }
        )
        return registry.to_views(), servers

    def invoke_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        registry = self._toolbox()
        tool = registry.get(name)
        if tool is None:
            raise SupervisorToolNotFoundError(f"Tool 未注册: {name}")
        descriptor = next(
            (item for item in registry.list_descriptors() if item.name == name),
            None,
        )
        payload = arguments or {}
        if not isinstance(payload, dict):
            raise SupervisorToolError("arguments 必须是对象")

        started = time.perf_counter()
        try:
            raw = tool.invoke(payload)
        except Exception as exc:  # noqa: BLE001 - 统一转换为可读错误
            logger.warning("supervisor tool %s failed: %s", name, exc)
            raise SupervisorToolInvocationError(f"Tool 调用失败 {name}: {exc}") from exc
        duration_ms = int((time.perf_counter() - started) * 1000)

        if isinstance(raw, str):
            result = raw
        else:
            try:
                result = json.dumps(raw, ensure_ascii=False)
            except TypeError:
                result = str(raw)
        if len(result) > MAX_TOOL_RESULT_CHARS:
            result = result[:MAX_TOOL_RESULT_CHARS] + "...[truncated]"
        return {
            "tool": name,
            "result": result,
            "duration_ms": duration_ms,
            "danger_level": descriptor.danger_level if descriptor else "unknown",
            "metadata": descriptor.metadata if descriptor else {},
        }
