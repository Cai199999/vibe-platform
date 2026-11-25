"""全栈生成领域模型。

契约确认后，``FullstackGraph`` 依次执行 DBACoder / BackendCoder /
FrontendCoder / InfraCoder，把沙盒从“单前端 Vite”升级为
“前端 + Node 后端 + 数据库 + 部署配置”：

  - DBA：生成 Sequelize ORM 与 SQLite 预览 DDL；
  - Backend：按契约 apiRoutes 生成 Express REST 服务；
  - Frontend：生成 API client，并挂载到 ``src/main.jsx``；
  - Infra：生成 Dockerfile / render.yaml / .env.example；
  - 重启沙盒，通过 Vite ``/api`` 反向代理完成真实联调。

本模块属于纯领域层：只依赖标准库与 Pydantic。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from ai_core.domain.architect import ArchitectContract
from ai_core.domain.common import CamelModel
from ai_core.domain.project_mode import ProjectMode
from ai_core.domain.visual_document import VisualDocument

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

    ``mode``：
      - ``fullstack``：DBA → Backend → Frontend → Infra → Build → Restart；
      - ``frontend``：契约确认后的前端骨架初始化，只执行
        FrontendCoder → BuildVerifier → SandboxRestart；
      - ``backend``：Supervisor 激活的 BackendCoder 专属子图，
        DBA → Backend → Build → Restart，不改前端；
      - ``dba``：Supervisor 激活的 DBA 专属子图，只生成数据库层并重启。
    """

    mode: Literal["fullstack", "frontend", "backend", "dba"] = "fullstack"
    # 前端对项目模式的事实判定。契约快照可能是旧版本，不能单独作为
    # “是否可以跑前端 CodeGen”的唯一依据。
    project_mode: ProjectMode | None = None
    verify_build: bool = True
    restart_sandbox: bool = True
    # Architect 预授权标准链路直连；出现异常自动收回。
    fast_path: bool = True
    # 可选：能力包 / 契约给出的白名单子集（[from, to, kind, requestedAction]）。
    fast_path_edges: list[list[str]] = Field(default_factory=list, max_length=32)
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
    # 离线优先链路：前端把已冻结（FROZEN + contentHash）的 VisualDocument
    # 随请求回传，CodeGen 以它为准；没有该字段时才回退契约里的草稿树。
    visual_document: VisualDocument | None = None
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

    node: Literal[
        "DBACoder",
        "BackendCoder",
        "FrontendCoder",
        "InfraCoder",
        "BuildVerifier",
        "SandboxRestart",
        "Sandbox",
    ]
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
    # 失败归因：CODE / CONTRACT / ENVIRONMENT；契约矛盾时前端弹窗重新澄清。
    failure_kind: str | None = None
    # 缺陷归因：CODE_DEFECT / CONTRACT_DEFECT，前端用于展示不同处置动作。
    defect_kind: str | None = None
    needs_clarification: bool = False
    clarification: dict[str, Any] = Field(default_factory=dict)
    # 风险自适应门禁：L1 非阻塞告警 / L3 真实 MySQL 验收结论。
    quality_tier: str = "L2_STANDARD"
    quality_badge: dict[str, str] | None = None
    warnings: list[str] = Field(default_factory=list)
    mysql_check: dict[str, Any] = Field(default_factory=dict)
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
