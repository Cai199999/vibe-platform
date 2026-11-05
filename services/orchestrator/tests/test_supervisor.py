"""Supervisor 路由、AgentRegistry、CustomAgent 与子图派发测试。

全部为纯内存 / 临时目录测试，不依赖 Docker、MySQL 或模型密钥。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ai_core.agent.schemas import AnnotationEnvelope
from ai_core.supervisor.registry import (
    AgentNotAvailableError,
    AgentRegistry,
)
from ai_core.supervisor.router import RuleBasedIntentClassifier
from ai_core.supervisor.schemas import SupervisorRequest
from ai_core.supervisor.service import (
    SubGraphHandle,
    SupervisorService,
    SupervisorUiContextRequiredError,
)


def _annotation(annotation_id: str = "ann_1") -> AnnotationEnvelope:
    return AnnotationEnvelope(
        annotation_id=annotation_id,
        target={"mode": "source", "source": {"file": "src/App.jsx"}},
    )


def _request(**overrides) -> SupervisorRequest:
    payload = {
        "projectId": "project_supervisor",
        "projectName": "Supervisor 测试",
        "message": "",
        "annotations": [],
        "options": {},
    }
    payload.update(overrides)
    return SupervisorRequest.model_validate(payload)


class RuleRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = AgentRegistry()
        self.classifier = RuleBasedIntentClassifier()

    def classify(self, request: SupervisorRequest):
        return self.classifier.classify(request, self.registry)

    def test_ui_with_annotations(self) -> None:
        prediction = self.classify(
            _request(message="把按钮颜色改成蓝色", annotations=[_annotation()])
        )
        self.assertEqual(prediction.target, "ui_graph")
        self.assertEqual(prediction.task_type, "ui_local_edit")
        self.assertTrue(prediction.dispatchable)
        self.assertAlmostEqual(prediction.confidence, 0.92)

    def test_ui_without_annotations_is_not_dispatchable(self) -> None:
        prediction = self.classify(_request(message="把按钮颜色改成蓝色"))
        self.assertEqual(prediction.target, "ui_graph")
        self.assertFalse(prediction.dispatchable)
        self.assertIn("annotations", prediction.missing_inputs)

    def test_architect_by_keywords(self) -> None:
        prediction = self.classify(
            _request(message="帮我设计全栈系统，后端用 Spring Boot，数据库用 MySQL")
        )
        self.assertEqual(prediction.target, "architect_graph")
        self.assertEqual(prediction.task_type, "global_architecture")
        self.assertGreaterEqual(prediction.confidence, 0.9)

    def test_both_by_ui_and_arch_signals(self) -> None:
        prediction = self.classify(
            _request(
                message="把按钮改蓝，同时规划全栈后端和数据库",
                annotations=[_annotation()],
            )
        )
        self.assertEqual(prediction.target, "both")
        self.assertEqual(prediction.task_type, "combined")
        self.assertTrue(prediction.dispatchable)

    def test_explicit_scope_overrides_keywords(self) -> None:
        prediction = self.classify(
            _request(
                message="全栈后端和数据库",
                annotations=[_annotation()],
                options={"scopeHint": "ui"},
            )
        )
        self.assertEqual(prediction.target, "ui_graph")
        self.assertEqual(prediction.strategy, "explicit_scope")

    def test_explicit_target_has_highest_priority(self) -> None:
        prediction = self.classify(
            _request(
                message="把按钮改蓝",
                annotations=[_annotation()],
                options={"targetAgent": "architect_graph"},
            )
        )
        self.assertEqual(prediction.target, "architect_graph")
        self.assertAlmostEqual(prediction.confidence, 0.99)

    def test_unknown_request_is_rejected(self) -> None:
        prediction = self.classify(_request(message="今天天气怎么样"))
        self.assertEqual(prediction.target, "none")
        self.assertFalse(prediction.dispatchable)

    def test_reserved_agent_cannot_be_routed(self) -> None:
        with self.assertRaises(AgentNotAvailableError):
            self.classify(_request(options={"targetAgent": "backend_coder"}))


class RegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write_manifest(self, name: str, payload: dict) -> None:
        (self.root / name).write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_builtins_and_reserved_agents_exist(self) -> None:
        registry = AgentRegistry()
        self.assertEqual(registry.require("ui_graph").status, "available")
        self.assertEqual(
            registry.require("architect_graph").status,
            "available",
        )
        self.assertEqual(registry.require("backend_coder").status, "reserved")
        self.assertEqual(registry.require("script_agent").status, "reserved")

    def test_custom_manifest_is_loaded_but_not_executable(self) -> None:
        self._write_manifest(
            "billing.json",
            {
                "agentId": "custom_billing",
                "displayName": "企业计费专家",
                "taskTypes": ["enterprise_billing"],
                "keywords": ["计费", "账单"],
                "metadata": {"tenantId": "tenant_demo"},
            },
        )
        registry = AgentRegistry(custom_agents_dir=self.root)
        loaded = registry.reload_custom_agents()
        self.assertEqual(len(loaded), 1)
        descriptor = registry.require("custom_billing")
        self.assertEqual(descriptor.status, "custom")
        self.assertEqual(descriptor.source, "manifest")
        self.assertEqual(descriptor.keywords, ("计费", "账单"))

        prediction = RuleBasedIntentClassifier().classify(
            _request(options={"targetAgent": "custom_billing"}),
            registry,
        )
        self.assertEqual(prediction.target, "custom_billing")
        self.assertFalse(prediction.dispatchable)
        self.assertIn("customAgentRuntime", prediction.missing_inputs)

    def test_custom_manifest_cannot_escape_directory(self) -> None:
        self._write_manifest(
            "escape.json",
            {
                "agentId": "custom_escape",
                "displayName": "路径穿越",
                "taskTypes": ["escape"],
                "systemPromptFile": "../secret.md",
            },
        )
        registry = AgentRegistry(custom_agents_dir=self.root)
        self.assertEqual(registry.reload_custom_agents(), [])
        self.assertIsNone(registry.get("custom_escape"))

    def test_custom_manifest_cannot_override_builtin(self) -> None:
        self._write_manifest(
            "hijack.json",
            {
                "agentId": "ui_graph",
                "displayName": "劫持 UI",
                "taskTypes": ["ui_local_edit"],
            },
        )
        registry = AgentRegistry(custom_agents_dir=self.root)
        self.assertEqual(registry.reload_custom_agents(), [])
        self.assertEqual(registry.require("ui_graph").display_name, "局部 UI 工程师")


class _FakeDispatcher:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.calls: list[tuple[str, str]] = []

    def start(self, request, descriptor) -> SubGraphHandle:
        self.calls.append((request.project_id, descriptor.agent_id))
        return SubGraphHandle(
            agent_id=descriptor.agent_id,
            run_id=f"fake_{descriptor.agent_id}",
            stream_path=f"/fake/{descriptor.agent_id}/events",
        )


class DispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = AgentRegistry()
        self.architect = _FakeDispatcher("architect_graph")
        self.ui = _FakeDispatcher("ui_graph")
        self.service = SupervisorService(
            self.registry,
            dispatchers={
                "architect_graph": self.architect,
                "ui_graph": self.ui,
            },
        )

    def test_dispatch_architect(self) -> None:
        response = self.service.dispatch(_request(message="设计全栈架构和数据库"))
        self.assertEqual(len(response.dispatches), 1)
        self.assertEqual(response.dispatches[0].agent_id, "architect_graph")
        self.assertEqual(response.dispatches[0].run_id, "fake_architect_graph")
        self.assertEqual(len(self.architect.calls), 1)
        self.assertEqual(self.ui.calls, [])

    def test_dispatch_both_has_deterministic_order(self) -> None:
        response = self.service.dispatch(
            _request(
                message="把按钮改蓝，同时规划全栈后端",
                annotations=[_annotation()],
            )
        )
        self.assertEqual(
            [item.agent_id for item in response.dispatches],
            ["architect_graph", "ui_graph"],
        )
        self.assertEqual(
            [item.order for item in response.dispatches],
            [1, 2],
        )

    def test_dispatch_ui_without_annotation_is_rejected(self) -> None:
        with self.assertRaises(SupervisorUiContextRequiredError):
            self.service.dispatch(_request(message="把按钮颜色改成蓝色"))
        self.assertEqual(self.ui.calls, [])

    def test_dispatch_custom_is_rejected_before_runtime(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        (root / "billing.json").write_text(
            json.dumps(
                {
                    "agentId": "custom_billing",
                    "displayName": "企业计费专家",
                    "taskTypes": ["enterprise_billing"],
                }
            ),
            encoding="utf-8",
        )
        registry = AgentRegistry(custom_agents_dir=root)
        registry.reload_custom_agents()
        service = SupervisorService(
            registry,
            dispatchers={"architect_graph": self.architect},
        )
        with self.assertRaises(Exception) as ctx:
            service.dispatch(_request(options={"targetAgent": "custom_billing"}))
        self.assertIn("不可派发", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
