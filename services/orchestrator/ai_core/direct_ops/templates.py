"""Direct Ops 基础组件模板。

只允许白名单内的原生 JSX 组件，不引入第三方 UI 库，也不生成业务组件。
模板保持单根节点；Card 额外包含一个子节点，用于验证 JSX 节点数量变化。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ComponentTemplate:
    component_type: str
    jsx: str
    imports: tuple[str, ...] = ()
    # 模板新增的 JSX 节点数量（用于写入前后守恒校验）。
    node_count: int = 1


# 直接新增的节点必须立即拥有源码身份。占位符由 DirectOperationService
# 在生成动作时替换为唯一 oid / node-id / 插入文件；否则新节点会成为
# 没有 data-oid 的孤儿，删除 / 移动只能靠行号猜测。
_IDENTITY = (
    'data-oid="__VIBE_OID__" '
    'data-node-id="__VIBE_NODE_ID__" '
    'data-source-file="__VIBE_SOURCE_FILE__" '
    'data-vibe-created="direct"'
)
_IDENTITY_CHILD = (
    'data-oid="__VIBE_OID___child1" '
    'data-node-id="__VIBE_NODE_ID___child1" '
    'data-source-file="__VIBE_SOURCE_FILE__" '
    'data-vibe-created="direct"'
)

COMPONENT_TEMPLATES: dict[str, ComponentTemplate] = {
    "Button": ComponentTemplate(
        component_type="Button",
        jsx=f'<button type="button" {_IDENTITY} '
        'className="vibe-direct-button">按钮</button>',
    ),
    "Input": ComponentTemplate(
        component_type="Input",
        jsx=f'<input {_IDENTITY} className="vibe-direct-input" '
        'placeholder="请输入" />',
    ),
    "Text": ComponentTemplate(
        component_type="Text",
        jsx=f'<p {_IDENTITY} className="vibe-direct-text">' "文本内容</p>",
    ),
    "Card": ComponentTemplate(
        component_type="Card",
        jsx=f'<div {_IDENTITY} data-source-kind="container" '
        'className="vibe-direct-card">'
        f'<p {_IDENTITY_CHILD} className="vibe-direct-text">'
        "卡片内容</p></div>",
        node_count=2,
    ),
    "Container": ComponentTemplate(
        component_type="Container",
        jsx=f'<div {_IDENTITY} data-source-kind="container" '
        'className="vibe-direct-container"></div>',
        node_count=1,
    ),
}


def apply_style_context(jsx: str, style_context: object) -> str:
    """把同级兄弟的 className 合并到新组件根节点。

    只接受安全字符，且最多 240 字符；不复制事件属性或任意 HTML。
    """
    if not isinstance(style_context, dict):
        return jsx
    raw = str(style_context.get("className") or "")
    extra = " ".join(
        token for token in re.sub(r"[^A-Za-z0-9_\-\s]", " ", raw).split() if token
    )[:240]
    if extra:
        match = re.search(r'className="([^"]*)"', jsx)
        if match:
            merged = f"{match.group(1)} {extra}".strip()
            start, end = match.span(1)
            jsx = f"{jsx[:start]}{merged}{jsx[end:]}"
    inline = re.sub(
        r"[^A-Za-z0-9#%.,:;\-\s()]",
        "",
        str(style_context.get("style") or ""),
    )[:300]
    if inline and 'style="' not in jsx:
        if "/>" in jsx:
            jsx = jsx.replace("/>", f' style="{inline}"/>', 1)
        else:
            jsx = jsx.replace(">", f' style="{inline}">', 1)
    return jsx


def ensure_imports(content: str, imports: tuple[str, ...] | list[str]) -> str:
    """确定性补全 import；已存在（忽略首尾空白）时跳过。

    只在最后一个顶层 import 之后插入，避免破坏文件头注释与 shebang。
    """
    if not imports:
        return content
    lines = content.splitlines(keepends=True)
    merged = "".join(lines)
    for statement in imports:
        raw = str(statement or "").strip().rstrip(";").strip()
        if not raw:
            continue
        pattern = re.compile(r"(^|\n)\s*" + re.escape(raw) + r"\s*;?\s*(\n|$)")
        if pattern.search(merged):
            continue
        last_import = -1
        for index, line in enumerate(lines):
            if re.match(r"^\s*import\b", line):
                last_import = index
        insert_at = last_import + 1 if last_import >= 0 else 0
        lines.insert(insert_at, raw + "\n")
        merged = "".join(lines)
    return merged


__all__ = ["COMPONENT_TEMPLATES", "ComponentTemplate", "ensure_imports"]
