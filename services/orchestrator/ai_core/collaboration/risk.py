"""高危操作判定。

原型只做确定性阈值判断，不依赖模型。真实接入时阈值来自配置中心。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CATEGORIES = frozenset({"consistency", "security", "scope", "cost", "data"})


class RiskError(ValueError):
    """风险参数非法。"""


@dataclass(frozen=True)
class RiskDecision:
    category: str
    level: str
    decision: str
    reason: str
    alternatives: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return self.decision in {"reject", "regenerate", "ask_user"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "level": self.level,
            "decision": self.decision,
            "reason": self.reason,
            "alternatives": list(self.alternatives),
        }


class RiskGate:
    """按类别执行保守策略：宁可拒绝，不静默继续。"""

    def __init__(
        self,
        *,
        max_pages: int = 12,
        max_entities: int = 10,
        max_scope_growth: float = 0.3,
    ) -> None:
        self.max_pages = max(1, int(max_pages))
        self.max_entities = max(1, int(max_entities))
        self.max_scope_growth = max(0.0, float(max_scope_growth))

    def evaluate(
        self,
        category: str,
        metrics: dict[str, Any] | None = None,
    ) -> RiskDecision:
        if category not in CATEGORIES:
            raise RiskError(f"未知风险类别: {category}")
        data = dict(metrics or {})

        if category == "consistency":
            errors = int(data.get("errors", 0))
            if errors > 0:
                return RiskDecision(
                    category=category,
                    level="high",
                    decision="regenerate",
                    reason=f"检测到 {errors} 处契约或代码不一致",
                    alternatives=("重新生成契约", "回到 Architect 复核目标"),
                )
            return _allow(category)

        if category == "security":
            issues = int(data.get("issues", 0))
            if issues > 0:
                return RiskDecision(
                    category=category,
                    level="high",
                    decision="reject",
                    reason=f"检测到 {issues} 项安全风险",
                    alternatives=("移除风险操作", "改用平台受控能力"),
                )
            return _allow(category)

        if category == "scope":
            pages = int(data.get("pages", 0))
            entities = int(data.get("entities", 0))
            growth = float(data.get("growth", 0.0))
            if pages > self.max_pages or entities > self.max_entities:
                return RiskDecision(
                    category=category,
                    level="high",
                    decision="ask_user",
                    reason=(
                        f"范围超出默认阈值：页面 {pages}/{self.max_pages}，"
                        f"实体 {entities}/{self.max_entities}"
                    ),
                    alternatives=("缩小首期范围", "拆分为多个阶段"),
                )
            if growth > self.max_scope_growth:
                return RiskDecision(
                    category=category,
                    level="high",
                    decision="ask_user",
                    reason=f"需求增长 {growth:.0%}，超过 {self.max_scope_growth:.0%}",
                    alternatives=("确认新增范围", "延后到下一版本"),
                )
            return _allow(category)

        if category == "cost":
            cost = float(data.get("cost", 0.0))
            budget = data.get("budget")
            if budget is not None and cost > float(budget):
                return RiskDecision(
                    category=category,
                    level="high",
                    decision="reject",
                    reason=f"预计成本 {cost:.2f} 超出预算 {float(budget):.2f}",
                    alternatives=("降低模型档位", "拆分任务后重试"),
                )
            return _allow(category)

        if data.get("destructive"):
            return RiskDecision(
                category="data",
                level="high",
                decision="reject",
                reason="包含不可逆的数据操作",
                alternatives=("改为可回滚迁移", "人工确认后执行"),
            )
        return _allow("data")


def _allow(category: str) -> RiskDecision:
    return RiskDecision(
        category=category,
        level="low",
        decision="allow",
        reason="未触发高危阈值",
    )


__all__ = ["CATEGORIES", "RiskDecision", "RiskError", "RiskGate"]
