"""FailureRouter / HITL 安全路径扩展测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ai_core.agent import graph as graph_module
from ai_core.agent.graph import (
    _build_pending_decision_payload,
    _is_visual_safe_target,
)
from ai_core.agent.service import RunExecutionService
from ai_core.direct_ops.changelog import MemoryChangeLogStore
from ai_core.direct_ops.failure_router import (
    ROUTE_DIRECT_TEMPLATE,
    ROUTE_LLM_CODER,
    route_add_component,
)
from ai_core.domain.decision import (
    ACTION_CANCEL,
    ACTION_RETRY,
    ACTION_VISUAL_ONLY,
)
from ai_core.domain.direct_ops import ChangeLogEntry
from ai_core.domain.agent import ExecuteRunRequest


class FailureRouterTest(unittest.TestCase):
    def test_whitelist_components_stay_deterministic(self) -> None:
        for component in ("Button", "Input", "Text", "Card", "Container"):
            route = route_add_component(component)
            self.assertEqual(route.route, ROUTE_DIRECT_TEMPLATE, component)

    def test_unknown_component_routes_to_llm(self) -> None:
        route = route_add_component("LoginForm")
        self.assertEqual(route.route, ROUTE_LLM_CODER)


def _state(*, create: bool = True, retry_count: int = 0) -> dict:
    annotation = {
        "annotationId": "ann_1",
        "action": "CREATE_NEW" if create else "MODIFY_EXISTING",
        "targetFile": "src/App.jsx",
    }
    return {
        "run_id": "run_1",
        "decision_retry_count": retry_count,
        "annotation_context": {"annotations": [{"annotation": annotation}]},
    }


def _anchor(*, protected: bool = True) -> dict:
    return {
        "anchorId": "anc_ann_1",
        "annotationId": "ann_1",
        "file": "src/App.jsx",
        "component": "App",
        "range": {"startOffset": 0, "endOffset": 10},
        "status": "RESOLVED",
        "protected": protected,
        "sharedReason": "被其它文件引用",
        "callSiteAnchor": None,
    }


class PendingDecisionOptionsTest(unittest.TestCase):
    def test_options_are_never_empty_and_include_retry_cancel(self) -> None:
        payload = _build_pending_decision_payload(
            _state(), "protected_component", _anchor(), 1
        )
        actions = {item["action"] for item in payload["options"]}
        self.assertIn(ACTION_RETRY, actions)
        self.assertIn(ACTION_CANCEL, actions)
        self.assertTrue(payload["options"])

    def test_visual_option_is_added_for_create(self) -> None:
        payload = _build_pending_decision_payload(
            _state(create=True), "protected_component", _anchor(), 1
        )
        actions = {item["action"] for item in payload["options"]}
        self.assertIn(ACTION_VISUAL_ONLY, actions)
        self.assertTrue(_is_visual_safe_target(_state(), _anchor()))

    def test_retry_disappears_after_limit(self) -> None:
        payload = _build_pending_decision_payload(
            _state(create=True, retry_count=2),
            "protected_component",
            _anchor(),
            2,
        )
        actions = {item["action"] for item in payload["options"]}
        self.assertNotIn(ACTION_RETRY, actions)
        self.assertIn(ACTION_VISUAL_ONLY, actions)
        self.assertIn(ACTION_CANCEL, actions)


class VisualOnlyChangeLogTest(unittest.TestCase):
    def test_visual_only_writes_applied_false_entry(self) -> None:
        store = MemoryChangeLogStore(max_entries=20)
        service = object.__new__(RunExecutionService)
        prepared = SimpleNamespace(
            run_id="run_visual",
            request=SimpleNamespace(project_id="proj_visual"),
            handle=SimpleNamespace(sandbox_id="sbx_visual"),
        )
        state = {
            "visual_only": True,
            "annotations": [{"action": "CREATE_NEW", "targetFile": "src/App.jsx"}],
        }

        with patch(
            "ai_core.agent.service.get_change_log_store",
            return_value=store,
        ):
            service._record_visual_only_change(prepared, state)

        entries = store.list_for_project("proj_visual")
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0].applied)
        self.assertTrue(entries[0].visual_only)
        self.assertEqual(entries[0].actor, "AI_RUN")
        self.assertEqual(entries[0].action, "ADD_COMPONENT")

    def test_visual_only_response_marks_not_committed(self) -> None:
        response = RunExecutionService._to_response(
            "run_visual",
            "sbx_visual",
            "http://preview",
            {"status": "REJECTED", "visual_only": True},
        )
        self.assertTrue(response.visual_only)
        self.assertFalse(response.committed)


if __name__ == "__main__":
    unittest.main()
