"""NodeIdentity：SDK / Resolver / Executor 共享的节点身份协议。

DOM、JSX 与 React 数据源不是 1:1 对应，因此所有 Direct Ops 必须先回答
“这个节点到底是什么”，再决定操作范围：

  - leaf：普通叶子节点，可直接改
  - container：可容纳子节点的 JSX 容器
  - map_item：循环模板内的节点
  - component_root：组件定义返回值根节点
  - route_root：页面路由根节点
  - unknown：无法可靠归类

ScopePolicy 只依赖本协议，不依赖 Docker / Babel / 前端实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

NodeKind = Literal[
    "leaf",
    "container",
    "map_item",
    "component_root",
    "route_root",
    "unknown",
]

OperationScope = Literal[
    "instance",  # 当前 DOM/调用点实例
    "all_instances",  # 同一源码渲染出的全部实例
    "definition",  # 组件定义本身
    "data_source",  # 驱动列表的数据源
    "replace_root",  # 用占位符替换根节点
    "visual_only",  # 仅视觉覆盖
    "blocked",
]


@dataclass(frozen=True)
class NodeIdentity:
    file: str
    line: int
    column: int
    component: str = ""
    node_type: str = ""
    kind: NodeKind = "unknown"
    selector: str = ""
    instance: int = 1
    instance_count: int = 1
    call_site_file: str = ""
    importer_count: int = 0
    in_map: bool = False
    is_route_root: bool = False
    is_component_root: bool = False
    is_shared: bool = False
    is_protected: bool = False
    has_call_site: bool = False
    allowed_insert_positions: tuple[str, ...] = ()
    range: dict[str, Any] = field(default_factory=dict)
    call_site_anchor: dict[str, Any] | None = None
    # Resolver 原始数据，供 Executor 使用：allowedFiles / forbiddenRanges 等。
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def editable_scope(self) -> OperationScope:
        if self.in_map:
            return "data_source"
        if self.kind == "route_root":
            return "replace_root"
        if self.has_call_site:
            return "instance"
        if self.instance_count > 1 and self.importer_count == 0:
            return "all_instances"
        if self.is_component_root:
            return "definition"
        return "instance"

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "component": self.component,
            "nodeType": self.node_type,
            "kind": self.kind,
            "selector": self.selector,
            "instance": self.instance,
            "instanceCount": self.instance_count,
            "callSiteFile": self.call_site_file,
            "importerCount": self.importer_count,
            "inMap": self.in_map,
            "isRouteRoot": self.is_route_root,
            "isComponentRoot": self.is_component_root,
            "isShared": self.is_shared,
            "isProtected": self.is_protected,
            "hasCallSite": self.has_call_site,
            "allowedInsertPositions": list(self.allowed_insert_positions),
            "range": dict(self.range),
            "editableScope": self.editable_scope,
        }


def identity_from_anchor(anchor: dict[str, Any]) -> NodeIdentity:
    """把 Resolver 的 anchor 字典归一为 NodeIdentity。

    不要在这里猜测业务语义；只使用 Resolver 已经确认的 AST / 调用点事实。
    """
    call_site = (
        anchor.get("callSiteAnchor")
        if isinstance(anchor.get("callSiteAnchor"), dict)
        else None
    )
    has_call_site = bool(call_site and call_site.get("file"))
    instance_count = int(anchor.get("instanceCount") or 1)
    importer_count = int(anchor.get("importerCount") or 0)
    in_map = bool(anchor.get("inMap"))
    is_route_root = bool(anchor.get("isRouteRoot"))

    allowed_positions = tuple(
        str(item) for item in anchor.get("allowedInsertPositions") or []
    )
    node_type = str(anchor.get("nodeType") or "")
    if in_map:
        kind: NodeKind = "map_item"
    elif is_route_root and not has_call_site and importer_count == 0:
        kind = "route_root"
    elif is_route_root:
        kind = "component_root"
    elif node_type in {"JSXElement", "JSXFragment"} and any(
        item.startswith("inside_") for item in allowed_positions
    ):
        kind = "container"
    elif node_type in {"JSXElement", "JSXFragment"}:
        kind = "leaf"
    else:
        kind = "unknown"

    return NodeIdentity(
        file=str(anchor.get("file") or ""),
        line=int((anchor.get("range") or {}).get("startLine") or 0),
        column=int((anchor.get("range") or {}).get("startColumn") or 0),
        component=str(anchor.get("component") or ""),
        node_type=node_type,
        kind=kind,
        selector=str(anchor.get("selector") or ""),
        instance=int(anchor.get("instance") or 1),
        instance_count=instance_count,
        call_site_file=str(anchor.get("callSiteFile") or ""),
        importer_count=importer_count,
        in_map=in_map,
        is_route_root=is_route_root,
        is_component_root=is_route_root and not in_map,
        is_shared=bool(anchor.get("isShared")),
        is_protected=bool(anchor.get("protected")),
        has_call_site=has_call_site,
        allowed_insert_positions=allowed_positions,
        range=dict(anchor.get("range") or {}),
        call_site_anchor=call_site,
        raw=dict(anchor),
    )


__all__ = [
    "NodeIdentity",
    "NodeKind",
    "OperationScope",
    "identity_from_anchor",
]
