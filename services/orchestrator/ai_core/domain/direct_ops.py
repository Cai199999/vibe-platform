"""Direct Ops 领域契约。

Direct Ops 是“用户明确点击、跳过 LLM”的确定性写路径。它仍然必须：
  - 携带 target / action / payload；
  - 服务端重新解析 target，不信任前端 selector；
  - 用 baseRevision 做乐观并发校验；
  - 成功后写 ChangeLog，供 Architect 回顾。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from ai_core.domain.common import CamelModel

DirectActionKind = Literal[
    "UPDATE_TEXT",
    "UPDATE_PROPS",
    "DELETE_NODE",
    "DELETE_DATA_ITEM",
    "ADD_COMPONENT",
    "MOVE_NODE",
    # 冻结 VisualDocument 一次性编译为真实 React 工程。
    "VISUAL_CODEGEN",
]

DirectActionRisk = Literal[
    "L0_DIRECT",
    "L1_VERIFY_DIRECT",
    "L2_HITL",
    "L3_AI_REQUIRED",
]

# Direct Ops 白名单基础组件；不含业务组件，避免绕过架构确认。
ALLOWED_ADD_COMPONENTS = ("Button", "Input", "Text", "Card", "Container")


class DirectErrorCode:
    """Direct Ops 稳定错误码；前端与错误注册表共用。"""

    ACTION_INVALID = "DIRECT_ACTION_INVALID"
    TARGET_NOT_FOUND = "DIRECT_TARGET_NOT_FOUND"
    TARGET_AMBIGUOUS = "DIRECT_TARGET_AMBIGUOUS"
    REVISION_MISMATCH = "DIRECT_WRITE_CONFLICT"
    UNSAFE_OPERATION = "DIRECT_UNSAFE_OPERATION"
    PROJECT_BUSY = "DIRECT_PROJECT_BUSY"
    TOOL_FAILED = "DIRECT_TOOL_FAILED"
    RESOLVER_UNAVAILABLE = "DIRECT_RESOLVER_UNAVAILABLE"
    ROLLBACK_FAILED = "DIRECT_ROLLBACK_FAILED"
    ROUTE_TO_LLM = "DIRECT_REQUIRES_LLM"


class DirectActionTarget(CamelModel):
    """前端定位线索；服务端会重新用 AST 解析。"""

    anchor_id: str | None = Field(default=None, max_length=128)
    annotation_id: str | None = Field(default=None, max_length=128)
    # 构建期 source map 的稳定身份；有 oid 时解析器必须优先走 oid 映射。
    oid: str | None = Field(default=None, max_length=80)
    source_range: dict[str, int] | None = None
    # 组件调用点 oid：UPDATE_PROPS 等实例级操作优先按它解析。
    call_site_oid: str | None = Field(default=None, max_length=96)
    # 运行时实例路径：父 oid 链 + 同模板序号，用于 map/repeat 消歧。
    instance_path: list[dict[str, Any]] | None = None
    instance_key: str | None = Field(default=None, max_length=2000)
    selector: str | None = Field(default=None, max_length=500)
    file: str | None = Field(default=None, max_length=500)
    line: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)
    component: str | None = Field(default=None, max_length=200)
    instance: int | None = Field(default=None, ge=1)
    instance_count: int | None = Field(default=None, ge=1)
    call_site_file: str | None = Field(default=None, max_length=500)
    # SDK 侧 NodeIdentity 基础事实；Resolver 会用 AST 补全 kind/scope。
    identity: dict[str, Any] | None = None
    placement: dict[str, Any] | None = None


class DirectActionOptions(CamelModel):
    source: str = "FLOATING_TOOLBAR"
    dry_run: bool = False
    verify_build: bool = False


class DirectActionPayload(CamelModel):
    """一次 Direct Action 的完整请求。"""

    direct_action_id: str = Field(min_length=1, max_length=64)
    idempotency_key: str | None = Field(default=None, max_length=160)
    run_id: str | None = Field(default=None, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    sandbox_id: str = Field(min_length=1, max_length=64)
    # 源码快照同步到 BFF 时用于租户隔离；缺省 tenant_local。
    tenant_id: str | None = Field(default=None, max_length=64)
    # 非 dryRun 必填；与沙盒当前 HEAD 不一致时拒绝写入。
    base_revision: str | None = Field(default=None, max_length=128)
    client_seq: int | None = Field(default=None, ge=0)
    target: DirectActionTarget
    action: DirectActionKind
    payload: dict[str, Any] = Field(default_factory=dict)
    options: DirectActionOptions = Field(default_factory=DirectActionOptions)

    @model_validator(mode="after")
    def _validate_action_payload(self) -> "DirectActionPayload":
        if not self.options.dry_run and not (self.base_revision or "").strip():
            raise ValueError("非 dryRun 请求必须提供 baseRevision")
        if self.action == "UPDATE_TEXT":
            text = str(self.payload.get("text") or "")
            if not text.strip():
                raise ValueError("UPDATE_TEXT 必须提供非空 payload.text")
            if len(text) > 2000:
                raise ValueError("UPDATE_TEXT 文案不能超过 2000 字符")
            if any(char in text for char in "<>{}\n\r"):
                raise ValueError("UPDATE_TEXT 文案不能包含 < > { } 或换行")
        if self.action == "UPDATE_PROPS":
            props = self.payload.get("props")
            if not isinstance(props, dict) or not props:
                raise ValueError("UPDATE_PROPS 必须提供非空 payload.props")
            if len(props) > 30:
                raise ValueError("UPDATE_PROPS 一次最多更新 30 个属性")
            for name, value in props.items():
                if (
                    not isinstance(name, str)
                    or not name
                    or len(name) > 80
                    or not all(char.isalnum() or char in "_$:-" for char in name)
                ):
                    raise ValueError(f"非法 prop 名称: {name!r}")
                if value is None or isinstance(value, bool):
                    continue
                if isinstance(value, (int, float)):
                    continue
                if not isinstance(value, str):
                    raise ValueError(f"prop {name} 只支持字符串 / 数字 / 布尔 / null")
                if len(value) > 2000:
                    raise ValueError(f"prop {name} 超过 2000 字符")
                if any(char in value for char in "<>{}\n\r"):
                    raise ValueError(f"prop {name} 不能包含 < > {{ }} 或换行")
        if self.action == "MOVE_NODE":
            placement = self.target.placement or {}
            if not isinstance(placement, dict) or not (
                placement.get("parentSelector")
                or placement.get("parentFile")
                or placement.get("parentLine")
            ):
                raise ValueError(
                    "MOVE_NODE 必须在 target.placement 中提供父容器定位信息"
                )
        if self.action == "ADD_COMPONENT":
            component_type = str(self.payload.get("componentType") or "").strip()
            if not component_type:
                raise ValueError("ADD_COMPONENT 必须提供 payload.componentType")
        return self


class DirectUndoRequest(CamelModel):
    project_id: str = Field(min_length=1, max_length=64)
    sandbox_id: str = Field(min_length=1, max_length=64)


class DirectDecisionRequest(CamelModel):
    """Direct Ops HITL 决策：用户从 ScopePolicy 返回的 options 中选择一项。"""

    decision_id: str = Field(min_length=1, max_length=64)
    option_id: str = Field(min_length=1, max_length=64)
    comment: str = Field(default="", max_length=500)


class DirectActionResponse(CamelModel):
    direct_action_id: str
    run_id: str | None = None
    status: Literal[
        "DRY_RUN",
        "SUCCEEDED",
        "FAILED",
        "HITL_REQUIRED",
    ] = "SUCCEEDED"
    action: DirectActionKind
    dry_run: bool = False
    risk: DirectActionRisk = "L0_DIRECT"
    resolved_target: dict[str, Any] = Field(default_factory=dict)
    revision_before: str = ""
    revision_after: str = ""
    commit_hash: str = ""
    change_id: str = ""
    latency_ms: int = 0
    timings: dict[str, float] = Field(default_factory=dict)
    # 源码快照同步结果：SYNCED / SKIPPED / FAILED / DISABLED。
    source_sync: dict[str, Any] | None = None
    # L2 HITL：前端展示范围选择；选择“模板/全部实例”后转 AI 或显式确认。
    decision_id: str | None = None
    options: list[dict[str, Any]] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None


class ChangeLogEntry(CamelModel):
    """Direct Ops 成功后的可回放摘要；不存完整源码。"""

    change_id: str
    project_id: str
    sandbox_id: str
    run_id: str | None = None
    actor: Literal["DIRECT_OP", "AI_RUN"] = "DIRECT_OP"
    action: DirectActionKind
    target: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    diff_summary: str = ""
    before_hash: str = ""
    after_hash: str = ""
    commit_hash: str = ""
    # 撤回：该动作执行前的主工作树 HEAD。
    before_commit: str = ""
    risk: DirectActionRisk = "L0_DIRECT"
    # 视觉占位 / 失败转译：applied=false 且 visual_only=true。
    applied: bool = True
    visual_only: bool = False
    created_at: datetime


__all__ = [
    "ALLOWED_ADD_COMPONENTS",
    "ChangeLogEntry",
    "DirectActionKind",
    "DirectActionOptions",
    "DirectActionPayload",
    "DirectActionResponse",
    "DirectActionRisk",
    "DirectActionTarget",
    "DirectDecisionRequest",
    "DirectErrorCode",
    "DirectUndoRequest",
]
