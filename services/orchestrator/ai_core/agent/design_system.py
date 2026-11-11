"""项目设计系统提取（Tailwind 主题 / 全局 CSS 变量）。

FrontendCoder 生成新组件时最容易犯的错是写死 ``#2563eb`` 这类具体色值，
导致页面逐渐偏离项目既有视觉体系。本模块从沙盒的 ``tailwind.config.*``
与全局 CSS 中提取主色 / 语义令牌，作为 Coder 的只读约束：

  - ``colors``：Tailwind 主题里显式声明的颜色；
  - ``cssVariables``：``:root`` / ``@theme`` 中的 CSS 变量；
  - ``suggestedClasses``：可直接使用的语义类名（bg-primary 等）。

提取失败只返回空 dict，绝不阻塞 Run。这里只做静态文本解析，不执行
用户项目里的 JavaScript，避免在服务端引入任意代码执行风险。
"""

from __future__ import annotations

import re
from typing import Any

# 沙盒模板可能使用多种 Tailwind 配置文件后缀。
DESIGN_CONFIG_CANDIDATES = (
    "tailwind.config.js",
    "tailwind.config.cjs",
    "tailwind.config.mjs",
    "tailwind.config.ts",
)
DESIGN_CSS_CANDIDATES = (
    "src/index.css",
    "src/App.css",
    "src/main.css",
    "src/styles.css",
    "src/globals.css",
    "src/styles/globals.css",
    "app/globals.css",
)

# 只保留与颜色相关的 CSS 变量，避免把间距 / 动画变量塞进 Prompt。
_TOKEN_HINTS = (
    "primary",
    "secondary",
    "accent",
    "brand",
    "surface",
    "background",
    "foreground",
    "text",
    "border",
    "muted",
    "danger",
    "success",
    "warning",
    "info",
)
_COLOR_VALUE_RE = re.compile(
    r"^(#[0-9a-fA-F]{3,8}|rgba?\(|hsla?\(|oklch\(|oklab\(|lab\(|lch\(|color\(|var\()",
)
_COLOR_TOKEN_RE = re.compile(
    r"""(?P<key>[A-Za-z_$][\w$-]*)\s*:\s*(?P<brace>\{)|"""
    r"""(?P<key_string>[A-Za-z_$][\w$-]*)\s*:\s*["'](?P<value>[^"']+)["']|"""
    r"""(?P<close>\})"""
)
_CSS_VAR_RE = re.compile(r"(--[\w-]+)\s*:\s*([^;}{]+);")
# 最多保留的令牌数量：Prompt 预算有限，语义色通常不会超过这个量级。
_MAX_TOKENS = 30


def _is_color_value(value: str) -> bool:
    return bool(_COLOR_VALUE_RE.match(str(value or "").strip()))


def _colors_block(config: str) -> str:
    """截取 ``colors`` 对象文本；找不到时返回空串。"""
    match = re.search(r"\bcolors\s*:\s*\{", config)
    if not match:
        return ""
    start = match.end() - 1
    depth = 0
    for index in range(start, len(config)):
        char = config[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return config[start : index + 1]
    return config[start:]


def _parse_colors(block: str) -> dict[str, str]:
    """解析 Tailwind colors 对象，支持嵌套（primary.DEFAULT）。

    故意不引入 JS 解析器：配置里可能包含函数 / require，静态扫描更安全，
    也能同时覆盖多行与单行写法。
    """
    if not block:
        return {}
    colors: dict[str, str] = {}
    stack: list[str] = []
    for match in _COLOR_TOKEN_RE.finditer(block):
        if match.group("brace"):
            stack.append(match.group("key"))
            continue
        if match.group("close"):
            if stack:
                stack.pop()
            continue
        value = match.group("value") or ""
        if not _is_color_value(value):
            continue
        key = match.group("key_string") or ""
        path = [*stack, key]
        if key == "DEFAULT" and len(path) > 1:
            path = path[:-1]
        name = "-".join(part for part in path if part)
        if name and len(colors) < _MAX_TOKENS:
            colors[name] = value
    return colors


def _parse_css_variables(css: str) -> dict[str, str]:
    variables: dict[str, str] = {}
    for match in _CSS_VAR_RE.finditer(css):
        name, value = match.group(1), match.group(2).strip()
        if len(variables) >= _MAX_TOKENS:
            break
        lowered = name.lower()
        if not any(hint in lowered for hint in _TOKEN_HINTS):
            continue
        if not _is_color_value(value):
            continue
        variables[name] = value
    return variables


def extract_design_system(files: dict[str, str]) -> dict[str, Any]:
    """从配置 / CSS 文件提取语义化设计令牌。

    返回空 dict 表示项目没有可识别的设计系统，Coder 继续使用
    reference_styles 或既有组件类名。
    """
    if not files:
        return {}
    sources: list[str] = []
    colors: dict[str, str] = {}
    css_variables: dict[str, str] = {}
    tailwind = False

    for path, content in files.items():
        text = str(content or "")
        if not text:
            continue
        if path in DESIGN_CONFIG_CANDIDATES:
            block = _colors_block(text)
            parsed = _parse_colors(block)
            if parsed or block:
                sources.append(path)
                tailwind = True
                colors.update(parsed)
            continue
        if path in DESIGN_CSS_CANDIDATES:
            parsed = _parse_css_variables(text)
            if parsed:
                sources.append(path)
                css_variables.update(parsed)
            if "@tailwind" in text or '@import "tailwindcss"' in text:
                tailwind = True

    if not colors and not css_variables and not tailwind:
        return {}

    suggested: list[str] = []
    for name in colors:
        suggested.extend([f"bg-{name}", f"text-{name}", f"border-{name}"])
    for name in css_variables:
        suggested.extend([f"bg-[var({name})]", f"text-[var({name})]"])
    # 去重并限制数量，避免 Prompt 被类名列表占满。
    suggested = list(dict.fromkeys(suggested))[:_MAX_TOKENS]

    return {
        "tailwind": tailwind,
        "sources": sources[:_MAX_TOKENS],
        "colors": dict(list(colors.items())[:_MAX_TOKENS]),
        "cssVariables": dict(list(css_variables.items())[:_MAX_TOKENS]),
        "suggestedClasses": suggested,
    }


def render_design_system(design: dict[str, Any] | None) -> str:
    """把设计系统渲染成 Coder 可读的紧凑文本；空系统返回空串。"""
    if not design:
        return ""
    parts: list[str] = []
    if design.get("tailwind"):
        parts.append("Tailwind 已配置")
    colors = design.get("colors") or {}
    if colors:
        parts.append(
            "主题色: " + ", ".join(f"{name}={value}" for name, value in colors.items())
        )
    variables = design.get("cssVariables") or {}
    if variables:
        parts.append(
            "CSS 变量: "
            + ", ".join(f"{name}={value}" for name, value in variables.items())
        )
    classes = design.get("suggestedClasses") or []
    if classes:
        parts.append("语义类名: " + ", ".join(classes))
    return "；".join(parts)


__all__ = [
    "DESIGN_CONFIG_CANDIDATES",
    "DESIGN_CSS_CANDIDATES",
    "extract_design_system",
    "render_design_system",
]
