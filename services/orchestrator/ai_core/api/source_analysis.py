"""存量 React 源码只读分析 API。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ai_core.domain.source_analysis import SourceAnalysisRequest
from ai_core.source_analysis.service import (
    SourceAnalysisError,
    SourceAnalysisInvalid,
    SourceAnalysisUnavailable,
    analyze_existing_react,
)

router = APIRouter(prefix="/api/v1/internal/source-analysis", tags=["source-analysis"])


@router.post("/analyze")
def analyze(request: SourceAnalysisRequest) -> dict[str, Any]:
    """返回只读组件树分析报告；绝不写源码或生成虚拟树。"""
    try:
        return analyze_existing_react(request.file_name, request.source)
    except (SourceAnalysisInvalid, SourceAnalysisUnavailable) as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except SourceAnalysisError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


__all__ = ["router"]
