"""Run 执行内部 API：同步兼容 + SSE 流式 + HITL 澄清。"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from docker.errors import DockerException

from ai_core.agent.events import RunEventBuffer, peer_event_buffer
from ai_core.agent.llm import AgentLLMConfigurationError
from ai_core.agent.schemas import (
    ClarificationAnswerResponse,
    ClarificationAnswersRequest,
    ClarificationListResponse,
    ExecuteRunRequest,
    ExecuteRunResponse,
    RollbackRunRequest,
    RollbackRunResponse,
    RunSnapshotResponse,
)
from ai_core.agent.service import (
    RunExecutionService,
    RunServiceError,
    TargetFileNotFoundError,
)
from ai_core.api.errors import sandbox_error_to_http
from ai_core.sandbox.base import SandboxError

router = APIRouter(prefix="/api/v1/internal/runs", tags=["runs"])

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


# ------------------------------------------------------------
# 同步接口（保留）
# ------------------------------------------------------------
@router.post("/execute", response_model=ExecuteRunResponse)
def execute_run(request: ExecuteRunRequest) -> ExecuteRunResponse:
    """同步执行一次单 Agent 闭环，供旧客户端与回归测试使用。"""
    service = RunExecutionService()
    try:
        return service.execute(request)
    except Exception as exc:  # noqa: BLE001 - 统一映射领域异常
        _raise_http(exc)


# ------------------------------------------------------------
# SSE 接口
# ------------------------------------------------------------
@router.post("/execute/stream")
def execute_run_stream(request: ExecuteRunRequest) -> StreamingResponse:
    """启动 Run 并直接返回 SSE。

    流开始前的校验错误返回 4xx JSON；开始后业务错误通过 ``run.failed``
    事件返回，HTTP 仍为 200。
    """
    service = RunExecutionService()
    try:
        active = service.start_stream(request)
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)
    return _sse_response(active.buffer, after_seq=0)


@router.get("/{run_id}/events/stream")
def stream_run_events(
    run_id: str,
    after_seq: int = Query(default=0, ge=0, alias="afterSeq"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """回放 ``seq > afterSeq`` 后继续订阅；终态事件到达后关闭连接。"""
    buffer = peer_event_buffer(run_id)
    if buffer is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "RUN_NOT_FOUND",
                "message": f"Run 事件流不存在或已过期: {run_id}",
            },
        )
    effective_after = after_seq
    if effective_after <= 0 and last_event_id:
        try:
            effective_after = max(0, int(last_event_id))
        except ValueError:
            effective_after = 0
    return _sse_response(buffer, after_seq=effective_after)


# ------------------------------------------------------------
# HITL 澄清
# ------------------------------------------------------------
@router.post(
    "/{run_id}/clarifications",
    response_model=ClarificationAnswerResponse,
    status_code=202,
)
def answer_clarification(
    run_id: str,
    request: ClarificationAnswersRequest,
) -> ClarificationAnswerResponse:
    """提交澄清答案并后台恢复状态机，返回 202。

    前端随后用 ``lastSeq`` 订阅 ``GET /{runId}/events/stream`` 继续时间轴。
    """
    service = RunExecutionService()
    try:
        return service.answer_clarification(run_id, request.clarification_id, request)
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)


@router.post(
    "/{run_id}/clarifications/{clarification_id}/answers",
    response_model=ClarificationAnswerResponse,
    status_code=202,
)
def answer_clarification_by_path(
    run_id: str,
    clarification_id: str,
    request: ClarificationAnswersRequest,
) -> ClarificationAnswerResponse:
    """与 03.API 的公开路径语义对齐的内部版本。"""
    service = RunExecutionService()
    try:
        return service.answer_clarification(run_id, clarification_id, request)
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)


@router.get(
    "/{run_id}/clarifications",
    response_model=ClarificationListResponse,
)
def list_clarifications(run_id: str) -> ClarificationListResponse:
    service = RunExecutionService()
    try:
        return service.list_clarifications(run_id)
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)


@router.get("/{run_id}", response_model=RunSnapshotResponse)
def get_run(run_id: str) -> RunSnapshotResponse:
    service = RunExecutionService()
    try:
        return service.snapshot(run_id)
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)


# ------------------------------------------------------------
# 回滚（不变）
# ------------------------------------------------------------
@router.post("/rollback", response_model=RollbackRunResponse)
def rollback_run(request: RollbackRunRequest) -> RollbackRunResponse:
    """把沙盒代码与单个标注 thread 回滚到指定成功轮次。"""
    service = RunExecutionService()
    try:
        return service.rollback(request)
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)


# ------------------------------------------------------------
# 内部工具
# ------------------------------------------------------------
async def _event_frames(buffer: RunEventBuffer, after_seq: int):
    # SSE 客户端重连间隔；实际重连仍由前端控制。
    yield "retry: 3000\n\n"
    async for event in buffer.subscribe(after_seq):
        if event is None:
            # 心跳注释不占用 seq，也不会触发客户端 onmessage。
            yield ": ping\n\n"
        else:
            yield event.to_sse()


def _sse_response(buffer: RunEventBuffer, *, after_seq: int) -> StreamingResponse:
    return StreamingResponse(
        _event_frames(buffer, after_seq),
        media_type="text/event-stream; charset=utf-8",
        headers=SSE_HEADERS,
    )


def _raise_http(exc: Exception) -> None:
    """把服务层异常统一映射为 HTTPException；未知异常继续抛出。"""
    if isinstance(exc, RunServiceError):
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, TargetFileNotFoundError):
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, AgentLLMConfigurationError):
        raise HTTPException(
            status_code=422,
            detail={"code": "LLM_NOT_CONFIGURED", "message": str(exc)},
        ) from exc
    if isinstance(exc, SandboxError):
        raise sandbox_error_to_http(exc) from exc
    if isinstance(exc, DockerException):
        raise HTTPException(
            status_code=503,
            detail={"code": "SANDBOX_DOCKER_UNAVAILABLE", "message": str(exc)},
        ) from exc
    raise exc
