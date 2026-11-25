"""全局 Run 并发舱壁测试。

锁定三条契约：
  1. 同时进入 Graph 的 Run 不超过配置上限；
  2. 上限之外的 Run 在后台线程等待，不抛错、不影响 HTTP 返回；
  3. worker 抛异常时槽位必须归还，不能永久泄漏。
"""

from __future__ import annotations

import threading
import time
import unittest

from ai_core.run_guard import RunConcurrencyGuard, reset_run_guard_for_tests


class RunConcurrencyGuardTest(unittest.TestCase):
    def test_blocks_excess_workers_until_slot_released(self) -> None:
        guard = RunConcurrencyGuard(2)
        entered: list[int] = []
        gate = threading.Event()
        lock = threading.Lock()

        def worker(index: int) -> None:
            with guard.slot():
                with lock:
                    entered.append(index)
                gate.wait(2)

        threads = [
            threading.Thread(target=worker, args=(index,), daemon=True)
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        deadline = time.time() + 2
        while len(entered) < 2 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(sorted(entered), [0, 1])
        self.assertEqual(guard.snapshot(), {"limit": 2, "inFlight": 2})

        third = threading.Thread(target=worker, args=(2,), daemon=True)
        third.start()
        time.sleep(0.15)
        self.assertNotIn(2, entered)
        self.assertEqual(guard.snapshot()["inFlight"], 2)

        gate.set()
        for thread in threads + [third]:
            thread.join(timeout=2)
        self.assertIn(2, entered)
        self.assertEqual(guard.snapshot()["inFlight"], 0)

    def test_wrap_releases_slot_after_exception(self) -> None:
        guard = RunConcurrencyGuard(1)

        def boom() -> None:
            raise RuntimeError("graph failed")

        wrapped = guard.wrap(boom)
        with self.assertRaises(RuntimeError):
            wrapped()

        self.assertEqual(guard.snapshot()["inFlight"], 0)
        with guard.slot():
            self.assertEqual(guard.snapshot()["inFlight"], 1)

    def test_reset_helper_replaces_global_guard(self) -> None:
        guard = reset_run_guard_for_tests(3)
        self.assertEqual(guard.limit, 3)


if __name__ == "__main__":
    unittest.main()
