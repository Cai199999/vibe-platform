"""Architect 收件箱：所有子 Agent 报告的唯一入口。"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from typing import Any

from ai_core.collaboration.protocol import AgentReport, ReportError

MAX_REPORTS_PER_RUN = 200


class InboxError(ValueError):
    """收件箱协议错误。"""


@dataclass(frozen=True)
class RelayView:
    """Architect 读取一份报告的视图。"""

    report_id: str
    mode: str
    summary: str
    payload: dict[str, Any] | None
    full_payload_ref: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "reportId": self.report_id,
            "mode": self.mode,
            "summary": self.summary,
            "payload": dict(self.payload) if self.payload is not None else None,
            "fullPayloadRef": self.full_payload_ref,
        }


class ArchitectInbox:
    """按 run 隔离、线程安全的报告收件箱。

    默认只保存摘要与完整载荷的引用；Architect 选择 ``full`` 时才返回
    完整载荷，避免把大对象带进主对话上下文。
    """

    def __init__(self, max_per_run: int = MAX_REPORTS_PER_RUN) -> None:
        self._lock = threading.Lock()
        self._max = max(1, int(max_per_run))
        self._by_run: dict[str, list[AgentReport]] = {}
        self._payloads: dict[str, dict[str, Any]] = {}

    def submit(
        self,
        report: AgentReport,
        *,
        payload: dict[str, Any] | None = None,
    ) -> AgentReport:
        if report.agent == "Architect":
            raise InboxError("Architect 不接收自己的报告")
        with self._lock:
            items = self._by_run.setdefault(report.run_id, [])
            items.append(report)
            if len(items) > self._max:
                del items[: len(items) - self._max]
            if payload is not None:
                self._payloads[report.full_payload_ref] = dict(payload)
        return report

    def summaries(self, run_id: str) -> list[dict[str, Any]]:
        """Architect 的默认视图：只有摘要，不含完整载荷。"""
        with self._lock:
            return [
                {
                    "reportId": report.report_id,
                    "agent": report.agent,
                    "taskId": report.task_id,
                    "status": report.status,
                    "summary": report.summary,
                    "originalTarget": report.original_target,
                    "deliveryMode": report.delivery_mode,
                    "createdAt": report.created_at,
                }
                for report in self._by_run.get(str(run_id), [])
            ]

    def relay(
        self,
        run_id: str,
        report_id: str,
        *,
        mode: str = "summary",
    ) -> RelayView:
        """Architect 决定读取摘要还是完整请求。"""
        if mode not in {"summary", "full"}:
            raise InboxError("mode 只能是 summary 或 full")
        with self._lock:
            report = next(
                (
                    item
                    for item in self._by_run.get(str(run_id), [])
                    if item.report_id == report_id
                ),
                None,
            )
            if report is None:
                raise InboxError(f"报告不存在: {report_id}")
            payload = (
                dict(self._payloads.get(report.full_payload_ref, {}))
                if mode == "full"
                else None
            )
        return RelayView(
            report_id=report.report_id,
            mode=mode,
            summary=report.summary,
            payload=payload,
            full_payload_ref=report.full_payload_ref,
        )

    def export_run(self, run_id: str) -> dict[str, Any]:
        """导出可持久化的最小状态（不含完整载荷）。"""
        with self._lock:
            return {
                "reports": [
                    asdict(report) for report in self._by_run.get(str(run_id), [])
                ]
            }

    def import_run(self, run_id: str, data: dict[str, Any] | None) -> None:
        reports: list[AgentReport] = []
        for item in (data or {}).get("reports", []):
            try:
                reports.append(AgentReport(**item))
            except (TypeError, ReportError):
                continue
        with self._lock:
            self._by_run[str(run_id)] = reports[-self._max :]

    def clear(self, run_id: str) -> None:
        with self._lock:
            self._by_run.pop(str(run_id), None)

    def reset(self) -> None:
        with self._lock:
            self._by_run.clear()
            self._payloads.clear()


__all__ = [
    "ArchitectInbox",
    "InboxError",
    "MAX_REPORTS_PER_RUN",
    "RelayView",
]
