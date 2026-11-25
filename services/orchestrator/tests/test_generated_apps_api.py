"""多服务生成产品入口 API。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_core.api.generated_apps import router


class GeneratedAppsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="vibe-generated-apps-")
        self.client = TestClient(self._app())

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _app(self) -> FastAPI:
        app = FastAPI()
        app.include_router(router)
        return app

    def test_generate_and_fetch_report(self) -> None:
        settings = SimpleNamespace(generated_apps_root=self.temp.name)
        with patch(
            "ai_core.api.generated_apps.get_settings",
            return_value=settings,
        ):
            response = self.client.post(
                "/api/v1/internal/generated-apps/generate",
                json={"name": "ticket-demo"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["status"], "ok")
            self.assertEqual(
                body["verification"]["idempotency"]["secondDuplicate"],
                True,
            )
            self.assertTrue(body["delivery"]["verify"]["ok"])
            self.assertIn("generate", [item["name"] for item in body["stages"]])

            run_id = body["runId"]
            fetched = self.client.get(f"/api/v1/internal/generated-apps/runs/{run_id}")
            self.assertEqual(fetched.status_code, 200)
            self.assertEqual(fetched.json()["runId"], run_id)

            missing = self.client.get("/api/v1/internal/generated-apps/runs/missing")
            self.assertEqual(missing.status_code, 404)

    def test_default_name_is_used(self) -> None:
        settings = SimpleNamespace(generated_apps_root=self.temp.name)
        with patch(
            "ai_core.api.generated_apps.get_settings",
            return_value=settings,
        ):
            response = self.client.post("/api/v1/internal/generated-apps/generate")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "ok")
            run_dir = Path(response.json()["runDir"])
            self.assertTrue((run_dir / "workspace" / "contract").exists())


if __name__ == "__main__":
    unittest.main()
