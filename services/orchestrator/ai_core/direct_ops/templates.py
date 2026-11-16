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


COMPONENT_TEMPLATES: dict[str, ComponentTemplate] = {
    "Button": ComponentTemplate(
        component_type="Button",
        jsx='<button type="button" data-vibe-created="direct" '
        'className="vibe-direct-button">按钮</button>',
    ),
    "Input": ComponentTemplate(
        component_type="Input",
        jsx='<input data-vibe-created="direct" className="vibe-direct-input" '
        'placeholder="请输入" />',
    ),
    "Text": ComponentTemplate(
        component_type="Text",
        jsx='<p data-vibe-created="direct" className="vibe-direct-text">'
        "文本内容</p>",
    ),
    "Card": ComponentTemplate(
        component_type="Card",
        jsx='<div data-vibe-created="direct" className="vibe-direct-card">'
        "<p>卡片内容</p></div>",
        node_count=2,
    ),
}


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
