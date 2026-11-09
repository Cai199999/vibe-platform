"""人工审批开关与 evaluator 分支单元测试。

运行（无需 Docker / 模型密钥）：

    PYTHONPATH=services/orchestrator python3 -m unittest discover \
        -s services/orchestrator/tests -v
"""

from __future__ import annotations

import unittest

from ai_core.agent.graph import AgentGraphRunner
from ai_core.agent.schemas import RunOptions


class ManualApprovalOptionsTest(unittest.TestCase):
    def test_default_is_off(self) -> None:
        options = RunOptions()
        self.assertFalse(options.manual_approval_required)

    def test_new_field_true(self) -> None:
        options = RunOptions.model_validate({"requireManualApproval": True})
        self.assertTrue(options.manual_approval_required)

    def test_new_field_false(self) -> None:
        options = RunOptions.model_validate({"requireManualApproval": False})
        self.assertFalse(options.manual_approval_required)

    def test_new_field_overrides_legacy_mode(self) -> None:
        options = RunOptions.model_validate(
            {"requireManualApproval": False, "approvalMode": "required"}
        )
        self.assertFalse(options.manual_approval_required)

    def test_legacy_required_mode_still_works(self) -> None:
        options = RunOptions.model_validate({"approvalMode": "required"})
        self.assertTrue(options.manual_approval_required)

    def test_legacy_off_mode(self) -> None:
        options = RunOptions.model_validate({"approvalMode": "off"})
        self.assertFalse(options.manual_approval_required)


class _EvaluatorHarness(AgentGraphRunner):
    """只测试纯函数节点，不初始化 LLM / sandbox / checkpoint。"""

    def __init__(self) -> None:
        self._max_retries = 3
        self.events: list[tuple[str, dict]] = []

    def _emit(self, event_type: str, payload: dict, **kwargs) -> None:  # noqa: ANN003
        self.events.append((event_type, payload))


class EvaluatorApprovalBranchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = _EvaluatorHarness()

    def _success_state(self, approval_required: bool) -> dict:
        return {
            "verify_result": {"success": True},
            "retry_count": 0,
            "max_retries": 3,
            "approval_required": approval_required,
        }

    def test_success_with_approval_pauses(self) -> None:
        update = self.runner._evaluator(self._success_state(True))
        self.assertEqual(update["next_action"], "approve")
        self.assertEqual(update["status"], "AWAITING_APPROVAL")
        self.assertEqual(
            self.runner._route_after_evaluator({**self._success_state(True), **update}),
            "approval_gate",
        )

    def test_success_without_approval_ends_succeeded(self) -> None:
        update = self.runner._evaluator(self._success_state(False))
        self.assertEqual(update["next_action"], "succeed")
        self.assertEqual(update["status"], "SUCCEEDED")
        self.assertIsNone(update["error"])
        self.assertEqual(
            self.runner._route_after_evaluator(
                {**self._success_state(False), **update}
            ),
            "succeeded",
        )

    def test_failure_retry_is_independent_of_approval_switch(self) -> None:
        state = {
            "verify_result": {"success": False, "error_logs": "boom"},
            "retry_count": 0,
            "max_retries": 3,
            "approval_required": True,
        }
        update = self.runner._evaluator(state)
        self.assertEqual(update["next_action"], "retry")
        self.assertEqual(update["status"], "REPAIRING")

    def test_failure_exhausted_is_independent_of_approval_switch(self) -> None:
        state = {
            "verify_result": {"success": False, "error_logs": "boom"},
            "retry_count": 3,
            "max_retries": 3,
            "approval_required": False,
        }
        update = self.runner._evaluator(state)
        self.assertEqual(update["next_action"], "fail")
        self.assertEqual(update["status"], "FAILED")
        self.assertEqual(
            self.runner._route_after_evaluator({**state, **update}),
            "failed",
        )

    def test_same_error_after_repair_aborts_immediately(self) -> None:
        state = {
            "verify_result": {
                "success": False,
                "error_logs": "boom",
                "error_context": "[error] Expected }",
                "error_fingerprint": "abc123",
            },
            "retry_count": 1,
            "max_retries": 3,
            "last_repair_error_fingerprint": "abc123",
        }
        update = self.runner._evaluator(state)
        self.assertEqual(update["next_action"], "fail")
        self.assertEqual(update["status"], "FAILED")
        self.assertEqual(update["error_code"], "REPEATED_ERROR")
        self.assertEqual(
            self.runner._route_after_evaluator({**state, **update}),
            "failed",
        )

    def test_different_error_still_retries(self) -> None:
        state = {
            "verify_result": {
                "success": False,
                "error_logs": "boom",
                "error_context": "[error] Cannot find module",
                "error_fingerprint": "def456",
            },
            "retry_count": 1,
            "max_retries": 3,
            "last_repair_error_fingerprint": "abc123",
        }
        update = self.runner._evaluator(state)
        self.assertEqual(update["next_action"], "retry")
        self.assertEqual(update["retry_count"], 2)
        self.assertEqual(update["last_repair_error_fingerprint"], "def456")

    def test_first_failure_records_fingerprint(self) -> None:
        state = {
            "verify_result": {
                "success": False,
                "error_logs": "boom",
                "error_fingerprint": "abc123",
            },
            "retry_count": 0,
            "max_retries": 3,
        }
        update = self.runner._evaluator(state)
        self.assertEqual(update["next_action"], "retry")
        self.assertEqual(update["last_repair_error_fingerprint"], "abc123")


if __name__ == "__main__":
    unittest.main()
