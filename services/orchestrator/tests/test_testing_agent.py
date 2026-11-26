"""测试层：计划、HTTP 执行、报告回流与 Agent Manifest。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_core.collaboration.error_router import GATE_ERRORS
from ai_core.collaboration.hub import ARCHITECT_HUB
from ai_core.supervisor.manifest import read_agent_manifest
from ai_core.testing.agent import TestAgent
from ai_core.fullstack.service import FullstackService
from ai_core.testing.metrics import TEST_METRICS
from ai_core.testing.models import CheckResult, TestReport, TestRequest
from ai_core.testing.planner import build_plan, core_resource, derive_sample_payload
from ai_core.testing.runner import HttpTestRunner

CONTRACT = {
    "apiRoutes": [
        {"method": "POST", "path": "/api/v1/users/login"},
        {"method": "GET", "path": "/api/v1/tasks"},
        {"method": "POST", "path": "/api/v1/tasks"},
        {"method": "GET", "path": "/api/v1/tasks/{task_id}"},
        {"method": "PATCH", "path": "/api/v1/tasks/{task_id}"},
        {"method": "DELETE", "path": "/api/v1/tasks/{task_id}"},
    ],
    "databaseDdl": """
CREATE TABLE tasks (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  tenant_id VARCHAR(64) NOT NULL,
  name VARCHAR(200) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'todo',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;
""",
}


class _Handler(BaseHTTPRequestHandler):
    records: dict[str, dict] = {}
    counter = 0

    def log_message(self, *args):  # noqa: D401 - 静默测试日志
        return

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def _send(self, status: int, payload: object = None) -> None:
        data = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/v1/tasks/") and self.path != "/api/v1/tasks":
            record_id = self.path.rsplit("/", 1)[-1]
            record = self.records.get(record_id)
            if record is None:
                self._send(404, {"message": "not found"})
                return
            self._send(200, record)
            return
        if self.path.startswith("/api/v1/tasks"):
            self._send(200, list(self.records.values()))
            return
        body = (
            '<!doctype html><html><body><div id="root"></div>'
            '<script type="module" src="/src/main.jsx"></script></body></html>'
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        if self.path.endswith("/users/login"):
            self._send(200, {"token": "test-token"})
            return
        if self.path == "/run":
            self._send(
                200,
                {
                    "status": "PASSED",
                    "message": "UI 表单提交成功，刷新后数据仍在",
                    "evidence": {"marker": "ui-test"},
                },
            )
            return
        payload = self._body()
        _Handler.counter += 1
        record_id = str(_Handler.counter)
        record = {"id": record_id, **payload}
        self.records[record_id] = record
        self._send(201, record)

    def do_PATCH(self):  # noqa: N802
        record_id = self.path.rsplit("/", 1)[-1]
        record = self.records.get(record_id)
        if record is None:
            self._send(404, {"message": "not found"})
            return
        record.update(self._body())
        self.records[record_id] = record
        self._send(200, record)

    def do_DELETE(self):  # noqa: N802
        record_id = self.path.rsplit("/", 1)[-1]
        self.records.pop(record_id, None)
        self._send(204, None)


class TestingAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.report_dir = tempfile.TemporaryDirectory()
        cls._old_report_path = os.environ.get("TEST_REPORT_PATH")
        os.environ["TEST_REPORT_PATH"] = str(
            Path(cls.report_dir.name) / "test-reports.jsonl"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        if cls._old_report_path is None:
            os.environ.pop("TEST_REPORT_PATH", None)
        else:
            os.environ["TEST_REPORT_PATH"] = cls._old_report_path
        cls.report_dir.cleanup()

    def setUp(self) -> None:
        _Handler.records = {}
        _Handler.counter = 0
        GATE_ERRORS.ledger.reset()
        ARCHITECT_HUB.reset()
        TEST_METRICS.reset()

    def tearDown(self) -> None:
        GATE_ERRORS.ledger.reset()
        ARCHITECT_HUB.reset()
        TEST_METRICS.reset()

    def _request(self, tier: str = "L2") -> TestRequest:
        return TestRequest(
            source_run_id="run_test_agent",
            project_id="proj_test",
            contract_id="contract_test",
            tier=tier,
            preview_url=self.base,
            api_base_url=self.base,
            contract=CONTRACT,
            timeout_seconds=5,
        )

    # ---- 计划 ---------------------------------------------------
    def test_plan_builds_login_and_crud_from_contract(self) -> None:
        self.assertEqual(core_resource(CONTRACT), "/api/v1/tasks")
        sample = derive_sample_payload(CONTRACT, "/api/v1/tasks")
        self.assertIn("name", sample)
        self.assertNotIn("id", sample)
        self.assertNotIn("tenant_id", sample)
        self.assertNotIn("created_at", sample)

        plan = build_plan(self._request("L2"))
        kinds = {check.kind for check in plan.checks}
        self.assertEqual(kinds, {"preview_shell", "login", "crud_flow"})
        self.assertIn("仅供参考", plan.advisory)
        self.assertTrue(plan.plan_id.startswith("tplan_"))

    def test_plan_adds_ui_check_when_runner_configured(self) -> None:
        request = self._request("L2").model_copy(
            update={"ui_runner_url": "http://127.0.0.1:8765"}
        )
        plan = build_plan(request)
        ui_checks = [check for check in plan.checks if check.kind == "ui_flow"]
        self.assertEqual(len(ui_checks), 1)
        self.assertEqual(
            ui_checks[0].metadata.get("runnerUrl"), "http://127.0.0.1:8765"
        )

    def test_ui_runner_pass_is_part_of_l2_report(self) -> None:
        request = self._request("L2").model_copy(update={"ui_runner_url": self.base})
        report = TestAgent().execute(request)
        self.assertEqual(report.status, "PASSED", report.model_dump())
        ui_check = next(check for check in report.checks if check.kind == "ui_flow")
        self.assertEqual(ui_check.status, "PASSED")
        self.assertEqual(report.summary["passed"], 4)

    def test_l3_plan_marks_production_check(self) -> None:
        plan = build_plan(self._request("L3"))
        kinds = {check.kind for check in plan.checks}
        self.assertIn("production_artifact", kinds)
        production = next(
            check for check in plan.checks if check.kind == "production_artifact"
        )
        self.assertEqual(production.metadata.get("requires"), "verify-sandbox")

    # ---- 执行 ---------------------------------------------------
    def test_http_runner_completes_business_flow(self) -> None:
        report = TestAgent().execute(self._request("L2"))
        self.assertEqual(report.status, "PASSED", report.model_dump())
        self.assertEqual(report.summary["failed"], 0)
        self.assertEqual(report.summary["passed"], 3)
        self.assertIn("仅供参考", report.advisory)
        crud = next(check for check in report.checks if check.kind == "crud_flow")
        self.assertEqual(crud.status, "PASSED")
        self.assertEqual(len(_Handler.records), 0)  # 删除后闭环

    def test_runner_reports_login_failure_with_code(self) -> None:
        class _FailRunner(HttpTestRunner):
            def _login(self, check, request):
                return CheckResult(
                    check_id=check.check_id,
                    kind=check.kind,
                    status="FAILED",
                    message="登录失败",
                    error_code="TEST_AUTH_FAILED",
                )

        report = TestAgent(runner_factory=lambda: _FailRunner()).execute(
            self._request("L2")
        )
        self.assertEqual(report.status, "FAILED")
        failures = [item for item in report.checks if item.status == "FAILED"]
        self.assertTrue(failures)
        stats = GATE_ERRORS.stats()
        self.assertGreaterEqual(stats["total"], 1)
        self.assertEqual(stats["byCode"][0]["code"], "TEST_AUTH_FAILED")
        inbox = ARCHITECT_HUB.snapshot("run_test_agent")["inbox"]
        self.assertTrue(inbox)
        self.assertEqual(inbox[-1]["agent"], "TestAgent")

    def test_l3_reports_skipped_instead_of_false_pass(self) -> None:
        report = TestAgent().execute(self._request("L3"))
        self.assertEqual(report.status, "SKIPPED")
        production = next(
            check for check in report.checks if check.kind == "production_artifact"
        )
        self.assertEqual(production.status, "SKIPPED")
        self.assertTrue(report.warnings)

    def test_report_can_be_read_back(self) -> None:
        agent = TestAgent()
        report = agent.execute(self._request("L1"))
        self.assertEqual(agent.report_for("run_test_agent"), report)
        self.assertEqual(agent.report_for(report.report_id), report)

    def test_metrics_and_persistence_are_observable(self) -> None:
        report = TestAgent().execute(self._request("L1"))
        metrics = TEST_METRICS.snapshot()
        self.assertEqual(metrics["totalRuns"], 1)
        self.assertEqual(metrics["checks"]["total"], 1)
        self.assertEqual(metrics["checkPassRate"], 1.0)
        self.assertIn("L1", metrics["byTier"])

        # 新实例只能靠 JSONL / RunStore 回源，证明报告可迭代复跑。
        reloaded = TestAgent()
        restored = reloaded.report_for("run_test_agent")
        self.assertIsNotNone(restored)
        self.assertEqual(restored.report_id, report.report_id)
        self.assertEqual(restored.tier, "L1")
        # 报告列表同样能从 JSONL 回源，供重启后的指标重建。
        latest = reloaded.latest(5)
        self.assertTrue(any(item.report_id == report.report_id for item in latest))

    # ---- Manifest ----------------------------------------------
    def test_manifest_is_valid_and_not_dispatchable(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "ai_core"
            / "testing"
            / "manifest.json"
        )
        manifest = read_agent_manifest(path)
        self.assertEqual(manifest.agent_id, "test_agent")
        self.assertEqual(
            manifest.task_types, ("test_plan", "test_execute", "test_report")
        )
        self.assertFalse(manifest.metadata.get("dispatchable", True))
        self.assertEqual(manifest.metadata.get("tiers"), ["L1", "L2", "L3"])


class FullstackTestAgentIntegrationTests(unittest.TestCase):
    class _Buffer:
        def __init__(self) -> None:
            self.events: list[dict] = []

        def emit(self, event_type, payload, dedupe_key=None):
            self.events.append({"type": event_type, "payload": payload})

    class _Contract:
        contract_id = "contract_fs"

        def model_dump(self, **kwargs):
            return {"contractId": self.contract_id}

    def _active(self):
        return SimpleNamespace(
            run_id="run_fs_test",
            request=SimpleNamespace(project_id="proj_fs"),
            contract=self._Contract(),
            preview_url="http://127.0.0.1:5173",
            warnings=[],
            test_report={},
            buffer=self._Buffer(),
        )

    def test_disabled_by_default_does_not_run(self) -> None:
        service = object.__new__(FullstackService)
        service._settings = SimpleNamespace(
            fullstack_test_agent_enabled=False,
            fullstack_test_agent_tier="L1",
            fullstack_test_agent_timeout_seconds=5,
        )
        active = self._active()
        service._run_optional_test_agent(active)
        self.assertEqual(active.test_report, {})
        self.assertEqual(active.buffer.events, [])

    def test_enabled_attaches_report_and_events_without_failing_run(self) -> None:
        report = TestReport(
            report_id="trep_fs",
            plan_id="tplan_fs",
            source_run_id="run_fs_test",
            tier="L1",
            status="FAILED",
            advisory="L1 仅供参考",
            summary={"total": 1, "passed": 0, "failed": 1, "error": 0, "skipped": 0},
        )
        fake_agent = SimpleNamespace(
            execute=lambda request: report,
        )
        service = object.__new__(FullstackService)
        service._settings = SimpleNamespace(
            fullstack_test_agent_enabled=True,
            fullstack_test_agent_tier="L1",
            fullstack_test_agent_timeout_seconds=5,
        )
        active = self._active()
        with patch("ai_core.testing.agent.TEST_AGENT", fake_agent):
            service._run_optional_test_agent(active)

        self.assertEqual(active.test_report["reportId"], "trep_fs")
        types = [event["type"] for event in active.buffer.events]
        self.assertEqual(types, ["fullstack.test.started", "fullstack.test.completed"])
        # 验收失败只追加 warning，不把生成结果改判失败。
        self.assertTrue(any("自动验收未通过" in item for item in active.warnings))


class TestingApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from ai_core.api.testing import router

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def test_plan_endpoint_is_side_effect_free(self) -> None:
        response = self.client.post(
            "/api/v1/internal/testing/plans",
            json={
                "tier": "L2",
                "previewUrl": "http://127.0.0.1:5173",
                "contract": CONTRACT,
                "sourceRunId": "run_api_plan",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["tier"], "L2")
        self.assertIn("advisory", body)
        self.assertTrue(body["checks"])

    def test_metrics_endpoint_exposes_aggregates(self) -> None:
        response = self.client.get("/api/v1/internal/testing/metrics")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("totalRuns", body)
        self.assertIn("checks", body)
        self.assertIn("checkPassRate", body)

    def test_missing_report_returns_structured_404(self) -> None:
        response = self.client.get("/api/v1/internal/testing/runs/not-found")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "TEST_REPORT_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
