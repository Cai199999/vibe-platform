"""HTTP 请求 / 响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


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


class CreateSnapshotRequest(BaseModel):
    """创建快照：把沙盒当前工作区提交为一个 Git checkpoint。

    备注必填且 ≤200 字，去除首尾空白后再校验，避免只有空格的无意义快照。
    """

    message: str = Field(min_length=1, max_length=200)

    @field_validator("message")
    @classmethod
    def _strip_message(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("快照备注不能为空")
        return cleaned


class SnapshotInfo(CamelResponse):
    """单条快照元信息。"""

    commit_hash: str
    short_hash: str
    message: str
    author: str
    committed_at: datetime


class SnapshotListResponse(CamelResponse):
    """快照列表；headCommit 为当前 HEAD，便于 UI 标记“当前版本”。"""

    sandbox_id: str
    head_commit: str | None = None
    snapshots: list[SnapshotInfo] = Field(default_factory=list)


class CreateSnapshotResponse(CamelResponse):
    """快照创建结果。"""

    sandbox_id: str
    commit_hash: str
    short_hash: str
    message: str
    committed_at: datetime


class RestoreSnapshotResponse(CamelResponse):
    """快照恢复结果。"""

    sandbox_id: str
    commit_hash: str
    message: str
    restored_at: datetime


class SnapshotDiffResponse(CamelResponse):
    """快照只读视图使用的 unified diff。"""

    sandbox_id: str
    commit_hash: str
    mode: Literal["commit", "current", "baseline"]
    diff: str
    truncated: bool


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
