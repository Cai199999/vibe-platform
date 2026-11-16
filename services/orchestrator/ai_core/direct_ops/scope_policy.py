"""ScopePolicy：根据 NodeIdentity 决定操作范围与兜底策略。

原则：
  - 普通实例：直接执行；
  - 同源码多实例：允许“全部实例”操作；
  - 组件定义被引用：优先 callSite；
  - map item：转数据源编辑 / 视觉隐藏 HITL；
  - 页面根节点：不删除，替换为空占位符；
  - 绝不返回“无路可走”的空决策。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ai_core.domain.node_identity import NodeIdentity, OperationScope


@dataclass(frozen=True)
class ScopeDecision:
    allowed: bool
    scope: OperationScope
    strategy: str  # direct | replace_placeholder | hitl | blocked
    reasons: list[str] = field(default_factory=list)
    options: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "scope": self.scope,
            "strategy": self.strategy,
            "reasons": list(self.reasons),
            "options": list(self.options),
        }


def _option(option_id: str, label: str, action: str, description: str = "") -> dict:
    return {
        "optionId": option_id,
        "label": label,
        "action": action,
        "description": description,
    }


def decide_scope(action: str, identity: NodeIdentity) -> ScopeDecision:
    action = str(action or "").upper()

    if action == "DELETE_NODE":
        if identity.kind == "map_item":
            return ScopeDecision(
                allowed=False,
                scope="data_source",
                strategy="hitl",
                reasons=["列表项由数据源生成，不能只删除一个 JSX 节点"],
                options=[
                    _option(
                        "remove_data_item",
                        "删除对应的数据项（AI 定位数据源）",
                        "AI_DATA_EDIT",
                    ),
                    _option(
                        "visual_hide",
                        "仅隐藏视觉上的当前项",
                        "VISUAL_ONLY",
                    ),
                    _option("cancel", "取消", "CANCEL"),
                ],
            )
        if identity.kind == "route_root":
            return ScopeDecision(
                allowed=True,
                scope="replace_root",
                strategy="replace_placeholder",
                reasons=["页面根节点不能直接删除，将替换为空占位符"],
            )
        if identity.has_call_site:
            return ScopeDecision(True, "instance", "direct")
        if identity.instance_count > 1 and identity.importer_count == 0:
            return ScopeDecision(
                True,
                "all_instances",
                "direct",
                reasons=[f"将删除该源码对应的全部 {identity.instance_count} 个实例"],
            )
        if identity.is_protected and identity.importer_count > 0:
            return ScopeDecision(
                allowed=False,
                scope="definition",
                strategy="hitl",
                reasons=["组件定义被其它文件引用，需要选择修改范围"],
                options=[
                    _option("detach_instance", "仅分离当前实例", "DETACH"),
                    _option(
                        "edit_definition", "修改组件定义（影响全部）", "DEFINITION"
                    ),
                    _option("cancel", "取消", "CANCEL"),
                ],
            )
        return ScopeDecision(True, "instance", "direct")

    if action == "MOVE_NODE":
        if identity.kind == "map_item":
            return ScopeDecision(
                allowed=False,
                scope="data_source",
                strategy="hitl",
                reasons=["列表项位置由数据源顺序决定"],
                options=[
                    _option("reorder_data", "调整数据源顺序（AI）", "AI_DATA_EDIT"),
                    _option("cancel", "取消", "CANCEL"),
                ],
            )
        if identity.kind == "route_root":
            return ScopeDecision(
                allowed=False,
                scope="replace_root",
                strategy="hitl",
                reasons=["页面根节点不支持直接移动，建议先包裹容器"],
                options=[
                    _option("wrap_root", "先用容器包裹根节点", "WRAP"),
                    _option("cancel", "取消", "CANCEL"),
                ],
            )
        if identity.has_call_site:
            return ScopeDecision(True, "instance", "direct")
        if identity.instance_count > 1 and identity.importer_count == 0:
            return ScopeDecision(
                True,
                "all_instances",
                "direct",
                reasons=[f"将移动该源码对应的全部 {identity.instance_count} 个实例"],
            )
        if identity.is_protected and identity.importer_count > 0:
            return ScopeDecision(
                allowed=False,
                scope="definition",
                strategy="hitl",
                reasons=["组件定义被引用，移动会影响多个页面"],
                options=[
                    _option("detach_instance", "仅分离当前实例", "DETACH"),
                    _option(
                        "edit_definition", "移动组件定义（影响全部）", "DEFINITION"
                    ),
                    _option("cancel", "取消", "CANCEL"),
                ],
            )
        return ScopeDecision(True, "instance", "direct")

    if action == "UPDATE_TEXT":
        if identity.kind == "map_item":
            return ScopeDecision(
                allowed=False,
                scope="data_source",
                strategy="hitl",
                reasons=["循环项文本通常来自数据源"],
                options=[
                    _option("edit_data", "编辑数据源字段（AI）", "AI_DATA_EDIT"),
                    _option("visual_override", "仅视觉覆盖当前项", "VISUAL_ONLY"),
                    _option("cancel", "取消", "CANCEL"),
                ],
            )
        if identity.is_protected and not identity.has_call_site:
            return ScopeDecision(
                allowed=False,
                scope="definition",
                strategy="hitl",
                reasons=["公共组件文本修改会影响全部实例"],
                options=[
                    _option("detach_instance", "仅分离当前实例", "DETACH"),
                    _option(
                        "edit_definition", "修改组件定义（影响全部）", "DEFINITION"
                    ),
                    _option("cancel", "取消", "CANCEL"),
                ],
            )
        return ScopeDecision(True, identity.editable_scope, "direct")

    if action == "ADD_COMPONENT":
        if identity.in_map:
            return ScopeDecision(
                allowed=False,
                scope="data_source",
                strategy="hitl",
                reasons=["循环容器新增元素需要修改数据源"],
                options=[
                    _option("add_data_item", "在数据源新增一项（AI）", "AI_DATA_EDIT"),
                    _option("cancel", "取消", "CANCEL"),
                ],
            )
        if identity.is_shared or identity.is_protected:
            return ScopeDecision(
                allowed=False,
                scope="definition",
                strategy="hitl",
                reasons=["目标容器是公共组件，新增会影响全部实例"],
                options=[
                    _option("detach_instance", "仅分离当前实例", "DETACH"),
                    _option(
                        "edit_definition", "修改组件定义（影响全部）", "DEFINITION"
                    ),
                    _option("cancel", "取消", "CANCEL"),
                ],
            )
        return ScopeDecision(True, "instance", "direct")

    return ScopeDecision(
        allowed=False,
        scope="blocked",
        strategy="blocked",
        reasons=[f"不支持的动作: {action}"],
    )


__all__ = ["ScopeDecision", "decide_scope"]
