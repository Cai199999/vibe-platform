"""Vibe Platform - Python AI Core 骨架。

M1 只提供健康检查与跨语言连通性验证；LangGraph 的具体 Graph、模型路由、
多 Agent 调度留在后续阶段实现。
"""

from __future__ import annotations

from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从环境变量 / .env 读取的服务配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "local"
    service_name: str = "python-ai"
    service_version: str = "0.1.0"

    # M1 只做占位；M2 由 ModelRouter 统一读取。
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    # 复用宿主机 Docker Redis；当前骨架不主动连接。
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379


settings = Settings()


def _package_version(package_name: str) -> str | None:
    """读取已安装依赖版本；未安装时返回 None，不影响健康检查。"""
    try:
        return version(package_name)
    except PackageNotFoundError:
        return None


class HealthResponse(BaseModel):
    """服务健康检查响应。"""

    status: Literal["UP", "DEGRADED"]
    service: str
    version: str
    timestamp: datetime
    language: str = "python"
    dependencies: dict[str, str] = {}


class PingResponse(BaseModel):
    """供 Java BFF 调用的最小连通性响应。"""

    status: Literal["UP"] = "UP"
    service: str
    version: str
    timestamp: datetime
    app_env: str
    langgraph_version: str | None = None


app = FastAPI(
    title="Vibe Platform AI Core",
    version=settings.service_version,
    description="Python AI Core skeleton: health check and Java BFF connectivity probe.",
)


@app.get("/api/v1/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Python AI Core 自身健康检查。"""
    return HealthResponse(
        status="UP",
        service=settings.service_name,
        version=settings.service_version,
        timestamp=datetime.now(timezone.utc),
        dependencies={
            "langgraph": _package_version("langgraph") or "NOT_INSTALLED",
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
