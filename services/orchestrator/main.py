"""Vibe Platform - Python AI Core。

M1 提供：
  - 健康检查 / Java BFF 连通性探针；
  - 单前端 React 沙盒的 Docker 生命周期 API；
  - LangGraph 依赖已就绪，具体 Graph / 多 Agent 编排在后续阶段实现。
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from typing import AsyncIterator

from fastapi import FastAPI

from ai_core.api.architect import router as architect_router
from ai_core.api.collaboration import router as collaboration_router
from ai_core.api.direct_ops import router as direct_ops_router
from ai_core.api.fullstack import router as fullstack_router
from ai_core.api.generated_apps import router as generated_apps_router
from ai_core.api.runs import router as runs_router
from ai_core.api.sandbox import router as sandbox_router
from ai_core.api.source_analysis import router as source_analysis_router
from ai_core.api.supervisor import router as supervisor_router
from ai_core.api.testing import router as testing_router
from ai_core.api.visual_agent import router as visual_agent_router
from ai_core.config import get_settings
from ai_core.persistence.run_store import get_run_store
from ai_core.run_guard import get_run_guard
from ai_core.error_envelope import install_error_handlers
from ai_core.sandbox import get_sandbox_provider
from ai_core.sandbox_gc import start_container_gc, start_draft_gc, start_idle_gc
from ai_core.schemas import HealthResponse, PingResponse

settings = get_settings()

# uvicorn 默认只配置自己的 logger，业务模块的 logging.getLogger(...).info
# 不会输出。这里显式配置 root handler，FullstackGraph 的节点日志才能
# 出现在 docker logs 中（生产可用 LOG_LEVEL=WARNING 降噪）。
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def _package_version(package_name: str) -> str | None:
    """读取已安装依赖版本；未安装时返回 None，不影响健康检查。"""
    try:
        return version(package_name)
    except PackageNotFoundError:
        return None


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """服务生命周期：启动孤儿草稿回收，退出时停止后台线程。"""
    provider = get_sandbox_provider()
    collector = start_draft_gc(provider, settings)
    container_collector = start_container_gc(provider, settings)
    idle_collector = start_idle_gc(provider, settings)
    try:
        yield
    finally:
        if collector is not None:
            collector.stop()
        if container_collector is not None:
            container_collector.stop()
        if idle_collector is not None:
            idle_collector.stop()
        # 刷出异步事件写队列；Redis 不可用时是 no-op。
        get_run_store().close()


app = FastAPI(
    title="Vibe Platform AI Core",
    version=settings.service_version,
    description=(
        "Python AI Core skeleton: health check, BFF connectivity probe and "
        "Docker sandbox lifecycle API."
    ),
    lifespan=lifespan,
)

# 全局错误信封：所有 JSON 接口统一 code/userMessage/errorOptions/traceId。
install_error_handlers(app)

app.include_router(sandbox_router)
app.include_router(runs_router)
app.include_router(direct_ops_router)
app.include_router(architect_router)
app.include_router(collaboration_router)
app.include_router(fullstack_router)
app.include_router(generated_apps_router)
app.include_router(supervisor_router)
app.include_router(testing_router)
app.include_router(visual_agent_router)
app.include_router(source_analysis_router)


@app.get("/api/v1/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Python AI Core 自身健康检查。

    Docker / 沙盒镜像状态只作为诊断信息返回，不把整个服务判为 DEGRADED：
    Docker 暂不可用不应阻断 Java BFF 等非沙盒功能。
    """
    provider = get_sandbox_provider()
    capacity = get_run_guard().snapshot()
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
            "run_capacity": f"{capacity['inFlight']}/{capacity['limit']}",
            "run_store": get_run_store().backend,
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
