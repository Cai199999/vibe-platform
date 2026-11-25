"""用户可见文案边界：不暴露代码级细节。"""

from __future__ import annotations

import re

_CODE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("代码块", re.compile(r"```")),
    ("堆栈", re.compile(r"Traceback \(most recent call last\)")),
    (
        "文件路径",
        re.compile(
            r"[\w./-]+\.(?:py|js|jsx|ts|tsx|java|go|rb|php|sql|yml|yaml|json)\b",
            re.IGNORECASE,
        ),
    ),
    ("函数调用", re.compile(r"\b[A-Za-z_]\w*\s*\(")),
    ("蛇形标识符", re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b")),
    ("驼峰标识符", re.compile(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b")),
)


class UserLanguageError(ValueError):
    """用户可见文案包含代码细节。"""


def violations(text: str) -> list[str]:
    """返回命中的代码细节类别；空列表表示可对用户展示。"""
    value = str(text or "")
    return [label for label, pattern in _CODE_PATTERNS if pattern.search(value)]


def ensure_user_language(text: str) -> None:
    found = violations(text)
    if found:
        raise UserLanguageError("用户文案包含代码细节：" + "、".join(found))


def to_user_language(text: str, *, fallback: str = "这一步需要你确认业务范围。") -> str:
    """原型版兜底：命中代码细节时换成业务化提示。"""
    return fallback if violations(text) else str(text or "")


__all__ = [
    "UserLanguageError",
    "ensure_user_language",
    "to_user_language",
    "violations",
]
