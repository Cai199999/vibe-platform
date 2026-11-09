"""对话历史压缩。

目标：多轮对话超过预算后，不再把完整 chatHistory / previousRuns 传给模型，
而是压缩成一段“核心需求与约束”摘要。

估算口径：CJK 字符约 0.7 token，其余字符约 0.25 token（4 字符 / token）。
这是成本控制用的保守近似，不用于计费对账。
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Callable

# CJK 标点与全角字符也按高 token 密度估算
_CJK_RE = re.compile(r"[　-〿㐀-䶿一-鿿＀-￯]")

# summarize(text, max_chars) -> 摘要
SummarizeFn = Callable[[str, int], str]


@dataclass
class CompressionResult:
    """一次历史压缩的结果。"""

    value: Any
    compressed: bool
    tokens_before: int
    tokens_after: int
    summary: str = ""


def estimate_tokens(text: str) -> int:
    """混合文本的 Token 估算。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = max(0, len(text) - cjk)
    return max(1, math.ceil(cjk * 0.7 + other / 4))


def fallback_summary(text: str, max_chars: int) -> str:
    """无模型时的确定性摘要：优先保留含约束 / 用户指令的行。"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""

    keywords = ("user", "用户", "要求", "约束", "必须", "禁止", "不要", "改为")
    picked = [line for line in lines if any(key in line for key in keywords)]
    if not picked:
        picked = lines

    merged = "；".join(picked[-4:])
    if len(merged) <= max_chars:
        return merged
    # 保留尾部：最新指令通常比早期对话更重要
    return merged[-max_chars:]


# ------------------------------------------------------------
# UI Graph
# ------------------------------------------------------------
def _annotation_of(item: Any) -> dict[str, Any]:
    """兼容 AnnotationEnvelope 包装与裸标注。"""
    if isinstance(item, dict) and isinstance(item.get("annotation"), dict):
        return item["annotation"]
    return item if isinstance(item, dict) else {}


def collect_annotation_history(context: dict[str, Any]) -> str:
    """把每个标注的 chatHistory / previousRuns 拼成待压测文本。"""
    parts: list[str] = []
    for item in context.get("annotations") or []:
        annotation = _annotation_of(item)
        thread = annotation.get("threadContext") or {}
        for message in thread.get("chatHistory") or []:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "")
            content = str(message.get("content") or "").strip()
            if content:
                parts.append(f"{role}: {content}")
        for run in thread.get("previousRuns") or []:
            if isinstance(run, dict):
                payload = json.dumps(run, ensure_ascii=False, default=str)
            else:
                payload = str(run)
            parts.append(f"run: {payload[:500]}")
    return "\n".join(parts)


def compress_annotation_context(
    context: dict[str, Any],
    summarize: SummarizeFn,
    *,
    threshold: int,
    max_chars: int,
) -> CompressionResult:
    """超过阈值时压缩 UI 标注历史；失败或收益为负时保持原样。"""
    raw = collect_annotation_history(context)
    tokens_before = estimate_tokens(raw)
    if tokens_before <= threshold:
        return CompressionResult(context, False, tokens_before, tokens_before)

    try:
        summary = (summarize(raw, max_chars) or "").strip()
    except Exception:  # noqa: BLE001 - 压缩失败不能阻断 Run
        summary = ""
    if not summary:
        summary = fallback_summary(raw, max_chars)
    if not summary:
        return CompressionResult(context, False, tokens_before, tokens_before)

    compressed = copy.deepcopy(context)
    for item in compressed.get("annotations") or []:
        annotation = _annotation_of(item)
        thread = annotation.setdefault("threadContext", {})
        thread["historySummary"] = summary
        thread["chatHistory"] = [
            {"role": "system", "content": f"历史摘要（已压缩）：{summary}"}
        ]
        thread["previousRuns"] = []

    tokens_after = estimate_tokens(collect_annotation_history(compressed))
    if tokens_after >= tokens_before:
        # 摘要比原文还长时不值得替换
        return CompressionResult(context, False, tokens_before, tokens_before)
    return CompressionResult(compressed, True, tokens_before, tokens_after, summary)


# ------------------------------------------------------------
# Architect Graph
# ------------------------------------------------------------
def collect_architect_history(history: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        content = str(item.get("content") or "").strip()
        if content:
            parts.append(f"{role}: {content}")
    return "\n".join(parts)


def compress_architect_history(
    history: list[dict[str, Any]],
    summarize: SummarizeFn,
    *,
    threshold: int,
    max_chars: int,
) -> CompressionResult:
    """超过阈值时把 Architect 的 ui_history 压缩为一条摘要。"""
    raw = collect_architect_history(history)
    tokens_before = estimate_tokens(raw)
    if tokens_before <= threshold:
        return CompressionResult(history, False, tokens_before, tokens_before)

    try:
        summary = (summarize(raw, max_chars) or "").strip()
    except Exception:  # noqa: BLE001 - 压缩失败不能阻断 Run
        summary = ""
    if not summary:
        summary = fallback_summary(raw, max_chars)
    if not summary:
        return CompressionResult(history, False, tokens_before, tokens_before)

    compressed = [{"role": "system", "content": f"历史摘要（已压缩）：{summary}"}]
    tokens_after = estimate_tokens(collect_architect_history(compressed))
    return CompressionResult(compressed, True, tokens_before, tokens_after, summary)
