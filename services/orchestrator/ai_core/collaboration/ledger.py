"""动作台账：没有记录的写操作视为未发生。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class LedgerError(ValueError):
    """动作记录不符合协议。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ActionRecord:
    run_id: str
    task_id: str
    actor: str
    tool: str
    target: str
    before_hash: str = ""
    after_hash: str = ""
    risk: str = "low"
    trace_id: str = ""
    created_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        if not str(self.run_id or "").strip():
            raise LedgerError("runId 不能为空")
        if not str(self.task_id or "").strip():
            raise LedgerError("缺少 taskId 的写操作必须拒绝")
        if not str(self.actor or "").strip():
            raise LedgerError("actor 不能为空")
        if not str(self.tool or "").strip():
            raise LedgerError("tool 不能为空")

    def to_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "taskId": self.task_id,
            "actor": self.actor,
            "tool": self.tool,
            "target": self.target,
            "beforeHash": self.before_hash,
            "afterHash": self.after_hash,
            "risk": self.risk,
            "traceId": self.trace_id,
            "createdAt": self.created_at,
        }


class ActionLedger:
    """按 run 保存写操作，供 Architect 查看与审计。"""

    def __init__(self, max_per_run: int = 500) -> None:
        self._lock = threading.Lock()
        self._max = max(1, int(max_per_run))
        self._by_run: dict[str, list[ActionRecord]] = {}

    def record(
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
        item = ActionRecord(
            run_id=str(run_id),
            task_id=str(task_id or ""),
            actor=str(actor),
            tool=str(tool),
            target=str(target),
            before_hash=str(before_hash),
            after_hash=str(after_hash),
            risk=str(risk),
            trace_id=str(trace_id),
        )
        with self._lock:
            items = self._by_run.setdefault(item.run_id, [])
            items.append(item)
            if len(items) > self._max:
                del items[: len(items) - self._max]
        return item

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [item.to_dict() for item in self._by_run.get(str(run_id), [])]

    def export_run(self, run_id: str) -> list[dict[str, Any]]:
        return self.list_for_run(run_id)

    def import_run(self, run_id: str, data: list[dict[str, Any]] | None) -> None:
        records: list[ActionRecord] = []
        for item in data or []:
            try:
                records.append(
                    ActionRecord(
                        run_id=str(item.get("runId") or run_id),
                        task_id=str(item.get("taskId") or ""),
                        actor=str(item.get("actor") or ""),
                        tool=str(item.get("tool") or ""),
                        target=str(item.get("target") or ""),
                        before_hash=str(item.get("beforeHash") or ""),
                        after_hash=str(item.get("afterHash") or ""),
                        risk=str(item.get("risk") or "low"),
                        trace_id=str(item.get("traceId") or ""),
                        created_at=str(item.get("createdAt") or _now()),
                    )
                )
            except LedgerError:
                continue
        with self._lock:
            self._by_run[str(run_id)] = records[-self._max :]

    def clear(self, run_id: str) -> None:
        with self._lock:
            self._by_run.pop(str(run_id), None)

    def reset(self) -> None:
        with self._lock:
            self._by_run.clear()


__all__ = ["ActionLedger", "ActionRecord", "LedgerError"]
