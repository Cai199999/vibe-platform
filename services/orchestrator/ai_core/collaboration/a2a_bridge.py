"""把现有 A2A 反馈接入 Architect 收件箱。

不替换 ``ai_core.a2a``：反馈仍由原有信封承载，这里只做三件事：
1. 把任意 ``toAgent`` 改道为 Architect；
2. 写入收件箱并保留完整载荷引用；
3. 通过现有 EventSink 发出可观测事件。
"""

from __future__ import annotations

from typing import Any, Mapping

from ai_core.agent.events import EventSink
from ai_core.collaboration.fastpath import FastPathGrant, FastPathPolicy
from ai_core.collaboration.inbox import ArchitectInbox
from ai_core.collaboration.ledger import ActionLedger, ActionRecord
from ai_core.collaboration.protocol import (
    AgentReport,
    report_from_a2a,
    route_a2a_to_architect,
)


class CollaborationBridge:
    def __init__(
        self,
        *,
        inbox: ArchitectInbox | None = None,
        ledger: ActionLedger | None = None,
        sink: EventSink | None = None,
        fastpath: FastPathPolicy | None = None,
    ) -> None:
        self.inbox = inbox or ArchitectInbox()
        self.ledger = ledger or ActionLedger()
        self.sink = sink
        self.fastpath = fastpath or FastPathPolicy()

    def issue_fast_path(
        self,
        *,
        run_id: str,
        task_id: str,
        edges=None,
        ttl_seconds: float = 600.0,
        max_steps: int = 50,
    ) -> FastPathGrant:
        """Architect 预先授权标准链路直连。"""
        grant = self.fastpath.issue(
            run_id=run_id,
            task_id=task_id,
            edges=edges,
            ttl_seconds=ttl_seconds,
            max_steps=max_steps,
        )
        self._emit(
            "agent.fastpath.granted",
            grant.to_dict(),
            dedupe_key=f"agent.fastpath.granted:{grant.grant_id}",
        )
        return grant

    def revoke_fast_path(self, grant_id: str, reason: str) -> FastPathGrant:
        grant = self.fastpath.revoke(grant_id, reason)
        self._emit(
            "agent.fastpath.revoked",
            grant.to_dict(),
            dedupe_key=f"agent.fastpath.revoked:{grant.grant_id}",
        )
        return grant

    def ingest_feedback(
        self,
        feedback: Any,
        *,
        task_id: str = "",
        grant_id: str = "",
        step_state: dict[str, Any] | None = None,
    ) -> AgentReport:
        """接收 A2A 反馈：符合快速通道则直连，否则改道 Architect。

        无论哪种模式，都会向 Architect 收件箱写入摘要；区别只是是否阻塞。
        """
        data = _feedback_dict(feedback)
        decision = self.fastpath.check(
            grant_id,
            from_agent=str(data.get("fromAgent") or ""),
            to_agent=str(data.get("toAgent") or ""),
            kind=str(data.get("kind") or ""),
            requested_action=str(data.get("requestedAction") or ""),
            step_state=step_state,
        )
        if decision.revoked:
            grant = self.fastpath.get(grant_id)
            self._emit(
                "agent.fastpath.revoked",
                grant.to_dict(),
                dedupe_key=f"agent.fastpath.revoked:{grant.grant_id}",
            )

        payload = _payload_of(feedback)
        if decision.allowed:
            report = report_from_a2a(
                feedback,
                task_id=task_id,
                delivery_mode="fast_path",
            )
            self.fastpath.record_step(grant_id)
            self.inbox.submit(report, payload=payload)
            self._emit(
                "agent.report",
                {
                    **report.to_dict(),
                    "direct": True,
                    "fastPathReason": decision.reason,
                },
                dedupe_key=f"agent.report:{report.report_id}",
            )
            return report

        report = route_a2a_to_architect(feedback, task_id=task_id)
        self.inbox.submit(report, payload=payload)
        self._emit(
            "agent.report",
            {
                **report.to_dict(),
                "direct": False,
                "rerouted": bool(report.original_target),
                "fastPathReason": decision.reason,
            },
            dedupe_key=f"agent.report:{report.report_id}",
        )
        return report

    def record_action(
        self,
        *,
        run_id: str,
        task_id: str,
        actor: str,
        tool: str,
        target: str,
        before_hash: str = "",
        after_hash: str = "",
        risk: str = "low",
        trace_id: str = "",
    ) -> ActionRecord:
        """记录写操作；缺少 taskId 时由 ActionRecord 直接拒绝。"""
        record = self.ledger.record(
            run_id=run_id,
            task_id=task_id,
            actor=actor,
            tool=tool,
            target=target,
            before_hash=before_hash,
            after_hash=after_hash,
            risk=risk,
            trace_id=trace_id,
        )
        self._emit(
            "agent.action",
            record.to_dict(),
            dedupe_key=(
                f"agent.action:{record.run_id}:{record.task_id}:"
                f"{record.actor}:{record.tool}:{record.created_at}"
            ),
        )
        return record

    def _emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str,
    ) -> None:
        if self.sink is None:
            return
        self.sink.emit(event_type, payload, dedupe_key=dedupe_key)


def _feedback_dict(feedback: Any) -> dict[str, Any]:
    if isinstance(feedback, Mapping):
        return dict(feedback)
    to_dict = getattr(feedback, "to_dict", None)
    return dict(to_dict()) if callable(to_dict) else {}


def _payload_of(feedback: Any) -> dict[str, Any]:
    payload = _feedback_dict(feedback).get("payload")
    return dict(payload) if isinstance(payload, Mapping) else {}


__all__ = ["CollaborationBridge"]
