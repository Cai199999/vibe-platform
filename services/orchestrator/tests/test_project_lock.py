"""项目级写锁：AI Run / Direct Ops 不能同时写主工作树。"""

from __future__ import annotations

import threading
import time
import unittest

from ai_core.project_lock import ProjectBusyError, ProjectWriteLock


class ProjectWriteLockTest(unittest.TestCase):
    def test_only_one_writer_can_hold_project_lock(self) -> None:
        lock = ProjectWriteLock()
        guard = threading.Lock()
        active = 0
        max_active = 0
        errors: list[str] = []

        def worker(index: int) -> None:
            nonlocal active, max_active
            try:
                for _ in range(20):
                    with lock.lease(
                        "proj_concurrent",
                        owner=f"worker-{index}",
                        timeout=3,
                    ):
                        with guard:
                            active += 1
                            max_active = max(max_active, active)
                        time.sleep(0.001)
                        with guard:
                            active -= 1
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(max_active, 1)
        self.assertEqual(lock.snapshot()["held"], {})

    def test_timeout_returns_project_busy(self) -> None:
        lock = ProjectWriteLock()
        lease = lock.acquire("proj_busy", owner="ai-run", timeout=1)
        try:
            with self.assertRaises(ProjectBusyError):
                lock.acquire("proj_busy", owner="direct-op", timeout=0.05)
        finally:
            lock.release(lease)

        snapshot = lock.snapshot()
        self.assertGreaterEqual(snapshot["timeouts"], 1)
        self.assertEqual(snapshot["held"], {})

    def test_different_projects_do_not_block_each_other(self) -> None:
        lock = ProjectWriteLock()
        lease_a = lock.acquire("proj_a", owner="a", timeout=1)
        lease_b = lock.acquire("proj_b", owner="b", timeout=1)
        try:
            self.assertEqual(
                set(lock.snapshot()["held"].values()),
                {"a", "b"},
            )
        finally:
            lock.release(lease_a)
            lock.release(lease_b)


if __name__ == "__main__":
    unittest.main()
