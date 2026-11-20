"""Direct Ops 尺寸契约：ADD_COMPONENT 初始尺寸与 SET_SIZE 模式映射。

尺寸只允许三种模式：

- ``hug``   ：适应内容（``w-fit`` / ``h-fit``）
- ``fill``  ：填满父容器（按父布局映射 ``flex-1`` / ``w-full`` / ``self-stretch``）
- ``fixed`` ：固定像素（``w-[Npx]`` / ``h-[Npx]``）

Fill 不能无脑写 ``h-full``：父容器是 block 且高度为 auto 时，子节点的百分比
高度会退化为 auto；父容器有固定高度时又可能把内容裁掉。这里按父容器的
display / flex-direction 选择最小风险的映射，block 高度用 ``min-h-full``。
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable

MAX_SIZE_PX = 4000
MIN_SIZE_PX = 1
DEFAULT_MIN_HEIGHT_PX = 30

SIZE_MODES = frozenset({"hug", "fill", "fixed"})

# flex 主轴尺寸类会覆盖 width / height。只清这些“flex item”类，
# 保留 `flex` / `flex-col` / `flex-wrap` 等容器布局类。
FLEX_SIZE_PREFIXES = (
    "flex-1",
    "flex-auto",
    "flex-initial",
    "flex-none",
    "flex-grow",
    "flex-shrink",
    "grow",
    "shrink",
    "basis-",
)

_CLASS_ATTR = re.compile(r"\bclassName\s*=\s*")


def clamp_px(value: object) -> int | None:
    """把任意输入收敛为 1..4000 的整数像素；非法值返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return max(MIN_SIZE_PX, min(MAX_SIZE_PX, int(round(number))))


def normalize_mode(value: object) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in SIZE_MODES else ""


def normalize_axis(value: object) -> dict[str, int | str] | None:
    """校验单个方向（width / height）的尺寸请求。"""
    if not isinstance(value, dict):
        return None
    mode = normalize_mode(value.get("mode"))
    if not mode:
        return None
    result: dict[str, int | str] = {"mode": mode}
    if mode == "fixed":
        pixels = clamp_px(value.get("value"))
        if pixels is None:
            pixels = clamp_px(value.get("px"))
        if pixels is None:
            return None
        result["px"] = pixels
    return result


# ---------------------------------------------------------------------------
# JSX className 解析 / 合并
# ---------------------------------------------------------------------------

def opening_tag_end(source: str) -> int:
    """返回第一个开始标签 ``>`` 的下标；找不到时返回文本长度。"""
    quote = ""
    depth = 0
    index = 0
    while index < len(source):
        char = source[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        elif char == ">" and depth == 0:
            return index
        index += 1
    return len(source)


def _read_static_literal(rest: str) -> tuple[bool, str]:
    """读取 ``className=`` 后的静态字符串；动态表达式返回 (False, "")。"""
    text = rest.lstrip()
    if text[:1] in {"'", '"'}:
        quote = text[0]
        end = text.find(quote, 1)
        if end == -1:
            return False, ""
        return True, text[1:end]
    if text.startswith("{"):
        inner = text[1:].lstrip()
        if inner[:1] in {"'", '"'}:
            quote = inner[0]
            end = inner.find(quote, 1)
            if end != -1:
                return True, inner[1:end]
            return False, ""
        if inner.startswith("`"):
            end = inner.find("`", 1)
            body = inner[1:end] if end != -1 else ""
            if end != -1 and "${" not in body:
                return True, body
        return False, ""
    return False, ""


def parse_class_name(node_source: object) -> tuple[bool, bool, str]:
    """解析节点开始标签里的 className。

    返回 ``(has_attr, is_static, value)``：

    - ``(False, True, "")``：源码没有 className，可以安全新增；
    - ``(True, True, "...")``：静态字符串，可确定性合并；
    - ``(True, False, "")``：clsx(...) / 模板插值等动态表达式，必须交给 AI。
    """
    text = str(node_source or "")
    end = opening_tag_end(text)
    opening = text[:end] if end else text
    match = _CLASS_ATTR.search(opening)
    if not match:
        return False, True, ""
    is_static, value = _read_static_literal(opening[match.end():])
    return True, is_static, value


def split_classes(value: object) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for token in str(value or "").split():
        if token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result


def _starts_with_any(token: str, prefixes: Iterable[str]) -> bool:
    return any(token.startswith(prefix) for prefix in prefixes)


def _layout_of(parent_layout: object) -> tuple[str, str]:
    data = parent_layout if isinstance(parent_layout, dict) else {}
    display = str(data.get("display") or "").strip().lower()
    direction = str(
        data.get("flexDirection") or data.get("flex_direction") or ""
    ).strip().lower()
    if display.startswith("inline-"):
        display = display[len("inline-") :]
    if display not in {"flex", "grid"}:
        display = "block"
    if direction not in {"row", "column"}:
        direction = "row" if "row" in direction else "column"
    return display, direction


def size_classes(
    width: dict[str, Any] | None,
    height: dict[str, Any] | None,
    parent_layout: object = None,
) -> list[str]:
    """把宽 / 高模式映射成 Tailwind 类名。"""
    display, direction = _layout_of(parent_layout)
    classes: list[str] = []
    if width:
        mode = str(width.get("mode") or "")
        if mode == "hug":
            classes.append("w-fit")
        elif mode == "fixed":
            classes.append(f"w-[{int(width['px'])}px]")
        elif display == "flex" and direction == "row":
            # 横向 flex 的宽度就是主轴：flex-1 才能和兄弟一起分配空间。
            classes.extend(["flex-1", "min-w-0"])
        else:
            classes.append("w-full")
    if height:
        mode = str(height.get("mode") or "")
        if mode == "hug":
            classes.append("h-fit")
        elif mode == "fixed":
            classes.append(f"h-[{int(height['px'])}px]")
        elif display == "flex" and direction == "column":
            classes.extend(["flex-1", "min-h-0"])
        elif display == "flex":
            classes.append("self-stretch")
        elif display == "grid":
            classes.append("h-full")
        else:
            # block：用 min-height 填满有确定高度的父容器；auto 高度时
            # 百分比 min-height 解析为 auto，不会把子节点强行撑出父容器。
            classes.append("min-h-full")
    return classes


def merge_size_classes(
    base: object,
    width: dict[str, Any] | None,
    height: dict[str, Any] | None,
    parent_layout: object = None,
) -> str:
    """移除冲突的尺寸类，再追加本次选择对应的类。"""
    tokens = split_classes(base)
    drop_prefixes: list[str] = []
    if width:
        drop_prefixes.extend(["w-", "min-w-", "max-w-"])
    if height:
        drop_prefixes.extend(["h-", "min-h-", "max-h-"])
    if width or height:
        # 显式尺寸要成为唯一事实：残留的 flex-1 / grow 会覆盖 width / height。
        drop_prefixes.extend(FLEX_SIZE_PREFIXES)
    add = size_classes(width, height, parent_layout)
    if "self-stretch" in add:
        drop_prefixes.append("self-")
    kept = [
        token
        for token in tokens
        if not (drop_prefixes and _starts_with_any(token, drop_prefixes))
    ]
    for token in add:
        if token not in kept:
            kept.append(token)
    return " ".join(kept)


def merge_jsx_class_name(
    jsx: str,
    add_tokens: Iterable[str],
    drop_prefixes: Iterable[str] = (),
) -> str:
    """把尺寸类合并进 JSX 根节点的 className（ADD_COMPONENT 模板用）。"""
    additions = [token for token in add_tokens if token]
    if not additions:
        return jsx
    drop = tuple(drop_prefixes)
    match = re.search(r'className="([^"]*)"', jsx)
    if match:
        base = match.group(1)
        tokens = [
            token
            for token in split_classes(base)
            if not (drop and _starts_with_any(token, drop))
        ]
        for token in additions:
            if token not in tokens:
                tokens.append(token)
        return jsx[: match.start(1)] + " ".join(tokens) + jsx[match.end(1) :]
    end = opening_tag_end(jsx)
    if not end:
        return jsx
    self_closing = jsx[:end].rstrip().endswith("/")
    insert_at = jsx.rfind("/", 0, end) if self_closing else end
    prefix = jsx[:insert_at]
    separator = "" if prefix.endswith((" ", "\n", "\t")) else " "
    return (
        prefix
        + f'{separator}className="{" ".join(additions)}"'
        + jsx[insert_at:]
    )


def add_component_size_classes(
    component_type: str,
    width_px: object,
    height_px: object,
) -> list[str]:
    """ADD_COMPONENT 的初始尺寸：用户圈选尺寸优先，否则给类型兜底。"""
    width = clamp_px(width_px)
    height = clamp_px(height_px)
    classes: list[str] = []
    if width:
        classes.append(f"w-[{width}px]")
    if height:
        classes.append(f"h-[{height}px]")
    if width or height:
        classes.append(f"min-h-[{DEFAULT_MIN_HEIGHT_PX}px]")
    elif component_type == "Container":
        classes.append("min-h-[120px]")
    elif component_type == "Card":
        classes.append("min-h-[96px]")
    else:
        classes.append(f"min-h-[{DEFAULT_MIN_HEIGHT_PX}px]")
    return classes


def apply_add_size_context(
    jsx: str,
    component_type: str,
    width_px: object,
    height_px: object,
) -> str:
    explicit = clamp_px(width_px) or clamp_px(height_px)
    drop_prefixes = (
        ("w-", "min-w-", "max-w-", "h-", "min-h-", "max-h-")
        if explicit
        else ()
    )
    return merge_jsx_class_name(
        jsx,
        add_component_size_classes(component_type, width_px, height_px),
        drop_prefixes,
    )


__all__ = [
    "DEFAULT_MIN_HEIGHT_PX",
    "MAX_SIZE_PX",
    "MIN_SIZE_PX",
    "SIZE_MODES",
    "add_component_size_classes",
    "apply_add_size_context",
    "clamp_px",
    "merge_jsx_class_name",
    "merge_size_classes",
    "normalize_axis",
    "normalize_mode",
    "opening_tag_end",
    "parse_class_name",
    "size_classes",
    "split_classes",
]
