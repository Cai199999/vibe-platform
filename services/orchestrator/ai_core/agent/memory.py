"""对话历史分层压缩与意图分级。

目标：多轮对话超过预算后，不再把完整 chatHistory / previousRuns 传给模型。

压缩策略（按优先级）：
  1. 用 tiktoken 精确计算 Token（不可用时回退到 CJK 密度估算）；
  2. 最近 N 轮（默认 10，1 轮 = user + assistant）原文保留；
  3. 更早的消息交给 FAST 模型摘要；
  4. 旧消息先经过轻量意图分级：高优（需求变更 / 约束 / 禁止项）逐字保留，
     低优（闲聊 / 重复）合并进摘要。

local_summary.md / global_summary.md / user_profile.md 的落盘由
``ai_core.context_memory`` 负责，本模块只处理「进入 Prompt 的上下文」。
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from ai_core.config import Settings, get_settings

# CJK 标点与全角字符也按高 token 密度估算
_CJK_RE = re.compile(r"[　-〿㐀-䶿一-鿿＀-￯]")

# summarize(text, max_chars) -> 摘要
SummarizeFn = Callable[[str, int], str]

# tiktoken 编码器按名称缓存；导入失败时全部回退到字符密度估算。
_ENCODERS: dict[str, Any] = {}
_TIKTOKEN_FAILED = False


# ------------------------------------------------------------
# Token 计算
# ------------------------------------------------------------
def count_tokens(text: str, encoding: str = "cl100k_base") -> int:
    """优先用 tiktoken 计算 Token；不可用时回退到保守估算。

    回退口径：CJK 字符约 0.7 token，其余字符约 0.25 token（4 字符 / token）。
    这是成本控制用的近似，不用于计费对账。
    """
    if not text:
        return 0
    global _TIKTOKEN_FAILED
    if not _TIKTOKEN_FAILED:
        try:
            encoder = _ENCODERS.get(encoding)
            if encoder is None:
                import tiktoken  # 延迟导入：离线环境 / 精简镜像可缺省

                encoder = tiktoken.get_encoding(encoding)
                _ENCODERS[encoding] = encoder
            return max(1, len(encoder.encode(str(text))))
        except Exception:  # noqa: BLE001 - 缺依赖或首次下载失败
            _TIKTOKEN_FAILED = True
    cjk = len(_CJK_RE.findall(text))
    other = max(0, len(text) - cjk)
    return max(1, math.ceil(cjk * 0.7 + other / 4))


def estimate_tokens(text: str) -> int:
    """兼容旧调用名。"""
    return count_tokens(text)


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
# 意图分级
# ------------------------------------------------------------
_HIGH_PRIORITY_HINTS = (
    "需求",
    "要求",
    "必须",
    "禁止",
    "不要",
    "不能",
    "约束",
    "改为",
    "改成",
    "换成",
    "统一",
    "保持",
    "记得",
    "注意",
    "需求变更",
    "现在改",
    "重新",
    "always",
    "never",
    "must",
    "should not",
    "constraint",
)

_LOW_PRIORITY_HINTS = ("谢谢", "好的", "可以", "嗯", "ok", "继续", "收到")


def heuristic_high_priority(text: str) -> bool:
    """确定性兜底：包含需求变更 / 约束关键词即视为高优。"""
    value = str(text or "").strip().lower()
    if not value:
        return False
    if any(token in value for token in _HIGH_PRIORITY_HINTS):
        return True
    # 明确闲聊且无约束词：低优；其余默认低优（交给摘要模型合并）。
    # 不能因为“消息很长”就判为高优，否则长需求会把压缩收益吃光。
    return False


class IntentClassifier:
    """用轻量模型给历史用户消息打「高优 / 低优」标签。

    一次调用批量分类，避免每条消息都产生一次网络请求；模型失败或没有
    密钥时回退到关键词规则。返回与输入等长的布尔数组（True = 高优）。
    """

    SYSTEM = (
        "你是对话意图分类器。对每条用户消息输出 high 或 low：\n"
        "- high：需求变更、技术约束、禁止事项、必须保留的决策；\n"
        "- low：寒暄、确认、重复、与当前任务无关的闲聊。\n"
        '只输出 JSON：{"labels":["high","low",...]}，顺序与输入一致。'
    )

    def __init__(
        self,
        settings: Settings | None = None,
        client: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client

    def _get_client(self) -> Any | None:
        if self._client is not None:
            return self._client
        if not self.settings.deepseek_api_key:
            return None
        try:
            from ai_core.adapters.model.deepseek import DeepSeekClient

            self._client = DeepSeekClient(self.settings)
        except Exception:  # noqa: BLE001 - 分类失败不能阻断压缩
            return None
        return self._client

    def classify(self, texts: list[str]) -> list[bool]:
        values = [str(item or "").strip()[:500] for item in texts]
        if not values:
            return []
        fallback = [heuristic_high_priority(item) for item in values]
        client = self._get_client()
        if client is None:
            return fallback
        try:
            payload = "\n".join(
                f"{index + 1}. {text}" for index, text in enumerate(values)
            )
            raw = client.generate(
                "intent",
                self.SYSTEM,
                f"用户消息：\n{payload}",
            )
            labels = _parse_labels(str(raw or ""), len(values))
            if labels is None:
                return fallback
            return labels
        except Exception:  # noqa: BLE001 - 退回关键词规则
            return fallback


def _parse_labels(raw: str, expected: int) -> list[bool] | None:
    """解析模型返回的标签；结构不符时返回 None 触发规则回退。"""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None

    labels: Iterable[Any]
    if isinstance(payload, dict):
        labels = payload.get("labels") or payload.get("priorities") or []
    elif isinstance(payload, list):
        labels = payload
    else:
        return None
    result: list[bool] = []
    for item in list(labels)[:expected]:
        if isinstance(item, dict):
            item = item.get("priority") or item.get("label")
        result.append(str(item).strip().lower() in {"high", "高", "高优", "1", "true"})
    if len(result) != expected:
        return None
    return result


# ------------------------------------------------------------
# 压缩结果
# ------------------------------------------------------------
@dataclass
class CompressionResult:
    """一次历史压缩的结果。"""

    value: Any
    compressed: bool
    tokens_before: int
    tokens_after: int
    summary: str = ""


# ------------------------------------------------------------
# UI Graph
# ------------------------------------------------------------
def _annotation_of(item: Any) -> dict[str, Any]:
    """兼容 AnnotationEnvelope 包装与裸标注。"""
    if isinstance(item, dict) and isinstance(item.get("annotation"), dict):
        return item["annotation"]
    return item if isinstance(item, dict) else {}


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    role = str(message.get("role") or "")
    content = str(message.get("content") or "").strip()
    return f"{role}: {content}" if content else ""


def _run_text(run: Any) -> str:
    if isinstance(run, dict):
        payload = json.dumps(run, ensure_ascii=False, default=str)
    else:
        payload = str(run)
    return f"run: {payload[:500]}"


def collect_annotation_history(context: dict[str, Any]) -> str:
    """把每个标注的摘要 / chatHistory / previousRuns 拼成待压缩文本。"""
    parts: list[str] = []
    for item in context.get("annotations") or []:
        annotation = _annotation_of(item)
        thread = annotation.get("threadContext") or {}
        summary = str(thread.get("historySummary") or "").strip()
        if summary:
            parts.append(f"historySummary: {summary}")
        for message in thread.get("chatHistory") or []:
            text = _message_text(message)
            if text:
                parts.append(text)
        for run in thread.get("previousRuns") or []:
            parts.append(_run_text(run))
    return "\n".join(parts)


def _classify_older_messages(
    older_messages: list[tuple[int, int, dict[str, Any]]],
    classifier: IntentClassifier | None,
) -> dict[tuple[int, int], bool]:
    """批量给旧消息中的 user 消息打标，返回 {(thread_idx, msg_idx): high}。"""
    user_items = [
        (thread_index, msg_index, message)
        for thread_index, msg_index, message in older_messages
        if str(message.get("role") or "") == "user"
        and str(message.get("content") or "").strip()
    ]
    if not user_items:
        return {}
    texts = [str(message.get("content") or "") for _, _, message in user_items]
    if classifier is None:
        labels = [heuristic_high_priority(text) for text in texts]
    else:
        labels = classifier.classify(texts)
        if len(labels) != len(texts):
            labels = [heuristic_high_priority(text) for text in texts]
    return {
        (thread_index, msg_index): bool(label)
        for (thread_index, msg_index, _), label in zip(user_items, labels)
    }


def _summarize_old(
    summarize: SummarizeFn,
    text: str,
    max_chars: int,
) -> str:
    if not text.strip():
        return ""
    try:
        summary = (summarize(text, max_chars) or "").strip()
    except Exception:  # noqa: BLE001 - 压缩失败不能阻断 Run
        summary = ""
    if not summary:
        summary = fallback_summary(text, max_chars)
    return summary[:max_chars]


def compress_annotation_context(
    context: dict[str, Any],
    summarize: SummarizeFn,
    *,
    threshold: int,
    max_chars: int,
    recent_turns: int = 10,
    classifier: IntentClassifier | None = None,
    encoding: str = "cl100k_base",
) -> CompressionResult:
    """超过阈值时分层压缩 UI 标注历史；失败或收益为负时保持原样。

    每个标注独立保留最近 ``recent_turns * 2`` 条消息原文；更早消息按意图
    分级：高优用户消息逐字写入摘要块，低优消息交给摘要模型。previousRuns
    只保留最近 2 条，其余折叠进摘要。
    """
    raw = collect_annotation_history(context)
    tokens_before = count_tokens(raw, encoding)
    if tokens_before <= threshold:
        return CompressionResult(context, False, tokens_before, tokens_before)

    recent_count = max(0, int(recent_turns)) * 2
    threads: list[tuple[int, dict[str, Any], list[dict[str, Any]]]] = []
    older_candidates: list[tuple[int, int, dict[str, Any]]] = []
    for thread_index, item in enumerate(context.get("annotations") or []):
        annotation = _annotation_of(item)
        thread = annotation.get("threadContext") or {}
        messages = [
            message
            for message in (thread.get("chatHistory") or [])
            if isinstance(message, dict)
        ]
        split = max(0, len(messages) - recent_count)
        older = messages[:split]
        threads.append((thread_index, annotation, messages))
        older_candidates.extend(
            (thread_index, index, message) for index, message in enumerate(older)
        )

    priorities = _classify_older_messages(older_candidates, classifier)

    compressed = copy.deepcopy(context)
    summaries: list[str] = []
    for thread_index, item in enumerate(compressed.get("annotations") or []):
        annotation = _annotation_of(item)
        thread = annotation.setdefault("threadContext", {})
        messages = [
            message
            for message in (thread.get("chatHistory") or [])
            if isinstance(message, dict)
        ]
        split = max(0, len(messages) - recent_count)
        older = messages[:split]
        recent = messages[split:]

        existing_summary = str(thread.get("historySummary") or "").strip()
        summary_parts: list[str] = []
        if existing_summary:
            summary_parts.append(existing_summary)
        high_priority: list[str] = []
        for index, message in enumerate(older):
            role = str(message.get("role") or "")
            content = str(message.get("content") or "").strip()
            if not content:
                continue
            if role == "user" and priorities.get((thread_index, index)):
                high_priority.append(content)
                continue
            summary_parts.append(_message_text(message))

        runs = [run for run in (thread.get("previousRuns") or []) if run]
        # 有旧对话时保留最近 2 条 Run 原文；若整个 thread 只有 2 条消息，
        # 旧对话为空，此前 previousRuns 仍可能很长，必须一并折叠进摘要，
        # 否则超过阈值却压不下去。
        keep_run_count = 2 if recent_count > 0 and split > 0 else 0
        if keep_run_count:
            old_runs, kept_runs = runs[:-keep_run_count], runs[-keep_run_count:]
        else:
            old_runs, kept_runs = runs, []
        summary_parts.extend(_run_text(run) for run in old_runs)

        summary = _summarize_old(summarize, "\n".join(summary_parts), max_chars)
        if summary or high_priority:
            block = "历史摘要（已压缩）：" + (summary or "（无）")
            if high_priority:
                # 高优消息最多保留 8 条，避免摘要块自己膨胀。
                block += "\n用户明确要求（逐字保留，优先级高于摘要）：\n" + "\n".join(
                    f"- {item}" for item in high_priority[:8]
                )
            new_history: list[dict[str, Any]] = [{"role": "system", "content": block}]
            new_history.extend(recent)
            thread["chatHistory"] = new_history
            thread["historySummary"] = summary
            thread["previousRuns"] = kept_runs
            summaries.append(summary)
        elif recent:
            # 没有可摘要内容（消息不足）时至少保留最近消息。
            thread["chatHistory"] = recent
            thread["previousRuns"] = kept_runs

    tokens_after = count_tokens(collect_annotation_history(compressed), encoding)
    if tokens_after >= tokens_before:
        # 摘要比原文还长时不值得替换
        return CompressionResult(context, False, tokens_before, tokens_before)
    return CompressionResult(
        compressed,
        True,
        tokens_before,
        tokens_after,
        "\n".join(item for item in summaries if item),
    )


# ------------------------------------------------------------
# Architect Graph
# ------------------------------------------------------------
def collect_architect_history(history: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in history:
        text = _message_text(item)
        if text:
            parts.append(text)
    return "\n".join(parts)


def compress_architect_history(
    history: list[dict[str, Any]],
    summarize: SummarizeFn,
    *,
    threshold: int,
    max_chars: int,
    recent_turns: int = 10,
    classifier: IntentClassifier | None = None,
    encoding: str = "cl100k_base",
) -> CompressionResult:
    """超过阈值时分层压缩 Architect 的 ui_history。

    与 UI Graph 相同：最近 N 轮原文保留，旧消息按高优 / 低优分级；高优
    用户消息逐字保留，其余合并为一条 system 摘要。
    """
    raw = collect_architect_history(history)
    tokens_before = count_tokens(raw, encoding)
    if tokens_before <= threshold:
        return CompressionResult(history, False, tokens_before, tokens_before)

    recent_count = max(0, int(recent_turns)) * 2
    split = max(0, len(history) - recent_count)
    older = [item for item in history[:split] if isinstance(item, dict)]
    recent = [item for item in history[split:] if isinstance(item, dict)]

    priorities = _classify_older_messages(
        [(0, index, message) for index, message in enumerate(older)],
        classifier,
    )
    high_priority: list[str] = []
    summary_parts: list[str] = []
    for index, message in enumerate(older):
        role = str(message.get("role") or "")
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        if role == "user" and priorities.get((0, index)):
            high_priority.append(content)
            continue
        summary_parts.append(_message_text(message))

    summary = _summarize_old(summarize, "\n".join(summary_parts), max_chars)
    compressed: list[dict[str, Any]] = []
    if summary or high_priority:
        block = "历史摘要（已压缩）：" + (summary or "（无）")
        if high_priority:
            block += "\n用户明确要求（逐字保留）：\n" + "\n".join(
                f"- {item}" for item in high_priority[:8]
            )
        compressed.append({"role": "system", "content": block})
    compressed.extend(recent)

    tokens_after = count_tokens(collect_architect_history(compressed), encoding)
    if tokens_after >= tokens_before:
        return CompressionResult(history, False, tokens_before, tokens_before)
    return CompressionResult(
        compressed,
        True,
        tokens_before,
        tokens_after,
        summary,
    )


__all__ = [
    "CompressionResult",
    "IntentClassifier",
    "collect_annotation_history",
    "collect_architect_history",
    "compress_annotation_context",
    "compress_architect_history",
    "count_tokens",
    "estimate_tokens",
    "fallback_summary",
    "heuristic_high_priority",
]
