"""``move_component``：把“移动组件”抽象成可复用 Tool。

设计目标：

1. 移动是 Agent 的一项标准能力，而不是前端“新建后自动进入拖拽模式”的
   隐式副作用；
2. 拖拽只是输入方式之一。最终都转化为自然语言指令，由 Agent 调用本
   Tool 生成确定性的 CSS 位移规则；
3. 位移只使用 CSS ``transform: translate(...)``，绝不修改 ``width`` /
   ``height``。这样拖拽预览阶段不会触发重新布局，也不会出现按钮被拉长
   或卡片跳变。

Tool 本身无状态：输入元素选择器与像素偏移，返回可直接追加到样式文件的
CSS 规则。真正的写文件仍由 UI Graph 的 CodeEditor 完成，保持 Tool 与
编辑器职责分离。
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.tools import BaseTool, tool

from ai_core.tools.base import ToolDescriptor, ToolRegistry

# 直接拖拽的节点标记形如 ``src/App.jsx:12:5``，因此选择器字符集必须允许
# 点号与路径斜杠；它们只出现在引号属性值内，不会破坏 CSS 结构。
_SELECTOR_SAFE = re.compile(r"^[A-Za-z0-9_\-:.\[\]=\"'# >+~*()/@%]+$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")


class MoveComponentError(ValueError):
    """移动参数不合法。"""


def normalize_element_selector(element_id: str) -> str:
    """把 ``element_id`` 归一化为安全的 CSS 选择器。

    接受两种输入：
      - 普通 data-source 标识：``cta-button`` → ``[data-source="cta-button"]``；
      - 完整 CSS 选择器：``[data-vibe-created="ann_1"]`` 原样保留。

    绝不拼接用户输入的 HTML / JS，只允许受限字符集，避免把 Tool 变成
    CSS 注入入口。
    """
    value = str(element_id or "").strip()
    if not value:
        raise MoveComponentError("element_id 不能为空")
    if len(value) > 200:
        raise MoveComponentError("element_id 过长")
    if _SAFE_IDENTIFIER.fullmatch(value):
        return f'[data-source="{value}"]'
    if not _SELECTOR_SAFE.fullmatch(value):
        raise MoveComponentError("element_id 不是受支持的 CSS 选择器")
    return value


def _finite(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MoveComponentError(f"{name} 必须是数字") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise MoveComponentError(f"{name} 必须是有限数字")
    # 单次移动限制在 ±2000px，避免模型输出离谱坐标把页面移出视口。
    if abs(number) > 2000:
        raise MoveComponentError(f"{name} 超出 ±2000px 限制")
    return number


def build_move_rule(element_id: str, x: Any, y: Any) -> str:
    """生成只改 transform 的 CSS 规则（纯函数，便于单元测试与复用）。"""
    selector = normalize_element_selector(element_id)
    dx = _finite(x, "x")
    dy = _finite(y, "y")
    # translate3d 交给合成层处理，拖拽和 HMR 期间都不会触发布局重算。
    return (
        f"{selector}{{"
        f"transform:translate3d({dx:.2f}px,{dy:.2f}px,0) !important;"
        "will-change:transform;"
        "}"
    )


def build_move_tool() -> BaseTool:
    """构造 LangChain Tool；参数签名固定为 ``element_id, x, y``。"""

    @tool("move_component")
    def move_component(element_id: str, x: float, y: float) -> str:
        """把指定组件按像素偏移移动。

        参数：
          - element_id：data-source 标识或 CSS 选择器；
          - x / y：相对当前位置的像素偏移。

        返回 JSON：包含 selector、offset 与只使用 CSS transform 的规则。
        该 Tool 不修改 width / height，拖拽预览必须复用同一规则。
        """
        try:
            selector = normalize_element_selector(element_id)
            rule = build_move_rule(element_id, x, y)
        except MoveComponentError as exc:
            return json.dumps(
                {"ok": False, "error": str(exc)},
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "ok": True,
                "selector": selector,
                "offset": {"x": float(x), "y": float(y)},
                "rule": rule,
                "strategy": "css-transform",
                "note": "仅使用 transform；禁止修改 width / height。",
            },
            ensure_ascii=False,
        )

    return move_component


def register_move_component_tool(registry: ToolRegistry) -> None:
    """注册标准移动 Tool；Agent 通过 Manifest 的 supported_tools 引用。"""
    registry.register(
        ToolDescriptor(
            name="move_component",
            description=(
                "按像素偏移移动组件，只输出 CSS transform 规则，"
                "不会修改 width / height。"
            ),
            danger_level="write",
            metadata={
                "strategy": "css-transform",
                "arguments": ["element_id", "x", "y"],
                "forbid": ["width", "height"],
            },
        ),
        build_move_tool(),
    )


__all__ = [
    "MoveComponentError",
    "build_move_rule",
    "build_move_tool",
    "normalize_element_selector",
    "register_move_component_tool",
]
