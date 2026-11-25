"""Architect 协作状态查询 API。"""

from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_core.a2a import build_feedback
from ai_core.api.collaboration import router
from ai_core.collaboration.hub import ARCHITECT_HUB


class CollaborationApiTest(unittest.TestCase):
    def setUp(self) -> None:
        ARCHITECT_HUB.reset()
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        ARCHITECT_HUB.reset()

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
            body["counts"], {"inbox": 1, "actions": 1, "fastPathGrants": 1}
        )
        self.assertEqual(body["inbox"][0]["agent"], "BackendCoder")
        self.assertEqual(body["actions"][0]["actor"], "Applier")
        self.assertTrue(body["fastPathGrants"][0]["grantId"].startswith("fp_"))

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
