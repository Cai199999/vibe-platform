"""Direct Ops 内部 API。

同步 JSON 接口：Direct Ops 的目标是低延迟，不需要 SSE 连接。
事件仍写入 ``direct.*`` 缓冲区，供后续 Observability / HITL 使用。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ai_core.direct_ops.service import DirectActionError, DirectOperationService
from ai_core.domain.direct_ops import DirectActionPayload, DirectActionResponse

router = APIRouter(prefix="/api/v1/internal/direct-ops", tags=["direct-ops"])


@router.get("/revision")
def get_revision(projectId: str, sandboxId: str) -> dict[str, str]:
    """读取当前主工作树 HEAD，供前端填写 baseRevision。"""
    service = DirectOperationService()
    try:
        return service.revision(projectId, sandboxId)
    except DirectActionError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": exc.code,
                "message": str(exc),
                **(exc.diagnostic or {}),
            },
        ) from exc


@router.post("/analyze-delete")
def analyze_delete(request: DirectActionPayload) -> dict:
    """DELETE_NODE 前置静态分析：只返回安全判定，不删除。"""
    service = DirectOperationService()
    try:
        return service.analyze_delete(request)
    except DirectActionError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": exc.code,
                "message": str(exc),
                **(exc.diagnostic or {}),
            },
        ) from exc


@router.post("/actions", response_model=DirectActionResponse)
def execute_direct_action(
    request: DirectActionPayload,
) -> DirectActionResponse:
    """执行一次确定性画布操作；Phase 1 仅 UPDATE_TEXT 真正落盘。"""
    service = DirectOperationService()
    try:
        return service.execute(request)
    except DirectActionError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": exc.code,
                "message": str(exc),
                **(exc.diagnostic or {}),
            },
        ) from exc


__all__ = ["router"]
