"""Run 执行内部 API。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from docker.errors import DockerException

from ai_core.agent.llm import AgentLLMConfigurationError
from ai_core.agent.schemas import ExecuteRunRequest, ExecuteRunResponse
from ai_core.agent.service import (
    FullStackNotSupportedError,
    RunExecutionService,
    TargetFileNotFoundError,
)
from ai_core.api.errors import sandbox_error_to_http
from ai_core.sandbox.base import SandboxError

router = APIRouter(prefix="/api/v1/internal/runs", tags=["runs"])


@router.post("/execute", response_model=ExecuteRunResponse)
def execute_run(request: ExecuteRunRequest) -> ExecuteRunResponse:
    """同步执行一次单 Agent 闭环。

    M1 为同步接口，便于后台联调；M2 改为 202 + 事件流。
    """
    service = RunExecutionService()
    try:
        return service.execute(request)
    except FullStackNotSupportedError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "FULLSTACK_NOT_SUPPORTED", "message": str(exc)},
        ) from exc
    except TargetFileNotFoundError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "TARGET_FILE_NOT_FOUND", "message": str(exc)},
        ) from exc
    except AgentLLMConfigurationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "LLM_NOT_CONFIGURED", "message": str(exc)},
        ) from exc
    except SandboxError as exc:
        raise sandbox_error_to_http(exc) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "SANDBOX_DOCKER_UNAVAILABLE", "message": str(exc)},
        ) from exc
