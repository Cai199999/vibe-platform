"""把契约的组件交互映射成 VisualDocument 的 interaction / binding。

这是“契约 → 代码”的通用桥：

- 契约只描述 ``component / event / apiRoute / logic``；
- VisualDocument 已有 ``interactions`` / ``bindings`` 与 CodeGen 运行时；
- 本模块按节点 ``name`` / 文案做确定性匹配，不依赖任何行业知识。

核心代码不认识任何行业；组件名、事件和路由都来自契约。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Iterable

from ai_core.domain.visual_document import (
    VisualBinding,
    VisualDocument,
    VisualInteraction,
    VisualNode,
)

logger = logging.getLogger(__name__)

# 优先匹配“真正承载交互”的节点类型，避免命中同名的标题文本节点。
_PREFERRED_TYPES = {
    "component",
    "form",
    "list",
    "table",
    "section",
    "button",
    "input",
    "modal",
}
_API_PREFIX = "/api"


def apply_contract_interactions(
    document: VisualDocument | dict[str, Any] | None,
    interactions: Iterable[Any] | None,
    *,
    source: str = "contract",
) -> VisualDocument | None:
    """返回带 interaction/binding 的文档副本；无匹配时原样返回。

    幂等：同一 ``nodeId + route + event`` 只追加一次。冻结哈希保持不变，
    因为这是 CodeGen 期的派生信息，不是用户对视觉树的修改。
    """
    if document is None or not interactions:
        return _coerce_document(document)
    doc = _coerce_document(document)
    if doc is None:
        return None
    nodes = _collect_nodes(doc)
    if not nodes:
        return doc

    existing_keys = {
        (binding.node_id, binding.endpoint_hint, binding.method)
        for binding in doc.bindings
    }
    bindings = list(doc.bindings)
    visual_interactions = list(doc.interactions)
    for raw in interactions:
        component = _field(raw, "component")
        event = (_field(raw, "event") or "click").strip().lower()
        route = _field(raw, "api_route") or _field(raw, "apiRoute")
        logic = _field(raw, "logic") or ""
        if not component or not route:
            continue
        node = _match_node(nodes, component)
        if node is None:
            logger.info(
                "contract interaction component=%s 未匹配到视觉节点，跳过",
                component,
            )
            continue
        action = _action_for(route, event)
        method = _method_for(event)
        key = (node.id, route, method)
        if key in existing_keys:
            continue
        existing_keys.add(key)
        digest = hashlib.sha1(
            f"{node.id}|{event}|{route}".encode("utf-8")
        ).hexdigest()[:12]
        binding_id = f"bind_{digest}"
        interaction_id = f"int_{digest}"
        bindings.append(
            VisualBinding(
                id=binding_id,
                node_id=node.id,
                kind="api",
                endpoint_hint=route[:500],
                method=method,
                status="CONFIRMED",
                source=source,
            )
        )
        visual_interactions.append(
            VisualInteraction(
                id=interaction_id,
                node_id=node.id,
                event=event[:40],
                action=action,
                intent=logic[:2000],
                binding_id=binding_id,
                status="CONFIRMED",
            )
        )
    return doc.model_copy(
        update={
            "bindings": bindings,
            "interactions": visual_interactions,
        }
    )


def _coerce_document(
    document: VisualDocument | dict[str, Any] | None,
) -> VisualDocument | None:
    if document is None:
        return None
    if isinstance(document, VisualDocument):
        return document
    if isinstance(document, dict):
        return VisualDocument.model_validate(document)
    return None


def _collect_nodes(document: VisualDocument) -> list[VisualNode]:
    nodes: list[VisualNode] = []

    def walk(node: VisualNode) -> None:
        nodes.append(node)
        for child in node.children:
            walk(child)

    if document.pages:
        for page in document.pages:
            walk(page.root)
    elif document.root is not None:
        walk(document.root)
    return nodes


def _match_node(nodes: list[VisualNode], component: str) -> VisualNode | None:
    target = component.strip()
    if not target:
        return None
    # 1) 精确 name + 交互型节点。
    for node in nodes:
        if node.name.strip() == target and node.type in _PREFERRED_TYPES:
            return node
    # 2) 精确 name（任意类型）。
    for node in nodes:
        if node.name.strip() == target:
            return node
    # 3) 文案包含组件名（例如节点名是“主内容区”，文案含“设施列表”）。
    for node in nodes:
        text = str(node.props.get("text") or node.props.get("label") or "")
        if target in text or target in node.name:
            return node
    return None


def _action_for(route: str, event: str) -> str:
    if not route.startswith(_API_PREFIX):
        return "navigate"
    if event in {"submit", "save"}:
        return "submit"
    return "call_api"


def _method_for(event: str) -> str:
    if event in {"submit", "save"}:
        return "POST"
    if event in {"delete", "remove"}:
        return "DELETE"
    return "GET"


def _field(value: Any, name: str) -> str:
    if isinstance(value, dict):
        raw = value.get(name)
    else:
        raw = getattr(value, name, None)
    return str(raw or "").strip()


__all__ = ["apply_contract_interactions"]
