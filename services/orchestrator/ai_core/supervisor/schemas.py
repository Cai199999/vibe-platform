"""Supervisor（多智能体总调度）数据契约。

Supervisor 只负责“意图路由 + Handoff”，不写代码，也不修改任何子图
（UI Graph / Architect Graph）的请求、事件或 checkpoint 语义。

对外 JSON 使用 camelCase，与平台既有契约保持一致。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from ai_core.agent.schemas import AnnotationEnvelope, CamelModel, RunOptions
from ai_core.architect.schemas import ArchitectRunOptions, ArchitectSource

ScopeHint = Literal["auto", "ui", "architect", "both"]
TargetKind = Literal["ui_graph", "architect_graph", "both", "custom", "none"]
AgentStatus = Literal["available", "reserved", "custom"]


class SupervisorOptions(CamelModel):
    """顶层调度选项。

    ``scopeHint`` 用于前端显式覆盖分类结果；``targetAgent`` 用于直接指定
    注册中心里的某个 Agent（包括企业 CustomAgent），优先级最高。
    两个子图的选项分别放在 ``ui`` / ``architect``，避免互相污染。
    """

    scope_hint: ScopeHint = "auto"
    # 显式指定 Agent id；必须已在 AgentRegistry 注册且可用。
    target_agent: str | None = Field(default=None, max_length=64)
    ui: RunOptions = Field(default_factory=RunOptions)
    architect: ArchitectRunOptions = Field(default_factory=ArchitectRunOptions)


class SupervisorRequest(CamelModel):
    """一次 Supervisor 路由 / 派发请求。"""

    tenant_id: str | None = None
    project_id: str = Field(min_length=1, max_length=64)
    project_name: str = Field(default="未命名项目", max_length=128)
    # 用户当前自然语言意图。可为空：仅有标注时也能路由到 UI。
    message: str = Field(default="", max_length=8000)
    # UI Graph 的标注输入；Dispatch 到 UI 时至少需要 1 个。
    annotations: list[AnnotationEnvelope] = Field(default_factory=list, max_length=3)
    # 复用已有沙盒；同时会注入 ArchitectSource。
    sandbox_id: str | None = Field(default=None, max_length=64)
    # 无沙盒时的前端源码回退（只作为初始模板，不是权威代码）。
    files: dict[str, str] = Field(default_factory=dict)
    # Architect 的源码来源；未显式提供时由 service 从 sandboxId/files 补齐。
    source: ArchitectSource = Field(default_factory=ArchitectSource)
    ui_history: list[dict[str, Any]] = Field(default_factory=list, max_length=30)
    options: SupervisorOptions = Field(default_factory=SupervisorOptions)


class SupervisorDecision(CamelModel):
    """Supervisor 的分类与路由决策。"""

    task_type: str
    target: str
    targets: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    signals: list[str] = Field(default_factory=list)
    # 当前是否满足派发条件（例如 UI 目标必须有标注）。
    dispatchable: bool = True
    # 缺少的输入，前端可据此提示用户补标注 / 补源码。
    missing_inputs: list[str] = Field(default_factory=list)
    strategy: str = "rule"


class AgentHandoff(CamelModel):
    """一个有序的 Sub-Graph 交接项。"""

    agent_id: str
    display_name: str
    graph: str
    order: int = Field(ge=1)
    stream_path_template: str | None = None


class SupervisorRouteResponse(CamelModel):
    trace_id: str
    project_id: str
    decision: SupervisorDecision
    handoffs: list[AgentHandoff] = Field(default_factory=list)
    registry_version: str = "1"


class SubGraphDispatch(CamelModel):
    """实际启动后的子 Run 句柄。"""

    agent_id: str
    run_id: str
    stream_path: str
    order: int = Field(ge=1)
    status: Literal["STARTED"] = "STARTED"


class SupervisorDispatchResponse(CamelModel):
    trace_id: str
    decision: SupervisorDecision
    dispatches: list[SubGraphDispatch] = Field(default_factory=list)
    started_at: datetime


class AgentView(CamelModel):
    """AgentRegistry 对外视图。"""

    agent_id: str
    display_name: str
    graph: str
    description: str = ""
    task_types: list[str] = Field(default_factory=list)
    status: AgentStatus
    source: Literal["builtin", "manifest"] = "builtin"
    version: str = "1.0.0"
    stream_path_template: str | None = None
    keywords: list[str] = Field(default_factory=list)
    manifest_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentListResponse(CamelModel):
    agents: list[AgentView]
    registry_version: str = "1"
    custom_agents_dir: str | None = None
