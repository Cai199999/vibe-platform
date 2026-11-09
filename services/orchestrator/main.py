"""Vibe Platform - Python AI Core。

M1 提供：
  - 健康检查 / Java BFF 连通性探针；
  - 单前端 React 沙盒的 Docker 生命周期 API；
  - LangGraph 依赖已就绪，具体 Graph / 多 Agent 编排在后续阶段实现。
"""

from __future__ import annotations

from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version

from fastapi import FastAPI

from ai_core.api.architect import router as architect_router
from ai_core.api.fullstack import router as fullstack_router
from ai_core.api.runs import router as runs_router
from ai_core.api.sandbox import router as sandbox_router
from ai_core.api.supervisor import router as supervisor_router
from ai_core.config import get_settings
from ai_core.sandbox import get_sandbox_provider
from ai_core.schemas import HealthResponse, PingResponse

settings = get_settings()


def _package_version(package_name: str) -> str | None:
    """读取已安装依赖版本；未安装时返回 None，不影响健康检查。"""
    try:
        return version(package_name)
    except PackageNotFoundError:
        return None


app = FastAPI(
    title="Vibe Platform AI Core",
    version=settings.service_version,
    description=(
        "Python AI Core skeleton: health check, BFF connectivity probe and "
        "Docker sandbox lifecycle API."
    ),
)

app.include_router(sandbox_router)
app.include_router(runs_router)
app.include_router(architect_router)
app.include_router(fullstack_router)
app.include_router(supervisor_router)


@app.get("/api/v1/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Python AI Core 自身健康检查。

    Docker / 沙盒镜像状态只作为诊断信息返回，不把整个服务判为 DEGRADED：
    Docker 暂不可用不应阻断 Java BFF 等非沙盒功能。
    """
    provider = get_sandbox_provider()
    return HealthResponse(
        status="UP",
        service=settings.service_name,
        version=settings.service_version,
        timestamp=datetime.now(timezone.utc),
        dependencies={
            "langgraph": _package_version("langgraph") or "NOT_INSTALLED",
            "docker": provider.docker_version(),
            "sandbox_image": provider.image_state(),
            # 只暴露“是否已配置”和实际生效的 provider，绝不返回密钥内容。
            "llm": "deepseek" if settings.deepseek_api_key else "mock",
        },
    )


@app.get("/api/v1/internal/ping", response_model=PingResponse)
def internal_ping() -> PingResponse:
    """内部连通性探针：只允许内网调用，不返回任何密钥或源码。"""
    return PingResponse(
        service=settings.service_name,
        version=settings.service_version,
        timestamp=datetime.now(timezone.utc),
        app_env=settings.app_env,
        langgraph_version=_package_version("langgraph"),
    )
