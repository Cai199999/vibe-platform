"""Tool 注册中心。

Tool 是 Agent 可以直接调用的最小能力单元（读文件、执行只读命令等），
与 Skill（Prompt + RAG + Tool 子集）严格分层：

  - Tool：无状态函数，必须声明 JSON 参数结构并可被 LangGraph ToolNode 调用；
  - Skill：把若干 Tool 与领域 Prompt 组合成可复用的能力包；
  - Agent：拥有状态机的执行者，决定何时调用哪个 Skill / Tool。

本模块只做注册与元数据展示；真正的工具调用循环由 LangGraph ToolNode
承载，避免在业务代码里手写 `if tool_name == ...` 分发。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import BaseTool


class ToolRegistryError(ValueError):
    """Tool 名称冲突或元数据不合法。"""

    code = "TOOL_REGISTRY_ERROR"


@dataclass(frozen=True)
class ToolDescriptor:
    """公开给 Manifest / 前端的 Tool 视图，不包含可调用对象。"""

    name: str
    description: str
    danger_level: str = "read"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_view(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "dangerLevel": self.danger_level,
            "metadata": dict(self.metadata),
        }


class ToolRegistry:
    """线程安全的 Tool 注册中心。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 这里只保存 LangChain BaseTool；所有工具都必须能被 ToolNode 直接挂载。
        self._tools: dict[str, BaseTool] = {}
        self._descriptors: dict[str, ToolDescriptor] = {}

    def register(
        self,
        descriptor: ToolDescriptor,
        tool: BaseTool,
        *,
        replace: bool = False,
    ) -> None:
        if not descriptor.name:
            raise ToolRegistryError("Tool 名称不能为空")
        with self._lock:
            if descriptor.name in self._tools and not replace:
                raise ToolRegistryError(f"Tool 已注册: {descriptor.name}")
            self._tools[descriptor.name] = tool
            self._descriptors[descriptor.name] = descriptor

    def get(self, name: str) -> BaseTool | None:
        with self._lock:
            return self._tools.get(name)

    def list_tools(self) -> list[BaseTool]:
        with self._lock:
            return [self._tools[name] for name in sorted(self._tools)]

    def list_descriptors(self) -> list[ToolDescriptor]:
        with self._lock:
            return [self._descriptors[name] for name in sorted(self._descriptors)]

    def to_views(self) -> list[dict[str, Any]]:
        return [item.to_view() for item in self.list_descriptors()]
