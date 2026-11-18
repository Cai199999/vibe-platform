"""SourceSync 补偿队列与重试 worker 测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_core.direct_ops.source_sync import SourceSnapshotSync
from ai_core.direct_ops.source_sync_queue import (
    SourceSyncQueue,
    SourceSyncRetryWorker,
)


class _Sandbox:
    def file_exists(self, sandbox_id: str, path: str, draft_id=None) -> bool:
        return True

    def read_file(self, sandbox_id: str, path: str) -> str:
        return "export default 1"


class _FailingSync(SourceSnapshotSync):
    def __init__(self) -> None:
        super().__init__(base_url="http://bff:8080", enabled=True)

    def sync(self, **_kwargs):
        return {"status": "FAILED", "message": "storage down"}


class _OkSync(SourceSnapshotSync):
    def __init__(self) -> None:
        super().__init__(base_url="http://bff:8080", enabled=True)

    def sync(self, **_kwargs):
        return {"status": "SYNCED", "revision": 2}


class SourceSyncQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "queue.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_add_due_remove(self) -> None:
        queue = SourceSyncQueue(self.path)
        key = queue.add(
            {
                "projectId": "proj_1",
                "sandboxId": "sbx_1",
                "tenantId": "tenant_local",
                "touchedFile": "src/App.jsx",
                "commitHash": "a" * 40,
            }
        )
        self.assertEqual(queue.size(), 1)
        due = queue.due()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["key"], key)
        queue.remove(key)
        self.assertEqual(queue.size(), 0)

    def test_reschedule_backoff_and_max_attempts(self) -> None:
        queue = SourceSyncQueue(self.path)
        key = queue.add(
            {
                "projectId": "proj_1",
                "sandboxId": "sbx_1",
                "touchedFile": "src/App.jsx",
                "commitHash": "b" * 40,
            }
        )
        self.assertTrue(queue.reschedule(key, "first failure"))
        self.assertEqual(queue.due(), [])
        self.assertTrue(queue.reschedule(key, "second failure"))
        self.assertEqual(queue.size(), 1)

    def test_worker_removes_successful_task(self) -> None:
        queue = SourceSyncQueue(self.path)
        queue.add(
            {
                "projectId": "proj_1",
                "sandboxId": "sbx_1",
                "touchedFile": "src/App.jsx",
                "commitHash": "c" * 40,
            }
        )
        worker = SourceSyncRetryWorker(
            queue,
            sync_factory=lambda: _OkSync(),
            sandbox_provider=_Sandbox(),  # type: ignore[arg-type]
            interval_seconds=60,
        )

        worker.run_once()

        self.assertEqual(queue.size(), 0)

    def test_worker_reschedules_failed_task(self) -> None:
        queue = SourceSyncQueue(self.path)
        key = queue.add(
            {
                "projectId": "proj_1",
                "sandboxId": "sbx_1",
                "touchedFile": "src/App.jsx",
                "commitHash": "e" * 40,
            }
        )
        worker = SourceSyncRetryWorker(
            queue,
            sync_factory=lambda: _FailingSync(),
            sandbox_provider=_Sandbox(),  # type: ignore[arg-type]
            interval_seconds=60,
        )

        worker.run_once()

        self.assertEqual(queue.size(), 1)
        task = queue._load()[key]  # noqa: SLF001 - 验证退避字段
        self.assertGreaterEqual(int(task["attempts"]), 1)
        self.assertGreater(float(task["nextAt"]), 0)


if __name__ == "__main__":
    unittest.main()
