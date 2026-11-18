"""VisualDocument → React 源码的确定性编译器。

设计边界（对应 docs/design/11_离线优先可视化引擎.md 第 7 节）：

  - 基础节点全部走硬编码 Compiler，不调用 LLM；
  - 每个节点注入稳定 ``data-node-id``，供生成后 Direct Ops 精确定位；
  - 只输出 JSX / CSS 文本，不启动沙盒、不写 Git；
  - 编译结果必须通过 ``visual_gate`` 的 Babel AST 门禁；
  - 编译门禁失败最多回退一次“扁平占位页”，绝不写不可编译代码；
  - 布局只允许 flex / grid / block，不产生任何绝对坐标。

该模块是纯函数层：输入 VisualDocument，输出文件字典与元数据。
真正的落盘、构建与重启仍由 FullstackGraph 负责。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

from ai_core.codegen.visual_gate import (
    VisualCodegenGateError,
    VisualCodegenGateUnavailable,
    verify_visual_source,
)
from ai_core.domain.visual_document import (
    CONTAINER_NODE_TYPES,
    VisualDocument,
    VisualNode,
    compute_content_hash,
    validate_visual_document,
)

DEFAULT_TOKENS: dict[str, dict[str, str]] = {
    "colors": {
        "primary": "#2563eb",
        "primarySoft": "#dbeafe",
        "surface": "#ffffff",
        "canvas": "#f1f5f9",
        "text": "#0f172a",
        "muted": "#64748b",
        "border": "#cbd5e1",
    },
    "spacing": {"xs": "4px", "sm": "8px", "md": "16px", "lg": "24px", "xl": "32px"},
    "radius": {"sm": "8px", "card": "12px", "pill": "999px"},
    "typography": {"body": "14px", "title": "22px", "caption": "12px"},
}

TAILWIND_SPACING = {
    "0": "0px",
    "0.5": "2px",
    "1": "4px",
    "1.5": "6px",
    "2": "8px",
    "2.5": "10px",
    "3": "12px",
    "3.5": "14px",
    "4": "16px",
    "5": "20px",
    "6": "24px",
    "7": "28px",
    "8": "32px",
    "9": "36px",
    "10": "40px",
    "11": "44px",
    "12": "48px",
    "14": "56px",
    "16": "64px",
}

TAILWIND_COLORS = {
    "white": "#ffffff",
    "black": "#000000",
    "transparent": "transparent",
    "slate-50": "#f8fafc",
    "slate-100": "#f1f5f9",
    "slate-200": "#e2e8f0",
    "slate-300": "#cbd5e1",
    "slate-400": "#94a3b8",
    "slate-500": "#64748b",
    "slate-600": "#475569",
    "slate-700": "#334155",
    "slate-800": "#1e293b",
    "slate-900": "#0f172a",
    "gray-50": "#f9fafb",
    "gray-100": "#f3f4f6",
    "gray-200": "#e5e7eb",
    "gray-300": "#d1d5db",
    "gray-500": "#6b7280",
    "gray-700": "#374151",
    "blue-50": "#eff6ff",
    "blue-100": "#dbeafe",
    "blue-500": "#3b82f6",
    "blue-600": "#2563eb",
    "blue-700": "#1d4ed8",
    "red-100": "#fee2e2",
    "red-500": "#ef4444",
    "red-600": "#dc2626",
    "green-100": "#dcfce7",
    "green-500": "#22c55e",
    "green-600": "#16a34a",
    "amber-100": "#fef3c7",
    "amber-500": "#f59e0b",
}

TAILWIND_FONT_SIZES = {
    "xs": "12px",
    "sm": "14px",
    "base": "16px",
    "lg": "18px",
    "xl": "20px",
    "2xl": "24px",
    "3xl": "30px",
    "4xl": "36px",
}

TAILWIND_RADII = {
    "rounded": "4px",
    "rounded-sm": "2px",
    "rounded-md": "6px",
    "rounded-lg": "8px",
    "rounded-xl": "12px",
    "rounded-2xl": "16px",
    "rounded-full": "999px",
}

FORBIDDEN_TAILWIND_PREFIXES = (
    "absolute",
    "fixed",
    "sticky",
    "inset-",
    "top-",
    "left-",
    "right-",
    "bottom-",
    "translate-",
)

TAG_MAP = {
    "page": "main",
    "section": "section",
    "container": "div",
    "grid": "div",
    "stack": "div",
    "text": "p",
    "button": "button",
    "input": "input",
    "image": "img",
    "list": "ul",
    "table": "table",
    "form": "form",
    "modal": "div",
    "component": "div",
}

VOID_TAGS = {"input", "img"}

SAFE_IMAGE_PREFIXES = ("http://", "https://", "/", "./", "../", "data:image/")

_INPUT_TYPES = frozenset(
    {"text", "email", "password", "number", "search", "tel", "url", "date"}
)
_VALID_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_IDENT_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_CSS_UNSAFE_RE = re.compile(r"[;{}<>\n\r]")
# Tailwind 任意值只接受尺寸 / 百分比 / CSS 变量等确定性字面量；
# 空格会在生成 class 时转成下划线（Tailwind 任意值语法）。
_TAILWIND_VALUE_RE = re.compile(r"^[A-Za-z0-9#%.,()/_\- ]+$")
_TAILWIND_CLASS_RE = re.compile(r"^[A-Za-z0-9:_\-\[\]().,%#/]+$")
_SOURCE_PLACEHOLDER = "__VISUAL_SOURCE_LINE__"
SOURCE_FILE = "src/App.jsx"
_INSPECT_SDK_FILE = "inspect_sdk.js"
_SOURCE_MAP_FILE = "vibe-source-map.json"


def _oid_for_node(node_id: str) -> str:
    """生成节点与构建期 oid 的稳定映射。

    Visual CodeGen 已经有全局唯一 ``data-node-id``；oid 只是把同一身份
    暴露给 source map / SDK，不另造第二套节点身份。
    """
    return f"oid_{node_id}"


@lru_cache(maxsize=1)
def _inspect_sdk_source() -> str:
    """读取沙盒模板里的只读选择 SDK。

    生成的 Vite 工程通过 ``public/inspect_sdk.js`` 自带这份 SDK，避免
    依赖 ``inspect_plugin.js`` 的 Babel / Vue 依赖，也保证导出包可独立运行。
    """
    candidates = (
        Path(__file__).resolve().parents[2] / "sandbox_template" / _INSPECT_SDK_FILE,
        Path("/app/sandbox_template") / _INSPECT_SDK_FILE,
        Path("/app") / _INSPECT_SDK_FILE,
    )
    for path in candidates:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            continue
    return ""


def _with_inspect_sdk(files: dict[str, str]) -> dict[str, str]:
    sdk = _inspect_sdk_source()
    if sdk:
        files["public/inspect_sdk.js"] = sdk
    return files


class VisualCodegenError(ValueError):
    """VisualDocument 无法编译为安全 React 源码。"""

    code = "VISUAL_CODEGEN_FAILED"
    status_code = 422

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


@dataclass(frozen=True)
class VisualCompileResult:
    files: dict[str, str]
    node_ids: tuple[str, ...]
    summary: str
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    repaired: bool = False


def _js_string(value: Any) -> str:
    """JSON 字符串即合法 JS 字符串；额外转义 JS 行分隔符。"""
    raw = json.dumps(str(value), ensure_ascii=False)
    return raw.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _js_object(style: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key, value in style.items():
        if value is None or value == "":
            continue
        name = key if _IDENT_RE.match(key) else _js_string(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            literal = str(value)
        else:
            literal = _js_string(value)
        parts.append(f"{name}: {literal}")
    return "{ " + ", ".join(parts) + " }"


def _iter_nodes(root: VisualNode | None) -> Iterable[VisualNode]:
    if root is None:
        return
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _collect_node_ids(root: VisualNode | None) -> tuple[str, ...]:
    return tuple(node.id for node in _iter_nodes(root))


def _document_pages(document: VisualDocument):
    if document.pages:
        return [
            (page.page_id, page.path, page.title, page.root) for page in document.pages
        ]
    if document.root is not None:
        return [("", "/", document.title, document.root)]
    return []


def _collect_document_node_ids(document: VisualDocument) -> tuple[str, ...]:
    return tuple(
        node.id
        for _page_id, _path, _title, root in _document_pages(document)
        for node in _iter_nodes(root)
    )


def _render_source_map(document: VisualDocument, app_source: str) -> str:
    """生成 ``public/vibe-source-map.json``。

    CodeGen 已知完整虚拟树，不需要 Babel 反推：oid、父容器与兄弟顺序
    直接来自 VisualNode。``nodeId`` 让 resolver 在生成工程里用
    ``[data-node-id="..."]`` 精确定位，不再依赖 file:line。
    """
    nodes: dict[str, dict[str, Any]] = {}
    pages = list(_document_pages(document))
    multi_page = len(pages) > 1

    def walk(
        node: VisualNode,
        parent_oid: str | None,
        sibling_index: int,
        sibling_count: int,
        component: str,
        depth: int,
    ) -> None:
        oid = _oid_for_node(node.id)
        marker = f'data-oid="{oid}"'
        offset = app_source.find(marker)
        line = app_source.count("\n", 0, offset) + 1 if offset >= 0 else 0
        nodes[oid] = {
            "oid": oid,
            "nodeId": node.id,
            "file": SOURCE_FILE,
            "line": line,
            "component": component,
            "tag": TAG_MAP[node.type],
            "isContainer": node.type in CONTAINER_NODE_TYPES,
            "parentOid": parent_oid,
            "siblingIndex": sibling_index,
            "siblingCount": sibling_count,
            "depth": depth,
        }
        for index, child in enumerate(node.children):
            walk(
                child,
                oid,
                index,
                len(node.children),
                component,
                depth + 1,
            )

    for page_index, (_page_id, _path, title, root) in enumerate(pages):
        component = _page_component_name(title, page_index) if multi_page else "App"
        walk(root, None, 0, 1, component, 0)

    source_hash = hashlib.sha256(app_source.encode("utf-8")).hexdigest()
    payload = {
        "version": 1,
        # revision 只由源码正文决定：contentHash 含 frozenAt 时间戳，
        # 用它会令同一份源码两次编译产生不同 manifest。
        "revision": source_hash[:16],
        "files": {
            SOURCE_FILE: {
                "hash": source_hash,
                "oids": sorted(nodes),
            }
        },
        "nodes": nodes,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _page_component_name(title: str, index: int) -> str:
    match = re.findall(r"[A-Za-z0-9]+", str(title or ""))
    name = "".join(part[:1].upper() + part[1:] for part in match[:3])
    if not name or not name[0].isalpha():
        name = f"Page{index + 1}"
    return f"Page{name}"


def _node_count(root: VisualNode | None) -> int:
    return sum(1 for _ in _iter_nodes(root))


def _safe_css_value(value: Any) -> str:
    text = str(value).strip()
    if not text or _CSS_UNSAFE_RE.search(text):
        return ""
    return text


def _css_var(group: str, key: str) -> str:
    # 设计令牌模型用复数分组（colors / spacing / radius / typography），
    # CSS 变量统一用单数语义前缀（--color-primary）。
    prefix = "color" if group == "colors" else group
    name = _CAMEL_BOUNDARY_RE.sub(r"\1-\2", str(key)).lower()
    name = re.sub(r"[^a-z0-9-]+", "-", name).strip("-")
    return f"--{prefix}-{name}"


def _merged_tokens(document: VisualDocument) -> dict[str, dict[str, str]]:
    merged: dict[str, dict[str, str]] = {
        group: dict(values) for group, values in DEFAULT_TOKENS.items()
    }
    tokens = document.design_tokens
    for group, values in (
        ("colors", tokens.colors),
        ("spacing", tokens.spacing),
        ("radius", tokens.radius),
        ("typography", tokens.typography),
    ):
        for key, value in (values or {}).items():
            safe = _safe_css_value(value)
            if safe:
                merged.setdefault(group, {})[str(key)] = safe
    return merged


def _render_css(document: VisualDocument) -> str:
    tokens = _merged_tokens(document)
    lines = [":root {"]
    for group in ("colors", "spacing", "radius", "typography"):
        for key, value in tokens.get(group, {}).items():
            lines.append(f"  {_css_var(group, key)}: {value};")
    lines.extend(
        [
            "}",
            "",
            "*,",
            "*::before,",
            "*::after {",
            "  box-sizing: border-box;",
            "}",
            "",
            "html,",
            "body,",
            "#root {",
            "  margin: 0;",
            "  min-height: 100%;",
            "}",
            "",
            "body {",
            "  font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;",
            "  background: var(--color-canvas);",
            "  color: var(--color-text);",
            "}",
            "",
            ".visual-node {",
            "  box-sizing: border-box;",
            "}",
            "",
            ".visual-button {",
            "  font: inherit;",
            "}",
            "",
            ".visual-input {",
            "  max-width: 100%;",
            "}",
            "",
            ".visual-repeat-item {",
            "  list-style: none;",
            "}",
            "",
            ".visual-table th,",
            ".visual-table td {",
            "  border: 1px solid var(--color-border);",
            "  padding: 8px 12px;",
            "  text-align: left;",
            "}",
            "",
            ".visual-page-nav {",
            "  display: flex;",
            "  gap: 8px;",
            "  flex-wrap: wrap;",
            "  padding: 10px 16px;",
            "  border-bottom: 1px solid var(--color-border);",
            "  background: var(--color-surface);",
            "}",
            "",
            ".visual-page-nav button {",
            "  padding: 6px 12px;",
            "  border: 1px solid var(--color-border);",
            "  border-radius: var(--radius-pill);",
            "  background: transparent;",
            "  color: var(--color-muted);",
            "  cursor: pointer;",
            "}",
            "",
            ".visual-page-nav button.is-active {",
            "  border-color: var(--color-primary);",
            "  background: var(--color-primary-soft);",
            "  color: var(--color-primary);",
            "}",
            "",
            ".visual-custom-placeholder {",
            "  border: 1px dashed var(--color-border);",
            "  border-radius: var(--radius-card);",
            "  padding: 12px;",
            "  color: var(--color-muted);",
            "  background: var(--color-canvas);",
            "}",
            "",
        ]
    )
    return "\n".join(lines)


def _default_style(node_type: str) -> dict[str, str]:
    if node_type == "page":
        return {
            "minHeight": "100vh",
            "width": "100%",
            "backgroundColor": "var(--color-surface)",
            "color": "var(--color-text)",
        }
    if node_type == "text":
        return {
            "margin": "0px",
            "color": "var(--color-text)",
            "fontSize": "var(--typography-body)",
            "lineHeight": "1.6",
        }
    if node_type == "button":
        return {
            "padding": "10px 16px",
            "border": "1px solid transparent",
            "borderRadius": "var(--radius-sm)",
            "backgroundColor": "var(--color-primary)",
            "color": "#ffffff",
            "fontSize": "var(--typography-body)",
            "fontWeight": "600",
            "cursor": "pointer",
        }
    if node_type == "input":
        return {
            "padding": "10px 12px",
            "border": "1px solid var(--color-border)",
            "borderRadius": "var(--radius-sm)",
            "backgroundColor": "var(--color-surface)",
            "color": "var(--color-text)",
            "fontSize": "var(--typography-body)",
            "width": "100%",
        }
    if node_type == "image":
        return {
            "display": "block",
            "width": "100%",
            "height": "auto",
            "borderRadius": "var(--radius-card)",
        }
    if node_type == "list":
        return {"listStyle": "none", "margin": "0px"}
    if node_type == "table":
        return {"width": "100%", "borderCollapse": "collapse"}
    if node_type == "modal":
        return {
            "padding": "20px",
            "border": "1px solid var(--color-border)",
            "borderRadius": "var(--radius-card)",
            "backgroundColor": "var(--color-surface)",
        }
    return {}


def _spacing_value(scale: str, *, allow_auto: bool = False) -> str | None:
    if scale == "auto" and allow_auto:
        return "auto"
    return TAILWIND_SPACING.get(scale)


def _apply_spacing(style: dict[str, str], token: str) -> bool:
    match = re.fullmatch(r"(m|p)([xytrbl])?-(\S+)", token)
    if not match:
        return False
    kind, axis, scale = match.groups()
    value = _spacing_value(scale, allow_auto=kind == "m")
    if value is None:
        return False
    prefix = "margin" if kind == "m" else "padding"
    if axis is None:
        style[prefix] = value
        return True
    axis_map = {
        "x": ("Left", "Right"),
        "y": ("Top", "Bottom"),
        "t": ("Top",),
        "r": ("Right",),
        "b": ("Bottom",),
        "l": ("Left",),
    }
    for side in axis_map.get(axis, ()):
        style[f"{prefix}{side}"] = value
    return True


def _apply_tailwind(style: dict[str, str], classes: Iterable[str]) -> list[str]:
    """把受支持的 Tailwind 子集翻译为内联样式，返回告警。"""
    warnings: list[str] = []
    for raw in classes:
        for token in str(raw or "").split():
            token = token.strip()
            if not token:
                continue
            probe = token[1:] if token.startswith("-") else token
            if any(
                probe == prefix or probe.startswith(prefix)
                for prefix in FORBIDDEN_TAILWIND_PREFIXES
            ):
                raise VisualCodegenError(
                    f"VisualNode 含绝对定位 Tailwind 类: {token}",
                    code="VISUAL_CODEGEN_FORBIDDEN_STYLE",
                )
            if token in {"flex", "grid", "block", "inline", "inline-flex", "hidden"}:
                style["display"] = token
                continue
            if token in {"flex-col", "flex-row"}:
                style["display"] = "flex"
                style["flexDirection"] = "column" if token == "flex-col" else "row"
                continue
            if token == "flex-wrap":
                style["flexWrap"] = "wrap"
                continue
            if token == "flex-1":
                style["flex"] = "1 1 0%"
                continue
            if token == "items-start":
                style["alignItems"] = "flex-start"
                continue
            if token == "items-center":
                style["alignItems"] = "center"
                continue
            if token == "items-end":
                style["alignItems"] = "flex-end"
                continue
            if token == "items-stretch":
                style["alignItems"] = "stretch"
                continue
            if token == "justify-start":
                style["justifyContent"] = "flex-start"
                continue
            if token == "justify-center":
                style["justifyContent"] = "center"
                continue
            if token == "justify-between":
                style["justifyContent"] = "space-between"
                continue
            if token == "justify-end":
                style["justifyContent"] = "flex-end"
                continue
            grid_cols = re.fullmatch(r"grid-cols-(\d{1,2})", token)
            if grid_cols:
                style["display"] = "grid"
                style["gridTemplateColumns"] = (
                    f"repeat({int(grid_cols.group(1))}, minmax(0, 1fr))"
                )
                continue
            if _apply_spacing(style, token):
                continue
            gap = re.fullmatch(r"gap-(\S+)", token)
            if gap:
                value = _spacing_value(gap.group(1))
                if value:
                    style["gap"] = value
                continue
            if token in TAILWIND_RADII:
                style["borderRadius"] = TAILWIND_RADII[token]
                continue
            if token == "border":
                style["borderWidth"] = "1px"
                style.setdefault("borderStyle", "solid")
                continue
            if token == "border-2":
                style["borderWidth"] = "2px"
                style.setdefault("borderStyle", "solid")
                continue
            if token in {"border-dashed", "border-dotted"}:
                style["borderStyle"] = token.removeprefix("border-")
                style.setdefault("borderWidth", "1px")
                continue
            if token.startswith("border-") and token[7:] in TAILWIND_COLORS:
                style["borderColor"] = TAILWIND_COLORS[token[7:]]
                continue
            if token.startswith("bg-") and token[3:] in TAILWIND_COLORS:
                style["backgroundColor"] = TAILWIND_COLORS[token[3:]]
                continue
            if token.startswith("text-") and token[5:] in TAILWIND_COLORS:
                style["color"] = TAILWIND_COLORS[token[5:]]
                continue
            if token.startswith("text-") and token[5:] in TAILWIND_FONT_SIZES:
                style["fontSize"] = TAILWIND_FONT_SIZES[token[5:]]
                continue
            if token in {"text-left", "text-center", "text-right"}:
                style["textAlign"] = token.removeprefix("text-")
                continue
            if token in {"font-medium", "font-semibold", "font-bold"}:
                style["fontWeight"] = {
                    "font-medium": "500",
                    "font-semibold": "600",
                    "font-bold": "700",
                }[token]
                continue
            if token == "w-full":
                style["width"] = "100%"
                continue
            if token in {"h-full", "min-h-screen"}:
                style["minHeight" if token == "min-h-screen" else "height"] = (
                    "100vh" if token == "min-h-screen" else "100%"
                )
                continue
            if token == "max-w-full":
                style["maxWidth"] = "100%"
                continue
            max_w = re.fullmatch(r"max-w-(\w+)", token)
            if max_w:
                style["maxWidth"] = {
                    "sm": "640px",
                    "md": "768px",
                    "lg": "1024px",
                    "xl": "1280px",
                    "2xl": "1536px",
                    "prose": "65ch",
                }.get(max_w.group(1), "")
                continue
            if token == "shadow":
                style["boxShadow"] = "0 1px 3px rgba(15, 23, 42, 0.12)"
                continue
            if token in {"shadow-sm", "shadow-md", "shadow-lg"}:
                style["boxShadow"] = {
                    "shadow-sm": "0 1px 2px rgba(15, 23, 42, 0.08)",
                    "shadow-md": "0 4px 12px rgba(15, 23, 42, 0.12)",
                    "shadow-lg": "0 12px 32px rgba(15, 23, 42, 0.18)",
                }[token]
                continue
            if token == "cursor-pointer":
                style["cursor"] = "pointer"
                continue
            if token == "overflow-hidden":
                style["overflow"] = "hidden"
                continue
            if token.startswith(("hover:", "focus:", "md:", "lg:", "sm:")):
                warnings.append(f"CodeGen 忽略响应式 / 伪类 Tailwind: {token}")
                continue
            # 未知工具类保持确定性忽略：不猜测、不拼接任意 CSS。
    return warnings


def _apply_tokens(style: dict[str, str], node: VisualNode) -> None:
    for ref in node.style.token_refs:
        name = str(ref or "").strip()
        if "." not in name:
            continue
        group, key = name.split(".", 1)
        variable = f"var({_css_var(group, key)})"
        if group == "color":
            if key in {"primary", "primarySoft", "surface", "canvas"}:
                style["backgroundColor"] = variable
            elif key == "border":
                style["borderColor"] = variable
            else:
                style["color"] = variable
        elif group == "radius":
            style["borderRadius"] = variable
        elif group == "typography":
            style["fontSize"] = variable


def _apply_layout(style: dict[str, str], node: VisualNode) -> None:
    layout = node.layout
    display = layout.display
    if display == "inline-flex":
        style["display"] = "inline-flex"
    else:
        style["display"] = display
    if display in {"flex", "inline-flex"}:
        style["flexDirection"] = "row" if layout.direction == "row" else "column"
        style["alignItems"] = {
            "start": "flex-start",
            "center": "center",
            "end": "flex-end",
            "stretch": "stretch",
        }[layout.align]
        style["justifyContent"] = {
            "start": "flex-start",
            "center": "center",
            "between": "space-between",
            "end": "flex-end",
        }[layout.justify]
        style["flexWrap"] = "wrap" if layout.wrap else "nowrap"
    if display == "grid":
        style["gridTemplateColumns"] = f"repeat({layout.columns}, minmax(0, 1fr))"
    if layout.gap:
        style["gap"] = layout.gap
    if layout.padding:
        style["padding"] = layout.padding
    if layout.min_width:
        style["minWidth"] = layout.min_width
    if layout.max_width:
        style["maxWidth"] = layout.max_width
        style.setdefault("margin", "0px auto")


def _build_style(node: VisualNode) -> tuple[dict[str, str], list[str]]:
    style = dict(_default_style(node.type))
    warnings = _apply_tailwind(style, node.style.tailwind)
    _apply_tokens(style, node)
    _apply_layout(style, node)
    # 剔除空值，保持生成结果稳定。
    return {
        key: value for key, value in style.items() if value not in {"", None}
    }, warnings


def _safe_image_src(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not text.lower().startswith(SAFE_IMAGE_PREFIXES):
        raise VisualCodegenError(
            f"图片地址协议不安全: {text[:80]}",
            code="VISUAL_CODEGEN_UNSAFE_IMAGE",
        )
    if re.search(r"[\s\"'<>]", text):
        raise VisualCodegenError(
            "图片地址包含非法字符",
            code="VISUAL_CODEGEN_UNSAFE_IMAGE",
        )
    return text


def _columns(node: VisualNode) -> list[str]:
    raw = node.props.get("columns") or node.props.get("headers") or []
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item).strip()][:12]


def _interaction_index(document: VisualDocument) -> dict[str, dict[str, Any]]:
    binding_by_id = {binding.id: binding for binding in document.bindings}
    result: dict[str, dict[str, Any]] = {}
    for interaction in document.interactions:
        if interaction.node_id in result:
            continue
        binding = binding_by_id.get(interaction.binding_id)
        endpoint = (binding.endpoint_hint if binding else "") or ""
        method = (binding.method if binding else "") or ""
        if endpoint and not endpoint.startswith("/"):
            endpoint = ""
        method = method.upper() if method.upper() in _VALID_METHODS else ""
        result[interaction.node_id] = {
            "id": interaction.id,
            "action": interaction.action,
            "endpoint": endpoint,
            "method": method,
            "targetPageId": interaction.target_page_id or "",
        }
    return result


def _default_tailwind(node_type: str) -> list[str]:
    """基础节点默认 Tailwind 类；用户 / 画布样式会覆盖同名语义。"""
    if node_type == "page":
        return [
            "min-h-screen",
            "w-full",
            "bg-[var(--color-surface)]",
            "text-[var(--color-text)]",
        ]
    if node_type == "text":
        return [
            "m-0",
            "text-[var(--typography-body)]",
            "leading-relaxed",
            "text-[var(--color-text)]",
        ]
    if node_type == "button":
        return [
            "inline-flex",
            "items-center",
            "justify-center",
            "rounded-[var(--radius-sm)]",
            "border",
            "border-transparent",
            "px-4",
            "py-2",
            "text-[var(--typography-body)]",
            "font-semibold",
            "cursor-pointer",
        ]
    if node_type == "input":
        return [
            "w-full",
            "rounded-[var(--radius-sm)]",
            "border",
            "border-[var(--color-border)]",
            "bg-[var(--color-surface)]",
            "px-3",
            "py-2",
            "text-[var(--typography-body)]",
            "text-[var(--color-text)]",
        ]
    if node_type == "image":
        return ["block", "w-full", "h-auto", "rounded-[var(--radius-card)]"]
    if node_type == "list":
        return ["m-0", "list-none", "p-0"]
    if node_type == "table":
        return ["w-full", "border-collapse"]
    if node_type == "modal":
        return [
            "rounded-[var(--radius-card)]",
            "border",
            "border-[var(--color-border)]",
            "bg-[var(--color-surface)]",
            "p-5",
        ]
    return []


def _safe_tailwind_value(value: Any) -> str:
    """把布局字段转成 Tailwind 任意值；不安全则返回空串。"""
    text = str(value or "").strip()
    if not text or not _TAILWIND_VALUE_RE.fullmatch(text):
        return ""
    return text.replace(" ", "_")


def _arbitrary(prefix: str, value: Any) -> str:
    safe = _safe_tailwind_value(value)
    return f"{prefix}-[{safe}]" if safe else ""


def _tailwind_classes(node: VisualNode) -> list[str]:
    """生成确定性的 Tailwind class 列表（去重、保持顺序）。"""
    classes: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        if not value or value in seen:
            return
        classes.append(value)
        seen.add(value)

    for item in _default_tailwind(node.type):
        add(item)

    user_classes: list[str] = []
    for raw in node.style.tailwind:
        for token in str(raw or "").split():
            token = token.strip()
            if not token:
                continue
            probe = token[1:] if token.startswith("-") else token
            if any(
                probe == prefix or probe.startswith(prefix)
                for prefix in FORBIDDEN_TAILWIND_PREFIXES
            ):
                raise VisualCodegenError(
                    f"VisualNode 含绝对定位 Tailwind 类: {token}",
                    code="VISUAL_CODEGEN_FORBIDDEN_STYLE",
                )
            if not _TAILWIND_CLASS_RE.fullmatch(token):
                raise VisualCodegenError(
                    f"Tailwind 类包含非法字符: {token}",
                    code="VISUAL_CODEGEN_FORBIDDEN_STYLE",
                )
            user_classes.append(token)

    # 如果用户显式给了背景 / 文字色 / 圆角，不叠加默认值，避免同属性
    # 两个工具类互相覆盖且结果依赖 Tailwind 内部 CSS 顺序。
    has_user_bg = any(item.startswith("bg-") for item in user_classes)
    has_user_text_color = any(
        item.startswith("text-") and item[5:] in TAILWIND_COLORS
        for item in user_classes
    )
    has_user_radius = any(item.startswith("rounded") for item in user_classes)

    if node.type == "button":
        if not has_user_bg:
            add("bg-[var(--color-primary)]")
        if not has_user_text_color:
            add("text-white")
        if has_user_radius:
            classes[:] = [
                item for item in classes if item != "rounded-[var(--radius-sm)]"
            ]
            seen.discard("rounded-[var(--radius-sm)]")
    for item in user_classes:
        add(item)

    for ref in node.style.token_refs:
        name = str(ref or "").strip()
        if "." not in name:
            continue
        group, key = name.split(".", 1)
        variable = f"var({_css_var(group, key)})"
        if group == "color":
            if key in {"primary", "primarySoft", "surface", "canvas"}:
                add(f"bg-[{variable}]")
            elif key == "border":
                add(f"border-[{variable}]")
            else:
                add(f"text-[{variable}]")
        elif group == "radius":
            add(f"rounded-[{variable}]")
        elif group == "typography":
            add(f"text-[{variable}]")

    layout = node.layout
    display = layout.display
    add("inline-flex" if display == "inline-flex" else display)
    if display in {"flex", "inline-flex"}:
        add("flex-row" if layout.direction == "row" else "flex-col")
        add(
            {
                "start": "items-start",
                "center": "items-center",
                "end": "items-end",
                "stretch": "items-stretch",
            }[layout.align]
        )
        add(
            {
                "start": "justify-start",
                "center": "justify-center",
                "between": "justify-between",
                "end": "justify-end",
            }[layout.justify]
        )
        add("flex-wrap" if layout.wrap else "flex-nowrap")
    if display == "grid":
        add(f"grid-cols-{max(1, min(12, int(layout.columns)))}")
    for prefix, value in (
        ("gap", layout.gap),
        ("p", layout.padding),
        ("min-w", layout.min_width),
        ("max-w", layout.max_width),
    ):
        add(_arbitrary(prefix, value))
    if layout.max_width:
        add("mx-auto")
    if node.codegen.mode == "custom":
        add("border")
        add("border-dashed")
        add("border-[var(--color-border)]")
        add("rounded-[var(--radius-card)]")
        add("p-3")
        add("text-[var(--color-muted)]")
    return classes


def _render_action_runtime(actions: list[dict[str, Any]]) -> list[str]:
    if not actions:
        return []
    return [
        f"const VISUAL_ACTIONS = {json.dumps(actions, ensure_ascii=False)};",
        "",
        "async function runVisualAction(actionId) {",
        "  const action = VISUAL_ACTIONS.find((item) => item.id === actionId);",
        "  if (!action) return;",
        "  if (action.action === 'navigate' && action.endpoint) {",
        "    window.location.assign(action.endpoint);",
        "    return;",
        "  }",
        "  if (action.endpoint) {",
        "    try {",
        "      const response = await fetch(action.endpoint, {",
        "        method: action.method || 'POST',",
        "        headers: { 'Content-Type': 'application/json' },",
        "        body: action.method === 'GET' ? undefined : JSON.stringify({}),",
        "      });",
        "      if (!response.ok) {",
        "        console.error('[visual-action] failed', action.id, response.status);",
        "      }",
        "    } catch (error) {",
        "      console.error('[visual-action] request failed', action.id, error);",
        "    }",
        "    return;",
        "  }",
        "  console.info('[visual-action]', action.id, action.action);",
        "}",
        "",
    ]


def _slug(value: str, fallback: str = "vibe-visual-app") -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", str(value).lower()).strip("-")
    return slug[:60] or fallback


def _render_package_json(document: VisualDocument) -> str:
    package = {
        "name": _slug(document.doc_id or document.title),
        "private": True,
        "version": "0.0.0",
        "type": "module",
        "scripts": {
            "dev": "vite --host 0.0.0.0",
            "build": "vite build",
            "preview": "vite preview --host 0.0.0.0",
        },
        "dependencies": {
            "react": "18.3.1",
            "react-dom": "18.3.1",
        },
        "devDependencies": {
            "@vitejs/plugin-react": "4.3.4",
            "vite": "5.4.21",
        },
    }
    return json.dumps(package, ensure_ascii=False, indent=2) + "\n"


def _render_vite_config() -> str:
    return (
        "import { defineConfig } from 'vite'\n"
        "import react from '@vitejs/plugin-react'\n"
        "\n"
        "export default defineConfig({\n"
        "  plugins: [react()],\n"
        "  server: {\n"
        "    host: '0.0.0.0',\n"
        "    port: 5173,\n"
        "    strictPort: true,\n"
        "    // 同源 /api 请求转发到沙盒内 Node 后端；纯前端项目没有后端时\n"
        "    // 不会主动请求 /api，代理配置本身无副作用。\n"
        "    proxy: {\n"
        "      '/api': {\n"
        "        target: process.env.FULLSTACK_API_URL || 'http://127.0.0.1:3000',\n"
        "        changeOrigin: true,\n"
        "      },\n"
        "    },\n"
        "  },\n"
        "})\n"
    )


def _render_index_html(document: VisualDocument) -> str:
    title = str(document.title or "Vibe Visual App")
    safe_title = (
        title.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN">\n'
        "  <head>\n"
        '    <meta charset="UTF-8" />\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
        f"    <title>{safe_title}</title>\n"
        '    <link rel="icon" href="data:," />\n'
        '    <script src="https://cdn.tailwindcss.com"></script>\n'
        "  </head>\n"
        "  <body>\n"
        '    <div id="root"></div>\n'
        '    <script type="module" src="/src/main.jsx"></script>\n'
        + (
            '    <script type="module" src="/inspect_sdk.js"></script>\n'
            if _inspect_sdk_source()
            else ""
        )
        + "  </body>\n"
        "</html>\n"
    )


def _render_main_jsx() -> str:
    return (
        "import React from 'react'\n"
        "import ReactDOM from 'react-dom/client'\n"
        "import App from './App.jsx'\n"
        "import './visual.css'\n"
        "\n"
        "ReactDOM.createRoot(document.getElementById('root')).render(\n"
        "  <React.StrictMode>\n"
        "    <App />\n"
        "  </React.StrictMode>,\n"
        ")\n"
    )


def _fill_source_lines(source: str) -> str:
    """把每个节点开标签里的占位符替换为它在 App.jsx 的真实行号。"""
    lines = source.split("\n")
    for index, line in enumerate(lines, 1):
        if _SOURCE_PLACEHOLDER in line:
            lines[index - 1] = line.replace(_SOURCE_PLACEHOLDER, str(index))
    return "\n".join(lines)


class _ReactCompiler:
    def __init__(
        self, document: VisualDocument, *, include_interactions: bool = False
    ) -> None:
        self.document = document
        self.include_interactions = include_interactions
        self.actions = _interaction_index(document) if include_interactions else {}
        self.multi_page = len(document.pages) > 1
        self.warnings: list[str] = []
        self.custom_node_ids: list[str] = []
        self.repeat_node_ids: list[str] = []

    # ---------------- 属性 / 样式 ----------------
    def _class_name(self, node: VisualNode) -> str:
        parts = ["visual-node", f"visual-{node.type}"]
        if node.codegen.mode == "custom":
            parts.append("visual-custom-placeholder")
        if node.style.class_name:
            parts.append(node.style.class_name)
        parts.extend(_tailwind_classes(node))
        return " ".join(parts)

    def _attrs(
        self,
        node: VisualNode,
        *,
        extra: Iterable[str] = (),
    ) -> str:
        source_kind = "container" if node.type in CONTAINER_NODE_TYPES else "element"
        attrs = [
            f"className={{{_js_string(self._class_name(node))}}}",
            f'data-oid="{_oid_for_node(node.id)}"',
            f'data-node-id="{node.id}"',
            f'data-source="{SOURCE_FILE}:{_SOURCE_PLACEHOLDER}"',
            f'data-source-kind="{source_kind}"',
            f'data-visual-type="{node.type}"',
        ]
        if not node.state.default_visible:
            attrs.append("hidden")
        if node.state.disabled:
            attrs.append("disabled={true}")
        if node.a11y.label and node.type in {"button", "input", "image", "modal"}:
            attrs.append(f"aria-label={{{_js_string(node.a11y.label)}}}")
        if self.include_interactions:
            action = self.actions.get(node.id)
            if action:
                if (
                    self.multi_page
                    and action.get("action") == "navigate"
                    and action.get("targetPageId")
                ):
                    attrs.append(
                        "onClick={() => onNavigate("
                        + _js_string(action["targetPageId"])
                        + ")}"
                    )
                else:
                    attrs.append(
                        f"onClick={{() => runVisualAction({_js_string(action['id'])})}}"
                    )
        attrs.extend(extra)
        return " ".join(attrs)

    # ---------------- 节点渲染 ----------------
    def _render_children(self, children: list[VisualNode], indent: int) -> list[str]:
        lines: list[str] = []
        for child in children:
            lines.extend(self._render_node(child, indent))
        return lines

    def _render_repeat_items(
        self,
        node: VisualNode,
        indent: int,
        *,
        item_tag: str,
    ) -> list[str]:
        count = max(1, int(node.repeat.sample_count if node.repeat else 0))
        if node.repeat and node.repeat.sample_count == 0:
            self.warnings.append(
                f"节点 {node.id} repeat.sampleCount=0，CodeGen 以 1 个模板样本渲染"
            )
        pad = " " * indent
        inner = " " * (indent + 2)
        lines = [
            f"{pad}{{Array.from({{ length: {count} }}).map((_, index) => (",
            f'{inner}<{item_tag} key={{String(index)}} className="visual-repeat-item">',
        ]
        lines.extend(self._render_children(node.children, indent + 4))
        lines.extend(
            [
                f"{inner}</{item_tag}>",
                f"{pad}))}}",
            ]
        )
        return lines

    def _render_table(
        self, node: VisualNode, tag: str, attrs: str, indent: int
    ) -> list[str]:
        columns = _columns(node)
        pad = " " * indent
        inner = " " * (indent + 2)
        lines = [f"{pad}<{tag} {attrs}>"]
        if columns:
            lines.append(f"{inner}<thead>")
            lines.append(f"{inner}  <tr>")
            for column in columns:
                lines.append(f"{inner}    <th>{{{_js_string(column)}}}</th>")
            lines.extend([f"{inner}  </tr>", f"{inner}</thead>"])
        lines.append(f"{inner}<tbody>")
        if node.children:
            lines.extend(self._render_repeat_items(node, indent + 2, item_tag="tr"))
        elif columns:
            lines.append(
                f"{inner}  {{Array.from({{ length: "
                f"{max(1, int(node.repeat.sample_count if node.repeat else 1))} "
                "}).map((_, rowIndex) => ("
            )
            lines.append(f"{inner}    <tr key={{String(rowIndex)}}>")
            for column_index in range(len(columns)):
                lines.append(f"{inner}      <td key={{String({column_index})}} />")
            lines.extend([f"{inner}    </tr>", f"{inner}  ))}}"])
        else:
            lines.append(f"{inner}  <tr><td /></tr>")
        lines.append(f"{inner}</tbody>")
        lines.append(f"{pad}</{tag}>")
        return lines

    def _render_node(self, node: VisualNode, indent: int) -> list[str]:
        tag = TAG_MAP[node.type]
        pad = " " * indent

        if node.codegen.mode == "custom":
            self.custom_node_ids.append(node.id)
            self.warnings.append(
                f"节点 {node.id} 为 custom，CodeGen 生成安全占位组件（不调用 LLM）"
            )
            attrs = self._attrs(node)
            label = node.name or "自定义组件"
            return [
                f"{pad}<div {attrs}>",
                f"{pad}  {{{_js_string(label)}}}",
                f"{pad}</div>",
            ]

        if node.type == "input":
            input_type = str(node.props.get("type") or "text").lower()
            if input_type not in _INPUT_TYPES:
                input_type = "text"
            placeholder = str(node.props.get("placeholder") or "")
            value = node.props.get("defaultValue", node.props.get("value", ""))
            extra = [
                f"type={_js_string(input_type)}",
                f"placeholder={{{_js_string(placeholder)}}}",
            ]
            if value not in (None, ""):
                extra.append(f"defaultValue={{{_js_string(value)}}}")
            if node.props.get("name"):
                extra.append(f"name={{{_js_string(node.props.get('name'))}}}")
            return [f"{pad}<input {self._attrs(node, extra=extra)} />"]

        if node.type == "image":
            src = _safe_image_src(node.props.get("src") or node.props.get("url") or "")
            alt = str(node.props.get("alt") or node.a11y.label or node.name or "")
            extra = [
                f"src={{{_js_string(src)}}}",
                f"alt={{{_js_string(alt)}}}",
                'loading="lazy"',
            ]
            return [f"{pad}<img {self._attrs(node, extra=extra)} />"]

        attrs = self._attrs(node)

        if node.type == "form":
            attrs += " onSubmit={(event) => event.preventDefault()}"
        if node.type == "modal":
            attrs += ' role="dialog" aria-modal="true"'

        if node.type in {"text", "button"}:
            text = node.props.get("text") or node.props.get("label") or node.name or ""
            if node.type == "button":
                text = text or "按钮"
            return [
                f"{pad}<{tag} {attrs}>",
                f"{pad}  {{{_js_string(text)}}}",
                f"{pad}</{tag}>",
            ]

        if node.type == "table":
            return self._render_table(node, tag, attrs, indent)

        if node.type == "list" and node.repeat:
            self.repeat_node_ids.append(node.id)
            lines = [f"{pad}<{tag} {attrs}>"]
            lines.extend(self._render_repeat_items(node, indent + 2, item_tag="li"))
            lines.append(f"{pad}</{tag}>")
            return lines

        if node.children:
            lines = [f"{pad}<{tag} {attrs}>"]
            lines.extend(self._render_children(node.children, indent + 2))
            lines.append(f"{pad}</{tag}>")
            return lines

        if tag in VOID_TAGS:
            return [f"{pad}<{tag} {attrs} />"]
        return [f"{pad}<{tag} {attrs} />"]

    # ---------------- 入口 ----------------
    def compile(self) -> VisualCompileResult:
        pages = _document_pages(self.document)
        lines: list[str] = []
        if self.multi_page:
            lines.append('import { useState } from "react"')
        lines.append('import "./visual.css"')
        lines.append("")
        lines.extend(_render_action_runtime(list(self.actions.values())))

        if self.multi_page:
            component_names: list[str] = []
            used_names: set[str] = set()
            for index, (_page_id, _path, title, root) in enumerate(pages):
                name = _page_component_name(title, index)
                if name in used_names:
                    name = f"{name}{index + 1}"
                used_names.add(name)
                component_names.append(name)
                lines.extend(
                    [
                        f"function {name}({{ onNavigate }}) {{",
                        "  return (",
                    ]
                )
                lines.extend(self._render_node(root, 4))
                lines.extend(["  )", "}", ""])
            lines.append("const VISUAL_PAGES = [")
            for index, (page_id, path, title, _root) in enumerate(pages):
                lines.append(
                    "  { id: "
                    + _js_string(page_id or f"page_{index + 1}")
                    + ", path: "
                    + _js_string(path or "/")
                    + ", title: "
                    + _js_string(title or f"页面 {index + 1}")
                    + f", Component: {component_names[index]} }},"
                )
            lines.extend(
                [
                    "];",
                    "",
                    "export default function App() {",
                    "  const [activePageId, setActivePageId] = useState(",
                    "    VISUAL_PAGES[0]?.id || '',",
                    "  );",
                    "  const activePage =",
                    "    VISUAL_PAGES.find((page) => page.id === activePageId) ||",
                    "    VISUAL_PAGES[0];",
                    "  if (!activePage) return null;",
                    "  const ActivePage = activePage.Component;",
                    "  return (",
                    "    <>",
                    '      <nav className="visual-page-nav">',
                    "        {VISUAL_PAGES.map((page) => (",
                    "          <button",
                    "            key={page.id}",
                    '            type="button"',
                    "            className={page.id === activePageId ? 'is-active' : ''}",
                    "            onClick={() => setActivePageId(page.id)}",
                    "          >",
                    "            {page.title}",
                    "          </button>",
                    "        ))}",
                    "      </nav>",
                    "      <ActivePage onNavigate={setActivePageId} />",
                    "    </>",
                    "  );",
                    "}",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    "export default function App() {",
                    "  return (",
                ]
            )
            root = pages[0][3] if pages else None
            if root is not None:
                lines.extend(self._render_node(root, 4))
            else:
                lines.append(
                    '    <main className="visual-node visual-page min-h-screen '
                    'w-full bg-[var(--color-surface)]" />'
                )
            lines.extend(["  )", "}", ""])

        app_source = _fill_source_lines("\n".join(lines))
        node_ids = _collect_document_node_ids(self.document)
        files = _with_inspect_sdk(
            {
                "src/App.jsx": app_source,
                "src/visual.css": _render_css(self.document),
                "src/main.jsx": _render_main_jsx(),
                "index.html": _render_index_html(self.document),
                "package.json": _render_package_json(self.document),
                "vite.config.js": _render_vite_config(),
                "public/vibe-source-map.json": _render_source_map(
                    self.document, app_source
                ),
            }
        )
        return VisualCompileResult(
            files=files,
            node_ids=node_ids,
            summary=(
                "VisualDocument 确定性编译："
                f"{len(pages)} 个页面、{len(node_ids)} 个静态节点、"
                f"{len(self.actions)} 个交互、完整 Vite 工程"
            ),
            warnings=tuple(dict.fromkeys(self.warnings)),
            metadata={
                "codegen": "visual-deterministic",
                "staticOnly": not self.include_interactions,
                "pageCount": len(pages),
                "nodeCount": len(node_ids),
                "customNodeCount": len(self.custom_node_ids),
                "repeatNodeCount": len(self.repeat_node_ids),
                "interactionCount": len(self.actions),
                "contentHash": self.document.metadata.content_hash,
            },
        )


def _render_fallback_css(document: VisualDocument) -> str:
    return _render_css(document)


def _fallback_result(
    document: VisualDocument,
    *,
    reason: str,
) -> VisualCompileResult:
    """构建失败 / 门禁失败时的最后一次安全产物：扁平占位页。"""
    node_ids = _collect_document_node_ids(document)
    lines = [
        'import "./visual.css"',
        "",
        "export default function App() {",
        "  return (",
        '    <main className="visual-node visual-page min-h-screen '
        'bg-[var(--color-surface)] p-6">',
        '      <h1 className="m-0 mb-4 text-[var(--typography-title)]">'
        "页面已降级为安全占位</h1>",
    ]
    for _page_id, _path, _title, root in _document_pages(document):
        for node in _iter_nodes(root):
            label = node.name or node.type
            lines.append(
                f'      <div data-oid="{_oid_for_node(node.id)}" '
                f'data-node-id="{node.id}" '
                f'data-source="{SOURCE_FILE}:{_SOURCE_PLACEHOLDER}" '
                f'data-visual-type="{node.type}" '
                'className="my-1.5 rounded-[var(--radius-sm)] border '
                'border-[var(--color-border)] px-3 py-2">'
                f"{{{_js_string(f'{label} ({node.type})')}}}</div>"
            )
    lines.extend(["    </main>", "  )", "}", ""])
    fallback_source = _fill_source_lines("\n".join(lines))
    return VisualCompileResult(
        files=_with_inspect_sdk(
            {
                "src/App.jsx": fallback_source,
                "src/visual.css": _render_fallback_css(document),
                "src/main.jsx": _render_main_jsx(),
                "index.html": _render_index_html(document),
                "package.json": _render_package_json(document),
                "vite.config.js": _render_vite_config(),
                "public/vibe-source-map.json": _render_source_map(
                    document, fallback_source
                ),
            }
        ),
        node_ids=node_ids,
        summary=f"CodeGen 安全占位页（{reason}）：{len(node_ids)} 个节点",
        warnings=tuple([f"CodeGen 已降级为安全占位页：{reason}"]),
        metadata={
            "codegen": "visual-fallback",
            "fallback": True,
            "nodeCount": len(node_ids),
            "contentHash": document.metadata.content_hash,
        },
    )


def _coerce_document(document: VisualDocument | Mapping[str, Any]) -> VisualDocument:
    if isinstance(document, VisualDocument):
        return document
    try:
        return VisualDocument.model_validate(document)
    except Exception as exc:  # noqa: BLE001 - Pydantic 异常统一转领域错误
        raise VisualCodegenError(
            f"VisualDocument Schema 不合法: {exc}",
            code="VISUAL_CODEGEN_INVALID_DOCUMENT",
        ) from exc


def _unbound_repeat_warnings(document: VisualDocument) -> list[str]:
    """未绑定 repeat 不再阻断静态 CodeGen，但必须显式告警。

    CodeGen 使用 VisualRepeat.sampleCount 渲染示例数据；真实业务数据仍需
    通过业务审查 / Direct Ops 绑定。冻结阶段允许进入业务审查。
    """
    warnings: list[str] = []
    for _page_id, _path, _title, root in _document_pages(document):
        for node in _iter_nodes(root):
            if not node.repeat:
                continue
            source = node.repeat.source
            if not any(
                binding.id == source or binding.entity == source
                for binding in document.bindings
            ):
                warnings.append(
                    f"节点 {node.id} 的 repeat.source={source} 未绑定；"
                    "CodeGen 将使用示例数据渲染，业务数据需后续绑定"
                )
    return warnings


def _with_extra_warnings(
    result: VisualCompileResult, warnings: list[str]
) -> VisualCompileResult:
    if not warnings:
        return result
    return VisualCompileResult(
        files=result.files,
        node_ids=result.node_ids,
        summary=result.summary,
        warnings=tuple(dict.fromkeys([*result.warnings, *warnings])),
        metadata=result.metadata,
        repaired=result.repaired,
    )


def _assert_frozen(document: VisualDocument) -> None:
    if document.status not in {"FROZEN", "COMPILED"}:
        raise VisualCodegenError(
            "CodeGen 只接受已冻结的 VisualDocument；请先在画布中确认视觉",
            code="VISUAL_CODEGEN_REQUIRES_FROZEN",
        )
    content_hash = str(document.metadata.content_hash or "")
    if not content_hash:
        raise VisualCodegenError(
            "FROZEN VisualDocument 缺少 contentHash，请重新冻结",
            code="VISUAL_CONTENT_HASH_MISSING",
        )
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", content_hash):
        raise VisualCodegenError(
            "FROZEN VisualDocument 的 contentHash 格式不合法",
            code="VISUAL_CONTENT_HASH_INVALID",
        )


def _gate_or_raise(result: VisualCompileResult) -> None:
    report = verify_visual_source(result.files, result.node_ids)
    if report.ok:
        return
    raise VisualCodegenGateError(
        f"CodeGen Babel AST 门禁未通过：{'；'.join(report.errors[:5])}",
        errors=report.errors,
    )


def compile_visual_document(
    document: VisualDocument | Mapping[str, Any],
    *,
    require_frozen: bool = True,
    allow_fallback: bool = True,
    include_interactions: bool = False,
) -> VisualCompileResult:
    """确定性编译，并通过 Babel AST 门禁。

    门禁失败时最多回退一次扁平占位页；占位页也必须过门禁。两次都失败
    才抛错，绝不把不可编译源码交给沙盒。
    """
    visual = _coerce_document(document)
    report = validate_visual_document(visual)
    if not report.ok:
        raise VisualCodegenError(
            "VisualDocument 校验失败：" + "；".join(report.errors[:5]),
            code="VISUAL_CODEGEN_INVALID_DOCUMENT",
        )
    if require_frozen:
        _assert_frozen(visual)
    repeat_warnings = _unbound_repeat_warnings(visual)

    try:
        primary = _ReactCompiler(
            visual, include_interactions=include_interactions
        ).compile()
    except VisualCodegenError:
        raise
    except Exception as exc:  # noqa: BLE001 - 编译器缺陷不能泄漏堆栈
        if not allow_fallback:
            raise VisualCodegenError(
                f"CodeGen 编译器异常: {type(exc).__name__}",
                code="VISUAL_CODEGEN_COMPILER_ERROR",
            ) from exc
        fallback = _fallback_result(visual, reason=f"编译器异常 {type(exc).__name__}")
        _gate_or_raise(fallback)
        return _with_extra_warnings(
            VisualCompileResult(
                files=fallback.files,
                node_ids=fallback.node_ids,
                summary=fallback.summary,
                warnings=fallback.warnings,
                metadata=fallback.metadata,
                repaired=True,
            ),
            repeat_warnings,
        )

    try:
        _gate_or_raise(primary)
        return _with_extra_warnings(primary, repeat_warnings)
    except VisualCodegenGateUnavailable:
        raise
    except VisualCodegenGateError as exc:
        if not allow_fallback:
            raise
        fallback = _fallback_result(visual, reason=str(exc))
        _gate_or_raise(fallback)
        return _with_extra_warnings(
            VisualCompileResult(
                files=fallback.files,
                node_ids=fallback.node_ids,
                summary=fallback.summary,
                warnings=tuple(
                    list(primary.warnings)
                    + list(fallback.warnings)
                    + [f"确定性编译未过门禁：{exc}"]
                ),
                metadata={
                    **fallback.metadata,
                    "primaryErrors": list(exc.errors[:20]),
                },
                repaired=True,
            ),
            repeat_warnings,
        )


def generate_visual_fallback(
    document: VisualDocument | Mapping[str, Any],
    *,
    reason: str = "构建失败",
) -> VisualCompileResult:
    """生成可直接构建的扁平占位页（供 BuildVerifier 一次性降级）。"""
    visual = _coerce_document(document)
    fallback = _fallback_result(visual, reason=reason)
    _gate_or_raise(fallback)
    return fallback


def visual_document_hash(document: VisualDocument | Mapping[str, Any]) -> str:
    """服务端重算的稳定内容哈希（用于审计与事件展示）。"""
    return compute_content_hash(_coerce_document(document))


__all__ = [
    "DEFAULT_TOKENS",
    "VisualCodegenError",
    "VisualCompileResult",
    "compile_visual_document",
    "generate_visual_fallback",
    "visual_document_hash",
]
