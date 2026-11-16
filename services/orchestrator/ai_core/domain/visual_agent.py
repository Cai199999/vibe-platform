"""局部 VisualNode 生成 Agent 的领域契约。

Coder 只输出 VisualDocument 的 VisualNode JSON 数组；
绝不接触真实 React 源码、沙盒或文件系统。
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ai_core.domain.common import CamelModel


class VisualNodeGenerationRequest(CamelModel):
    project_id: str = Field(min_length=1, max_length=80)
    document_id: str = Field(default="", max_length=80)
    node_id: str = Field(default="", max_length=80)
    instruction: str = Field(min_length=1, max_length=2000)
    # 当前选中节点（VisualNode JSON）；服务端只读，不接受前端声明的作用域。
    context: dict[str, Any] = Field(default_factory=dict)
    existing_ids: list[str] = Field(default_factory=list, max_length=2000)
    max_nodes: int = Field(default=40, ge=1, le=80)


class VisualNodeGenerationResponse(CamelModel):
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    model: str = ""
    latency_ms: int = 0
    repaired: bool = False
    trace_id: str = ""


class VisualNodeGenerationError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 500,
        diagnostic: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.diagnostic = dict(diagnostic or {})


__all__ = [
    "VisualNodeGenerationError",
    "VisualNodeGenerationRequest",
    "VisualNodeGenerationResponse",
]
