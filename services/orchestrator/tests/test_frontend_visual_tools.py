"""前端 Agent 语义工具门面测试。

验证重点：
  - 未绑定上下文 fail-closed；
  - 所有工具复用 DirectOperationService，而不是自己写 AST；
  - node_id 被转换为 data-node-id 选择器；
  - MOVE / INSERT 的 placement 结构正确；
  - dryRun 不读取 revision，真实写入自动补齐 revision。
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from ai_core.domain.direct_ops import DirectActionPayload
from ai_core.tools.base import ToolRegistry
from ai_core.tools.frontend_visual_tools import (
    bind_frontend_tool_context,
    build_delete_node_tool,
    build_get_frontend_revision_tool,
    build_insert_component_tool,
    build_move_node_tool,
    build_undo_last_frontend_change_tool,
    build_update_class_tool,
    build_update_props_tool,
    build_update_style_tool,
    build_update_text_tool,
    register_frontend_visual_tools,
    reset_frontend_tool_context,
)

PROJECT_ID = "proj_frontend_tool"
SANDBOX_ID = "sbx_frontend_tool"
REVISION = "a" * 40


class _FakeResponse:
    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {
            "directActionId": "dact_test",
            "status": "SUCCEEDED",
            "action": "UPDATE_TEXT",
            "risk": "L0_DIRECT",
            "revisionBefore": REVISION,
            "revisionAfter": "b" * 40,
            "changeId": "chg_test",
        }


class _FakeService:
    def __init__(self) -> None:
        self.calls: list[DirectActionPayload] = []
        self.revision_calls = 0
        self.undo_calls = 0

    def revision(self, project_id: str, sandbox_id: str) -> dict[str, str]:
        self.revision_calls += 1
        return {
            "projectId": project_id,
            "sandboxId": sandbox_id,
            "revision": REVISION,
        }

    def execute(self, payload: DirectActionPayload) -> _FakeResponse:
        self.calls.append(payload)
        return _FakeResponse()

    def undo_last(self, project_id: str, sandbox_id: str) -> dict[str, str]:
        self.undo_calls += 1
        return {
            "undoneChangeId": "chg_before",
            "restoredCommit": REVISION,
            "summary": f"undo {project_id}/{sandbox_id}",
        }


def _payload(result: str) -> dict[str, Any]:
    return json.loads(result)


class FrontendVisualToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = _FakeService()
        self.token = bind_frontend_tool_context(
            self.service,
            PROJECT_ID,
            SANDBOX_ID,
            run_id="run_test",
        )
        self.context_bound = True

    def tearDown(self) -> None:
        if self.context_bound:
            reset_frontend_tool_context(self.token)

    def test_unbound_tools_fail_closed(self) -> None:
        reset_frontend_tool_context(self.token)
        self.context_bound = False
        result = _payload(
            build_update_text_tool().invoke(
                {"text": "新文案", "node_id": "node_hero_title"}
            )
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "FRONTEND_TOOL_CONTEXT_REQUIRED")
        self.assertFalse(result["retryable"])

    def test_update_text_uses_node_id_selector_and_revision(self) -> None:
        result = _payload(
            build_update_text_tool().invoke(
                {"text": "新文案", "node_id": "node_hero_title"}
            )
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(len(self.service.calls), 1)
        payload = self.service.calls[0]
        self.assertEqual(payload.action, "UPDATE_TEXT")
        self.assertEqual(payload.project_id, PROJECT_ID)
        self.assertEqual(payload.sandbox_id, SANDBOX_ID)
        self.assertEqual(payload.run_id, "run_test")
        self.assertEqual(payload.target.selector, '[data-node-id="node_hero_title"]')
        self.assertEqual(payload.payload["text"], "新文案")
        self.assertEqual(payload.base_revision, REVISION)
        self.assertEqual(self.service.revision_calls, 1)

    def test_update_text_accepts_explicit_selector_without_revision_lookup(
        self,
    ) -> None:
        result = _payload(
            build_update_text_tool().invoke(
                {
                    "text": "新文案",
                    "selector": '[data-source="cta"]',
                    "base_revision": REVISION,
                }
            )
        )
        self.assertTrue(result["ok"])
        self.assertEqual(self.service.revision_calls, 0)
        self.assertEqual(self.service.calls[0].base_revision, REVISION)

    def test_dry_run_does_not_fetch_revision(self) -> None:
        result = _payload(
            build_update_text_tool().invoke(
                {"text": "预览", "node_id": "node_title", "dry_run": True}
            )
        )
        self.assertTrue(result["ok"])
        self.assertEqual(self.service.revision_calls, 0)
        self.assertTrue(self.service.calls[0].options.dry_run)
        self.assertIsNone(self.service.calls[0].base_revision)

    def test_invalid_node_id_is_rejected_before_service(self) -> None:
        result = _payload(
            build_update_text_tool().invoke({"text": "新文案", "node_id": 'x"] , *'})
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "DIRECT_TARGET_INVALID")
        self.assertEqual(self.service.calls, [])

    def test_invalid_text_is_reported_as_argument_error(self) -> None:
        result = _payload(
            build_update_text_tool().invoke(
                {"text": "<script>", "node_id": "node_title"}
            )
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "FRONTEND_TOOL_INVALID_ARGUMENT")
        self.assertEqual(self.service.calls, [])

    def test_move_node_builds_parent_placement(self) -> None:
        result = _payload(
            build_move_node_tool().invoke(
                {
                    "node_id": "node_cta",
                    "parent_node_id": "node_card",
                    "insert_position": "inside_index",
                    "sibling_index": 2,
                }
            )
        )
        self.assertTrue(result["ok"])
        payload = self.service.calls[0]
        self.assertEqual(payload.action, "MOVE_NODE")
        self.assertEqual(payload.target.selector, '[data-node-id="node_cta"]')
        self.assertEqual(
            payload.target.placement,
            {
                "insertPosition": "inside_index",
                "parentSelector": '[data-node-id="node_card"]',
                "siblingIndex": 2,
            },
        )

    def test_move_node_requires_parent(self) -> None:
        result = _payload(build_move_node_tool().invoke({"node_id": "node_cta"}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "DIRECT_TARGET_REQUIRED")
        self.assertEqual(self.service.calls, [])

    def test_insert_component_targets_parent_container(self) -> None:
        result = _payload(
            build_insert_component_tool().invoke(
                {
                    "component_type": "Button",
                    "parent_node_id": "node_card",
                    "insert_position": "inside_start",
                }
            )
        )
        self.assertTrue(result["ok"])
        payload = self.service.calls[0]
        self.assertEqual(payload.action, "ADD_COMPONENT")
        self.assertEqual(payload.target.selector, '[data-node-id="node_card"]')
        self.assertEqual(payload.payload["componentType"], "Button")
        self.assertEqual(
            payload.target.placement,
            {"insertPosition": "inside_start"},
        )

    def test_delete_node_uses_selector(self) -> None:
        result = _payload(
            build_delete_node_tool().invoke({"selector": '[data-source="old"]'})
        )
        self.assertTrue(result["ok"])
        payload = self.service.calls[0]
        self.assertEqual(payload.action, "DELETE_NODE")
        self.assertEqual(payload.target.selector, '[data-source="old"]')

    def test_get_revision_and_undo_reuse_service(self) -> None:
        revision = _payload(build_get_frontend_revision_tool().invoke({}))
        self.assertTrue(revision["ok"])
        self.assertEqual(revision["revision"], REVISION)

        undone = _payload(build_undo_last_frontend_change_tool().invoke({}))
        self.assertTrue(undone["ok"])
        self.assertEqual(undone["undoneChangeId"], "chg_before")
        self.assertEqual(self.service.undo_calls, 1)

    def test_update_props_targets_call_site_oid(self) -> None:
        result = _payload(
            build_update_props_tool().invoke(
                {
                    "props": {"title": "新标题"},
                    "node_id": "csoid_user_card",
                }
            )
        )

        self.assertTrue(result["ok"])
        call = self.service.calls[-1]
        self.assertEqual(call.action, "UPDATE_PROPS")
        self.assertEqual(call.target.call_site_oid, "csoid_user_card")
        self.assertEqual(call.payload["props"], {"title": "新标题"})

    def test_update_class_and_style_tools(self) -> None:
        class_result = _payload(
            build_update_class_tool().invoke(
                {
                    "class_name": "text-red-600",
                    "node_id": "oid_title0001",
                }
            )
        )
        style_result = _payload(
            build_update_style_tool().invoke(
                {
                    "style": {"fontSize": "20px"},
                    "node_id": "oid_title0001",
                }
            )
        )

        self.assertTrue(class_result["ok"])
        self.assertTrue(style_result["ok"])
        self.assertEqual(self.service.calls[-2].action, "UPDATE_CLASS")
        self.assertEqual(self.service.calls[-2].payload["className"], "text-red-600")
        self.assertEqual(self.service.calls[-1].action, "UPDATE_STYLE")
        self.assertEqual(self.service.calls[-1].payload["style"], {"fontSize": "20px"})

    def test_registration_exposes_semantic_tools(self) -> None:
        registry = ToolRegistry()
        register_frontend_visual_tools(registry)
        names = {item.name for item in registry.list_descriptors()}
        self.assertEqual(
            names,
            {
                "update_text",
                "update_props",
                "update_class",
                "update_style",
                "move_node",
                "insert_component",
                "delete_node",
                "get_frontend_revision",
                "undo_last_frontend_change",
            },
        )


if __name__ == "__main__":
    unittest.main()
