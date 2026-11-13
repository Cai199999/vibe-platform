"""Phase 2：多轮决策分流领域契约。

Resolver 遇到“目标歧义 / 公共组件保护”时不再硬失败，而是把选择权交给
用户。本模块只描述**安全路径**：

  - ``select_target``      ：歧义候选之间选择，服务端回填精确区间重解析；
  - ``modify_call_site``   ：公共组件只在 callSite 做实例级修改；
  - ``cancel``             ：放弃本次修改，丢弃草稿。

绝对底线：任何“强行修改公共组件定义 / 豁免保护标识”的动作都不属于
``DecisionAction``，服务端与工具层都会拒绝，用户无权绕过安全门禁。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from ai_core.domain.common import CamelModel

# ---- 挂起状态 / SSE 事件 ----
AWAITING_DECISION = "AWAITING_DECISION"
DECISION_REQUIRED_EVENT = "decision.required"
DECISION_ANSWERED_EVENT = "decision.answered"

# ---- 决策类型 ----
DECISION_KIND_AMBIGUOUS = "target_ambiguous"
DECISION_KIND_PROTECTED = "protected_component"

# ---- 安全动作（白名单）----
ACTION_SELECT_TARGET = "select_target"
ACTION_MODIFY_CALL_SITE = "modify_call_site"
ACTION_CANCEL = "cancel"

DecisionAction = Literal["select_target", "modify_call_site", "cancel"]

SAFE_DECISION_ACTIONS = frozenset(
    {ACTION_SELECT_TARGET, ACTION_MODIFY_CALL_SITE, ACTION_CANCEL}
)

# 防御性黑名单：即使前端 / 历史检查点伪造了这些动作，服务端也直接拒绝。
FORBIDDEN_DECISION_ACTIONS = frozenset(
    {
        "force_modify_shared",
        "override_protection",
        "modify_component_definition",
        "force",
        "bypass",
    }
)

# 决策轮次硬上限：3 轮后仍无法确定安全路径，强制关闭 Run。
MAX_DECISION_ROUNDS = 3
DECISION_ERROR_LIMIT = "DECISION_LIMIT"
DECISION_ERROR_CANCELLED = "DECISION_CANCELLED"
DECISION_ERROR_OPTION_INVALID = "DECISION_OPTION_INVALID"


def decision_id_for(run_id: str, anchor_id: str, round_no: int) -> str:
    """确定性决策 ID：同一轮重放 / 重连可幂等定位同一条挂起决策。"""
    return f"dec_{run_id}_{anchor_id}_{round_no}"


class DecisionOption(CamelModel):
    """一个可执行的安全选项；``action`` 必须来自安全白名单。"""

    option_id: str
    action: DecisionAction
    label: str
    description: str = ""
    recommended: bool = False
    # 选项附带的证据：候选区间 / callSite 文件等，均由服务端生成。
    payload: dict[str, Any] = Field(default_factory=dict)


class PendingDecision(CamelModel):
    """一次待用户决策的挂起记录（SSE ``decision.required`` 的载荷）。"""

    decision_id: str
    run_id: str | None = None
    annotation_id: str | None = None
    thread_id: str | None = None
    kind: Literal["target_ambiguous", "protected_component"]
    round: int = Field(default=1, ge=1)
    max_rounds: int = Field(default=MAX_DECISION_ROUNDS, ge=1, le=MAX_DECISION_ROUNDS)
    reason: str = ""
    summary: str = ""
    # 目标证据：文件 / 范围 / 组件名 / 共享原因，仅供展示与审计。
    target: dict[str, Any] = Field(default_factory=dict)
    options: list[DecisionOption] = Field(default_factory=list)
    status: Literal["PENDING", "ANSWERED", "CANCELLED", "EXPIRED"] = "PENDING"
    expires_at: datetime | None = None
    answered_at: datetime | None = None
    selected_option_id: str | None = None


class DecisionRequest(CamelModel):
    """用户选择的恢复请求。"""

    option_id: str = Field(min_length=1, max_length=160)
    comment: str | None = Field(default=None, max_length=1000)
    expected_version: int | None = None
    # 内部兼容：POST 路径没有 decisionId 时可放在 body。
    decision_id: str | None = Field(default=None, max_length=160)


class DecisionRunStatus(CamelModel):
    run_id: str
    status: str


class DecisionStatus(CamelModel):
    decision_id: str
    status: Literal["ANSWERED", "CANCELLED"]
    option_id: str | None = None
    answered_at: datetime


class DecisionAnswerResponse(CamelModel):
    decision: DecisionStatus
    run: DecisionRunStatus


class DecisionListResponse(CamelModel):
    run_id: str
    decisions: list[PendingDecision] = Field(default_factory=list)


__all__ = [
    "ACTION_CANCEL",
    "ACTION_MODIFY_CALL_SITE",
    "ACTION_SELECT_TARGET",
    "AWAITING_DECISION",
    "DECISION_ANSWERED_EVENT",
    "DECISION_ERROR_CANCELLED",
    "DECISION_ERROR_LIMIT",
    "DECISION_ERROR_OPTION_INVALID",
    "DECISION_KIND_AMBIGUOUS",
    "DECISION_KIND_PROTECTED",
    "DECISION_REQUIRED_EVENT",
    "DecisionAction",
    "DecisionAnswerResponse",
    "DecisionListResponse",
    "DecisionOption",
    "DecisionRequest",
    "DecisionRunStatus",
    "DecisionStatus",
    "FORBIDDEN_DECISION_ACTIONS",
    "MAX_DECISION_ROUNDS",
    "PendingDecision",
    "SAFE_DECISION_ACTIONS",
    "decision_id_for",
]
