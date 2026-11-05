"""Supervisor 内部 API：路由、派发与 AgentRegistry 查询。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ai_core.supervisor.gates import SupervisorGateError
from ai_core.supervisor.registry import (
    AgentNotAvailableError,
    AgentNotFoundError,
    get_agent_registry,
    reload_agent_registry,
)
from ai_core.supervisor.schemas import (
    AgentListResponse,
    SupervisorDispatchResponse,
    SupervisorGateResponse,
    SupervisorRequest,
    SupervisorRouteResponse,
)
from ai_core.supervisor.service import (
    SupervisorService,
    SupervisorServiceError,
)

router = APIRouter(prefix="/api/v1/internal/supervisor", tags=["supervisor"])


@router.post("/route", response_model=SupervisorRouteResponse)
def route_request(request: SupervisorRequest) -> SupervisorRouteResponse:
    """只做意图路由，不启动任何子图。"""
    service = SupervisorService(get_agent_registry())
    try:
        return service.route(request)
    except Exception as exc:  # noqa: BLE001 - 统一映射领域异常
        _raise_http(exc)


@router.post(
    "/dispatch",
    response_model=SupervisorDispatchResponse,
    status_code=202,
)
def dispatch_request(request: SupervisorRequest) -> SupervisorDispatchResponse:
    """按路由计划启动子图，返回子 Run 与 SSE 订阅路径。"""
    service = SupervisorService(get_agent_registry())
    try:
        return service.dispatch(request)
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)


@router.get("/gates/{gate_id}", response_model=SupervisorGateResponse)
def get_gate(gate_id: str) -> SupervisorGateResponse:
    """查询 both 门控；Architect 取消 / 失败会自动转为 FAILED。"""
    service = SupervisorService(get_agent_registry())
    try:
        return service.get_gate(gate_id)
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)


@router.post("/gates/{gate_id}/release", response_model=SupervisorGateResponse)
def release_gate(gate_id: str) -> SupervisorGateResponse:
    """契约确认后放行 UI Graph；重复调用幂等，返回同一个 UI Run。"""
    service = SupervisorService(get_agent_registry())
    try:
        return service.release_gate(gate_id)
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)


@router.get("/agents", response_model=AgentListResponse)
def list_agents() -> AgentListResponse:
    """列出内置、预留与企业 Manifest Agent。"""
    service = SupervisorService(get_agent_registry())
    return service.list_agents()


@router.post("/agents/reload", response_model=AgentListResponse)
def reload_agents() -> AgentListResponse:
    """重新扫描内置 Agent Manifest 与企业 CustomAgent Manifest。

    仅限内部网络调用；Manifest 只做声明解析，不允许执行代码，也不会
    覆盖已注册的内置 Agent。
    """
    registry = reload_agent_registry()
    return SupervisorService(registry).list_agents()


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, AgentNotFoundError):
        raise HTTPException(
            status_code=404,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, AgentNotAvailableError):
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, SupervisorGateError):
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, SupervisorServiceError):
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    raise HTTPException(
        status_code=500,
        detail={
            "code": "SUPERVISOR_INTERNAL_ERROR",
            "message": "Supervisor 处理失败",
        },
    ) from exc
