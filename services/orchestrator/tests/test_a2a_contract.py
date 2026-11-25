"""A2A 反馈契约与问题叙事 fixtures 的确定性测试。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from ai_core.a2a_contract import (
    A2A_ALLOWED,
    A2A_REPAIR_KINDS,
    validate_a2a_feedback,
    validate_a2a_sequence,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = (
    REPO_ROOT
    / "apps"
    / "platform-web"
    / "src"
    / "workspace"
    / "__tests__"
    / "fixtures"
    / "a2a"
)


def _load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


class A2AContractTests(unittest.TestCase):
    def test_fixtures_match_expected_narrative(self) -> None:
        for name in (
            "code_defect_resolved.json",
            "contract_defect_revision.json",
            "retry_exhausted_escalated.json",
            "replay_duplicate.json",
        ):
            fixture = _load(name)
            items = [event["payload"] for event in fixture["events"]]
            result = validate_a2a_sequence(
                items,
                run_terminal=fixture.get("runTerminal"),
            )
            expected = fixture["expected"]
            self.assertEqual(result["stepCount"], expected["stepCount"], name)
            self.assertEqual(result["loops"], expected["loops"], name)
            self.assertEqual(
                result["terminalReason"], expected["terminalReason"], name
            )
            self.assertEqual(result["errors"], expected.get("errors", []), name)
            if "warnings" in expected:
                self.assertEqual(result["warnings"], expected["warnings"], name)

    def test_out_of_matrix_is_rejected(self) -> None:
        feedback = {
            "feedbackId": "a2a_bad",
            "runId": "fsk_bad",
            "fromAgent": "Applier",
            "toAgent": "Architect",
            "kind": "code_repair",
            "requestedAction": "REPAIR_CODE",
            "summary": "不允许的方向",
            "evidence": [],
        }
        errors = validate_a2a_feedback(feedback)
        self.assertTrue(errors)
        self.assertIn("不在 A2A 允许矩阵内", errors[0])

    def test_missing_required_field_is_rejected(self) -> None:
        errors = validate_a2a_feedback(
            {
                "feedbackId": "a2a_missing",
                "runId": "fsk_missing",
                "fromAgent": "Applier",
                "toAgent": "DBACoder",
                "kind": "schema_repair",
                "requestedAction": "REGENERATE_DB",
            }
        )
        self.assertTrue(any("summary" in item for item in errors))

    def test_duplicate_feedback_id_is_deduped(self) -> None:
        fixture = _load("replay_duplicate.json")
        items = [event["payload"] for event in fixture["events"]]
        result = validate_a2a_sequence(items, run_terminal="SUCCEEDED")
        self.assertEqual(result["stepCount"], 1)
        self.assertEqual(result["warnings"], ["duplicate feedbackId a2a_replay_1"])

    def test_actual_runtime_kinds_are_covered(self) -> None:
        actual = {
            ("Applier", "DBACoder", "schema_repair", "REGENERATE_DB"),
            ("Applier", "BackendCoder", "code_repair", "REPAIR_CODE"),
            ("BackendCoder", "Architect", "contract_revision", "REVISE_CONTRACT"),
            ("BackendCoder", "Architect", "contract_review", "REVIEW_CONTRACT"),
            (
                "ConsistencyVerifier",
                "Architect",
                "contract_revision",
                "REVISE_CONTRACT",
            ),
            (
                "ConsistencyVerifier",
                "BackendCoder",
                "route_repair",
                "ADD_BACKEND_ROUTE",
            ),
            ("DBACoder", "BackendCoder", "schema_ready", "REBIND_ORM"),
            (
                "Architect",
                "BackendCoder",
                "contract_revision_started",
                "WAIT_FOR_CONTRACT",
            ),
            (
                "Architect",
                "BackendCoder",
                "contract_confirmed",
                "GENERATE_WITH_CONTRACT",
            ),
        }
        self.assertTrue(actual.issubset(A2A_ALLOWED))
        self.assertIn("route_repair", A2A_REPAIR_KINDS)


if __name__ == "__main__":
    unittest.main()
