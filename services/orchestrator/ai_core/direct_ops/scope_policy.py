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


def _repeated_instances(identity: NodeIdentity) -> int:
    """运行时路径里任一 oid 的 count > 1，即同一模板渲染多次。"""
    counts = [
        int(item.get("count") or 1)
        for item in identity.instance_path
        if isinstance(item, dict)
    ]
    return max(counts) if counts else max(1, identity.instance_count)


def _template_hitl(
    identity: NodeIdentity,
    action_label: str,
    *,
    visual_option: bool = True,
) -> ScopeDecision:
    count = _repeated_instances(identity)
    options = [
        _option(
            "edit_template",
            f"{action_label}模板（影响全部 {count} 个实例）",
            "DEFINITION",
        )
    ]
    if visual_option:
        options.append(
            _option(
                "visual_override",
                "仅视觉覆盖当前实例",
                "VISUAL_ONLY",
            )
        )
    options.append(_option("cancel", "取消", "CANCEL"))
    return ScopeDecision(
        allowed=False,
        scope="all_instances" if count > 1 else "definition",
        strategy="hitl",
        reasons=[
            f"同一源码模板渲染 {count} 个实例；{action_label}会同时影响全部实例",
            f"instanceKey={identity.instance_key or '(unknown)'}",
        ],
        options=options,
    )


def decide_scope(action: str, identity: NodeIdentity) -> ScopeDecision:
    action = str(action or "").upper()

    if action == "DELETE_DATA_ITEM":
        map_source = identity.raw.get("mapSource") or {}
        kind = str(map_source.get("kind") or "")
        if kind in {"array_literal", "variable_array"}:
            return ScopeDecision(True, "data_source", "direct")
        return ScopeDecision(
            allowed=False,
            scope="data_source",
            strategy="hitl",
            reasons=["数据源不是静态数组，无法确定性删除单个元素"],
            options=[
                _option(
                    "ai_data_edit",
                    "交给 AI 定位并修改数据源",
                    "AI_DATA_EDIT",
                ),
                _option("cancel", "取消", "CANCEL"),
            ],
        )

    if action == "DELETE_NODE":
        if identity.kind == "route_root":
            return ScopeDecision(
                allowed=True,
                scope="replace_root",
                strategy="replace_placeholder",
                reasons=["页面根节点不能直接删除，将替换为空占位符"],
            )
        # 低级操作不转 Agent：父容器连同子树直接删除；
        # 共享/循环/多实例由前端高亮受影响范围。
        return ScopeDecision(True, "instance", "direct")

    if action == "MOVE_NODE":
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
        # 低级操作不转 Agent：同文件父容器连同子树直接移动。
        return ScopeDecision(True, "instance", "direct")

    if action == "UPDATE_TEXT":
        # 低级操作不转 Agent；共享/循环/多实例由前端高亮提示。
        return ScopeDecision(True, "instance", "direct")

    if action == "UPDATE_PROPS":
        if identity.is_call_site:
            # 低级操作不转 Agent；循环/多实例由前端高亮提示影响范围。
            return ScopeDecision(True, "instance", "direct")
        return ScopeDecision(
            allowed=False,
            scope="blocked",
            strategy="blocked",
            reasons=["没有解析到组件调用点，不能修改 Props"],
        )

    if action == "UPDATE_CLASS":
        # 低级操作不转 Agent；共享/循环影响由前端高亮提示。
        return ScopeDecision(True, "instance", "direct")

    if action == "UPDATE_STYLE":
        # 低级操作不转 Agent；共享/循环影响由前端高亮提示。
        return ScopeDecision(True, "instance", "direct")

    if action == "ADD_COMPONENT":
        # 低级操作不转 Agent；循环/共享影响由前端高亮提示。
        return ScopeDecision(True, "instance", "direct")

    return ScopeDecision(
        allowed=False,
        scope="blocked",
        strategy="blocked",
        reasons=[f"不支持的动作: {action}"],
    )


__all__ = ["ScopeDecision", "decide_scope"]
