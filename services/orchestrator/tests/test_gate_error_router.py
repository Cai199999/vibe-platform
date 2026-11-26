"""确定性门禁错误目录、Architect 分派与高频统计。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ai_core.collaboration.error_router import (
    MAX_FIX_ATTEMPTS,
    GateError,
    GateErrorLedger,
    GateErrorRouter,
    all_specs,
    error_fingerprint,
    spec_for,
)


class GateCatalogTests(unittest.TestCase):
    def test_known_codes_have_owners(self) -> None:
        self.assertIn("DBACoder", spec_for("ORM_SCHEMA_MISMATCH").owners)
        self.assertIn("Architect", spec_for("CONTRACT_DEFECT").owners)
        self.assertIn("InfraCoder", spec_for("SANDBOX_ERROR").owners)
        self.assertIn("BackendCoder", spec_for("CODE_DEFECT").owners)

    def test_envelope_codes_are_in_catalog(self) -> None:
        codes = {spec.code for spec in all_specs()}
        self.assertIn("RUN_NOT_FOUND", codes)
        self.assertIn("CONTRACT_VALIDATION_FAILED", codes)
        self.assertGreater(len(codes), 20)

    def test_unknown_code_falls_back_to_architect(self) -> None:
        spec = spec_for("SOMETHING_NEW_FAILED")
        self.assertEqual(spec.owners, ("Architect",))
        self.assertEqual(spec.stage, "unknown")

    def test_catalog_summary_lists_owner_per_code(self) -> None:
        router = GateErrorRouter(
            GateErrorLedger(path=Path(tempfile.gettempdir()) / "unused.jsonl")
        )
        summary = router.catalog_summary(limit=10)
        self.assertIn("ORM_SCHEMA_MISMATCH", summary)
        self.assertIn("→", summary)

    def test_fingerprint_ignores_volatile_numbers(self) -> None:
        first = error_fingerprint("ORM_SCHEMA_MISMATCH", "缺少列 12 在 server/db.js")
        second = error_fingerprint("ORM_SCHEMA_MISMATCH", "缺少列 99 在 server/db.js")
        self.assertEqual(first, second)


class GateRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.ledger = GateErrorLedger(path=Path(self.directory.name) / "errors.jsonl")
        self.router = GateErrorRouter(self.ledger)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_plan_dispatches_to_owner(self) -> None:
        assignment = self.router.plan(
            GateError(
                code="ORM_SCHEMA_MISMATCH",
                message="缺少列",
                run_id="run_1",
                source="DBACoder",
                attempt=1,
                max_attempts=MAX_FIX_ATTEMPTS,
            )
        )
        self.assertEqual(assignment.assigned_to, "DBACoder")
        self.assertFalse(assignment.exhausted)

    def test_plan_allows_last_attempt_and_escalates_after(self) -> None:
        last_allowed = self.router.plan(
            GateError(
                code="CODE_DEFECT",
                message="语法错误",
                attempt=MAX_FIX_ATTEMPTS,
                max_attempts=MAX_FIX_ATTEMPTS,
            )
        )
        self.assertFalse(last_allowed.exhausted)
        self.assertEqual(last_allowed.assigned_to, "BackendCoder")

        exhausted = self.router.plan(
            GateError(
                code="CODE_DEFECT",
                message="语法错误",
                attempt=MAX_FIX_ATTEMPTS + 1,
                max_attempts=MAX_FIX_ATTEMPTS,
            )
        )
        self.assertTrue(exhausted.exhausted)
        self.assertEqual(exhausted.next_agents, ("Architect",))
        self.assertEqual(exhausted.assigned_to, "Architect")

    def test_ingest_feedback_dedupes_and_records_jsonl(self) -> None:
        assignments = self.router.ingest_feedback(
            {
                "runId": "run_fb",
                "fromAgent": "Applier",
                "toAgent": "DBACoder",
                "kind": "schema_repair",
                "requestedAction": "REGENERATE_DB",
                "summary": "ORM 字段错位",
                "payload": {
                    "attempt": 0,
                    "maxAttempts": 3,
                    "issues": [
                        {
                            "code": "ORM_SCHEMA_MISMATCH",
                            "message": "缺少列 created_at",
                        },
                        {
                            "code": "ORM_SCHEMA_MISMATCH",
                            "message": "缺少列 created_at",
                        },
                    ],
                },
            }
        )
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0].assigned_to, "DBACoder")
        self.assertEqual(assignments[0].error.attempt, 1)

        stats = self.router.stats()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["byCode"][0]["code"], "ORM_SCHEMA_MISMATCH")
        self.assertEqual(stats["byAgent"][0]["agent"], "DBACoder")

        lines = Path(self.ledger.path).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        item = json.loads(lines[0])
        self.assertEqual(item["code"], "ORM_SCHEMA_MISMATCH")
        self.assertEqual(item["phase"], "feedback")

    def test_ingest_feedback_ignores_non_repair_reports(self) -> None:
        assignments = self.router.ingest_feedback(
            {
                "runId": "run_ok",
                "fromAgent": "FrontendCoder",
                "toAgent": "Architect",
                "kind": "handoff",
                "requestedAction": "ACK",
                "summary": "前端完成",
                "payload": {},
            }
        )
        self.assertEqual(assignments, [])
        self.assertEqual(self.router.stats()["total"], 0)

    def test_feedback_at_budget_escalates_to_architect(self) -> None:
        assignments = self.router.ingest_feedback(
            {
                "runId": "run_exhausted",
                "fromAgent": "BackendCoder",
                "toAgent": "Architect",
                "kind": "contract_review",
                "requestedAction": "REVIEW_CONTRACT",
                "summary": "自愈耗尽",
                "payload": {
                    "attempt": 3,
                    "maxAttempts": 3,
                    "issues": [
                        {"code": "JOIN_ALIAS_MISMATCH", "message": "别名不存在"}
                    ],
                },
            }
        )
        self.assertEqual(len(assignments), 1)
        self.assertTrue(assignments[0].exhausted)
        self.assertEqual(assignments[0].assigned_to, "Architect")
        self.assertEqual(self.router.stats()["exhausted"], 1)

    def test_stats_recover_from_jsonl_after_restart(self) -> None:
        assignment = self.router.plan(
            GateError(
                code="TEST_AUTH_FAILED",
                message="登录失败",
                run_id="run_restart",
                attempt=1,
            )
        )
        self.router.ledger.record(assignment, source="TestAgent", phase="testing")
        reloaded = GateErrorRouter(
            GateErrorLedger(path=Path(self.directory.name) / "errors.jsonl")
        )
        stats = reloaded.stats()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["byCode"][0]["code"], "TEST_AUTH_FAILED")
        self.assertTrue(reloaded.ledger.for_run("run_restart"))

    def test_final_failure_and_validation_issues_are_recorded(self) -> None:
        final = self.router.record_final_failure(
            run_id="run_final",
            code="FULLSTACK_GENERATION_FAILED",
            message="构建失败",
            stage="fullstack.BuildVerifier",
            source="BuildVerifier",
        )
        self.assertTrue(final.exhausted)
        self.assertEqual(final.assigned_to, "Architect")

        assignments = self.router.record_validation_issues(
            run_id="run_contract",
            issues=["缺少 frontend_routes", "页面完整度不足"],
            attempt=1,
            max_attempts=3,
        )
        self.assertEqual(len(assignments), 2)
        self.assertTrue(all(item.assigned_to == "Architect" for item in assignments))

        stats = self.router.stats()
        self.assertEqual(stats["finalFailures"], 1)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(len(self.ledger.for_run("run_contract")), 2)


if __name__ == "__main__":
    unittest.main()
