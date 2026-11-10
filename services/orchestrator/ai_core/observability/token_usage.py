"""LLM Token 用量与成本核算。

设计目标：

1. **零侵入**：模型适配器（``DeepSeekClient``）在每次真实调用后调用
   :func:`record_model_response`，业务节点不需要感知 SDK 的 usage 结构；
2. **按节点归因**：LangGraph 节点执行期间通过 :func:`collect_usage`
   激活一个采集器，节点结束后读取本次调用的 token / 费用；
3. **可配置价格**：价格默认使用 DeepSeek 官方人民币目录价，允许通过
   ``LLM_PRICING`` 环境变量按模型覆盖，避免价格调整后硬编码失真。

usage 字段兼容两条链路：

- LangChain ``AIMessage.usage_metadata``（langchain-openai >= 0.3）；
- OpenAI 兼容网关的 ``response_metadata.token_usage``（含 DeepSeek 的
  ``prompt_cache_hit_tokens``）。

任何采集异常都必须被吞掉：成本统计不能影响主流程。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# 默认价格：单位「元 / 100 万 tokens」。
# 来源为 DeepSeek 官方目录价（人民币），仅作为本地核算默认值；
# 生产环境请通过 LLM_PRICING 按实际账单配置。
DEFAULT_MODEL_PRICING: dict[str, dict[str, float]] = {
    "deepseek-chat": {"input": 2.0, "cached_input": 0.5, "output": 8.0},
    "deepseek-reasoner": {"input": 4.0, "cached_input": 1.0, "output": 16.0},
}

DEFAULT_CURRENCY = "CNY"


@dataclass
class TokenUsage:
    """一次调用 / 一个节点 / 整次 Run 的 token 汇总。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    calls: int = 0
    cost: float = 0.0
    currency: str = DEFAULT_CURRENCY

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: "TokenUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.calls += other.calls
        self.cost += other.cost
        # 同一进程内通常只有一种计价货币；若混用，保留后写入者并告警。
        if other.calls and other.currency:
            if self.calls > other.calls and self.currency != other.currency:
                logger.warning(
                    "token usage currency mismatch: %s vs %s",
                    self.currency,
                    other.currency,
                )
            self.currency = other.currency

    def as_dict(self) -> dict[str, Any]:
        """前端 / 报告使用的 camelCase 结构。"""
        return {
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "cachedInputTokens": self.cached_input_tokens,
            "totalTokens": self.total_tokens,
            "calls": self.calls,
            "cost": round(self.cost, 6),
            "currency": self.currency,
        }


@dataclass
class UsageCollector:
    """节点级采集器；只保存聚合值，避免把每次调用的明细写进事件。"""

    usage: TokenUsage = field(default_factory=TokenUsage)

    def record(self, usage: TokenUsage) -> None:
        self.usage.add(usage)

    def snapshot(self) -> TokenUsage:
        """返回副本，防止调用方后续修改污染采集器。"""
        current = self.usage
        return TokenUsage(
            input_tokens=current.input_tokens,
            output_tokens=current.output_tokens,
            cached_input_tokens=current.cached_input_tokens,
            calls=current.calls,
            cost=current.cost,
            currency=current.currency,
        )


# ContextVar 在 LangGraph 的同步节点调用链里天然隔离并发 Run：
# 不同请求运行在各自线程 / 上下文中，不会串账。
_CURRENT_COLLECTOR: ContextVar[UsageCollector | None] = ContextVar(
    "llm_usage_collector",
    default=None,
)


@contextmanager
def collect_usage(collector: UsageCollector | None = None) -> Iterator[UsageCollector]:
    """在 ``with`` 作用域内把模型调用计入采集器。"""
    active = collector or UsageCollector()
    token = _CURRENT_COLLECTOR.set(active)
    try:
        yield active
    finally:
        _CURRENT_COLLECTOR.reset(token)


def record_model_response(
    *,
    task: str,
    model: str,
    response: Any,
    pricing: dict[str, dict[str, float]] | None = None,
    currency: str = DEFAULT_CURRENCY,
) -> TokenUsage | None:
    """从 LangChain 响应中提取 usage 并计入当前采集器。

    没有激活的采集器时直接返回 ``None``（例如 Architect 独立调用），
    保证该函数可以在任何调用路径安全使用。
    """
    collector = _CURRENT_COLLECTOR.get()
    if collector is None:
        return None
    try:
        usage = extract_usage(response, model=model, pricing=pricing, currency=currency)
    except Exception:  # noqa: BLE001 - 统计失败绝不影响模型结果
        logger.warning(
            "failed to extract token usage task=%s model=%s", task, model, exc_info=True
        )
        return None
    if usage.calls == 0:
        return None
    collector.record(usage)
    logger.debug(
        "llm usage task=%s model=%s input=%s output=%s cached=%s cost=%.6f %s",
        task,
        model,
        usage.input_tokens,
        usage.output_tokens,
        usage.cached_input_tokens,
        usage.cost,
        usage.currency,
    )
    return usage


def extract_usage(
    response: Any,
    *,
    model: str,
    pricing: dict[str, dict[str, float]] | None = None,
    currency: str = DEFAULT_CURRENCY,
) -> TokenUsage:
    """兼容 LangChain 新 / 旧两种 usage 结构，统一换算成本。"""
    input_tokens, output_tokens, cached_tokens = _read_usage_numbers(response)
    price = resolve_pricing(model, pricing)
    billable_input = max(0, input_tokens - cached_tokens)
    cost = (
        billable_input * price.get("input", 0.0)
        + cached_tokens * price.get("cached_input", price.get("input", 0.0))
        + output_tokens * price.get("output", 0.0)
    ) / 1_000_000.0
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_tokens,
        calls=1,
        cost=cost,
        currency=currency,
    )


def resolve_pricing(
    model: str,
    overrides: dict[str, dict[str, float]] | None = None,
) -> dict[str, float]:
    """按「精确匹配 > 最长前缀匹配 > default」解析模型价格。"""
    table: dict[str, dict[str, float]] = {
        key: dict(value) for key, value in DEFAULT_MODEL_PRICING.items()
    }
    for key, value in (overrides or {}).items():
        if isinstance(value, dict):
            table[key] = {**table.get(key, {}), **value}

    if model in table:
        return table[model]
    for key in sorted(table, key=len, reverse=True):
        if key != "default" and model.startswith(key):
            return table[key]
    return table.get("default", {"input": 0.0, "cached_input": 0.0, "output": 0.0})


def _read_usage_numbers(response: Any) -> tuple[int, int, int]:
    """返回 ``(input, output, cached_input)``；字段缺失按 0 处理。"""
    usage_metadata = getattr(response, "usage_metadata", None)
    if isinstance(usage_metadata, dict) and usage_metadata:
        input_tokens = _as_int(usage_metadata.get("input_tokens"))
        output_tokens = _as_int(usage_metadata.get("output_tokens"))
        details = usage_metadata.get("input_token_details")
        cached = 0
        if isinstance(details, dict):
            cached = _as_int(
                details.get("cache_read")
                or details.get("cached")
                or details.get("cache_read_input_tokens")
            )
        return input_tokens, output_tokens, cached

    metadata = getattr(response, "response_metadata", None) or {}
    token_usage: dict[str, Any] = {}
    if isinstance(metadata, dict):
        raw = metadata.get("token_usage") or metadata.get("usage") or {}
        if isinstance(raw, dict):
            token_usage = raw
    return (
        _as_int(token_usage.get("prompt_tokens") or token_usage.get("input_tokens")),
        _as_int(
            token_usage.get("completion_tokens") or token_usage.get("output_tokens")
        ),
        _as_int(
            token_usage.get("prompt_cache_hit_tokens")
            or token_usage.get("cached_tokens")
        ),
    )


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "DEFAULT_CURRENCY",
    "DEFAULT_MODEL_PRICING",
    "TokenUsage",
    "UsageCollector",
    "collect_usage",
    "extract_usage",
    "record_model_response",
    "resolve_pricing",
]
