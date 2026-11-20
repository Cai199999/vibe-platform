"""从 Architect 的 VisualDocument 提取“组件默认样式”。

目标：新增组件选择“和现有主按钮一致”时，不必再让用户点选真实 DOM；
架构师阶段产出的 VisualDocument 已经定义了按钮 / 输入框 / 卡片等节点的
真实 Tailwind 类。这里按节点语义抽取一组 representative default，随契约
下发，前端样式澄清可以直接把它当作 reference。

类名计算复用 CodeGen 的 ``_tailwind_classes``，保证“默认样式 = 最终显示
的样式”，不在前端另维护一份 Tailwind 默认表。
"""

from __future__ import annotations

import re
from typing import Any

from ai_core.domain.visual_document import VisualDocument, VisualNode

_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_TAG_BY_TYPE = {
    "button": "button",
    "input": "input",
    "text": "p",
    "card": "div",
    "table": "table",
    "image": "img",
    "list": "ul",
    "modal": "div",
    "page": "main",
}
_MAX_CLASSES = 80


def _iter_nodes(root: VisualNode | None):
    if root is None:
        return
    yield root
    for child in root.children or []:
        yield from _iter_nodes(child)


def _all_nodes(document: VisualDocument) -> list[VisualNode]:
    nodes: list[VisualNode] = []
    pages = getattr(document, "pages", None) or []
    if pages:
        for page in pages:
            nodes.extend(_iter_nodes(getattr(page, "root", None)))
    else:
        nodes.extend(_iter_nodes(getattr(document, "root", None)))
    return nodes


def _node_text(node: VisualNode) -> str:
    props = node.props or {}
    for key in ("text", "label", "title", "placeholder"):
        value = props.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return " ".join(
        part
        for part in (
            str(node.name or ""),
            str(node.semantics.role or "") if node.semantics else "",
            str(node.component_ref or ""),
        )
        if part
    )


def _classes_for(node: VisualNode) -> list[str]:
    try:
        from ai_core.codegen.visual import _tailwind_classes

        return list(_tailwind_classes(node))[:_MAX_CLASSES]
    except Exception:  # noqa: BLE001 - 单节点样式异常不能拖垮契约
        classes: list[str] = []
        for raw in node.style.tailwind or []:
            classes.extend(str(raw or "").split())
        return classes[:_MAX_CLASSES]


def _css_var(group: str, key: str) -> str:
    prefix = "color" if group in {"color", "colors"} else group
    name = _CAMEL_BOUNDARY.sub(r"\1-\2", str(key or "")).lower()
    name = re.sub(r"[^a-z0-9-]+", "-", name).strip("-")
    return f"var(--{prefix}-{name})"


def _styles_for(node: VisualNode) -> dict[str, str]:
    styles: dict[str, str] = {}
    for raw in node.style.token_refs or []:
        text = str(raw or "").strip()
        if "." not in text:
            continue
        group, key = text.split(".", 1)
        variable = _css_var(group, key)
        if group in {"color", "colors"}:
            if key in {"primary", "primarySoft", "surface", "canvas"}:
                styles.setdefault("backgroundColor", variable)
            elif key == "border":
                styles.setdefault("borderColor", variable)
            else:
                styles.setdefault("color", variable)
        elif group == "radius":
            styles.setdefault("borderRadius", variable)
        elif group == "typography":
            styles.setdefault("fontSize", variable)
    return styles


def _describe(node: VisualNode, label: str) -> dict[str, Any]:
    return {
        "label": label,
        "tag": _TAG_BY_TYPE.get(str(node.type), "div"),
        "classes": _classes_for(node),
        "styles": _styles_for(node),
        "source": None,
    }


def _pick(nodes: list[VisualNode], patterns: tuple[str, ...]) -> VisualNode | None:
    for node in nodes:
        text = _node_text(node).lower()
        if any(pattern in text for pattern in patterns):
            return node
    return None


def component_style_defaults(document: VisualDocument | None) -> dict[str, dict[str, Any]]:
    """返回 ``{primary_button / secondary_button / login_button / ...}``。"""
    if document is None:
        return {}
    nodes = _all_nodes(document)
    if not nodes:
        return {}
    by_type: dict[str, list[VisualNode]] = {}
    for node in nodes:
        by_type.setdefault(str(node.type), []).append(node)

    defaults: dict[str, dict[str, Any]] = {}
    buttons = by_type.get("button", [])
    primary = _pick(
        buttons, ("primary", "主按钮", "主色", "主要")
    ) or (buttons[0] if buttons else None)
    secondary = _pick(
        buttons,
        ("secondary", "次按钮", "次要", "描边", "幽灵", "ghost", "outline"),
    )
    if secondary is primary:
        secondary = None
    if secondary is None and len(buttons) > 1:
        secondary = buttons[1]
    login = _pick(buttons, ("登录", "login", "signin", "sign in"))

    if primary is not None:
        defaults["primary_button"] = _describe(primary, "主按钮")
        defaults["button"] = _describe(primary, "按钮")
    if secondary is not None:
        defaults["secondary_button"] = _describe(secondary, "次要按钮")
    if login is not None:
        defaults["login_button"] = _describe(login, "登录按钮")

    for node_type, key in (
        ("input", "input"),
        ("text", "text"),
        ("card", "card"),
        ("table", "table"),
        ("image", "image"),
        ("list", "list"),
    ):
        candidates = by_type.get(node_type) or []
        if candidates:
            defaults[key] = _describe(candidates[0], key)
    return defaults


__all__ = ["component_style_defaults"]
