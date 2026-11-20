"""Agent-to-Agent（A2A）反馈协议。

平台里的多 Agent 不是单向流水线：Applier / ConsistencyVerifier / DBACoder /
BackendCoder / Architect 之间需要携带诊断、请求修复、回传修订结果。这里提供
一个统一的反馈信封与运行级日志：

- 每个反馈都有 ``from_agent`` / ``to_agent`` / ``kind`` / ``requested_action``
  与 ``evidence``，便于前端渲染“谁向谁提出了什么”；
- ``A2AFeedbackLog`` 只做内存保存（单进程、按 run 隔离、有上限），
  失败事件 / 运行快照可以直接读取，不依赖数据库；
- 事件名统一为 ``a2a.feedback``，与既有 ``fullstack.*`` / ``architect.*``
  事件并行，不替换原有诊断事件。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

MAX_FEEDBACK_PER_RUN = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class A2AFeedback:
    feedback_id: str
    run_id: str
    from_agent: str
    to_agent: str
    kind: str
    requested_action: str
    summary: str
    evidence: tuple[str, ...] = ()
    contract_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "feedbackId": self.feedback_id,
            "runId": self.run_id,
            "fromAgent": self.from_agent,
            "toAgent": self.to_agent,
            "kind": self.kind,
            "requestedAction": self.requested_action,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "contractId": self.contract_id,
            "payload": dict(self.payload),
            "createdAt": self.created_at or _now(),
        }


def build_feedback(
    *,
    run_id: str,
    from_agent: str,
    to_agent: str,
    kind: str,
    requested_action: str,
    summary: str,
    evidence: Iterable[str] = (),
    contract_id: str = "",
    payload: dict[str, Any] | None = None,
) -> A2AFeedback:
    return A2AFeedback(
        feedback_id=f"a2a_{uuid.uuid4().hex[:20]}",
        run_id=str(run_id or ""),
        from_agent=str(from_agent or ""),
        to_agent=str(to_agent or ""),
        kind=str(kind or "feedback"),
        requested_action=str(requested_action or "REVIEW"),
        summary=str(summary or "")[:2000],
        evidence=tuple(str(item)[:1000] for item in evidence if str(item).strip()),
        contract_id=str(contract_id or ""),
        payload=dict(payload or {}),
        created_at=_now(),
    )


class A2AFeedbackLog:
    """按 run_id 保存反馈；只保留最近 ``MAX_FEEDBACK_PER_RUN`` 条。"""

    def __init__(self, max_per_run: int = MAX_FEEDBACK_PER_RUN) -> None:
        self._lock = threading.Lock()
        self._max = max(1, int(max_per_run))
        self._by_run: dict[str, list[dict[str, Any]]] = {}

    def publish(self, feedback: A2AFeedback | dict[str, Any]) -> dict[str, Any]:
        item = (
            feedback.to_dict()
            if isinstance(feedback, A2AFeedback)
            else dict(feedback)
        )
        run_id = str(item.get("runId") or "")
        with self._lock:
            items = self._by_run.setdefault(run_id, [])
            items.append(item)
            if len(items) > self._max:
                del items[: len(items) - self._max]
        return item

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._by_run.get(str(run_id), [])]

    def clear(self, run_id: str) -> None:
        with self._lock:
            self._by_run.pop(str(run_id), None)

    def reset(self) -> None:
        with self._lock:
            self._by_run.clear()


# 单进程单例：FullstackGraph / ArchitectGraph 共用。
A2A_LOG = A2AFeedbackLog()


__all__ = [
    "A2AFeedback",
    "A2AFeedbackLog",
    "A2A_LOG",
    "MAX_FEEDBACK_PER_RUN",
    "build_feedback",
]
