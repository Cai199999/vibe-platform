"""全流程原型：拓扑校验、迁移演进与端到端闭环。"""

from __future__ import annotations

import dataclasses
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from ai_core.prototype.generator import generate_workspace
from ai_core.prototype.pipeline import run_full_flow
from ai_core.prototype.planner import TopologyError, plan_topology
from ai_core.prototype.runtime import (
    applied_migrations,
    run_migrations,
    schema_columns,
)
from ai_core.prototype.spec import default_ticket_spec


class PlannerTest(unittest.TestCase):
    def test_missing_consumer_is_rejected(self) -> None:
        spec = default_ticket_spec()
        broken_event = dataclasses.replace(
            spec.events[0],
            consumer="missing-worker",
        )
        broken = dataclasses.replace(spec, events=(broken_event,))
        with self.assertRaises(TopologyError):
            plan_topology(broken)

    def test_valid_spec_plans_flows(self) -> None:
        topology = plan_topology(default_ticket_spec())
        self.assertEqual(len(topology.flows), 1)
        self.assertEqual(topology.flows[0]["agent"], "RoutingAgent")
        self.assertIn("migration", topology.deployment_order)


class MigrationTest(unittest.TestCase):
    def test_migrations_are_idempotent_and_evolve_schema(self) -> None:
        temp = Path(tempfile.mkdtemp(prefix="vibe-migrate-"))
        try:
            workspace = temp / "workspace"
            spec = default_ticket_spec()
            generate_workspace(workspace, spec, plan_topology(spec))
            db = workspace / "data" / "app.db"

            first = run_migrations(workspace, db)
            second = run_migrations(workspace, db)
            self.assertEqual(
                first,
                ["001_init.sql", "002_ai_fields.sql"],
            )
            self.assertEqual(second, [])
            self.assertIn("ai_category", schema_columns(db, "tickets"))
            self.assertEqual(
                applied_migrations(db),
                ["001_init.sql", "002_ai_fields.sql"],
            )
        finally:
            shutil.rmtree(temp, ignore_errors=True)


class FullFlowTest(unittest.TestCase):
    def test_end_to_end(self) -> None:
        temp = Path(tempfile.mkdtemp(prefix="vibe-full-flow-"))
        try:
            report = run_full_flow(
                output_root=temp,
                run_id="run_test",
            )
            self.assertEqual(report["status"], "ok")
            idempotency = report["verification"]["idempotency"]
            self.assertFalse(idempotency["firstDuplicate"])
            self.assertTrue(idempotency["secondDuplicate"])
            self.assertEqual(idempotency["processedEvents"], 1)
            self.assertEqual(
                [item["aiCategory"] for item in report["verification"]["tickets"]],
                ["IT", "财务"],
            )
            self.assertTrue(report["delivery"]["verify"]["ok"])
            modes = [item["deliveryMode"] for item in report["agent"]["inbox"]]
            self.assertIn("fast_path", modes)
            self.assertIn("hub", modes)
            report_path = Path(report["reportPath"])
            self.assertTrue(report_path.exists())
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "ok")
            self.assertGreaterEqual(payload["artifacts"], 20)
        finally:
            shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
