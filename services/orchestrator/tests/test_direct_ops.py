"""Direct Ops Phase 0/1：契约、写锁、revision 与 UPDATE_TEXT 闭环。"""

from __future__ import annotations

import threading
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from ai_core.direct_ops.changelog import (
    MemoryChangeLogStore,
    get_change_log_store,
)
from ai_core.direct_ops.service import (
    DirectActionError,
    DirectOperationService,
    get_direct_event_buffer,
)
from ai_core.domain.direct_ops import (
    ChangeLogEntry,
    DirectActionPayload,
    DirectErrorCode,
)
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

MOVED = """export default function App() {
  return (
    <main>
      <p>说明</p>
      <h1>待办</h1>
    </main>
  );
}
"""

DELETED = """export default function App() {
  return (
    <main>
      <h1>待办</h1>
    </main>
  );
}
"""

ADDED = """export default function App() {
  return (
    <main>
      <h1>待办</h1>
      <button type="button" data-vibe-created="direct">按钮</button>
    </main>
  );
}
"""


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
        "inMap": False,
        "isRouteRoot": False,
        "deleteSafe": not shared,
        "deleteReasons": ["被 2 个文件引用"] if shared else [],
        "instanceCount": 1,
        "importerCount": 0,
        "sharedByInstance": False,
        "sharedByImport": False,
    }


class _FakeSandbox:
    def __init__(self, anchor: dict[str, Any] | None = None) -> None:
        self.head = BASE_SHA
        self.anchor = anchor or _anchor()
        self.parent_anchor = {
            "anchorId": "anc_dact_move_parent",
            "annotationId": "dact_move_parent",
            "status": "RESOLVED",
            "file": "src/App.jsx",
            "component": "App",
            "range": {"startOffset": 20, "endOffset": 80},
            "isShared": False,
            "protected": False,
            "sharedReason": "",
        }
        self.patches: list[dict[str, str]] = []
        self.commits: list[str] = []
        self.resets: list[str] = []
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

    def git_reset_hard(self, sandbox_id: str, commit_hash: str) -> None:
        self.resets.append(commit_hash)
        self.head = commit_hash

    def resolve_targets(
        self,
        sandbox_id: str,
        anchors: list[dict[str, Any]],
        draft_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.resolutions.append(anchors)
        request = anchors[0] if anchors else {}
        anchor_id = str(request.get("anchorId") or "")
        if anchor_id.endswith("_parent"):
            return [dict(self.parent_anchor, anchorId=anchor_id)]
        return [dict(self.anchor, anchorId=anchor_id)]

    def read_file(self, sandbox_id: str, path: str) -> str:
        return SOURCE

    def apply_ast_edit(
        self, sandbox_id: str, edit: dict[str, Any], draft_id: str | None = None
    ) -> dict[str, Any]:
        self.edits.append(dict(edit))
        operation = edit.get("operation")
        if operation == "move_node":
            return {"status": "OK", "content": MOVED}
        if operation == "insert":
            return {
                "status": "OK",
                "content": ADDED,
                "nodeCountBefore": 3,
                "nodeCountAfter": 4,
            }
        if operation == "delete":
            return {
                "status": "OK",
                "content": DELETED,
                "nodeCountBefore": 3,
                "nodeCountAfter": 2,
                "targetNodeCount": 1,
            }
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
    bff_base_url = ""
    source_snapshot_sync_enabled = False
    source_snapshot_sync_timeout_seconds = 1.0
    source_snapshot_queue_enabled = False
    source_snapshot_queue_path = "/tmp/vibe-test-source-sync-queue.json"
    source_snapshot_retry_interval_seconds = 1.0


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


def _move_payload(**overrides: Any) -> DirectActionPayload:
    data = {
        "directActionId": "dact_move",
        "idempotencyKey": "proj_direct:dact_move",
        "projectId": PROJECT_ID,
        "sandboxId": SANDBOX_ID,
        "baseRevision": BASE_SHA,
        "action": "MOVE_NODE",
        "target": {
            "anchorId": "anc_dact_move",
            "file": "src/App.jsx",
            "line": 4,
            "column": 7,
            "component": "App",
            "placement": {
                "parentSelector": "[data-source='main']",
                "parentFile": "src/App.jsx",
                "insertPosition": "inside_end",
                "siblingIndex": 1,
            },
        },
        "payload": {},
        "options": {"source": "FLOATING_TOOLBAR", "dryRun": False},
    }
    data.update(overrides)
    return DirectActionPayload.model_validate(data)


class DirectOperationMoveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryChangeLogStore(max_entries=50)
        self.store.clear()
        self.sandbox = _FakeSandbox()
        self.service = DirectOperationService(
            sandbox=self.sandbox,  # type: ignore[arg-type]
            settings=_Settings(),  # type: ignore[arg-type]
        )
        self.service._changelog = self.store

    def tearDown(self) -> None:
        from ai_core.direct_ops import service as service_module

        with service_module._COMPLETED_LOCK:
            service_module._COMPLETED.clear()

    def test_move_node_commits_and_changelog(self) -> None:
        payload = _move_payload()

        response = self.service.execute(payload)

        self.assertEqual(response.status, "SUCCEEDED")
        self.assertEqual(response.risk, "L0_DIRECT")
        self.assertEqual(self.sandbox.edits[0]["operation"], "move_node")
        self.assertEqual(self.sandbox.edits[0]["insertPosition"], "inside_end")
        self.assertEqual(len(self.sandbox.commits), 1)
        entries = self.store.list_for_project(PROJECT_ID)
        self.assertEqual(len(entries), 1)
        self.assertIn("移动到", entries[0].summary)

    def test_repeated_instances_move_is_blocked_without_dialog(self) -> None:
        self.sandbox.anchor = {
            **_anchor(shared=True),
            "instanceCount": 6,
            "importerCount": 0,
            "isRouteRoot": False,
            "sharedReason": "同源码位置渲染 6 次",
        }
        self.sandbox.parent_anchor = {
            **self.sandbox.parent_anchor,
            "isShared": True,
            "protected": True,
        }

        with self.assertRaises(DirectActionError) as ctx:
            self.service.execute(_move_payload())

        self.assertEqual(ctx.exception.code, DirectErrorCode.UNSAFE_OPERATION)
        self.assertEqual(self.sandbox.edits, [])

    def test_shared_component_moves_at_call_site(self) -> None:
        self.sandbox.anchor = {
            **_anchor(shared=True),
            "file": "src/components/Card.jsx",
            "component": "Card",
            "callSiteFile": "src/Page.jsx",
            "callSiteAnchor": {
                "file": "src/Page.jsx",
                "range": {"startOffset": 10, "endOffset": 40},
                "component": "Page",
                "nodeType": "JSXElement",
            },
        }
        self.sandbox.parent_anchor = {
            **self.sandbox.parent_anchor,
            "file": "src/Page.jsx",
        }

        response = self.service.execute(_move_payload())

        self.assertEqual(response.status, "SUCCEEDED")
        self.assertEqual(response.risk, "L0_DIRECT")
        self.assertEqual(self.sandbox.edits[0]["file"], "src/Page.jsx")

    def test_cross_file_move_requires_ai(self) -> None:
        self.sandbox.parent_anchor = {
            **self.sandbox.parent_anchor,
            "file": "src/Other.jsx",
        }
        payload = _move_payload()

        with self.assertRaises(DirectActionError) as ctx:
            self.service.execute(payload)

        self.assertEqual(ctx.exception.code, DirectErrorCode.UNSAFE_OPERATION)
        self.assertEqual(self.sandbox.patches, [])

    def test_parent_oid_is_derived_from_data_oid_selector(self) -> None:
        payload = _move_payload(
            target={
                **_move_payload().target.model_dump(by_alias=True),
                "placement": {
                    "parentSelector": '[data-oid="oid_main"]',
                    "parentFile": "src/App.jsx",
                    "insertPosition": "inside_index",
                    "siblingIndex": 0,
                },
            }
        )

        response = self.service.execute(payload)

        self.assertEqual(response.status, "SUCCEEDED")
        parent_request = self.sandbox.resolutions[-1][0]
        self.assertEqual(parent_request["oid"], "oid_main")
        self.assertEqual(self.sandbox.edits[0]["insertIndex"], 0)

    def test_explicit_parent_oid_wins_over_selector(self) -> None:
        payload = _move_payload(
            target={
                **_move_payload().target.model_dump(by_alias=True),
                "placement": {
                    "parentSelector": '[data-oid="oid_from_selector"]',
                    "parentOid": "oid_explicit",
                    "parentFile": "src/App.jsx",
                    "insertPosition": "inside_end",
                    "siblingIndex": 1,
                },
            }
        )

        self.service.execute(payload)

        self.assertEqual(
            self.sandbox.resolutions[-1][0]["oid"],
            "oid_explicit",
        )

    def test_move_into_call_site_slot_is_direct(self) -> None:
        self.sandbox.parent_anchor = {
            **self.sandbox.parent_anchor,
            "containerScope": "call_site_slot",
            "callSiteOid": "csoid_slot",
        }
        payload = _move_payload(
            target={
                **_move_payload().target.model_dump(by_alias=True),
                "placement": {
                    "parentSelector": "[data-source='main']",
                    "parentFile": "src/App.jsx",
                    "parentCallSiteOid": "csoid_slot",
                    "insertPosition": "inside_end",
                    "siblingIndex": 1,
                },
            }
        )

        response = self.service.execute(payload)

        self.assertEqual(response.status, "SUCCEEDED")
        self.assertEqual(self.sandbox.edits[0]["operation"], "move_node")
        self.assertEqual(
            self.sandbox.resolutions[-1][0]["callSiteOid"],
            "csoid_slot",
        )

    def test_move_into_component_definition_requires_hitl(self) -> None:
        self.sandbox.parent_anchor = {
            **self.sandbox.parent_anchor,
            "containerScope": "component_definition",
        }
        payload = _move_payload()

        response = self.service.execute(payload)

        self.assertEqual(response.status, "HITL_REQUIRED")
        self.assertEqual(response.risk, "L2_HITL")
        self.assertTrue(response.decision_id)
        self.assertTrue(any("组件定义" in reason for reason in response.reasons))
        self.assertEqual(self.sandbox.edits, [])

    def test_move_into_repeated_template_requires_hitl(self) -> None:
        self.sandbox.parent_anchor = {
            **self.sandbox.parent_anchor,
            "containerScope": "repeated_template",
        }
        payload = _move_payload()

        response = self.service.execute(payload)

        self.assertEqual(response.status, "HITL_REQUIRED")
        self.assertEqual(response.risk, "L2_HITL")
        self.assertTrue(response.decision_id)
        self.assertTrue(any("循环模板" in reason for reason in response.reasons))
        self.assertEqual(self.sandbox.edits, [])

    def test_move_requires_placement(self) -> None:
        with self.assertRaises(ValueError):
            DirectActionPayload.model_validate(
                {
                    **_move_payload().model_dump(by_alias=True),
                    "target": {
                        "anchorId": "anc_dact_move",
                        "file": "src/App.jsx",
                        "line": 4,
                        "column": 7,
                    },
                }
            )


def _add_payload(**overrides: Any) -> DirectActionPayload:
    data = {
        "directActionId": "dact_add",
        "idempotencyKey": "proj_direct:dact_add",
        "projectId": PROJECT_ID,
        "sandboxId": SANDBOX_ID,
        "baseRevision": BASE_SHA,
        "action": "ADD_COMPONENT",
        "target": {
            "anchorId": "anc_dact_add",
            "file": "src/App.jsx",
            "line": 3,
            "column": 5,
            "component": "App",
        },
        "payload": {"componentType": "Button"},
        "options": {"source": "FLOATING_TOOLBAR", "dryRun": False},
    }
    data.update(overrides)
    return DirectActionPayload.model_validate(data)


def _delete_payload(**overrides: Any) -> DirectActionPayload:
    data = {
        "directActionId": "dact_delete",
        "idempotencyKey": "proj_direct:dact_delete",
        "projectId": PROJECT_ID,
        "sandboxId": SANDBOX_ID,
        "baseRevision": BASE_SHA,
        "action": "DELETE_NODE",
        "target": {
            "anchorId": "anc_dact_delete",
            "file": "src/App.jsx",
            "line": 4,
            "column": 7,
            "component": "App",
        },
        "payload": {},
        "options": {"source": "FLOATING_TOOLBAR", "dryRun": False},
    }
    data.update(overrides)
    return DirectActionPayload.model_validate(data)


class DirectOperationDeleteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryChangeLogStore(max_entries=50)
        self.store.clear()
        self.sandbox = _FakeSandbox()
        self.service = DirectOperationService(
            sandbox=self.sandbox,  # type: ignore[arg-type]
            settings=_Settings(),  # type: ignore[arg-type]
        )
        self.service._changelog = self.store

    def tearDown(self) -> None:
        from ai_core.direct_ops import service as service_module

        with service_module._COMPLETED_LOCK:
            service_module._COMPLETED.clear()

    def test_safe_delete_commits_and_changelog(self) -> None:
        payload = _delete_payload()

        response = self.service.execute(payload)

        self.assertEqual(response.status, "SUCCEEDED")
        self.assertEqual(response.risk, "L0_DIRECT")
        self.assertEqual(self.sandbox.edits[0]["operation"], "delete")
        self.assertEqual(self.sandbox.patches, [{"src/App.jsx": DELETED}])
        self.assertEqual(len(self.sandbox.commits), 1)
        entries = self.store.list_for_project(PROJECT_ID)
        self.assertIn("内的节点", entries[0].summary)
        self.assertIn("<h1>", entries[0].summary)

    def test_repeated_instance_delete_is_blocked_without_dialog(self) -> None:
        self.sandbox.anchor = {
            **_anchor(shared=True),
            "instanceCount": 4,
            "importerCount": 0,
            "isRouteRoot": False,
            "sharedReason": "同源码位置渲染 4 次",
        }

        with self.assertRaises(DirectActionError) as ctx:
            self.service.execute(_delete_payload())

        self.assertEqual(ctx.exception.code, DirectErrorCode.UNSAFE_OPERATION)
        self.assertEqual(self.sandbox.edits, [])

    def test_shared_component_deletes_call_site_instance(self) -> None:
        self.sandbox.anchor = {
            **_anchor(shared=True),
            "file": "src/components/Card.jsx",
            "callSiteFile": "src/Page.jsx",
            "callSiteAnchor": {
                "file": "src/Page.jsx",
                "range": {"startOffset": 10, "endOffset": 40},
                "component": "Page",
                "nodeType": "JSXElement",
            },
        }

        response = self.service.execute(_delete_payload())

        self.assertEqual(response.status, "SUCCEEDED")
        self.assertEqual(response.risk, "L0_DIRECT")
        self.assertEqual(self.sandbox.edits[0]["file"], "src/Page.jsx")

    def test_root_delete_replaces_with_placeholder(self) -> None:
        self.sandbox.anchor = {
            **_anchor(),
            "deleteSafe": False,
            "isRouteRoot": True,
            "deleteReasons": ["组件是页面顶层 JSX 根节点"],
        }

        response = self.service.execute(_delete_payload())

        self.assertEqual(response.status, "SUCCEEDED")
        edit = self.sandbox.edits[0]
        self.assertEqual(edit["operation"], "modify")
        self.assertIn("empty-placeholder", edit["jsxCode"])

    def test_static_map_delete_auto_removes_one_data_item(self) -> None:
        self.sandbox.anchor = {
            **_anchor(),
            "inMap": True,
            "deleteSafe": False,
            "deleteReasons": ["组件位于 map 循环内"],
            "mapSource": {
                "kind": "variable_array",
                "variable": "items",
                "start": 10,
                "end": 80,
                "count": 3,
            },
            "instance": 2,
            "instanceCount": 3,
        }
        payload = _delete_payload()

        response = self.service.execute(payload)

        self.assertEqual(response.status, "SUCCEEDED")
        self.assertEqual(response.action, "DELETE_DATA_ITEM")
        self.assertEqual(
            self.sandbox.edits[0]["operation"],
            "delete_array_item",
        )
        self.assertEqual(self.sandbox.edits[0]["itemIndex"], 1)

    def test_dynamic_map_delete_is_blocked_without_dialog(self) -> None:
        self.sandbox.anchor = {
            **_anchor(),
            "inMap": True,
            "deleteSafe": False,
            "deleteReasons": ["组件位于 map 循环内"],
        }
        payload = _delete_payload()

        with self.assertRaises(DirectActionError) as ctx:
            self.service.execute(payload)

        self.assertEqual(ctx.exception.code, DirectErrorCode.UNSAFE_OPERATION)
        self.assertEqual(self.sandbox.edits, [])

    def test_delete_node_count_mismatch_blocks_write(self) -> None:
        original = self.sandbox.apply_ast_edit

        def broken(sandbox_id: str, edit: dict[str, Any], draft_id: str | None = None):
            result = original(sandbox_id, edit, draft_id)
            result["targetNodeCount"] = 2
            return result

        self.sandbox.apply_ast_edit = broken  # type: ignore[method-assign]

        with self.assertRaises(DirectActionError) as ctx:
            self.service.execute(_delete_payload())

        self.assertEqual(ctx.exception.code, DirectErrorCode.TOOL_FAILED)
        self.assertEqual(self.sandbox.patches, [])


class DirectOperationPropsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryChangeLogStore(max_entries=50)
        self.store.clear()
        anchor = dict(_anchor())
        anchor.update(
            {
                "file": "src/App.jsx",
                "component": "App",
                "callSiteOid": "csoid_user_card",
                "isCallSite": True,
                "nodeType": "JSXElement",
                "range": {
                    "startLine": 5,
                    "startColumn": 7,
                    "endLine": 5,
                    "endColumn": 40,
                    "startOffset": 60,
                    "endOffset": 100,
                },
            }
        )
        self.sandbox = _FakeSandbox(anchor=anchor)
        self.service = DirectOperationService(
            sandbox=self.sandbox,  # type: ignore[arg-type]
            settings=_Settings(),  # type: ignore[arg-type]
        )
        self.service._changelog = self.store

    def tearDown(self) -> None:
        from ai_core.direct_ops import service as service_module

        with service_module._COMPLETED_LOCK:
            service_module._COMPLETED.clear()

    def test_update_props_targets_call_site_not_definition(self) -> None:
        payload = DirectActionPayload.model_validate(
            {
                "directActionId": "dact_props",
                "idempotencyKey": "proj_direct:dact_props",
                "projectId": PROJECT_ID,
                "sandboxId": SANDBOX_ID,
                "tenantId": "tenant_local",
                "baseRevision": BASE_SHA,
                "action": "UPDATE_PROPS",
                "target": {
                    "anchorId": "anc_props",
                    "callSiteOid": "csoid_user_card",
                    "file": "src/UserCard.jsx",
                    "component": "UserCard",
                },
                "payload": {"props": {"title": "来自调用点的新标题"}},
                "options": {"source": "FLOATING_TOOLBAR", "dryRun": False},
            }
        )

        response = self.service.execute(payload)

        self.assertEqual(response.status, "SUCCEEDED")
        self.assertEqual(self.sandbox.edits[0]["operation"], "update_props")
        self.assertEqual(
            self.sandbox.edits[0]["props"],
            {"title": "来自调用点的新标题"},
        )
        # Fake resolver 返回的是 App.jsx 调用点，而不是 UserCard.jsx 定义。
        self.assertEqual(self.sandbox.edits[0]["file"], "src/App.jsx")
        self.assertEqual(self.sandbox.patches, [{"src/App.jsx": UPDATED}])
        entries = self.store.list_for_project(PROJECT_ID)
        self.assertIn("调用点 Props", entries[0].summary)

    def test_update_class_rewrites_class_name(self) -> None:
        payload = DirectActionPayload.model_validate(
            {
                "directActionId": "dact_class",
                "idempotencyKey": "proj_direct:dact_class",
                "projectId": PROJECT_ID,
                "sandboxId": SANDBOX_ID,
                "tenantId": "tenant_local",
                "baseRevision": BASE_SHA,
                "action": "UPDATE_CLASS",
                "target": {
                    "anchorId": "anc_class",
                    "file": "src/App.jsx",
                    "selector": "[data-oid='oid_title']",
                    "instance": 1,
                    "instanceCount": 1,
                },
                "payload": {"className": "text-red-600 font-bold"},
                "options": {"source": "FLOATING_TOOLBAR", "dryRun": False},
            }
        )

        response = self.service.execute(payload)

        self.assertEqual(response.status, "SUCCEEDED")
        self.assertEqual(self.sandbox.edits[0]["operation"], "update_props")
        self.assertEqual(
            self.sandbox.edits[0]["props"],
            {"className": "text-red-600 font-bold"},
        )
        self.assertEqual(self.sandbox.patches, [{"src/App.jsx": UPDATED}])
        entries = self.store.list_for_project(PROJECT_ID)
        self.assertIn("样式类", entries[0].summary)

    def test_update_style_rewrites_inline_props(self) -> None:
        payload = DirectActionPayload.model_validate(
            {
                "directActionId": "dact_style",
                "idempotencyKey": "proj_direct:dact_style",
                "projectId": PROJECT_ID,
                "sandboxId": SANDBOX_ID,
                "tenantId": "tenant_local",
                "baseRevision": BASE_SHA,
                "action": "UPDATE_STYLE",
                "target": {
                    "anchorId": "anc_style",
                    "file": "src/App.jsx",
                    "selector": "[data-oid='oid_title']",
                    "instance": 1,
                    "instanceCount": 1,
                },
                "payload": {"style": {"fontSize": "20px", "opacity": 0.8}},
                "options": {"source": "FLOATING_TOOLBAR", "dryRun": False},
            }
        )

        response = self.service.execute(payload)

        self.assertEqual(response.status, "SUCCEEDED")
        self.assertEqual(self.sandbox.edits[0]["operation"], "update_style")
        self.assertEqual(
            self.sandbox.edits[0]["style"],
            {"fontSize": "20px", "opacity": 0.8},
        )
        self.assertEqual(self.sandbox.patches, [{"src/App.jsx": UPDATED}])
        entries = self.store.list_for_project(PROJECT_ID)
        self.assertIn("inline style", entries[0].summary)

    def test_update_props_call_site_in_map_requires_hitl(self) -> None:
        anchor = dict(_anchor())
        anchor.update(
            {
                "file": "src/App.jsx",
                "component": "App",
                "callSiteOid": "csoid_user_card",
                "isCallSite": True,
                "inMap": True,
                "instanceCount": 1,
                "instancePath": [
                    {"oid": "oid_list", "index": 0, "count": 3},
                    {"oid": "oid_card", "index": 0, "count": 3},
                ],
                "instanceKey": "oid_list[0]/oid_card[0]",
                "nodeType": "JSXElement",
                "range": {
                    "startLine": 5,
                    "startColumn": 7,
                    "endLine": 5,
                    "endColumn": 40,
                    "startOffset": 60,
                    "endOffset": 100,
                },
            }
        )
        sandbox = _FakeSandbox(anchor=anchor)
        service = DirectOperationService(
            sandbox=sandbox,  # type: ignore[arg-type]
            settings=_Settings(),  # type: ignore[arg-type]
        )
        service._changelog = self.store
        payload = DirectActionPayload.model_validate(
            {
                "directActionId": "dact_props_map",
                "idempotencyKey": "proj_direct:dact_props_map",
                "projectId": PROJECT_ID,
                "sandboxId": SANDBOX_ID,
                "tenantId": "tenant_local",
                "baseRevision": BASE_SHA,
                "action": "UPDATE_PROPS",
                "target": {
                    "anchorId": "anc_props_map",
                    "callSiteOid": "csoid_user_card",
                    "instanceKey": "oid_list[0]/oid_card[0]",
                },
                "payload": {"props": {"title": "x"}},
                "options": {"source": "FLOATING_TOOLBAR", "dryRun": False},
            }
        )

        response = service.execute(payload)

        self.assertEqual(response.status, "SUCCEEDED")
        self.assertEqual(response.risk, "L0_DIRECT")
        self.assertEqual(sandbox.edits[0]["operation"], "update_props")
        self.assertEqual(
            sandbox.edits[0]["props"],
            {"title": "x"},
        )

    def test_update_props_without_call_site_is_blocked(self) -> None:
        anchor = dict(_anchor())
        anchor.update(
            {
                "file": "src/UserCard.jsx",
                "component": "UserCard",
                "nodeType": "JSXElement",
                "protected": True,
                "isShared": True,
                "instanceCount": 2,
            }
        )
        sandbox = _FakeSandbox(anchor=anchor)
        service = DirectOperationService(
            sandbox=sandbox,  # type: ignore[arg-type]
            settings=_Settings(),  # type: ignore[arg-type]
        )
        service._changelog = self.store
        payload = DirectActionPayload.model_validate(
            {
                "directActionId": "dact_props_definition",
                "idempotencyKey": "proj_direct:dact_props_definition",
                "projectId": PROJECT_ID,
                "sandboxId": SANDBOX_ID,
                "tenantId": "tenant_local",
                "baseRevision": BASE_SHA,
                "action": "UPDATE_PROPS",
                "target": {
                    "anchorId": "anc_props_definition",
                    "file": "src/UserCard.jsx",
                },
                "payload": {"props": {"title": "x"}},
                "options": {"source": "FLOATING_TOOLBAR", "dryRun": False},
            }
        )

        with self.assertRaises(DirectActionError) as raised:
            service.execute(payload)

        self.assertEqual(raised.exception.code, DirectErrorCode.UNSAFE_OPERATION)
        self.assertEqual(sandbox.edits, [])


class DataItemDeleteDecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryChangeLogStore(max_entries=50)
        self.store.clear()
        anchor = {
            **_anchor(),
            "inMap": True,
            "instance": 2,
            "instanceCount": 2,
            "deleteSafe": False,
            "deleteReasons": ["位于 map 循环内"],
            "mapSource": {
                "kind": "variable_array",
                "variable": "items",
                "start": 10,
                "end": 80,
                "count": 2,
            },
        }
        self.sandbox = _FakeSandbox(anchor=anchor)
        self.service = DirectOperationService(
            sandbox=self.sandbox,  # type: ignore[arg-type]
            settings=_Settings(),  # type: ignore[arg-type]
        )
        self.service._changelog = self.store

    def tearDown(self) -> None:
        from ai_core.direct_ops import service as service_module

        with service_module._COMPLETED_LOCK:
            service_module._COMPLETED.clear()

    def _data_payload(self) -> DirectActionPayload:
        return DirectActionPayload.model_validate(
            {
                "directActionId": "dact_data",
                "idempotencyKey": "proj_direct:dact_data",
                "projectId": PROJECT_ID,
                "sandboxId": SANDBOX_ID,
                "tenantId": "tenant_local",
                "baseRevision": BASE_SHA,
                "action": "DELETE_DATA_ITEM",
                "target": {
                    "anchorId": "anc_data",
                    "file": "src/App.jsx",
                    "selector": "[data-oid='oid_item']",
                    "instance": 2,
                    "instanceCount": 2,
                },
                "payload": {},
                "options": {"source": "FLOATING_TOOLBAR", "dryRun": False},
            }
        )

    def test_remove_data_item_executes_deterministic_array_delete(self) -> None:
        response = self.service.execute(self._data_payload())

        self.assertEqual(response.status, "SUCCEEDED")
        self.assertTrue(response.commit_hash)
        self.assertEqual(self.sandbox.edits[0]["operation"], "delete_array_item")
        self.assertEqual(self.sandbox.edits[0]["itemIndex"], 1)
        self.assertEqual(self.sandbox.patches, [{"src/App.jsx": UPDATED}])
        entries = self.store.list_for_project(PROJECT_ID)
        self.assertIn("数据源的第 2 项", entries[0].summary)

    def test_dynamic_map_source_requires_hitl(self) -> None:
        self.sandbox.anchor = {
            **self.sandbox.anchor,
            "mapSource": {"kind": "dynamic", "variable": "items"},
        }

        response = self.service.execute(self._data_payload())

        self.assertEqual(response.status, "HITL_REQUIRED")
        self.assertEqual(self.sandbox.edits, [])


class DirectOperationAddTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryChangeLogStore(max_entries=50)
        self.store.clear()
        self.sandbox = _FakeSandbox()
        self.service = DirectOperationService(
            sandbox=self.sandbox,  # type: ignore[arg-type]
            settings=_Settings(),  # type: ignore[arg-type]
        )
        self.service._changelog = self.store

    def tearDown(self) -> None:
        from ai_core.direct_ops import service as service_module

        with service_module._COMPLETED_LOCK:
            service_module._COMPLETED.clear()

    def test_add_component_inserts_and_validates_node_delta(self) -> None:
        payload = _add_payload()

        response = self.service.execute(payload)

        self.assertEqual(response.status, "SUCCEEDED")
        self.assertEqual(response.risk, "L0_DIRECT")
        self.assertEqual(self.sandbox.edits[0]["operation"], "insert")
        jsx = self.sandbox.edits[0]["jsxCode"]
        self.assertIn("<button", jsx)
        self.assertIn('data-oid="oid_direct_', jsx)
        self.assertIn('data-node-id="nd_direct_', jsx)
        self.assertIn('data-source-file="src/App.jsx"', jsx)
        self.assertNotIn("__VIBE_", jsx)
        self.assertEqual(self.sandbox.patches, [{"src/App.jsx": ADDED}])
        self.assertEqual(len(self.sandbox.commits), 1)
        entries = self.store.list_for_project(PROJECT_ID)
        self.assertIn("新增 Button", entries[0].summary)

    def test_add_component_inherits_sibling_class_names(self) -> None:
        payload = _add_payload(
            payload={
                "componentType": "Button",
                "styleContext": {
                    "className": "toolbar primary",
                    "style": "border-radius: 8px",
                },
            }
        )

        self.service.execute(payload)

        jsx = self.sandbox.edits[0]["jsxCode"]
        self.assertIn("vibe-direct-button", jsx)
        self.assertIn("toolbar primary", jsx)
        self.assertIn('style="border-radius: 8px"', jsx)

    def test_unknown_component_routes_to_llm_before_sandbox_access(self) -> None:
        def boom(_sandbox_id: str) -> Any:
            raise AssertionError("路由前不应访问沙盒")

        self.sandbox.get = boom  # type: ignore[method-assign]

        with self.assertRaises(DirectActionError) as ctx:
            self.service.execute(_add_payload(payload={"componentType": "LoginForm"}))
        self.assertEqual(ctx.exception.code, DirectErrorCode.ROUTE_TO_LLM)
        self.assertEqual(self.sandbox.patches, [])

    def test_node_delta_mismatch_blocks_write(self) -> None:
        original = self.sandbox.apply_ast_edit

        def broken(sandbox_id: str, edit: dict[str, Any], draft_id: str | None = None):
            result = original(sandbox_id, edit, draft_id)
            result["nodeCountAfter"] = result["nodeCountBefore"]
            return result

        self.sandbox.apply_ast_edit = broken  # type: ignore[method-assign]

        with self.assertRaises(DirectActionError) as ctx:
            self.service.execute(_add_payload())

        self.assertEqual(ctx.exception.code, DirectErrorCode.TOOL_FAILED)
        self.assertEqual(self.sandbox.patches, [])


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

    def test_resolver_receives_instance_identity(self) -> None:
        payload = _payload(
            target={
                "anchorId": "anc_dact_test",
                "selector": "[data-source='same']",
                "file": "src/App.jsx",
                "line": 4,
                "column": 7,
                "component": "App",
                "instance": 2,
                "instanceCount": 3,
            }
        )
        self.direct_action_ids.append(payload.direct_action_id)

        self.service.execute(payload)

        request = self.sandbox.resolutions[0][0]
        self.assertEqual(request["instance"], 2)
        self.assertEqual(request["instanceCount"], 3)

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

        response = self.service.execute(payload)

        self.assertEqual(response.status, "SUCCEEDED")
        self.assertEqual(response.risk, "L0_DIRECT")
        self.assertEqual(self.sandbox.patches, [{"src/App.jsx": UPDATED}])

        dry = _payload(
            directActionId="dact_dry_shared",
            idempotencyKey="proj_direct:dact_dry_shared",
            baseRevision=None,
            options={"dryRun": True},
        )
        self.direct_action_ids.append(dry.direct_action_id)
        response = self.service.execute(dry)
        self.assertEqual(response.status, "DRY_RUN")
        self.assertEqual(response.risk, "L0_DIRECT")

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


class NodeIdentityScopePolicyTest(unittest.TestCase):
    def _identity(self, **overrides: Any):
        from ai_core.domain.node_identity import NodeIdentity

        data = {
            "file": "src/App.jsx",
            "line": 3,
            "column": 5,
            "component": "App",
            "node_type": "JSXElement",
            "kind": "leaf",
            "allowed_insert_positions": ("before", "after"),
            "range": {"startOffset": 1, "endOffset": 10},
        }
        data.update(overrides)
        return NodeIdentity(**data)

    def test_instance_path_repeated_template_is_direct(self) -> None:
        from ai_core.direct_ops.scope_policy import decide_scope

        identity = self._identity(
            instance_count=1,
            instance_path=(
                {"oid": "oid_list", "index": 2, "count": 3},
                {"oid": "oid_item", "index": 2, "count": 3},
            ),
            instance_key="oid_list[0]/oid_item[2]",
        )
        decision = decide_scope("UPDATE_TEXT", identity)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.strategy, "direct")

    def test_leaf_delete_is_direct_instance(self) -> None:
        from ai_core.direct_ops.scope_policy import decide_scope

        decision = decide_scope("DELETE_NODE", self._identity())
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.scope, "instance")
        self.assertEqual(decision.strategy, "direct")

    def test_dynamic_map_item_delete_is_blocked_without_dialog(self) -> None:
        from ai_core.direct_ops.scope_policy import decide_scope

        decision = decide_scope(
            "DELETE_NODE",
            self._identity(kind="map_item", in_map=True),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.strategy, "blocked")
        self.assertEqual(decision.options, [])

    def test_map_item_static_array_offers_remove_data_item(self) -> None:
        from ai_core.direct_ops.scope_policy import decide_scope

        decision = decide_scope(
            "DELETE_NODE",
            self._identity(
                kind="map_item",
                in_map=True,
                raw={"mapSource": {"kind": "variable_array", "count": 3}},
            ),
        )
        self.assertFalse(decision.allowed)
        option_ids = [item["optionId"] for item in decision.options]
        self.assertIn("remove_data_item", option_ids)
        self.assertEqual(
            next(
                item
                for item in decision.options
                if item["optionId"] == "remove_data_item"
            )["action"],
            "AI_DATA_EDIT",
        )

    def test_map_item_move_is_blocked_without_dialog(self) -> None:
        from ai_core.direct_ops.scope_policy import decide_scope

        decision = decide_scope(
            "MOVE_NODE",
            self._identity(kind="map_item", in_map=True),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.strategy, "blocked")
        self.assertEqual(decision.options, [])

    def test_route_root_delete_uses_placeholder(self) -> None:
        from ai_core.direct_ops.scope_policy import decide_scope

        decision = decide_scope(
            "DELETE_NODE",
            self._identity(kind="route_root", is_route_root=True),
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.scope, "replace_root")
        self.assertEqual(decision.strategy, "replace_placeholder")

    def test_repeated_instances_move_is_blocked_without_dialog(self) -> None:
        from ai_core.direct_ops.scope_policy import decide_scope

        decision = decide_scope(
            "MOVE_NODE",
            self._identity(
                kind="leaf",
                instance_count=6,
                importer_count=0,
                is_shared=True,
            ),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.strategy, "blocked")
        self.assertEqual(decision.options, [])


class DeleteStaticAnalysisTest(unittest.TestCase):
    def test_safe_leaf_node(self) -> None:
        from ai_core.direct_ops.static_analysis import analyze_delete_anchor

        result = analyze_delete_anchor(
            {"isShared": False, "inMap": False, "isRouteRoot": False}
        )
        self.assertTrue(result.safe)
        self.assertEqual(result.reasons, [])

    def test_shared_component_with_call_site_is_safe_to_delete(self) -> None:
        from ai_core.direct_ops.static_analysis import analyze_delete_anchor

        result = analyze_delete_anchor(
            {
                "isShared": True,
                "callSiteAnchor": {
                    "file": "src/Page.jsx",
                    "range": {"startOffset": 1, "endOffset": 2},
                },
                "inMap": False,
                "isRouteRoot": True,
            }
        )
        self.assertTrue(result.safe)
        self.assertTrue(result.is_shared)

    def test_repeated_source_instances_can_be_deleted(self) -> None:
        from ai_core.direct_ops.static_analysis import analyze_delete_anchor

        result = analyze_delete_anchor(
            {
                "isShared": True,
                "instanceCount": 4,
                "importerCount": 0,
                "sharedReason": "同源码位置渲染 4 次",
                "inMap": False,
                "isRouteRoot": False,
            }
        )
        self.assertTrue(result.safe)
        self.assertTrue(result.repeated_only)
        self.assertEqual(result.instance_count, 4)

    def test_shared_map_and_route_root_are_unsafe(self) -> None:
        from ai_core.direct_ops.static_analysis import analyze_delete_anchor

        result = analyze_delete_anchor(
            {
                "isShared": True,
                "sharedReason": "被 2 个文件引用",
                "inMap": True,
                "isRouteRoot": True,
            }
        )
        self.assertFalse(result.safe)
        self.assertTrue(result.is_shared)
        self.assertTrue(result.in_map)
        self.assertTrue(result.is_route_root)
        self.assertEqual(len(result.reasons), 3)

    def test_service_analyze_delete_returns_reasons(self) -> None:
        sandbox = _FakeSandbox(
            anchor={
                **_anchor(),
                "inMap": True,
                "isRouteRoot": False,
                "deleteSafe": False,
                "deleteReasons": ["位于 map 循环内"],
            }
        )
        service = DirectOperationService(
            sandbox=sandbox,  # type: ignore[arg-type]
            settings=_Settings(),  # type: ignore[arg-type]
        )
        payload = DirectActionPayload.model_validate(
            {
                **_payload().model_dump(by_alias=True),
                "action": "DELETE_NODE",
                "payload": {},
                "options": {"dryRun": True},
                "baseRevision": None,
            }
        )

        result = service.analyze_delete(payload)

        self.assertFalse(result["safe"])
        self.assertTrue(result["inMap"])
        self.assertEqual(result["strategy"], "blocked")
        self.assertEqual(result["options"], [])

    def test_map_item_delete_analysis_is_direct(self) -> None:
        sandbox = _FakeSandbox(
            anchor={
                **_anchor(),
                "inMap": True,
                "isRouteRoot": False,
                "deleteSafe": False,
                "deleteReasons": ["位于 map 循环内"],
            }
        )
        service = DirectOperationService(
            sandbox=sandbox,  # type: ignore[arg-type]
            settings=_Settings(),  # type: ignore[arg-type]
        )
        store = MemoryChangeLogStore(max_entries=20)
        service._changelog = store
        payload = DirectActionPayload.model_validate(
            {
                **_payload().model_dump(by_alias=True),
                "action": "DELETE_NODE",
                "payload": {},
                "options": {"dryRun": True},
                "baseRevision": None,
            }
        )

        analysis = service.analyze_delete(payload)

        self.assertNotIn("decisionId", analysis)
        self.assertEqual(analysis["strategy"], "blocked")
        self.assertTrue(analysis["inMap"])
        self.assertFalse(analysis["safe"])
        self.assertEqual(analysis["options"], [])

    def test_static_map_delete_analysis_is_auto_data_item(self) -> None:
        sandbox = _FakeSandbox(
            anchor={
                **_anchor(),
                "inMap": True,
                "isRouteRoot": False,
                "deleteSafe": False,
                "deleteReasons": ["位于 map 循环内"],
                "mapSource": {
                    "kind": "variable_array",
                    "variable": "items",
                    "start": 10,
                    "end": 80,
                    "count": 3,
                },
                "instance": 2,
            }
        )
        service = DirectOperationService(
            sandbox=sandbox,  # type: ignore[arg-type]
            settings=_Settings(),  # type: ignore[arg-type]
        )
        payload = DirectActionPayload.model_validate(
            {
                **_payload().model_dump(by_alias=True),
                "action": "DELETE_NODE",
                "payload": {},
                "options": {"dryRun": True},
                "baseRevision": None,
            }
        )

        analysis = service.analyze_delete(payload)

        self.assertTrue(analysis["safe"])
        self.assertEqual(analysis["strategy"], "direct")
        self.assertNotIn("decisionId", analysis)
        self.assertEqual(analysis["autoAction"], "DELETE_DATA_ITEM")


class DirectUndoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryChangeLogStore(max_entries=20)
        self.sandbox = _FakeSandbox()
        self.sandbox.head = NEXT_SHA
        self.service = DirectOperationService(
            sandbox=self.sandbox,  # type: ignore[arg-type]
            settings=_Settings(),  # type: ignore[arg-type]
        )
        self.service._changelog = self.store
        self.store.append(
            ChangeLogEntry(
                change_id="chg_undo",
                project_id=PROJECT_ID,
                sandbox_id=SANDBOX_ID,
                action="UPDATE_TEXT",
                target={"file": "src/App.jsx", "component": "App"},
                summary="将 App 的文案改为“我的任务”",
                commit_hash=NEXT_SHA,
                before_commit=BASE_SHA,
                created_at=datetime.now(timezone.utc),
            )
        )

    def test_undo_last_resets_to_before_commit(self) -> None:
        result = self.service.undo_last(PROJECT_ID, SANDBOX_ID)

        self.assertEqual(result["restoredCommit"], BASE_SHA)
        self.assertEqual(self.sandbox.resets, [BASE_SHA])
        entries = self.store.list_for_project(PROJECT_ID)
        self.assertTrue(entries[-1].summary.startswith("撤回"))
        self.assertFalse(entries[-1].applied)

    def test_undo_refuses_when_newer_commit_exists(self) -> None:
        self.sandbox.head = "c" * 40

        with self.assertRaises(DirectActionError) as ctx:
            self.service.undo_last(PROJECT_ID, SANDBOX_ID)

        self.assertEqual(ctx.exception.code, DirectErrorCode.REVISION_MISMATCH)
        self.assertEqual(self.sandbox.resets, [])


class ComponentTemplateTest(unittest.TestCase):
    def test_ensure_imports_is_idempotent_and_inserted_after_last_import(self) -> None:
        from ai_core.direct_ops.templates import ensure_imports

        content = "import React from 'react'\n\nexport default function App() {}\n"
        once = ensure_imports(content, ["import Button from './Button'"])
        twice = ensure_imports(once, ["import Button from './Button'"])

        self.assertEqual(once, twice)
        self.assertEqual(once.count("import Button"), 1)
        self.assertLess(once.index("import React"), once.index("import Button"))

    def test_card_template_declares_two_nodes(self) -> None:
        from ai_core.direct_ops.templates import COMPONENT_TEMPLATES

        self.assertEqual(COMPONENT_TEMPLATES["Card"].node_count, 2)
        self.assertIn("vibe-direct-card", COMPONENT_TEMPLATES["Card"].jsx)


class ChangeLogPersistenceTest(unittest.TestCase):
    def test_jsonl_store_survives_reload(self) -> None:
        import tempfile
        from pathlib import Path

        from ai_core.direct_ops.changelog import JsonlChangeLogStore

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "changelog.jsonl"
            first = JsonlChangeLogStore(path, max_entries=10)
            first.append(
                ChangeLogEntry(
                    change_id="chg_persist",
                    project_id="proj_persist",
                    sandbox_id=SANDBOX_ID,
                    action="ADD_COMPONENT",
                    target={"file": "src/App.jsx", "component": "main"},
                    summary="在 main 中新增 Button",
                    before_hash="sha256:a",
                    after_hash="sha256:b",
                    commit_hash="d" * 40,
                    risk="L0_DIRECT",
                    created_at=datetime.now(timezone.utc),
                )
            )

            second = JsonlChangeLogStore(path, max_entries=10)
            entries = second.list_for_project("proj_persist")

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].change_id, "chg_persist")
        self.assertIn("新增 Button", entries[0].summary)


class ChangeLogArchitectBridgeTest(unittest.TestCase):
    def test_recent_changelog_is_available_to_architect(self) -> None:
        from unittest.mock import patch

        from ai_core.architect.service import _recent_direct_changes
        from ai_core.direct_ops import changelog as changelog_module

        store = MemoryChangeLogStore(max_entries=10)
        store.append(
            ChangeLogEntry(
                change_id="chg_arch_test",
                project_id="proj_arch_bridge",
                sandbox_id=SANDBOX_ID,
                action="UPDATE_TEXT",
                target={"file": "src/App.jsx", "component": "App"},
                summary="将 App 的文案改为“我的任务”",
                before_hash="sha256:a",
                after_hash="sha256:b",
                commit_hash="c" * 40,
                risk="L0_DIRECT",
                created_at=datetime.now(timezone.utc),
            )
        )

        with patch.object(changelog_module, "_change_log_store", store):
            changes = _recent_direct_changes("proj_arch_bridge")

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["action"], "UPDATE_TEXT")
        self.assertIn("我的任务", changes[0]["summary"])
        self.assertIn("createdAt", changes[0])


if __name__ == "__main__":
    unittest.main()
