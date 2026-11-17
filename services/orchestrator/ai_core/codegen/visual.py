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

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ai_core.codegen.visual_gate import (
    VisualCodegenGateError,
    VisualCodegenGateUnavailable,
    verify_visual_source,
)
from ai_core.domain.visual_document import (
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
        }
    return result


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


class _ReactCompiler:
    def __init__(self, document: VisualDocument) -> None:
        self.document = document
        self.actions = _interaction_index(document)
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
        return " ".join(parts)

    def _attrs(
        self,
        node: VisualNode,
        style: dict[str, str],
        *,
        extra: Iterable[str] = (),
    ) -> str:
        attrs = [
            f"className={{{_js_string(self._class_name(node))}}}",
            f'data-node-id="{node.id}"',
            f'data-visual-type="{node.type}"',
            f"style={{{_js_object(style)}}}",
        ]
        if not node.state.default_visible:
            attrs.append("hidden")
        if node.state.disabled:
            attrs.append("disabled={true}")
        if node.a11y.label and node.type in {"button", "input", "image", "modal"}:
            attrs.append(f"aria-label={{{_js_string(node.a11y.label)}}}")
        action = self.actions.get(node.id)
        if action:
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
        style, warnings = _build_style(node)
        self.warnings.extend(warnings)
        pad = " " * indent

        if node.codegen.mode == "custom":
            self.custom_node_ids.append(node.id)
            self.warnings.append(
                f"节点 {node.id} 为 custom，CodeGen 生成安全占位组件（不调用 LLM）"
            )
            style = dict(style)
            style.setdefault("color", "var(--color-muted)")
            attrs = self._attrs(node, style, extra=[])
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
            return [f"{pad}<input {self._attrs(node, style, extra=extra)} />"]

        if node.type == "image":
            src = _safe_image_src(node.props.get("src") or node.props.get("url") or "")
            alt = str(node.props.get("alt") or node.a11y.label or node.name or "")
            extra = [
                f"src={{{_js_string(src)}}}",
                f"alt={{{_js_string(alt)}}}",
                'loading="lazy"',
            ]
            return [f"{pad}<img {self._attrs(node, style, extra=extra)} />"]

        attrs = self._attrs(node, style)

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
        root = self.document.root
        lines = ['import "./visual.css"', ""]
        lines.extend(_render_action_runtime(list(self.actions.values())))
        lines.extend(
            [
                "export default function App() {",
                "  return (",
            ]
        )
        if root is not None:
            lines.extend(self._render_node(root, 4))
        else:
            lines.append(
                '    <main className="visual-node visual-page" '
                'style={{ minHeight: "100vh", backgroundColor: '
                '"var(--color-surface)" }} />'
            )
        lines.extend(["  )", "}", ""])
        files = {
            "src/App.jsx": "\n".join(lines),
            "src/visual.css": _render_css(self.document),
        }
        node_ids = _collect_node_ids(root)
        return VisualCompileResult(
            files=files,
            node_ids=node_ids,
            summary=(
                "VisualDocument 确定性编译："
                f"{len(node_ids)} 个节点、{len(self.actions)} 个交互"
            ),
            warnings=tuple(dict.fromkeys(self.warnings)),
            metadata={
                "codegen": "visual-deterministic",
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
    node_ids = _collect_node_ids(document.root)
    lines = [
        'import "./visual.css"',
        "",
        "export default function App() {",
        "  return (",
        '    <main className="visual-node visual-page" style={{ minHeight: '
        '"100vh", padding: "24px", backgroundColor: "var(--color-surface)" }}>',
        '      <h1 style={{ fontSize: "var(--typography-title)", margin: '
        '"0px 0px 16px" }}>页面已降级为安全占位</h1>',
    ]
    for node in _iter_nodes(document.root):
        style = (
            'style={{ padding: "8px 12px", margin: "6px 0px", '
            'border: "1px solid var(--color-border)", borderRadius: '
            '"var(--radius-sm)" }}'
        )
        label = node.name or node.type
        lines.append(
            f'      <div data-node-id="{node.id}" '
            f'data-visual-type="{node.type}" {style}>'
            f"{{{_js_string(f'{label} ({node.type})')}}}</div>"
        )
    lines.extend(["    </main>", "  )", "}", ""])
    return VisualCompileResult(
        files={
            "src/App.jsx": "\n".join(lines),
            "src/visual.css": _render_fallback_css(document),
        },
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

    try:
        primary = _ReactCompiler(visual).compile()
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
        return VisualCompileResult(
            files=fallback.files,
            node_ids=fallback.node_ids,
            summary=fallback.summary,
            warnings=fallback.warnings,
            metadata=fallback.metadata,
            repaired=True,
        )

    try:
        _gate_or_raise(primary)
        return primary
    except VisualCodegenGateUnavailable:
        raise
    except VisualCodegenGateError as exc:
        if not allow_fallback:
            raise
        fallback = _fallback_result(visual, reason=str(exc))
        _gate_or_raise(fallback)
        return VisualCompileResult(
            files=fallback.files,
            node_ids=fallback.node_ids,
            summary=fallback.summary,
            warnings=tuple(
                list(primary.warnings)
                + list(fallback.warnings)
                + [f"确定性编译未过门禁：{exc}"]
            ),
            metadata={**fallback.metadata, "primaryErrors": list(exc.errors[:20])},
            repaired=True,
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
