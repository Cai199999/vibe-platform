"""Direct Ops 的新增组件路由。

原则：白名单基础组件绝不进入 LLM；只有目录外组件才交给 Coder。
模板执行失败时由调用方转入 HITL（重试 / 视觉占位 / 取消）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_core.direct_ops.templates import COMPONENT_TEMPLATES

ROUTE_DIRECT_TEMPLATE = "DIRECT_TEMPLATE"
ROUTE_LLM_CODER = "LLM_CODER"


@dataclass(frozen=True)
class AddComponentRoute:
    route: str
    component_type: str
    reason: str


def route_add_component(component_type: str) -> AddComponentRoute:
    value = str(component_type or "").strip()
    if value in COMPONENT_TEMPLATES:
        return AddComponentRoute(
            route=ROUTE_DIRECT_TEMPLATE,
            component_type=value,
            reason="命中 Direct Ops 白名单模板",
        )
    return AddComponentRoute(
        route=ROUTE_LLM_CODER,
        component_type=value,
        reason="组件不在白名单，需要 AI Coder 生成",
    )


__all__ = [
    "AddComponentRoute",
    "ROUTE_DIRECT_TEMPLATE",
    "ROUTE_LLM_CODER",
    "route_add_component",
]
