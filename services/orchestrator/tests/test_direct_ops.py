"""Direct Ops Phase 0/1：契约、写锁、revision 与 UPDATE_TEXT 闭环。"""

from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from typing import Any

from ai_core.direct_ops.changelog import MemoryChangeLogStore
from ai_core.direct_ops.service import (
    DirectActionError,
    DirectOperationService,
    get_direct_event_buffer,
)
from ai_core.domain.direct_ops import DirectActionPayload, DirectErrorCode
from ai_core.project_lock import project_write_lock

PROJECT_ID = "proj_direct"
SANDBOX_ID = "sbx_direct"
BASE_SHA = "a" * 40
NEXT_SHA = "b" * 40

SOURCE = """export default function App() {
  return (
    <main>
      <h1>待办</h1>
    </main>
  );
}
"""

UPDATED = SOURCE.replace("待办", "我的任务")


def _anchor(*, shared: bool = False) -> dict[str, Any]:
    return {
        "anchorId": "anc_dact_test",
        "annotationId": "dact_test",
        "status": "RESOLVED",
        "file": "src/App.jsx",
        "component": "App",
        "range": {"startOffset": 0, "endOffset": 10},
        "nodeSource": "<h1>待办</h1>",
        "isShared": shared,
        "protected": shared,
        "sharedReason": "被 2 个文件引用" if shared else "",
    }


class _FakeSandbox:
    def __init__(self, anchor: dict[str, Any] | None = None) -> None:
        self.head = BASE_SHA
        self.anchor = anchor or _anchor()
        self.patches: list[dict[str, str]] = []
        self.commits: list[str] = []
        self.edits: list[dict[str, Any]] = []
        self.resolutions: list[list[dict[str, Any]]] = []

    def get(self, sandbox_id: str) -> Any:
        return SimpleNamespace(project_id=PROJECT_ID, sandbox_id=sandbox_id)

    def git_head(self, sandbox_id: str) -> str:
        return self.head

    def git_commit(self, sandbox_id: str, message: str) -> str:
        self.commits.append(message)
        self.head = NEXT_SHA
        return NEXT_SHA

    def resolve_targets(
        self,
        sandbox_id: str,
        anchors: list[dict[str, Any]],
        draft_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.resolutions.append(anchors)
        return [dict(self.anchor)]

    def read_file(self, sandbox_id: str, path: str) -> str:
        return SOURCE

    def apply_ast_edit(
        self, sandbox_id: str, edit: dict[str, Any], draft_id: str | None = None
    ) -> dict[str, Any]:
        self.edits.append(dict(edit))
        return {"status": "OK", "content": UPDATED}

    def apply_patch(
        self,
        sandbox_id: str,
        files: dict[str, str] | None = None,
        patch: str | None = None,
        deleted_paths: list[str] | None = None,
        draft_id: str | None = None,
    ) -> list[str]:
        self.patches.append(dict(files or {}))
        return sorted(files or {})

    def verify(self, sandbox_id: str, check: str = "build") -> Any:
        return SimpleNamespace(success=True)


class _Settings:
    direct_ops_lock_timeout_seconds = 0.05
    project_write_lock_timeout_seconds = 1.0
    direct_ops_changelog_limit = 20


def _payload(**overrides: Any) -> DirectActionPayload:
    data = {
        "directActionId": "dact_test",
        "idempotencyKey": "proj_direct:dact_test",
        "projectId": PROJECT_ID,
        "sandboxId": SANDBOX_ID,
        "baseRevision": BASE_SHA,
        "action": "UPDATE_TEXT",
        "target": {
            "anchorId": "anc_dact_test",
            "selector": "[data-source='title']",
            "file": "src/App.jsx",
            "line": 4,
            "column": 7,
            "component": "App",
        },
        "payload": {"text": "我的任务"},
        "options": {"source": "FLOATING_TOOLBAR", "dryRun": False},
    }
    data.update(overrides)
    return DirectActionPayload.model_validate(data)


class DirectOperationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        store = MemoryChangeLogStore(max_entries=50)
        store.clear()
        self.store = store
        self.sandbox = _FakeSandbox()
        self.service = DirectOperationService(
            sandbox=self.sandbox,  # type: ignore[arg-type]
            settings=_Settings(),  # type: ignore[arg-type]
        )
        self.service._changelog = store
        self.direct_action_ids: list[str] = []

    def tearDown(self) -> None:
        # 清空缓冲区，避免测试之间共享事件。
        from ai_core.direct_ops import service as service_module

        with service_module._EVENT_LOCK:
            for action_id in self.direct_action_ids:
                service_module._EVENT_BUFFERS.pop(action_id, None)
        with service_module._COMPLETED_LOCK:
            service_module._COMPLETED.clear()

    def test_dry_run_does_not_write_or_commit(self) -> None:
        payload = _payload(baseRevision=None, options={"dryRun": True})
        self.direct_action_ids.append(payload.direct_action_id)

        response = self.service.execute(payload)

        self.assertEqual(response.status, "DRY_RUN")
        self.assertEqual(response.risk, "L0_DIRECT")
        self.assertTrue(response.resolved_target["wouldChange"])
        self.assertEqual(self.sandbox.patches, [])
        self.assertEqual(self.sandbox.commits, [])
        self.assertEqual(self.store.list_for_project(PROJECT_ID), [])

    def test_update_text_writes_commits_and_changelog(self) -> None:
        payload = _payload()
        self.direct_action_ids.append(payload.direct_action_id)

        response = self.service.execute(payload)

        self.assertEqual(response.status, "SUCCEEDED")
        self.assertEqual(response.revision_before, BASE_SHA)
        self.assertEqual(response.revision_after, NEXT_SHA)
        self.assertEqual(response.commit_hash, NEXT_SHA)
        self.assertEqual(self.sandbox.patches, [{"src/App.jsx": UPDATED}])
        self.assertEqual(len(self.sandbox.commits), 1)
        self.assertEqual(self.sandbox.edits[0]["operation"], "update_text")
        entries = self.store.list_for_project(PROJECT_ID)
        self.assertEqual(len(entries), 1)
        self.assertIn("我的任务", entries[0].summary)
        self.assertIn("+      <h1>我的任务</h1>", entries[0].diff_summary)
        buffer = get_direct_event_buffer(payload.direct_action_id)
        self.assertIsNotNone(buffer)
        types = [event.type for event in buffer.snapshot()]
        self.assertIn("direct.started", types)
        self.assertIn("direct.succeeded", types)

    def test_stale_revision_is_rejected_before_write(self) -> None:
        payload = _payload(baseRevision="c" * 40)
        self.direct_action_ids.append(payload.direct_action_id)

        with self.assertRaises(DirectActionError) as ctx:
            self.service.execute(payload)

        self.assertEqual(ctx.exception.code, DirectErrorCode.REVISION_MISMATCH)
        self.assertEqual(self.sandbox.patches, [])

    def test_shared_component_requires_hitl(self) -> None:
        self.sandbox.anchor = _anchor(shared=True)
        payload = _payload()
        self.direct_action_ids.append(payload.direct_action_id)

        with self.assertRaises(DirectActionError) as ctx:
            self.service.execute(payload)

        self.assertEqual(ctx.exception.code, DirectErrorCode.UNSAFE_OPERATION)
        self.assertEqual(self.sandbox.patches, [])

        dry = _payload(
            directActionId="dact_dry_shared",
            idempotencyKey="proj_direct:dact_dry_shared",
            baseRevision=None,
            options={"dryRun": True},
        )
        self.direct_action_ids.append(dry.direct_action_id)
        response = self.service.execute(dry)
        self.assertEqual(response.status, "DRY_RUN")
        self.assertEqual(response.risk, "L2_HITL")

    def test_project_busy_returns_direct_error(self) -> None:
        lease = project_write_lock.acquire(PROJECT_ID, owner="ai-run", timeout=1)
        try:
            payload = _payload(directActionId="dact_busy", idempotencyKey="busy")
            self.direct_action_ids.append(payload.direct_action_id)
            with self.assertRaises(DirectActionError) as ctx:
                self.service.execute(payload)
            self.assertEqual(ctx.exception.code, DirectErrorCode.PROJECT_BUSY)
        finally:
            project_write_lock.release(lease)

    def test_idempotent_replay_does_not_write_twice(self) -> None:
        payload = _payload()
        self.direct_action_ids.append(payload.direct_action_id)

        first = self.service.execute(payload)
        second = self.service.execute(payload)

        self.assertEqual(first.commit_hash, second.commit_hash)
        self.assertEqual(len(self.sandbox.patches), 1)
        self.assertEqual(len(self.sandbox.commits), 1)


if __name__ == "__main__":
    unittest.main()
