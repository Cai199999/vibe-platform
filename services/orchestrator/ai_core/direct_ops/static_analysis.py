"""DELETE_NODE 静态安全判定。

只分析、不删除。判定依据来自 Resolver 的 AST 结果：
  - 公共组件 / 多实例；
  - 位于 .map(...) 回调内部；
  - 页面 / 组件文件的最外层 JSX 根节点。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DeleteAnalysis:
    safe: bool
    reasons: list[str] = field(default_factory=list)
    is_shared: bool = False
    in_map: bool = False
    is_route_root: bool = False
    instance_count: int = 1
    repeated_only: bool = False
    anchor: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "safe": self.safe,
            "reasons": list(self.reasons),
            "isShared": self.is_shared,
            "inMap": self.in_map,
            "isRouteRoot": self.is_route_root,
            "instanceCount": self.instance_count,
            "repeatedOnly": self.repeated_only,
            "anchor": dict(self.anchor),
        }


def analyze_delete_anchor(anchor: dict[str, Any]) -> DeleteAnalysis:
    is_shared = bool(anchor.get("isShared") or anchor.get("protected"))
    in_map = bool(anchor.get("inMap"))
    is_route_root = bool(anchor.get("isRouteRoot"))
    call_site = anchor.get("callSiteAnchor")
    has_call_site = (
        isinstance(call_site, dict) and bool(call_site.get("file")) and not in_map
    )
    instance_count = int(anchor.get("instanceCount") or 1)
    importer_count = int(anchor.get("importerCount") or 0)
    # 仅因“同一源码渲染多次”而 shared，且不是 import 出来的定义：
    # 删除会确定性地移除全部实例，可以允许用户确认后执行。
    repeated_only = (
        is_shared
        and instance_count > 1
        and importer_count == 0
        and not has_call_site
        and not in_map
        and not is_route_root
    )
    reasons: list[str] = []
    if is_shared and not has_call_site and not repeated_only:
        reasons.append(str(anchor.get("sharedReason") or "组件被多处引用或渲染"))
    if in_map:
        reasons.append("组件位于 map 循环内，删除会影响多个实例")
    # callSite 存在时，删除的是页面中的实例 JSX；组件定义文件里的
    # “顶层 JSX 根节点”不应阻止删除。
    if is_route_root and not has_call_site and not repeated_only:
        reasons.append("组件是页面顶层 JSX 根节点")
    return DeleteAnalysis(
        safe=not reasons,
        reasons=reasons,
        is_shared=is_shared,
        in_map=in_map,
        is_route_root=is_route_root,
        instance_count=instance_count,
        repeated_only=repeated_only,
        anchor=anchor,
    )


__all__ = ["DeleteAnalysis", "analyze_delete_anchor"]
