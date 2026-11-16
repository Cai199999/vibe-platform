"""局部 VisualNode 生成 API（离线画布魔法生成）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ai_core.domain.visual_agent import (
    VisualNodeGenerationError,
    VisualNodeGenerationRequest,
    VisualNodeGenerationResponse,
)
from ai_core.visual_agent.service import VisualNodeGenerator

router = APIRouter(prefix="/api/v1/internal/visual-agent", tags=["visual-agent"])


@router.post("/nodes", response_model=VisualNodeGenerationResponse)
def generate_nodes(
    request: VisualNodeGenerationRequest,
) -> VisualNodeGenerationResponse:
    """只返回 VisualNode JSON 数组，不写源码、不启动沙盒。"""
    try:
        return VisualNodeGenerator().generate(request)
    except VisualNodeGenerationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": exc.code,
                "message": str(exc),
                **(exc.diagnostic or {}),
            },
        ) from exc


__all__ = ["router"]
