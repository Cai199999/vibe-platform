"""Agent 协作的结构化消息协议。

原型阶段只做最小闭环：复用现有 ``A2AFeedback`` 作为传输信封，在其上叠加
面向 Architect 的 ``AgentReport``。这样不会推翻现有 A2A，也不需要新的
消息中间件。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

ARCHITECT = "Architect"
REPORT_STATUSES = frozenset({"done", "blocked", "needs_clarification", "failed"})
DELIVERY_MODES = frozenset({"hub", "fast_path"})
MAX_SUMMARY_CHARS = 200


class ReportError(ValueError):
    """子 Agent 报告不符合协议。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _as_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    to_dict = getattr(item, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    raise ReportError("反馈必须是 dict 或带 to_dict() 的对象")


def _clip(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


@dataclass(frozen=True)
class AgentReport:
    """子 Agent 向 Architect 的单次汇报。"""

    report_id: str
    run_id: str
    agent: str
    task_id: str
    status: str
    summary: str
    decisions: tuple[str, ...] = ()
    risks: tuple[dict[str, Any], ...] = ()
    needs: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[dict[str, Any], ...] = ()
    full_payload_ref: str = ""
    original_target: str = ""
    delivery_mode: str = "hub"
    created_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        if not str(self.agent or "").strip():
            raise ReportError("agent 不能为空")
        if str(self.agent) == ARCHITECT:
            raise ReportError("Architect 不向自己提交 AgentReport")
        if not str(self.task_id or "").strip():
            raise ReportError("taskId 不能为空")
        if str(self.status) not in REPORT_STATUSES:
            raise ReportError(f"未知 status: {self.status}")
        if str(self.delivery_mode) not in DELIVERY_MODES:
            raise ReportError(f"未知 deliveryMode: {self.delivery_mode}")
        if not str(self.summary or "").strip():
            raise ReportError("summary 必填；缺失视为非法输出")

    def to_dict(self) -> dict[str, Any]:
        return {
            "reportId": self.report_id,
            "runId": self.run_id,
            "agent": self.agent,
            "taskId": self.task_id,
            "status": self.status,
            "summary": self.summary,
            "decisions": list(self.decisions),
            "risks": [dict(item) for item in self.risks],
            "needs": [dict(item) for item in self.needs],
            "artifacts": [dict(item) for item in self.artifacts],
            "fullPayloadRef": self.full_payload_ref,
            "originalTarget": self.original_target,
            "deliveryMode": self.delivery_mode,
            "createdAt": self.created_at,
        }


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value if str(item).strip())


def _dict_tuple(value: Any) -> tuple[dict[str, Any], ...]:
    if not value:
        return ()
    if isinstance(value, Mapping):
        return (dict(value),)
    return tuple(dict(item) for item in value if isinstance(item, Mapping))


def report_from_a2a(
    item: Any,
    *,
    task_id: str = "",
    status: str = "done",
    delivery_mode: str = "hub",
) -> AgentReport:
    """把既有 A2A 反馈转换为 Architect 可读报告。

    原信封中的 ``toAgent`` 不会被丢弃：若它不是 Architect，会记录到
    ``original_target``，由 Architect 决定是否转发。
    """
    data = _as_dict(item)
    summary = _clip(data.get("summary", ""))
    payload = data.get("payload")
    payload = dict(payload) if isinstance(payload, Mapping) else {}
    feedback_id = str(data.get("feedbackId") or _new_id("a2a"))
    run_id = str(data.get("runId") or "")
    original_target = str(data.get("toAgent") or "")
    agent = str(data.get("fromAgent") or "").strip()
    if not agent:
        raise ReportError("A2A 反馈缺少 fromAgent")

    requested_action = str(data.get("requestedAction") or "")
    if status == "done" and requested_action.upper() in {
        "REPAIR_CODE",
        "REGENERATE_DB",
        "REGENERATE_ORM",
        "REPAIR_BACKEND_JOIN",
        "ADD_BACKEND_ROUTE",
        "REVISE_CONTRACT",
        "REVIEW_CONTRACT",
    }:
        status = "blocked"

    ref = str(
        payload.get("fullPayloadRef") or f"a2a://{run_id or 'unknown'}/{feedback_id}"
    )
    return AgentReport(
        report_id=_new_id("rep"),
        run_id=run_id,
        agent=agent,
        task_id=task_id or str(payload.get("taskId") or feedback_id),
        status=status,
        summary=summary,
        decisions=_string_tuple(payload.get("decisions")),
        risks=_dict_tuple(payload.get("risks")),
        needs=_dict_tuple(payload.get("needs")),
        artifacts=_dict_tuple(payload.get("artifacts")),
        full_payload_ref=ref,
        original_target=(
            original_target
            if delivery_mode == "fast_path"
            else ("" if original_target == ARCHITECT else original_target)
        ),
        delivery_mode=delivery_mode,
    )


def route_a2a_to_architect(
    item: Any,
    *,
    task_id: str = "",
    status: str = "done",
) -> AgentReport:
    """把任意 A2A 反馈改道为“提交给 Architect”。"""
    return report_from_a2a(item, task_id=task_id, status=status)


__all__ = [
    "ARCHITECT",
    "DELIVERY_MODES",
    "MAX_SUMMARY_CHARS",
    "REPORT_STATUSES",
    "AgentReport",
    "ReportError",
    "report_from_a2a",
    "route_a2a_to_architect",
]
