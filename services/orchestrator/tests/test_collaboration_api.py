"""Architect 协作状态查询 API。"""

from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_core.a2a import build_feedback
from ai_core.api.collaboration import router
from ai_core.collaboration.error_router import GATE_ERRORS
from ai_core.collaboration.hub import ARCHITECT_HUB


class CollaborationApiTest(unittest.TestCase):
    def setUp(self) -> None:
        ARCHITECT_HUB.reset()
        GATE_ERRORS.ledger.reset()
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        ARCHITECT_HUB.reset()
        GATE_ERRORS.ledger.reset()

    def test_returns_inbox_actions_and_grants(self) -> None:
        run_id = "run_api"
        ARCHITECT_HUB.ingest_feedback(
            build_feedback(
                run_id=run_id,
                from_agent="BackendCoder",
                to_agent="Architect",
                kind="contract_review",
                requested_action="REVIEW_CONTRACT",
                summary="需要复核契约",
                payload={"taskId": "task_api"},
            ),
            task_id="task_api",
        )
        ARCHITECT_HUB.fastpath.issue(
            run_id=run_id,
            task_id="task_api",
        )
        ARCHITECT_HUB.ledger.record(
            run_id=run_id,
            task_id="task_api",
            actor="Applier",
            tool="apply_patch",
            target="server/index.js",
            after_hash="abc",
        )

        response = self.client.get(f"/api/v1/internal/collaboration/runs/{run_id}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["runId"], run_id)
        self.assertEqual(
            body["counts"],
            {
                "inbox": 1,
                "actions": 1,
                "fastPathGrants": 1,
                "gateErrors": 0,
            },
        )
        self.assertEqual(body["inbox"][0]["agent"], "BackendCoder")
        self.assertEqual(body["actions"][0]["actor"], "Applier")
        self.assertTrue(body["fastPathGrants"][0]["grantId"].startswith("fp_"))

    def test_error_catalog_and_high_frequency_stats(self) -> None:
        catalog = self.client.get(
            "/api/v1/internal/collaboration/errors/catalog"
        ).json()
        self.assertEqual(catalog["maxAttempts"], 3)
        codes = {item["code"] for item in catalog["codes"]}
        self.assertIn("ORM_SCHEMA_MISMATCH", codes)
        self.assertIn("CONTRACT_VALIDATION_FAILED", codes)

        GATE_ERRORS.ingest_feedback(
            {
                "runId": "run_gate",
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
                        }
                    ],
                },
            }
        )

        stats = self.client.get("/api/v1/internal/collaboration/errors/stats").json()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["topErrors"][0]["code"], "ORM_SCHEMA_MISMATCH")

        run_errors = self.client.get(
            "/api/v1/internal/collaboration/runs/run_gate/errors"
        ).json()
        self.assertEqual(run_errors["count"], 1)
        self.assertEqual(run_errors["errors"][0]["assignedTo"], "DBACoder")

        snapshot = self.client.get(
            "/api/v1/internal/collaboration/runs/run_gate"
        ).json()
        self.assertEqual(snapshot["counts"]["gateErrors"], 1)

    def test_limit_is_applied(self) -> None:
        run_id = "run_limit"
        for index in range(3):
            ARCHITECT_HUB.ledger.record(
                run_id=run_id,
                task_id="task",
                actor="Applier",
                tool="apply_patch",
                target=f"file-{index}.js",
            )
        response = self.client.get(
            f"/api/v1/internal/collaboration/runs/{run_id}",
            params={"limit": 2},
        )
        body = response.json()
        self.assertEqual(len(body["actions"]), 2)
        self.assertEqual(body["counts"]["actions"], 3)


if __name__ == "__main__":
    unittest.main()
