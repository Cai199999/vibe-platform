"""领域异常 -> HTTP 错误映射。"""

from __future__ import annotations

from fastapi import HTTPException

from ai_core.ports.sandbox import (
    SandboxError,
    SandboxGitError,
    SandboxNotFoundError,
    SandboxPatchError,
    SandboxStartupError,
    SandboxValidationError,
)


def sandbox_error_to_http(exc: SandboxError) -> HTTPException:
    if isinstance(exc, SandboxNotFoundError):
        return HTTPException(
            status_code=404,
            detail={"code": "SANDBOX_NOT_FOUND", "message": str(exc)},
        )
    if isinstance(exc, SandboxValidationError):
        return HTTPException(
            status_code=422,
            detail={"code": "SANDBOX_INVALID_REQUEST", "message": str(exc)},
        )
    if isinstance(exc, SandboxPatchError):
        return HTTPException(
            status_code=422,
            detail={"code": "SANDBOX_PATCH_FAILED", "message": str(exc)},
        )
    if isinstance(exc, SandboxGitError):
        return HTTPException(
            status_code=409,
            detail={"code": "SANDBOX_GIT_FAILED", "message": str(exc)},
        )
    if isinstance(exc, SandboxStartupError):
        return HTTPException(
            status_code=503,
            detail={"code": "SANDBOX_UNAVAILABLE", "message": str(exc)},
        )
    return HTTPException(
        status_code=500,
        detail={"code": "SANDBOX_ERROR", "message": str(exc)},
    )
