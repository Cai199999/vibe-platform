"""Architect 内部 API：启动 SSE、澄清回答、契约决策、取消与快照。"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from ai_core.agent.events import RunEventBuffer, get_persisted_event_buffer
from ai_core.architect.document_classifier import classify_document_reuse
from ai_core.architect.llm import ArchitectLLMConfigurationError
from ai_core.error_envelope import stream_event_frames
from ai_core.domain.architect import (
    ArchitectAnswerResponse,
    ArchitectAnswersRequest,
    ArchitectCancelRequest,
    ArchitectCancelResponse,
    ArchitectConflictDecisionRequest,
    ArchitectConflictDecisionResponse,
    ArchitectDecisionRequest,
    ArchitectDecisionResponse,
    ArchitectHelperRequest,
    ArchitectHelperResponse,
    ArchitectRunRequest,
    ArchitectSnapshotResponse,
    DocumentClassifyRequest,
    DocumentClassifyResponse,
    SolutionDiscoveryRequest,
    SolutionDiscoveryResponse,
)
from ai_core.architect.discovery import discover_solutions
from ai_core.architect.service import (
    ArchitectService,
    ArchitectServiceError,
    get_active_architect_run,
)
from ai_core.ports.rag import RAGProviderError

router = APIRouter(prefix="/api/v1/internal/architect", tags=["architect"])

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post("/runs/stream")
def start_architect_stream(request: ArchitectRunRequest) -> StreamingResponse:
    """启动 Architect Run 并立即返回 SSE。"""
    service = ArchitectService()
    try:
        active = service.start_stream(request)
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)
    return _sse_response(active.buffer, after_seq=0)


@router.post("/documents/classify", response_model=DocumentClassifyResponse)
def classify_document(request: DocumentClassifyRequest) -> DocumentClassifyResponse:
    """判断上传文档是长期复用还是一次性；不写入任何持久存储。"""
    decision = classify_document_reuse(
        request.name,
        request.content,
        project_summary=request.project_summary,
        llm_provider=request.llm_provider,
    )
    return DocumentClassifyResponse(
        decision=decision.decision,
        reuse=decision.reuse,
        reason=decision.reason,
        confidence=decision.confidence,
        source=decision.source,
        signals=decision.signals,
    )


@router.post(
    "/solutions/discover",
    response_model=SolutionDiscoveryResponse,
)
def discover_solution_options(
    request: SolutionDiscoveryRequest,
) -> SolutionDiscoveryResponse:
    """澄清前的方案侦察：需求分析 + 联网/本地复用检索 + 自研方案。

    只读接口：不创建 Run、不写项目状态。检索失败会降级为自研方案，
    由用户在 UI 中显式选择后再启动正式 Architect 流程。
    """
    try:
        return discover_solutions(request)
    except Exception as exc:  # noqa: BLE001 - 统一错误信封
        _raise_http(exc)


@router.get("/runs/{run_id}/events/stream")
def stream_architect_events(
    run_id: str,
    after_seq: int = Query(default=0, ge=0, alias="afterSeq"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    active = get_active_architect_run(run_id)
    if active is not None:
        buffer = active.buffer
    else:
        buffer = get_persisted_event_buffer(
            run_id,
            terminal_type="architect.failed",
        )
    if buffer is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "ARCHITECT_RUN_NOT_FOUND",
                "message": f"Architect Run 不存在或已过期: {run_id}",
            },
        )
    effective_after = after_seq
    if effective_after <= 0 and last_event_id:
        try:
            effective_after = max(0, int(last_event_id))
        except ValueError:
            effective_after = 0
    return _sse_response(buffer, after_seq=effective_after)


@router.post(
    "/runs/{run_id}/clarifications/{clarification_id}/answers",
    response_model=ArchitectAnswerResponse,
    status_code=202,
)
def answer_architect_clarification(
    run_id: str,
    clarification_id: str,
    request: ArchitectAnswersRequest,
) -> ArchitectAnswerResponse:
    service = ArchitectService()
    try:
        return service.answer_clarification(run_id, clarification_id, request)
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)


@router.post(
    "/runs/{run_id}/contracts/{contract_id}/decision",
    response_model=ArchitectDecisionResponse,
    status_code=202,
)
def decide_architect_contract(
    run_id: str,
    contract_id: str,
    request: ArchitectDecisionRequest,
) -> ArchitectDecisionResponse:
    service = ArchitectService()
    try:
        return service.decide_contract(run_id, contract_id, request)
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)


@router.post(
    "/runs/{run_id}/contract-conflicts/{conflict_id}/decision",
    response_model=ArchitectConflictDecisionResponse,
    status_code=202,
)
def decide_architect_contract_conflict(
    run_id: str,
    conflict_id: str,
    request: ArchitectConflictDecisionRequest,
) -> ArchitectConflictDecisionResponse:
    """契约与 DDL 命名矛盾：映射已有表 / 新建表 / 取消。"""
    service = ArchitectService()
    try:
        return service.decide_conflict(run_id, conflict_id, request)
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)


@router.post(
    "/runs/{run_id}/helper",
    response_model=ArchitectHelperResponse,
)
def ask_architect_helper(
    run_id: str,
    request: ArchitectHelperRequest,
) -> ArchitectHelperResponse:
    """影子 LLM：只读问答，不推进主架构状态机。"""
    service = ArchitectService()
    try:
        return service.helper(run_id, request)
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)


@router.post("/runs/{run_id}/cancel", response_model=ArchitectCancelResponse)
def cancel_architect_run(
    run_id: str,
    request: ArchitectCancelRequest | None = None,
) -> ArchitectCancelResponse:
    service = ArchitectService()
    try:
        return service.cancel(run_id, request or ArchitectCancelRequest())
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)


@router.get("/runs/{run_id}", response_model=ArchitectSnapshotResponse)
def get_architect_run(run_id: str) -> ArchitectSnapshotResponse:
    service = ArchitectService()
    try:
        return service.snapshot(run_id)
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)


async def _event_frames(buffer: RunEventBuffer, after_seq: int):
    async for frame in stream_event_frames(
        buffer,
        after_seq,
        terminal_event_type="architect.failed",
    ):
        yield frame


def _sse_response(buffer: RunEventBuffer, *, after_seq: int) -> StreamingResponse:
    return StreamingResponse(
        _event_frames(buffer, after_seq),
        media_type="text/event-stream; charset=utf-8",
        headers=SSE_HEADERS,
    )


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, ArchitectServiceError):
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, ArchitectLLMConfigurationError):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ARCHITECT_LLM_NOT_CONFIGURED",
                "message": str(exc),
            },
        ) from exc
    if isinstance(exc, RAGProviderError):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ARCHITECT_RAG_UNAVAILABLE",
                "message": str(exc),
            },
        ) from exc
    raise exc
