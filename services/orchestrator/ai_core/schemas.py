"""HTTP 请求 / 响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _to_camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class CamelResponse(BaseModel):
    """对外响应：Python 内部 snake_case，JSON 输出 camelCase。"""

    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True)


# ------------------------------------------------------------
# 健康检查
# ------------------------------------------------------------
class HealthResponse(BaseModel):
    """服务健康检查响应。"""

    status: Literal["UP", "DEGRADED"]
    service: str
    version: str
    timestamp: datetime
    language: str = "python"
    dependencies: dict[str, str] = Field(default_factory=dict)


class PingResponse(BaseModel):
    """供 Java BFF 调用的最小连通性响应。"""

    status: Literal["UP"] = "UP"
    service: str
    version: str
    timestamp: datetime
    app_env: str
    langgraph_version: str | None = None


# ------------------------------------------------------------
# Sandbox
# ------------------------------------------------------------
class CreateSandboxRequest(BaseModel):
    """创建单前端 React 沙盒。"""

    project_id: str = Field(min_length=1, max_length=64)
    # key: 相对于工作目录的文件路径；value: 完整文件内容。
    files: dict[str, str] = Field(default_factory=dict)
    template: Literal["react-vite"] = "react-vite"


class SandboxInfo(BaseModel):
    """沙盒句柄。"""

    sandbox_id: str
    container_id: str
    container_name: str
    project_id: str
    state: str
    preview_url: str
    host_port: int
    internal_port: int
    template: str
    created_at: datetime


class ApplyPatchRequest(BaseModel):
    """更新沙盒文件。

    支持两种方式，可同时使用：
      - files: 全量文件覆盖（适合模型直接输出完整文件）；
      - patch: unified diff（通过容器内 ``git apply`` 应用）。
    """

    sandbox_id: str = Field(min_length=1, max_length=64)
    files: dict[str, str] = Field(default_factory=dict)
    patch: str | None = None
    deleted_paths: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_change(self) -> "ApplyPatchRequest":
        if not self.files and not self.patch and not self.deleted_paths:
            raise ValueError("files、patch、deleted_paths 至少提供一个")
        return self


class ApplyPatchResponse(BaseModel):
    """补丁应用结果。"""

    sandbox_id: str
    changed_files: list[str]
    hot_reload: bool
    applied_at: datetime


class VerifyResponse(BaseModel):
    """构建 / 测试结果。"""

    sandbox_id: str
    check: Literal["build", "test"]
    success: bool
    exit_code: int
    timed_out: bool
    duration_ms: int
    logs: str
    logs_truncated: bool
    finished_at: datetime


class DestroySandboxResponse(BaseModel):
    """销毁结果。"""

    sandbox_id: str
    destroyed: bool
    destroyed_at: datetime


class ImportSandboxResponse(CamelResponse):
    """导入 zip / 图片后返回的沙盒与源码摘要。

    ``files`` 只包含文本源码（有体积上限），用于前端画布定位与刷新恢复；
    完整项目（含二进制资源）已经写入沙盒。
    """

    sandbox_id: str
    preview_url: str
    project_id: str
    kind: Literal["react", "html"]
    entry_file: str
    file_count: int
    files: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    source_name: str = ""
    created_at: datetime
