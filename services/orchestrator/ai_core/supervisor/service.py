"""Supervisor 服务：路由决策 + 真实子图派发。

路由与派发分离：
  - ``route`` 只返回计划，适合前端先展示“我准备怎么做”；
  - ``dispatch`` 才启动子图，返回独立 SSE 路径。

子图服务通过 Dispatcher 注入，便于单元测试；生产实现只做参数映射，
不复制 UI / Architect 的任何业务逻辑。
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from ai_core.domain.agent import ExecuteRunRequest
from ai_core.domain.architect import ArchitectRunRequest
from ai_core.supervisor.gates import (
    GateRecord,
    SupervisorGateRegistry,
    SupervisorGateStateError,
    get_gate_registry,
)
from ai_core.supervisor.graph import SupervisorGraphRunner
from ai_core.supervisor.registry import AgentDescriptor, AgentRegistry
from ai_core.domain.supervisor import (
    AgentHandoff,
    AgentListResponse,
    SubGraphDispatch,
    SupervisorDispatchResponse,
    SupervisorGateResponse,
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


class SupervisorContractNotConfirmedError(SupervisorServiceError):
    code = "SUPERVISOR_CONTRACT_NOT_CONFIRMED"
    status_code = 409


class SupervisorGateAbortedError(SupervisorServiceError):
    code = "SUPERVISOR_GATE_ABORTED"
    status_code = 409


def _get_architect_run(run_id: str) -> Any:  # noqa: ANN401 - 延迟导入避免启动耦合
    from ai_core.architect.service import get_active_architect_run

    return get_active_architect_run(run_id)


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
        gate_registry: SupervisorGateRegistry | None = None,
        architect_run_getter: Callable[[str], Any] | None = None,
    ) -> None:
        self._registry = registry
        self._graph = graph or SupervisorGraphRunner(registry)
        self._dispatchers = (
            dispatchers if dispatchers is not None else build_default_dispatchers()
        )
        self._gates = gate_registry or get_gate_registry()
        self._architect_run_getter = architect_run_getter or _get_architect_run

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

        # 乱序保护：项目存在待确认的 both 门控时，禁止任何新的子图启动。
        self._reconcile_project(request.project_id)
        if decision.handoff == "handoff_both":
            return self._dispatch_both(request, route)

        self._gates.ensure_project_available(request.project_id)
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

    def _dispatch_both(
        self,
        request: SupervisorRequest,
        route: SupervisorRouteResponse,
    ) -> SupervisorDispatchResponse:
        """both：只启动 Architect，UI 被门控锁在契约确认之后。

        先预留 project 级锁，再启动 Architect；预留期间其它请求无法绕过
        门控启动 UI。Architect 启动成功后才落门控，失败则释放预留。
        """
        reservation = self._gates.reserve_project(request.project_id)
        try:
            architect_handoff = next(
                item for item in route.handoffs if item.agent_id == "architect_graph"
            )
            architect_dispatch = self._dispatch_one(request, architect_handoff)
        except Exception:
            self._gates.release_reservation(request.project_id, reservation)
            raise

        try:
            gate = self._gates.create(
                request.project_id,
                architect_dispatch.run_id,
                request,
                reservation=reservation,
            )
        except Exception:
            # 门控落库失败时不能留下孤儿 Architect；尽力取消后释放锁。
            self._best_effort_cancel_architect(architect_dispatch.run_id)
            raise

        logger.info(
            "supervisor both gated gate_id=%s project_id=%s architect_run_id=%s",
            gate.gate_id,
            gate.project_id,
            gate.architect_run_id,
        )
        return SupervisorDispatchResponse(
            trace_id=route.trace_id,
            decision=route.decision,
            dispatches=[architect_dispatch],
            gate=gate.to_view(),
            started_at=datetime.now(timezone.utc),
        )

    # --------------------------------------------------------
    # both 门控释放
    # --------------------------------------------------------
    def get_gate(self, gate_id: str) -> SupervisorGateResponse:
        gate = self._gates.get(gate_id)
        self._reconcile_gate(gate)
        gate = self._gates.get(gate_id)
        dispatches = [gate.ui_dispatch] if gate.ui_dispatch else []
        return SupervisorGateResponse(gate=gate.to_view(), dispatches=dispatches)

    def release_gate(self, gate_id: str) -> SupervisorGateResponse:
        """校验契约后启动 UI；重复 release 返回同一个 UI Run。"""
        gate = self._gates.get(gate_id)
        self._reconcile_gate(gate)
        gate = self._gates.get(gate_id)

        if gate.status == "RELEASED":
            return SupervisorGateResponse(
                gate=gate.to_view(),
                dispatches=[gate.ui_dispatch] if gate.ui_dispatch else [],
            )
        self._assert_contract_confirmed(gate)

        # 抢占释放权：并发 release 只有一个能真正启动 UI。
        gate = self._gates.begin_release(gate_id)
        handoff = self._ui_handoff(order=2)
        try:
            ui_dispatch = self._dispatch_one(gate.request, handoff)
        except Exception as exc:
            self._gates.fail(gate_id, f"启动 UI Graph 失败: {exc}")
            logger.exception("supervisor gate release failed gate_id=%s", gate_id)
            raise

        gate = self._gates.complete_release(gate_id, ui_dispatch)
        logger.info(
            "supervisor gate released gate_id=%s ui_run_id=%s",
            gate_id,
            ui_dispatch.run_id,
        )
        return SupervisorGateResponse(
            gate=gate.to_view(),
            dispatches=[ui_dispatch],
        )

    def _assert_contract_confirmed(self, gate: GateRecord) -> None:
        if gate.status == "FAILED":
            raise SupervisorGateAbortedError(
                f"门控已失败，不能释放: {gate.message or '架构澄清已中止'}"
            )
        if gate.status == "RELEASING":
            raise SupervisorGateStateError("门控正在释放，请稍后查询状态")

        # architect.contract.confirmed 事件先于服务端 active.status 落定，
        # 客户端收到事件后立刻 release 时给一个短暂收敛窗口，避免竞态误报。
        deadline = time.monotonic() + 2.0
        while True:
            active = self._architect_run_getter(gate.architect_run_id)
            if active is None:
                message = "Architect Run 已不存在（服务可能重启）"
                self._gates.fail(gate.gate_id, message)
                raise SupervisorGateAbortedError(message)
            status = getattr(active, "status", None)
            contract = getattr(active, "contract", None)
            contract_status = getattr(contract, "status", None)
            if status == "CONFIRMED" and contract_status == "CONFIRMED":
                return
            if status in {"CANCELLED", "CANCELLING", "FAILED"}:
                message = f"Architect 已处于 {status}，门控中止"
                self._gates.fail(gate.gate_id, message)
                raise SupervisorGateAbortedError(message)
            if status == "CONFIRMING_CONTRACT" and time.monotonic() < deadline:
                time.sleep(0.05)
                continue
            raise SupervisorContractNotConfirmedError(
                "契约尚未确认，必须先完成 architect.contract.confirmed"
            )

    def _reconcile_project(self, project_id: str) -> None:
        gate = self._gates.active_for_project(project_id)
        if gate is not None:
            self._reconcile_gate(gate)

    def _reconcile_gate(self, gate: GateRecord) -> None:
        """Architect 被取消 / 失败 / 丢失时自动将门控置为 FAILED。"""
        if not gate.active:
            return
        active = self._architect_run_getter(gate.architect_run_id)
        if active is None:
            self._gates.fail(gate.gate_id, "Architect Run 已不存在")
            return
        status = getattr(active, "status", None)
        if status in {"CANCELLED", "CANCELLING", "FAILED"}:
            # CANCELLING 表示取消请求已受理但执行线程尚未退出；此时契约
            # 已不可能再确认，必须立即中止门控，否则会误报“契约未确认”。
            self._gates.fail(gate.gate_id, f"Architect 已处于 {status}")

    def _ui_handoff(self, *, order: int) -> AgentHandoff:
        descriptor = self._registry.require_routable("ui_graph")
        return AgentHandoff(
            agent_id=descriptor.agent_id,
            display_name=descriptor.display_name,
            graph=descriptor.graph,
            order=order,
            stream_path_template=descriptor.stream_path_template,
        )

    @staticmethod
    def _best_effort_cancel_architect(run_id: str) -> None:
        try:
            from ai_core.domain.architect import ArchitectCancelRequest
            from ai_core.architect.service import ArchitectService

            ArchitectService().cancel(
                run_id,
                ArchitectCancelRequest(reason="supervisor gate create failed"),
            )
        except Exception:  # noqa: BLE001 - 清理失败不覆盖原始异常
            logger.warning("failed to cancel orphan architect run %s", run_id)

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
            skill_registry_version=self._registry.skill_registry.version,
            skills=self._registry.to_skill_views(),
            custom_agents_dir=self._registry.custom_agents_dir,
        )
