"""运行态持久化：事件回放、快照与已确认契约恢复。"""

from __future__ import annotations

import asyncio
import unittest

from ai_core.agent.events import (
    RunEventBuffer,
    get_persisted_event_buffer,
    reset_event_buffers_for_tests,
)
from ai_core.config import Settings
from ai_core.persistence.run_store import (
    MemoryRunStore,
    get_run_store,
    reset_run_store_for_tests,
)


class MemoryRunStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            run_store_backend="memory",
            run_event_max=10,
        )
        self.store = MemoryRunStore(self.settings)
        reset_run_store_for_tests(self.store)
        reset_event_buffers_for_tests()

    def tearDown(self) -> None:
        reset_event_buffers_for_tests()
        reset_run_store_for_tests(MemoryRunStore(self.settings))

    def test_append_and_replay_respects_after_seq(self) -> None:
        for seq in range(1, 5):
            self.store.append_event(
                "run_a",
                {"eventId": f"e{seq}", "runId": "run_a", "seq": seq, "type": "x"},
            )

        replay = self.store.list_events("run_a", after_seq=2)

        self.assertEqual([item["seq"] for item in replay], [3, 4])

    def test_snapshot_and_contract_roundtrip(self) -> None:
        self.store.save_snapshot("ui", "run_a", {"status": "RUNNING", "lastSeq": 3})
        self.store.save_contract("c1", {"contractId": "c1", "status": "CONFIRMED"})

        self.assertEqual(
            self.store.load_snapshot("ui", "run_a"),
            {"status": "RUNNING", "lastSeq": 3},
        )
        self.assertEqual(
            self.store.load_contract("c1"),
            {"contractId": "c1", "status": "CONFIRMED"},
        )
        self.assertIsNone(self.store.load_contract("missing"))

    def test_event_buffer_persists_and_hydrates(self) -> None:
        buffer = RunEventBuffer("run_persist")
        buffer.emit("run.started", {"runId": "run_persist"})
        buffer.emit("run.progress", {"step": "planner"})

        restored = RunEventBuffer("run_persist")
        self.assertTrue(restored.hydrate_from_store())
        self.assertEqual(restored.last_seq, 2)
        self.assertEqual(
            [event.type for event in restored.snapshot()],
            ["run.started", "run.progress"],
        )
        self.assertTrue(restored.restored)

    def test_snapshot_provider_persists_run_and_confirmed_contract(self) -> None:
        buffer = RunEventBuffer("run_snapshot")
        buffer.attach_snapshot(
            "architect",
            lambda: {
                "runId": "run_snapshot",
                "status": "CONFIRMED",
                "contract": {
                    "contractId": "ctr_1",
                    "status": "CONFIRMED",
                },
            },
        )

        buffer.emit("architect.contract.confirmed", {})

        snapshot = get_run_store().load_snapshot("architect", "run_snapshot")
        self.assertEqual(snapshot["status"], "CONFIRMED")
        self.assertEqual(
            get_run_store().load_contract("ctr_1"),
            {"contractId": "ctr_1", "status": "CONFIRMED"},
        )

    def test_restored_paused_run_gets_interrupted_event(self) -> None:
        buffer = RunEventBuffer("run_paused")
        buffer.emit("architect.started", {"runId": "run_paused"})
        buffer.emit("architect.clarification.required", {"questionId": "q1"})
        reset_event_buffers_for_tests()

        restored = get_persisted_event_buffer(
            "run_paused", terminal_type="architect.failed"
        )

        assert restored is not None
        self.assertEqual(restored.snapshot()[-1].type, "architect.failed")
        self.assertEqual(restored.snapshot()[-1].payload["code"], "RUN_INTERRUPTED")

        async def collect() -> list[str]:
            return [event.type async for event in restored.subscribe(0)]

        self.assertEqual(
            asyncio.run(collect()),
            [
                "architect.started",
                "architect.clarification.required",
                "architect.failed",
            ],
        )

    def test_restored_confirmed_contract_is_not_marked_interrupted(self) -> None:
        buffer = RunEventBuffer("run_confirmed")
        buffer.emit("architect.started", {"runId": "run_confirmed"})
        buffer.emit("architect.contract.confirmed", {"contractId": "ctr_done"})
        reset_event_buffers_for_tests()

        restored = get_persisted_event_buffer(
            "run_confirmed", terminal_type="architect.failed"
        )

        assert restored is not None
        self.assertEqual(
            restored.snapshot()[-1].type,
            "architect.contract.confirmed",
        )

    def test_restored_run_without_terminal_gets_interrupted_event(self) -> None:
        buffer = RunEventBuffer("run_orphan")
        buffer.emit("run.started", {"runId": "run_orphan"})
        reset_event_buffers_for_tests()

        restored = get_persisted_event_buffer("run_orphan", terminal_type="run.failed")

        self.assertIsNotNone(restored)
        assert restored is not None
        events = restored.snapshot()
        self.assertEqual(events[-1].type, "run.failed")
        self.assertEqual(events[-1].payload["code"], "RUN_INTERRUPTED")
        self.assertEqual(events[-1].seq, 2)


if __name__ == "__main__":
    unittest.main()
