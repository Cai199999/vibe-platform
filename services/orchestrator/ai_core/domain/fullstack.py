"""全栈生成领域模型。

契约确认后，``FullstackGraph`` 依次执行 DBACoder / BackendCoder /
FrontendCoder，把沙盒从“单前端 Vite”升级为“前端 + Node 轻后端”：

  - DBA：把契约 DDL 落为 ``server/schema.sql``；
  - Backend：按契约 apiRoutes 生成零依赖 Node HTTP 服务；
  - Frontend：生成 API client，并挂载到 ``src/main.jsx``；
  - 重启沙盒，通过 Vite ``/api`` 反向代理完成真实联调。

本模块属于纯领域层：只依赖标准库与 Pydantic。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from ai_core.domain.architect import ArchitectContract
from ai_core.domain.common import CamelModel

FullstackRunStatus = Literal["RUNNING", "SUCCEEDED", "FAILED"]


class FullstackSource(CamelModel):
    """生成全栈代码时使用的源码状态。

    优先复用已存在的沙盒；没有沙盒时，才使用 ``files`` 创建新沙盒。
    """

    sandbox_id: str | None = Field(default=None, max_length=64)
    files: dict[str, str] = Field(default_factory=dict)


class FullstackRunOptions(CamelModel):
    """生成选项。

    ``verify_build`` / ``restart_sandbox`` 允许测试关闭真实副作用；
    生产默认均为 true。
    """

    verify_build: bool = True
    restart_sandbox: bool = True
    max_output_chars: int = Field(default=4000, ge=500, le=20_000)


class FullstackRunRequest(CamelModel):
    """启动一次全栈代码生成。

    ``contract`` 是可选的权威快照：前端从
    ``architect.contract.confirmed`` 事件中拿到完整契约后直接回传，
    避免跨 Run 查询带来的竞态；如果未传，服务端会按 contractId 找回
    已确认的 Architect 契约。
    """

    run_id: str | None = Field(default=None, max_length=64)
    tenant_id: str | None = None
    project_id: str = Field(min_length=1, max_length=64)
    contract_id: str = Field(min_length=1, max_length=128)
    sandbox_id: str | None = Field(default=None, max_length=64)
    contract: ArchitectContract | None = None
    source: FullstackSource = Field(default_factory=FullstackSource)
    options: FullstackRunOptions = Field(default_factory=FullstackRunOptions)

    @model_validator(mode="after")
    def _contract_id_matches(self) -> "FullstackRunRequest":
        if self.contract and self.contract.contract_id != self.contract_id:
            raise ValueError("contractId 与 contract.contractId 不一致")
        if not self.sandbox_id and self.source.sandbox_id:
            self.sandbox_id = self.source.sandbox_id
        return self


class FullstackGeneratedFile(CamelModel):
    """一个生成文件（用于 SSE 与快照展示）。"""

    path: str
    kind: Literal["sql", "backend", "frontend", "config"]
    summary: str = ""
    content: str = ""
    truncated: bool = False


class FullstackStep(CamelModel):
    """一个生成节点的执行结果。"""

    node: Literal["DBACoder", "BackendCoder", "FrontendCoder", "Sandbox"]
    label: str
    status: Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED"] = "PENDING"
    summary: str = ""
    files: list[FullstackGeneratedFile] = Field(default_factory=list)
    duration_ms: int = 0
    error: str | None = None


class FullstackRunSnapshot(CamelModel):
    """全栈生成 Run 的快照（刷新 / 重连恢复）。"""

    run_id: str
    project_id: str
    contract_id: str
    sandbox_id: str | None = None
    status: FullstackRunStatus = "RUNNING"
    preview_url: str | None = None
    steps: list[FullstackStep] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    error: str | None = None
    error_code: str | None = None
    last_seq: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "FullstackGeneratedFile",
    "FullstackRunOptions",
    "FullstackRunRequest",
    "FullstackRunSnapshot",
    "FullstackRunStatus",
    "FullstackSource",
    "FullstackStep",
]
