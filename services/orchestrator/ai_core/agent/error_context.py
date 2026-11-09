"""构建失败日志的精准截断。

Vite / esbuild / Rollup 的失败日志通常包含：

    src/App.jsx:12:5: ERROR: Expected "}" but found ...

真正有用的信息只有 Error Message 与报错行附近的一小段代码。本模块负责：

1. 提取错误位置（file:line:col / line N）；
2. 提取 Error Message；
3. 只保留报错行前后 ``context_lines`` 行代码；
4. 生成归一化错误指纹，用于“同错不重试”。

无法定位行号时回退为 stderr 尾部，而不是把完整 stdout 全部回传。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# 常见构建工具的错误位置格式：
#   src/App.jsx:12:5
#   src/App.jsx (12:5)
#   /app/src/App.jsx:12
_LOCATION_RE = re.compile(
    r"(?P<file>[A-Za-z0-9_./\\-]+\.(?:jsx?|tsx?|mjs|cjs|css|html?|vue|svelte))"
    r"(?:\(|:)(?P<line>\d+)(?::(?P<col>\d+))?"
)

# 退化格式：line 12
_LINE_RE = re.compile(r"\bline\s+(?P<line>\d+)\b", re.IGNORECASE)

_ERROR_HINT_RE = re.compile(
    r"(error|failed|failure|✘|✖|syntaxerror|typeerror|referenceerror|"
    r"err_|cannot find|expected|unexpected|unterminated|invalid)",
    re.IGNORECASE,
)

# 指纹归一化：路径、行列号、十六进制地址、纯数字都替换为占位符
_FP_PATH_RE = _LOCATION_RE
_FP_HEX_RE = re.compile(r"0x[0-9a-fA-F]+")
_FP_NUMBER_RE = re.compile(r"\b\d+\b")


@dataclass(frozen=True)
class ErrorLocation:
    file: str
    line: int
    column: int | None = None


def _clean(text: str) -> str:
    """去掉 ANSI 控制符与回车，统一按行处理。"""
    return _ANSI_RE.sub("", text or "").replace("\r\n", "\n").replace("\r", "\n")


def extract_error_location(stderr: str) -> ErrorLocation | None:
    """提取第一个带文件名的错误位置；没有文件名时回退 ``line N``。"""
    text = _clean(stderr)
    for match in _LOCATION_RE.finditer(text):
        line = int(match.group("line"))
        if line <= 0:
            continue
        column = match.group("col")
        return ErrorLocation(
            file=match.group("file"),
            line=line,
            column=int(column) if column else None,
        )

    for match in _LINE_RE.finditer(text):
        line = int(match.group("line"))
        if line > 0:
            return ErrorLocation(file="", line=line)
    return None


def error_file_hint(stderr: str) -> str | None:
    """返回错误日志中出现的文件路径，用于从 current_files 选择源码。"""
    location = extract_error_location(stderr)
    return location.file if location and location.file else None


def _error_message(text: str, limit: int = 800) -> str:
    """提取含 error 提示的行，去重并限长。"""
    lines: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or not _ERROR_HINT_RE.search(line):
            continue
        # 跳过纯进度行，例如 "building 12 modules"
        if line.startswith(("vite v", "transforming", "✓")):
            continue
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
        if len(lines) >= 8:
            break

    if not lines:
        # 没有任何关键字：取尾部非空行，至少保留一点上下文
        tail = [line.strip() for line in text.splitlines() if line.strip()][-6:]
        lines = tail

    message = "\n".join(lines)
    if len(message) > limit:
        message = message[:limit] + "\n...[error truncated]"
    return message


def _code_context(code: str, line: int, context_lines: int) -> str:
    """输出报错行前后各 N 行，带行号并以 ``>`` 标记当前行。"""
    if not code:
        return ""
    lines = code.splitlines()
    if not lines:
        return ""
    index = min(max(line - 1, 0), len(lines) - 1)
    start = max(0, index - context_lines)
    end = min(len(lines), index + context_lines + 1)

    rendered: list[str] = []
    for offset in range(start, end):
        marker = ">" if offset == index else " "
        content = lines[offset][:200]
        rendered.append(f"{marker}{offset + 1:>5} | {content}")
    return "\n".join(rendered)


def extract_error_context(
    stderr: str,
    code: str,
    context_lines: int = 10,
    max_chars: int = 2000,
) -> str:
    """把完整构建日志压缩为“Error Message + 约 20 行代码”。"""
    text = _clean(stderr)
    if not text.strip():
        return "构建失败，但未返回错误日志。"

    location = extract_error_location(text)
    message = _error_message(text)
    parts = ["[error]", message]

    if location is not None:
        position = location.file or "<unknown>"
        if location.column is not None:
            position = f"{position}:{location.line}:{location.column}"
        else:
            position = f"{position}:{location.line}"
        parts.extend(["\n[location]", position])

        snippet = _code_context(code, location.line, context_lines)
        if snippet:
            start = max(1, location.line - context_lines)
            end = min(len(code.splitlines()), location.line + context_lines)
            parts.extend([f"\n[code-context] lines {start}-{end}", snippet])
    else:
        # 无法定位：只保留日志尾部，避免整份 stdout 进入 Prompt
        tail = text.strip()[-1200:]
        parts.extend(["\n[stderr-tail]", tail])

    rendered = "\n".join(parts)
    if len(rendered) > max_chars:
        head = max_chars - 400
        rendered = rendered[:head] + "\n...[context truncated]...\n" + rendered[-400:]
    return rendered


def extract_error_fingerprint(stderr: str) -> str:
    """生成归一化错误指纹。

    同一类错误即使文件名、行列号变化，也应得到相同指纹，从而触发
    “连续两次同错即终止”。无法提取时返回空串，调用方应跳过去重。
    """
    text = _clean(stderr)
    if not text.strip():
        return ""

    hints: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or not _ERROR_HINT_RE.search(line):
            continue
        normalized = _FP_PATH_RE.sub("<loc>", line)
        normalized = _FP_HEX_RE.sub("<hex>", normalized)
        normalized = _FP_NUMBER_RE.sub("<n>", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if normalized and normalized not in hints:
            hints.append(normalized)
        if len(hints) >= 3:
            break

    if not hints:
        tail = [line.strip() for line in text.splitlines() if line.strip()][-1:]
        hints = [re.sub(r"\s+", " ", item) for item in tail]
    if not hints:
        return ""

    digest = hashlib.sha1("||".join(hints).encode("utf-8")).hexdigest()
    return digest[:16]
