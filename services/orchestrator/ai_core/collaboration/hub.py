"""进程级 Architect 收件箱：跨图共享 + RunStore 快照持久化。"""

from __future__ import annotations

import logging
from typing import Any

from ai_core.collaboration.a2a_bridge import CollaborationBridge
from ai_core.collaboration.fastpath import FastPathGrant, FastPathPolicy
from ai_core.collaboration.inbox import ArchitectInbox
from ai_core.collaboration.ledger import ActionLedger, ActionRecord
from ai_core.collaboration.protocol import AgentReport

logger = logging.getLogger(__name__)

SNAPSHOT_KIND = "collaboration"


class ArchitectHub:
    """共享收件箱 / 台账 / 快速通道，按 run 持久化。"""

    def __init__(self) -> None:
        self.inbox = ArchitectInbox()
        self.ledger = ActionLedger()
        self.fastpath = FastPathPolicy()
        self._loaded_runs: set[str] = set()
        self._known_runs: set[str] = set()
        self._store: Any | None = None

    def _run_store(self) -> Any:
        if self._store is None:
            from ai_core.persistence.run_store import get_run_store

            self._store = get_run_store()
        return self._store

    def save(self, run_id: str) -> None:
        run_id = str(run_id or "")
        if not run_id:
            return
        try:
            self._run_store().save_snapshot(
                SNAPSHOT_KIND,
                run_id,
                {
                    "inbox": self.inbox.export_run(run_id),
                    "actions": self.ledger.export_run(run_id),
                    "grants": self.fastpath.export_run(run_id),
                },
            )
            self._loaded_runs.add(run_id)
            self._known_runs.add(run_id)
        except Exception:  # pragma: no cover - 持久化失败不阻断主流程
            logger.exception("协作状态持久化失败 run=%s", run_id)

    def ensure_loaded(self, run_id: str) -> None:
        run_id = str(run_id or "")
        if not run_id or run_id in self._loaded_runs:
            return
        try:
            data = self._run_store().load_snapshot(SNAPSHOT_KIND, run_id)
        except Exception:  # pragma: no cover
            logger.exception("协作状态读取失败 run=%s", run_id)
            data = None
        if data:
            self.inbox.import_run(run_id, data.get("inbox"))
            self.ledger.import_run(run_id, data.get("actions"))
            self.fastpath.import_run(run_id, data.get("grants"))
        self._loaded_runs.add(run_id)

    def ingest_feedback(
        self,
        feedback: Any,
        *,
        task_id: str = "",
        sink: Any | None = None,
        grant_id: str = "",
        step_state: dict[str, Any] | None = None,
    ) -> AgentReport | None:
        data = _as_dict(feedback)
        if str(data.get("fromAgent") or "") == "Architect":
            return None
        bridge = self._bridge(sink)
        report = bridge.ingest_feedback(
            feedback,
            task_id=task_id,
            grant_id=grant_id,
            step_state=step_state,
        )
        self.save(report.run_id)
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
        sink: Any | None = None,
    ) -> ActionRecord:
        record = self._bridge(sink).record_action(
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
        self.save(record.run_id)
        return record

    def issue_fast_path(
        self,
        *,
        run_id: str,
        task_id: str,
        edges: Any = None,
        ttl_seconds: float = 600.0,
        max_steps: int = 50,
        sink: Any | None = None,
    ) -> FastPathGrant:
        grant = self._bridge(sink).issue_fast_path(
            run_id=run_id,
            task_id=task_id,
            edges=edges,
            ttl_seconds=ttl_seconds,
            max_steps=max_steps,
        )
        self.save(run_id)
        return grant

    def revoke_fast_path(
        self,
        grant_id: str,
        reason: str,
        *,
        sink: Any | None = None,
    ) -> FastPathGrant:
        grant = self._bridge(sink).revoke_fast_path(grant_id, reason)
        self.save(grant.run_id)
        return grant

    def snapshot(self, run_id: str) -> dict[str, Any]:
        self.ensure_loaded(run_id)
        return {
            "inbox": self.inbox.summaries(run_id),
            "actions": self.ledger.list_for_run(run_id),
            "grants": self.fastpath.list_for_run(run_id),
        }

    def summaries(self, run_id: str) -> list[dict[str, Any]]:
        return self.snapshot(run_id)["inbox"]

    def reset(self) -> None:
        # 测试 / 本地清理：内存与快照一起清空，避免下次 ensure_loaded 复活旧数据。
        for run_id in list(self._known_runs):
            try:
                self._run_store().save_snapshot(
                    SNAPSHOT_KIND,
                    run_id,
                    {
                        "inbox": {"reports": []},
                        "actions": [],
                        "grants": [],
                    },
                )
            except Exception:  # pragma: no cover
                logger.exception("协作状态清理失败 run=%s", run_id)
        self.inbox.reset()
        self.ledger.reset()
        self.fastpath.reset()
        self._loaded_runs.clear()
        self._known_runs.clear()

    def _bridge(self, sink: Any | None) -> CollaborationBridge:
        return CollaborationBridge(
            inbox=self.inbox,
            ledger=self.ledger,
            fastpath=self.fastpath,
            sink=sink,
        )


def _as_dict(feedback: Any) -> dict[str, Any]:
    if isinstance(feedback, dict):
        return dict(feedback)
    to_dict = getattr(feedback, "to_dict", None)
    return dict(to_dict()) if callable(to_dict) else {}


ARCHITECT_HUB = ArchitectHub()


__all__ = ["ARCHITECT_HUB", "ArchitectHub", "SNAPSHOT_KIND"]
