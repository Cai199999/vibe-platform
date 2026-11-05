"""Supervisor 意图分类器。

当前实现为确定性规则，优势是离线可测、无模型成本、行为可解释。
``IntentClassifier`` 协议为后续接入 LLM 分类器保留替换点：
Supervisor Graph / AgentRegistry / Handoff 协议都不需要改变。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from ai_core.supervisor.registry import (
    ARCHITECT_KEYWORDS,
    UI_KEYWORDS,
    AgentRegistry,
)
from ai_core.supervisor.schemas import SupervisorRequest

# 组合意图的额外信号：用户明确要求“同时 / 两者 / 先...再...”。
COMBINED_KEYWORDS: tuple[str, ...] = (
    "同时",
    "两者",
    "都要",
    "一起",
    "先改",
    "再规划",
    "既",
    "并且",
    "外加",
    "再加上",
)


@dataclass(frozen=True)
class IntentPrediction:
    """一次分类结果，供 Graph 写入决策。"""

    task_type: str
    target: str
    confidence: float
    reason: str
    signals: list[str] = field(default_factory=list)
    dispatchable: bool = True
    missing_inputs: list[str] = field(default_factory=list)
    strategy: str = "rule"


class IntentClassifier(Protocol):
    """分类器协议：可替换为 LLM 分类器而不改 Supervisor Graph。"""

    def classify(
        self,
        request: SupervisorRequest,
        registry: AgentRegistry,
    ) -> IntentPrediction:
        """返回结构化意图预测。"""


def _contains_term(message: str, term: str) -> bool:
    """中文按子串匹配；短英文按单词边界匹配，避免 api 命中 rapid。"""
    if not term:
        return False
    if term.isascii() and term.isalpha() and len(term) <= 8:
        return (
            re.search(
                rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])",
                message,
            )
            is not None
        )
    return term in message


def _match_terms(
    message: str,
    terms: tuple[str, ...],
    *,
    prefix: str,
) -> list[str]:
    return [f"{prefix}:{term}" for term in terms if _contains_term(message, term)]


def _ui_dispatchability(
    request: SupervisorRequest,
) -> tuple[bool, list[str]]:
    if request.annotations:
        return True, []
    return False, ["annotations"]


class RuleBasedIntentClassifier:
    """确定性意图分类器。

    优先级：
      1. targetAgent 显式指定；
      2. scopeHint 显式覆盖；
      3. UI + 架构关键词 → both；
      4. 架构关键词 → architect；
      5. 标注 / UI 关键词 → UI；
      6. 无有效信号 → rejected。
    """

    def classify(
        self,
        request: SupervisorRequest,
        registry: AgentRegistry,
    ) -> IntentPrediction:
        # ----------------------------------------------------
        # 1. 显式 Agent 优先：企业 CustomAgent / 指定专家
        # ----------------------------------------------------
        explicit_agent = request.options.target_agent
        if explicit_agent:
            descriptor = registry.require_routable(explicit_agent)
            if descriptor.status == "custom":
                # Manifest 已注册，但运行时尚未接入：只返回路由计划。
                return IntentPrediction(
                    task_type=(
                        descriptor.task_types[0] if descriptor.task_types else "custom"
                    ),
                    target=descriptor.agent_id,
                    confidence=0.99,
                    reason=f"用户显式指定企业 Agent: {descriptor.display_name}",
                    signals=[f"explicit:{descriptor.agent_id}"],
                    dispatchable=False,
                    missing_inputs=["customAgentRuntime"],
                    strategy="explicit_agent",
                )
            return IntentPrediction(
                task_type=(
                    descriptor.task_types[0]
                    if descriptor.task_types
                    else "explicit_agent"
                ),
                target=descriptor.agent_id,
                confidence=0.99,
                reason=f"用户显式指定 Agent: {descriptor.display_name}",
                signals=[f"explicit:{descriptor.agent_id}"],
                strategy="explicit_agent",
            )

        # ----------------------------------------------------
        # 2. 显式 scopeHint 覆盖关键词规则
        # ----------------------------------------------------
        hint = request.options.scope_hint
        dispatchable, missing = _ui_dispatchability(request)
        if hint == "ui":
            return IntentPrediction(
                task_type="ui_local_edit",
                target="ui_graph",
                confidence=0.97,
                reason="用户显式指定局部 UI 修改",
                signals=["scope:ui"],
                dispatchable=dispatchable,
                missing_inputs=missing,
                strategy="explicit_scope",
            )
        if hint == "architect":
            return IntentPrediction(
                task_type="global_architecture",
                target="architect_graph",
                confidence=0.97,
                reason="用户显式指定全局架构设计",
                signals=["scope:architect"],
                strategy="explicit_scope",
            )
        if hint == "both":
            return IntentPrediction(
                task_type="combined",
                target="both",
                confidence=0.97,
                reason="用户显式要求同时进行 UI 修改与全局架构设计",
                signals=["scope:both"],
                dispatchable=dispatchable,
                missing_inputs=missing,
                strategy="explicit_scope",
            )

        # ----------------------------------------------------
        # 3. 关键词规则
        # ----------------------------------------------------
        message = request.message.casefold().strip()
        ui_signals = _match_terms(message, UI_KEYWORDS, prefix="ui")
        arch_signals = _match_terms(message, ARCHITECT_KEYWORDS, prefix="arch")
        combined_hits = [
            f"combined:{term}"
            for term in COMBINED_KEYWORDS
            if _contains_term(message, term)
        ]
        has_annotations = bool(request.annotations)

        if ui_signals and arch_signals:
            return IntentPrediction(
                task_type="combined",
                target="both",
                confidence=0.88,
                reason="同时命中局部 UI 与全局架构信号",
                signals=[*ui_signals, *arch_signals, *combined_hits],
                dispatchable=dispatchable,
                missing_inputs=missing,
            )

        if arch_signals:
            # 即使带标注，只要没有 UI 关键词，也优先视为全局架构设计，
            # 避免把“设计整个后端”误当成局部修改。
            return IntentPrediction(
                task_type="global_architecture",
                target="architect_graph",
                confidence=0.90,
                reason="命中全局架构 / 全栈关键词",
                signals=arch_signals,
            )

        if ui_signals or has_annotations:
            if has_annotations and message:
                confidence = 0.92
                reason = "命中局部 UI 关键词且提供了画布标注"
            elif has_annotations:
                confidence = 0.84
                reason = "提供了画布标注，按局部 UI 修改处理"
            else:
                confidence = 0.72
                reason = "命中局部 UI 关键词，但缺少画布标注"
            return IntentPrediction(
                task_type="ui_local_edit",
                target="ui_graph",
                confidence=confidence,
                reason=reason,
                signals=[*(ui_signals or ["annotation:present"]), *combined_hits],
                dispatchable=dispatchable,
                missing_inputs=missing,
            )

        # ----------------------------------------------------
        # 4. 无有效信号：不猜测，要求用户补上下文
        # ----------------------------------------------------
        return IntentPrediction(
            task_type="unknown",
            target="none",
            confidence=0.10 if message else 0.0,
            reason=("未识别到明确的局部 UI 或全局架构意图，请补充指令或画布标注"),
            signals=["no_intent_signal"],
            dispatchable=False,
            missing_inputs=["明确意图", "annotations"],
        )


def build_default_classifier() -> IntentClassifier:
    return RuleBasedIntentClassifier()
