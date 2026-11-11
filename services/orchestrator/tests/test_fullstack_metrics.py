"""全栈核心指标与只读 AST 诊断的单元测试。"""

from __future__ import annotations

import unittest

from ai_core.fullstack.coders import generate_backend_bundle, generate_dba_bundle
from ai_core.fullstack.diagnostics import DIAG_JOIN_ALIAS, check_orm_syntax
from ai_core.observability.fullstack_metrics import (
    FULLSTACK_METRICS,
    METRIC_APPLIER_BLOCK_RATE,
    METRIC_CONTRACT_MISRETRY_COUNT,
    METRIC_CONTRACT_REVISION_RATE,
    METRIC_MULTI_TABLE_AST_PASS_RATE,
)
from tests.test_fullstack import _master_detail_contract


class FullstackMetricsTest(unittest.TestCase):
    def setUp(self) -> None:
        FULLSTACK_METRICS.reset()

    tearDown = setUp

    def test_four_core_metrics(self) -> None:
        for _ in range(4):
            FULLSTACK_METRICS.record_applier_attempt()
        FULLSTACK_METRICS.record_applier_block(join_issue=True)
        FULLSTACK_METRICS.record_contract_misretry()
        FULLSTACK_METRICS.record_multi_table_ast(passed=True)
        FULLSTACK_METRICS.record_multi_table_ast(passed=True)
        FULLSTACK_METRICS.record_multi_table_ast(passed=False)
        FULLSTACK_METRICS.record_contract_confirmation()
        FULLSTACK_METRICS.record_contract_confirmation()
        FULLSTACK_METRICS.record_contract_revision()

        snapshot = FULLSTACK_METRICS.snapshot()
        self.assertAlmostEqual(snapshot[METRIC_APPLIER_BLOCK_RATE]["rate"], 0.25)
        self.assertEqual(snapshot[METRIC_CONTRACT_MISRETRY_COUNT], 1)
        self.assertAlmostEqual(
            snapshot[METRIC_MULTI_TABLE_AST_PASS_RATE]["rate"], 2 / 3, places=5
        )
        self.assertAlmostEqual(snapshot[METRIC_CONTRACT_REVISION_RATE]["rate"], 0.5)
        self.assertEqual(snapshot["details"]["applierBlockedByJoin"], 1)

    def test_zero_denominator_returns_zero_rate(self) -> None:
        snapshot = FULLSTACK_METRICS.snapshot()
        for metric in (
            METRIC_APPLIER_BLOCK_RATE,
            METRIC_MULTI_TABLE_AST_PASS_RATE,
            METRIC_CONTRACT_REVISION_RATE,
        ):
            self.assertEqual(snapshot[metric]["rate"], 0.0)
            self.assertEqual(snapshot[metric]["denominator"], 0)


class OrmDiagnosticsTest(unittest.TestCase):
    def setUp(self) -> None:
        FULLSTACK_METRICS.reset()

    def tearDown(self) -> None:
        FULLSTACK_METRICS.reset()

    @staticmethod
    def _generated_files() -> tuple[dict[str, str], str]:
        contract = _master_detail_contract()
        dba = generate_dba_bundle(contract)
        backend = generate_backend_bundle(
            contract,
            dba_schema=dba.files["server/schema.core.json"],
        )
        files = dict(dba.files)
        files.update(backend.files)
        return files, contract

    def test_bad_join_alias_is_diagnosed_and_counted(self) -> None:
        files, contract = self._generated_files()
        self.assertIn("/:id/resumes", files["server/index.js"])
        files["server/index.js"] = files["server/index.js"].replace(
            "/:id/resumes", "/:id/resumez"
        )

        report = check_orm_syntax(files, contract)
        self.assertTrue(report.applicable)
        self.assertTrue(report.multi_table)
        self.assertFalse(report.ok)
        self.assertTrue(report.has_join_issue)
        self.assertTrue(any(issue.code == DIAG_JOIN_ALIAS for issue in report.issues))
        snapshot = FULLSTACK_METRICS.snapshot()
        self.assertEqual(snapshot[METRIC_MULTI_TABLE_AST_PASS_RATE]["numerator"], 0)
        self.assertEqual(snapshot[METRIC_MULTI_TABLE_AST_PASS_RATE]["denominator"], 1)

    def test_correct_multi_table_orm_passes(self) -> None:
        files, contract = self._generated_files()
        report = check_orm_syntax(files, contract)
        self.assertTrue(report.ok, report.summary())
        self.assertTrue(report.multi_table)
        snapshot = FULLSTACK_METRICS.snapshot()
        self.assertEqual(snapshot[METRIC_MULTI_TABLE_AST_PASS_RATE]["rate"], 1.0)

    def test_frontend_only_files_are_not_applicable(self) -> None:
        files, contract = self._generated_files()
        files.pop("server/db.js")
        report = check_orm_syntax(files, contract)
        self.assertFalse(report.applicable)
        self.assertEqual(
            FULLSTACK_METRICS.snapshot()[METRIC_MULTI_TABLE_AST_PASS_RATE][
                "denominator"
            ],
            0,
        )


if __name__ == "__main__":
    unittest.main()
