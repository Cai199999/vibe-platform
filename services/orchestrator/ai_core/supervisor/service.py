"""Supervisor 服务：路由决策 + 真实子图派发。

路由与派发分离：
  - ``route`` 只返回计划，适合前端先展示“我准备怎么做”；
  - ``dispatch`` 才启动子图，返回独立 SSE 路径。

子图服务通过 Dispatcher 注入，便于单元测试；生产实现只做参数映射，
不复制 UI / Architect 的任何业务逻辑。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from ai_core.agent.schemas import ExecuteRunRequest
from ai_core.architect.schemas import ArchitectRunRequest
from ai_core.supervisor.graph import SupervisorGraphRunner
from ai_core.supervisor.registry import AgentDescriptor, AgentRegistry
from ai_core.supervisor.schemas import (
    AgentHandoff,
    AgentListResponse,
    SubGraphDispatch,
    SupervisorDispatchResponse,
    SupervisorRequest,
    SupervisorRouteResponse,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# 领域异常
# ------------------------------------------------------------
class SupervisorServiceError(Exception):
    code = "SUPERVISOR_ERROR"
    status_code = 409


class SupervisorNotDispatchableError(SupervisorServiceError):
    code = "SUPERVISOR_NOT_DISPATCHABLE"
    status_code = 422


class SupervisorUiContextRequiredError(SupervisorServiceError):
    code = "SUPERVISOR_UI_CONTEXT_REQUIRED"
    status_code = 422


class SupervisorDispatchFailedError(SupervisorServiceError):
    code = "SUPERVISOR_DISPATCH_FAILED"
    status_code = 503


# ------------------------------------------------------------
# 子图派发协议
# ------------------------------------------------------------
@dataclass(frozen=True)
class SubGraphHandle:
    agent_id: str
    run_id: str
    stream_path: str


class SubGraphDispatcher(Protocol):
    agent_id: str

    def start(
        self,
        request: SupervisorRequest,
        descriptor: AgentDescriptor,
    ) -> SubGraphHandle:
        """启动子图并返回其 Run 句柄。"""


class ArchitectSubGraphDispatcher:
    """把统一请求映射为 ArchitectRunRequest，交给既有 ArchitectService。"""

    agent_id = "architect_graph"

    def __init__(
        self,
        service_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._service_factory = service_factory

    def _new_service(self) -> Any:  # noqa: ANN401 - 延迟导入避免启动耦合
        if self._service_factory is not None:
            return self._service_factory()
        from ai_core.architect.service import ArchitectService

        return ArchitectService()

    def start(
        self,
        request: SupervisorRequest,
        descriptor: AgentDescriptor,
    ) -> SubGraphHandle:
        source = request.source.model_copy(deep=True)
        if not source.sandbox_id and request.sandbox_id:
            source.sandbox_id = request.sandbox_id
        if not source.files and request.files:
            source.files = dict(request.files)
        architect_request = ArchitectRunRequest(
            project_id=request.project_id,
            project_name=request.project_name,
            source=source,
            ui_history=list(request.ui_history),
            options=request.options.architect,
        )
        active = self._new_service().start_stream(architect_request)
        return SubGraphHandle(
            agent_id=descriptor.agent_id,
            run_id=active.run_id,
            stream_path=(f"/api/v1/architect/runs/{active.run_id}/events/stream"),
        )


class UiSubGraphDispatcher:
    """把统一请求映射为 ExecuteRunRequest，交给既有 RunExecutionService。"""

    agent_id = "ui_graph"

    def __init__(
        self,
        service_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._service_factory = service_factory

    def _new_service(self) -> Any:  # noqa: ANN401 - 延迟导入避免启动耦合
        if self._service_factory is not None:
            return self._service_factory()
        from ai_core.agent.service import RunExecutionService

        return RunExecutionService()

    def start(
        self,
        request: SupervisorRequest,
        descriptor: AgentDescriptor,
    ) -> SubGraphHandle:
        if not request.annotations:
            raise SupervisorUiContextRequiredError(
                "派发 UI Graph 至少需要 1 个画布标注；"
                "请先圈选元素或显式选择全局架构设计"
            )
        ui_request = ExecuteRunRequest(
            tenant_id=request.tenant_id,
            project_id=request.project_id,
            annotations=list(request.annotations),
            sandbox_id=request.sandbox_id,
            files=dict(request.files),
            options=request.options.ui,
        )
        active = self._new_service().start_stream(ui_request)
        return SubGraphHandle(
            agent_id=descriptor.agent_id,
            run_id=active.run_id,
            stream_path=f"/api/v1/runs/{active.run_id}/events/stream",
        )


def build_default_dispatchers() -> dict[str, SubGraphDispatcher]:
    return {
        "architect_graph": ArchitectSubGraphDispatcher(),
        "ui_graph": UiSubGraphDispatcher(),
    }


# ------------------------------------------------------------
# SupervisorService
# ------------------------------------------------------------
class SupervisorService:
    def __init__(
        self,
        registry: AgentRegistry,
        graph: SupervisorGraphRunner | None = None,
        dispatchers: dict[str, SubGraphDispatcher] | None = None,
    ) -> None:
        self._registry = registry
        self._graph = graph or SupervisorGraphRunner(registry)
        self._dispatchers = (
            dispatchers if dispatchers is not None else build_default_dispatchers()
        )

    # --------------------------------------------------------
    # 路由
    # --------------------------------------------------------
    def route(self, request: SupervisorRequest) -> SupervisorRouteResponse:
        trace_id = f"sup_{uuid.uuid4().hex[:20]}"
        outcome = self._graph.run(request)
        return SupervisorRouteResponse(
            trace_id=trace_id,
            project_id=request.project_id,
            decision=outcome.decision,
            handoffs=outcome.handoffs,
            registry_version=self._registry.version,
        )

    # --------------------------------------------------------
    # 派发
    # --------------------------------------------------------
    def dispatch(self, request: SupervisorRequest) -> SupervisorDispatchResponse:
        route = self.route(request)
        decision = route.decision
        if not decision.dispatchable:
            if "annotations" in decision.missing_inputs:
                raise SupervisorUiContextRequiredError(
                    "目标包含 UI Graph，但请求没有画布标注"
                )
            raise SupervisorNotDispatchableError(
                "当前路由不可派发: "
                + (", ".join(decision.missing_inputs) or decision.reason)
            )

        dispatches: list[SubGraphDispatch] = []
        for handoff in route.handoffs:
            dispatch = self._dispatch_one(request, handoff)
            dispatches.append(dispatch)
        return SupervisorDispatchResponse(
            trace_id=route.trace_id,
            decision=decision,
            dispatches=dispatches,
            started_at=datetime.now(timezone.utc),
        )

    def _dispatch_one(
        self,
        request: SupervisorRequest,
        handoff: AgentHandoff,
    ) -> SubGraphDispatch:
        dispatcher = self._dispatchers.get(handoff.agent_id)
        if dispatcher is None:
            raise SupervisorNotDispatchableError(
                f"Agent 尚未接入运行时: {handoff.agent_id}"
            )
        descriptor = self._registry.require_routable(handoff.agent_id)
        try:
            handle = dispatcher.start(request, descriptor)
        except SupervisorServiceError:
            raise
        except Exception as exc:  # noqa: BLE001 - 子图启动错误统一映射
            logger.exception(
                "supervisor dispatch failed agent=%s",
                handoff.agent_id,
            )
            raise SupervisorDispatchFailedError(
                f"启动子图失败（{handoff.agent_id}）: {exc}"
            ) from exc
        logger.info(
            "supervisor dispatched agent=%s run_id=%s",
            handle.agent_id,
            handle.run_id,
        )
        return SubGraphDispatch(
            agent_id=handle.agent_id,
            run_id=handle.run_id,
            stream_path=handle.stream_path,
            order=handoff.order,
            status="STARTED",
        )

    # --------------------------------------------------------
    # 注册中心
    # --------------------------------------------------------
    def list_agents(self) -> AgentListResponse:
        return AgentListResponse(
            agents=self._registry.to_views(),
            registry_version=self._registry.version,
            custom_agents_dir=self._registry.custom_agents_dir,
        )
