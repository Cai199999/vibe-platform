"""沙盒生命周期内部 API。

这些接口只供 Java BFF / 内部编排调用，不直接暴露给浏览器。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from docker.errors import DockerException

from ai_core.api.errors import sandbox_error_to_http
from ai_core.sandbox import get_sandbox_provider
from ai_core.sandbox.base import SandboxError
from ai_core.schemas import (
    ApplyPatchRequest,
    ApplyPatchResponse,
    CreateSandboxRequest,
    DestroySandboxResponse,
    SandboxInfo,
    VerifyResponse,
)

router = APIRouter(prefix="/api/v1/internal/sandbox", tags=["sandbox"])


@router.post("/create", response_model=SandboxInfo, status_code=201)
def create_sandbox(request: CreateSandboxRequest) -> SandboxInfo:
    """创建并启动一个 React + Vite 单前端沙盒。"""
    provider = get_sandbox_provider()
    try:
        handle = provider.create(
            project_id=request.project_id,
            files=request.files,
            template=request.template,
        )
    except SandboxError as exc:
        raise sandbox_error_to_http(exc) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "SANDBOX_DOCKER_UNAVAILABLE", "message": str(exc)},
        ) from exc

    return SandboxInfo(
        sandbox_id=handle.sandbox_id,
        container_id=handle.container_id,
        container_name=handle.container_name,
        project_id=handle.project_id,
        state=handle.state,
        preview_url=handle.preview_url,
        host_port=handle.host_port,
        internal_port=handle.internal_port,
        template=handle.template,
        created_at=handle.created_at,
    )


@router.post("/apply-patch", response_model=ApplyPatchResponse)
def apply_patch(request: ApplyPatchRequest) -> ApplyPatchResponse:
    """写入全量文件或 unified diff，触发 Vite 热更新。"""
    provider = get_sandbox_provider()
    try:
        changed = provider.apply_patch(
            sandbox_id=request.sandbox_id,
            files=request.files,
            patch=request.patch,
            deleted_paths=request.deleted_paths,
        )
    except SandboxError as exc:
        raise sandbox_error_to_http(exc) from exc

    return ApplyPatchResponse(
        sandbox_id=request.sandbox_id,
        changed_files=changed,
        hot_reload=True,
        applied_at=datetime.now(timezone.utc),
    )


@router.get("/verify", response_model=VerifyResponse)
def verify(
    sandbox_id: str = Query(min_length=1),
    check: Literal["build", "test"] = "build",
) -> VerifyResponse:
    """在沙盒内执行 npm run build / npm run test。"""
    provider = get_sandbox_provider()
    try:
        result = provider.verify(sandbox_id=sandbox_id, check=check)
    except SandboxError as exc:
        raise sandbox_error_to_http(exc) from exc

    return VerifyResponse(
        sandbox_id=sandbox_id,
        check=check,
        success=result.success,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        duration_ms=result.duration_ms,
        logs=result.logs,
        logs_truncated=result.logs_truncated,
        finished_at=datetime.now(timezone.utc),
    )


@router.delete("/{sandbox_id}", response_model=DestroySandboxResponse)
def destroy(sandbox_id: str) -> DestroySandboxResponse:
    """销毁沙盒容器，幂等语义由 404 表达。"""
    provider = get_sandbox_provider()
    try:
        provider.destroy(sandbox_id)
    except SandboxError as exc:
        raise sandbox_error_to_http(exc) from exc

    return DestroySandboxResponse(
        sandbox_id=sandbox_id,
        destroyed=True,
        destroyed_at=datetime.now(timezone.utc),
    )
