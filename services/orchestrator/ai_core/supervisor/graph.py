"""Supervisor Graph：顶层意图路由与 Sub-Graph Handoff。

图本身不做任何代码生成，也不持有子图的 checkpoint。它只输出：
  - ``SupervisorDecision``：分类、目标、置信度、可派发性；
  - ``AgentHandoff[]``：有序的子图交接计划。

这样 UI Graph / Architect Graph 可以独立演进，Supervisor 只依赖
``AgentRegistry`` 的能力声明。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ai_core.supervisor.registry import (
    AgentDescriptor,
    AgentRegistry,
    AgentRegistryError,
)
from ai_core.supervisor.router import (
    IntentClassifier,
    IntentPrediction,
    build_default_classifier,
)
from ai_core.supervisor.schemas import (
    AgentHandoff,
    SupervisorDecision,
    SupervisorRequest,
)

logger = logging.getLogger(__name__)


class SupervisorState(TypedDict, total=False):
    """Supervisor 的轻量路由状态。"""

    request: SupervisorRequest
    prediction: IntentPrediction
    decision: dict[str, Any]
    handoffs: list[dict[str, Any]]
    status: str
    error: str


@dataclass(frozen=True)
class SupervisorGraphOutcome:
    decision: SupervisorDecision
    handoffs: list[AgentHandoff] = field(default_factory=list)
    status: str = "ROUTED"


class SupervisorGraphRunner:
    """LangGraph Supervisor 状态机。"""

    def __init__(
        self,
        registry: AgentRegistry,
        classifier: IntentClassifier | None = None,
        *,
        recursion_limit: int = 10,
    ) -> None:
        self._registry = registry
        self._classifier = classifier or build_default_classifier()
        self._recursion_limit = recursion_limit
        self._graph = self._build_graph()

    # --------------------------------------------------------
    # 构图
    # --------------------------------------------------------
    def _build_graph(self):
        builder = StateGraph(SupervisorState)
        builder.add_node("classify_intent", self._classify_intent)
        builder.add_node("resolve_agents", self._resolve_agents)
        builder.add_node("handoff_ui", self._handoff_ui)
        builder.add_node("handoff_architect", self._handoff_architect)
        builder.add_node("handoff_both", self._handoff_both)
        builder.add_node("handoff_custom", self._handoff_custom)
        builder.add_node("rejected", self._rejected)
        builder.add_node("finalize", self._finalize)

        builder.add_edge(START, "classify_intent")
        builder.add_edge("classify_intent", "resolve_agents")
        builder.add_conditional_edges(
            "resolve_agents",
            self._route_after_resolve,
            {
                "ui": "handoff_ui",
                "architect": "handoff_architect",
                "both": "handoff_both",
                "custom": "handoff_custom",
                "rejected": "rejected",
            },
        )
        for node in (
            "handoff_ui",
            "handoff_architect",
            "handoff_both",
            "handoff_custom",
            "rejected",
        ):
            builder.add_edge(node, "finalize")
        builder.add_edge("finalize", END)
        return builder.compile()

    # --------------------------------------------------------
    # 节点
    # --------------------------------------------------------
    def _classify_intent(self, state: SupervisorState) -> dict[str, Any]:
        request = state["request"]
        prediction = self._classifier.classify(request, self._registry)
        return {
            "prediction": prediction,
            "status": "CLASSIFYING",
        }

    def _resolve_agents(self, state: SupervisorState) -> dict[str, Any]:
        request = state["request"]
        prediction = state["prediction"]

        if prediction.target == "none":
            return {
                "decision": self._empty_decision(
                    prediction,
                    reason=prediction.reason,
                ).model_dump(),
                "handoffs": [],
                "status": "REJECTED",
            }

        try:
            descriptors = self._resolve_descriptors(prediction)
        except AgentRegistryError as exc:
            logger.warning("supervisor resolve failed: %s", exc)
            return {
                "decision": self._empty_decision(
                    prediction,
                    reason=f"{prediction.reason}；{exc}",
                ).model_dump(),
                "handoffs": [],
                "status": "REJECTED",
                "error": str(exc),
            }

        handoffs = [
            AgentHandoff(
                agent_id=item.agent_id,
                display_name=item.display_name,
                graph=item.graph,
                order=index,
                stream_path_template=item.stream_path_template,
            )
            for index, item in enumerate(descriptors, start=1)
        ]

        dispatchable = bool(prediction.dispatchable)
        missing_inputs = list(prediction.missing_inputs)
        if not all(item.status == "available" for item in descriptors):
            dispatchable = False
            if "customAgentRuntime" not in missing_inputs:
                missing_inputs.append("customAgentRuntime")

        decision = SupervisorDecision(
            task_type=prediction.task_type,
            target=prediction.target,
            targets=[item.agent_id for item in descriptors],
            confidence=prediction.confidence,
            reason=prediction.reason,
            signals=list(prediction.signals),
            dispatchable=dispatchable,
            missing_inputs=missing_inputs,
            strategy=prediction.strategy,
        )
        return {
            "decision": decision.model_dump(),
            "handoffs": [item.model_dump() for item in handoffs],
            "status": "ROUTED",
        }

    def _handoff_ui(self, state: SupervisorState) -> dict[str, Any]:
        return state

    def _handoff_architect(self, state: SupervisorState) -> dict[str, Any]:
        return state

    def _handoff_both(self, state: SupervisorState) -> dict[str, Any]:
        return state

    def _handoff_custom(self, state: SupervisorState) -> dict[str, Any]:
        return state

    def _rejected(self, state: SupervisorState) -> dict[str, Any]:
        return {"status": "REJECTED"}

    def _finalize(self, state: SupervisorState) -> dict[str, Any]:
        return state

    def _route_after_resolve(self, state: SupervisorState) -> str:
        if state.get("status") == "REJECTED":
            return "rejected"
        prediction = state["prediction"]
        if prediction.target == "both":
            return "both"
        if prediction.target == "ui_graph":
            return "ui"
        if prediction.target == "architect_graph":
            return "architect"
        return "custom"

    # --------------------------------------------------------
    # 工具
    # --------------------------------------------------------
    def _resolve_descriptors(
        self,
        prediction: IntentPrediction,
    ) -> list[AgentDescriptor]:
        if prediction.target == "both":
            # 固定顺序：Architect 先冻结契约上下文，UI 再执行局部修改。
            ids = ["architect_graph", "ui_graph"]
            return [self._registry.require_routable(item) for item in ids]
        return [self._registry.require_routable(prediction.target)]

    @staticmethod
    def _empty_decision(
        prediction: IntentPrediction,
        *,
        reason: str,
    ) -> SupervisorDecision:
        return SupervisorDecision(
            task_type=prediction.task_type,
            target="none",
            targets=[],
            confidence=prediction.confidence,
            reason=reason,
            signals=list(prediction.signals),
            dispatchable=False,
            missing_inputs=list(prediction.missing_inputs),
            strategy=prediction.strategy,
        )

    # --------------------------------------------------------
    # 执行入口
    # --------------------------------------------------------
    def run(self, request: SupervisorRequest) -> SupervisorGraphOutcome:
        state: SupervisorState = {
            "request": request,
            "status": "STARTED",
        }
        result = self._graph.invoke(
            state,
            {"recursion_limit": self._recursion_limit},
        )
        decision = SupervisorDecision.model_validate(result["decision"])
        handoffs = [
            AgentHandoff.model_validate(item) for item in result.get("handoffs", [])
        ]
        return SupervisorGraphOutcome(
            decision=decision,
            handoffs=handoffs,
            status=result.get("status", "ROUTED"),
        )
