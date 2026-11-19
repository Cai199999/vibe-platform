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
        # 循环模板：所有渲染实例共享同一段源码，直接 DELETE_NODE 会把
        # 全部项一起删掉。静态数组优先提供“只删当前数据项”；动态数据
        # 或整模板删除交给 AI，绝不静默全删。
        if identity.in_map:
            map_source = identity.raw.get("mapSource") or {}
            kind = str(map_source.get("kind") or "")
            if kind in {"array_literal", "variable_array"}:
                # execute / analyze_delete 会把这类删除自动转成
                # DELETE_DATA_ITEM；保留策略描述用于直接调用与审计。
                return ScopeDecision(
                    allowed=False,
                    scope="data_source",
                    strategy="hitl",
                    reasons=["将只删除当前数据项"],
                    options=[
                        _option(
                            "remove_data_item",
                            "只删除当前数据项",
                            "AI_DATA_EDIT",
                        ),
                        _option("cancel", "取消", "CANCEL"),
                    ],
                )
            # 动态数据无法确定“删除哪一个渲染项”，不弹安全路径，
            # 直接给出可执行的错误提示。
            return ScopeDecision(
                allowed=False,
                scope="repeated_template",
                strategy="blocked",
                reasons=[
                    "该组件位于动态循环模板，无法单独删除当前项；"
                    "请使用 AI 修改数据源"
                ],
            )
        # 同一源码被渲染多次但没有调用点 / map 信息：删除源节点会
        # 确定性移除全部实例，不弹安全路径，直接要求 AI 处理。
        if (
            _repeated_instances(identity) > 1
            and not identity.has_call_site
            and identity.importer_count == 0
        ):
            return ScopeDecision(
                allowed=False,
                scope="all_instances",
                strategy="blocked",
                reasons=[
                    f"同一源码渲染 {_repeated_instances(identity)} 个实例，"
                    "无法确定删除哪一个；请使用 AI 修改"
                ],
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
        # 循环模板内的实例共享同一段 JSX；移动模板无法表达“只移动当前
        # 项”，HMR 后会按数组重新渲染，看起来像成功后又回退。
        if identity.in_map:
            return ScopeDecision(
                allowed=False,
                scope="repeated_template",
                strategy="blocked",
                reasons=[
                    "循环模板中的项不能单独移动；移动会作用于全部实例，"
                    "请使用 AI 调整数据源顺序"
                ],
            )
        # 同一源码渲染多次且没有调用点：移动会改到所有实例。
        if (
            _repeated_instances(identity) > 1
            and not identity.has_call_site
            and identity.importer_count == 0
        ):
            return ScopeDecision(
                allowed=False,
                scope="all_instances",
                strategy="blocked",
                reasons=[
                    f"同一源码渲染 {_repeated_instances(identity)} 个实例，"
                    "无法单独移动；请使用 AI 修改"
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
