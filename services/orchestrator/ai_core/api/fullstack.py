"""全栈生成内部 API：启动 SSE、重放、快照。

公开入口由 Java BFF 暴露为 ``POST /api/v1/runs/fullstack``；
前端不需要直接访问本文件的 internal 路径。
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from ai_core.agent.events import RunEventBuffer, peer_event_buffer
from ai_core.api.errors import sandbox_error_to_http
from ai_core.domain.fullstack import FullstackRunRequest, FullstackRunSnapshot
from ai_core.error_envelope import stream_event_frames
from ai_core.fullstack.service import (
    FullstackService,
    FullstackServiceError,
)
from ai_core.observability.fullstack_metrics import snapshot_fullstack_metrics
from ai_core.ports.sandbox import SandboxError

router = APIRouter(prefix="/api/v1/internal/fullstack", tags=["fullstack"])

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post("/runs/stream")
def start_fullstack_stream(request: FullstackRunRequest) -> StreamingResponse:
    """启动全栈生成并返回 SSE。

    流开始前的契约 / 沙盒错误返回 4xx / 5xx JSON；开始后的生成错误
    通过 ``fullstack.failed`` 事件送达，HTTP 仍为 200。
    """
    service = FullstackService()
    try:
        active = service.start_stream(request)
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)
    return _sse_response(active.buffer, after_seq=0)


@router.get("/runs/{run_id}/events/stream")
def stream_fullstack_events(
    run_id: str,
    after_seq: int = Query(default=0, ge=0, alias="afterSeq"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """回放 ``seq > afterSeq`` 后继续订阅；终态事件后关闭连接。"""
    buffer = peer_event_buffer(run_id)
    if buffer is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "FULLSTACK_RUN_NOT_FOUND",
                "message": f"Fullstack Run 事件流不存在或已过期: {run_id}",
            },
        )
    effective_after = after_seq
    if effective_after <= 0 and last_event_id:
        try:
            effective_after = max(0, int(last_event_id))
        except ValueError:
            effective_after = 0
    return _sse_response(buffer, after_seq=effective_after)


@router.get("/runs/{run_id}", response_model=FullstackRunSnapshot)
def get_fullstack_run(run_id: str) -> FullstackRunSnapshot:
    service = FullstackService()
    try:
        return service.snapshot(run_id)
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)


@router.get("/metrics")
def get_fullstack_metrics() -> dict[str, object]:
    """四个核心指标的进程内聚合快照。

    只暴露计数与比率，不包含源码 / run_id，供监控系统周期抓取。
    """
    return snapshot_fullstack_metrics()


# ------------------------------------------------------------
# 内部工具
# ------------------------------------------------------------
async def _event_frames(buffer: RunEventBuffer, after_seq: int):
    async for frame in stream_event_frames(
        buffer,
        after_seq,
        terminal_event_type="fullstack.failed",
    ):
        yield frame


def _sse_response(buffer: RunEventBuffer, *, after_seq: int) -> StreamingResponse:
    return StreamingResponse(
        _event_frames(buffer, after_seq),
        media_type="text/event-stream; charset=utf-8",
        headers=SSE_HEADERS,
    )


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, FullstackServiceError):
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, SandboxError):
        raise sandbox_error_to_http(exc) from exc
    raise exc


__all__ = ["router"]
