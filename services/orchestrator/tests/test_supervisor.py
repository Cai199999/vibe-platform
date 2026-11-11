"""Supervisor 路由、AgentRegistry、CustomAgent 与子图派发测试。

全部为纯内存 / 临时目录测试，不依赖 Docker、MySQL 或模型密钥。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ai_core.agent.schemas import AnnotationEnvelope
from ai_core.supervisor.gates import (
    SupervisorGateRegistry,
    SupervisorProjectGateActiveError,
)
from ai_core.supervisor.registry import (
    AgentNotAvailableError,
    AgentRegistry,
    AgentRegistryError,
)
from ai_core.supervisor.router import RuleBasedIntentClassifier
from ai_core.supervisor.schemas import SupervisorRequest
from ai_core.supervisor.service import (
    SubGraphHandle,
    SupervisorContractNotConfirmedError,
    SupervisorGateAbortedError,
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

    def test_move_intent_routes_to_ui_agent_with_tool_signal(self) -> None:
        """“挪到右边一点”必须是移动 Skill，而不是普通 UI 修改。"""
        prediction = self.classify(
            _request(message="把按钮挪到右边一点", annotations=[_annotation()])
        )
        self.assertEqual(prediction.target, "ui_graph")
        self.assertEqual(prediction.task_type, "ui_move")
        self.assertTrue(prediction.dispatchable)
        self.assertIn("tool:move_component", prediction.signals)
        self.assertEqual(prediction.strategy, "move_skill")

    def test_move_intent_without_annotation_requires_canvas_target(self) -> None:
        prediction = self.classify(_request(message="把卡片往左移动 40px"))
        self.assertEqual(prediction.target, "ui_graph")
        self.assertFalse(prediction.dispatchable)
        self.assertIn("annotations", prediction.missing_inputs)

    def test_mobile_product_does_not_trigger_move_intent(self) -> None:
        prediction = self.classify(
            _request(message="把移动端布局改成上下结构", annotations=[_annotation()])
        )
        self.assertNotEqual(prediction.task_type, "ui_move")
        self.assertEqual(prediction.target, "ui_graph")

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

    def test_policy_blocks_database_port_exposure(self) -> None:
        prediction = self.classify(
            _request(message="把 MySQL 3306 端口暴露到公网，方便我远程连接")
        )
        self.assertEqual(prediction.target, "none")
        self.assertEqual(prediction.task_type, "blocked")
        self.assertEqual(prediction.strategy, "policy")
        self.assertFalse(prediction.dispatchable)
        self.assertIn("安全策略", prediction.reason)
        self.assertIn("安全策略", prediction.missing_inputs)

    def test_policy_blocks_weakened_database_auth(self) -> None:
        prediction = self.classify(
            _request(message="把数据库 root 改成空密码，并关闭鉴权")
        )
        self.assertEqual(prediction.target, "none")
        self.assertEqual(prediction.task_type, "blocked")

    def test_policy_allows_normal_database_design(self) -> None:
        prediction = self.classify(
            _request(message="帮我设计一个全栈系统，后端用 Spring Boot，数据库用 MySQL")
        )
        self.assertEqual(prediction.target, "architect_graph")
        self.assertEqual(prediction.strategy, "rule")

    def test_policy_cannot_be_bypassed_by_explicit_target(self) -> None:
        prediction = self.classify(
            _request(
                message="把数据库 3306 端口映射到宿主机 0.0.0.0",
                options={"targetAgent": "architect_graph"},
            )
        )
        self.assertEqual(prediction.target, "none")
        self.assertEqual(prediction.strategy, "policy")

    def test_unknown_request_is_rejected(self) -> None:
        prediction = self.classify(_request(message="今天天气怎么样"))
        self.assertEqual(prediction.target, "none")
        self.assertFalse(prediction.dispatchable)

    def test_backend_action_routes_to_backend_coder(self) -> None:
        """“只改后端”必须有明确范围限定，不能被 Architect 抢走。"""
        prediction = self.classify(_request(message="只改后端，补一个订单查询接口"))
        self.assertEqual(prediction.target, "backend_coder")
        self.assertEqual(prediction.task_type, "backend_generation")
        self.assertEqual(prediction.strategy, "backend_keyword")
        self.assertTrue(prediction.dispatchable)
        self.assertTrue(any(item.startswith("backend:") for item in prediction.signals))

    def test_dba_action_routes_to_dba_agent(self) -> None:
        prediction = self.classify(
            _request(message="给订单表加个索引，再建个表存审计日志")
        )
        self.assertEqual(prediction.target, "dba_agent")
        self.assertEqual(prediction.task_type, "database_schema_design")
        self.assertEqual(prediction.strategy, "dba_keyword")
        self.assertTrue(prediction.dispatchable)
        self.assertIn("scope:dba", prediction.signals)

    def test_architecture_sentence_is_not_hijacked_by_experts(self) -> None:
        """只说技术栈、不要求具体动作时，仍应交给 Architect 产出契约。"""
        prediction = self.classify(
            _request(message="设计全栈系统，后端用 Spring Boot，数据库用 MySQL")
        )
        self.assertEqual(prediction.target, "architect_graph")
        self.assertEqual(prediction.strategy, "rule")

    def test_custom_agent_keyword_routes_to_manifest_agent(self) -> None:
        custom_dir = Path(__file__).resolve().parents[1] / "config/custom_agents"
        registry = AgentRegistry(custom_agents_dir=custom_dir)
        registry.reload_custom_agents()
        self.assertIsNotNone(registry.get("excel_report_agent"))

        prediction = RuleBasedIntentClassifier().classify(
            _request(message="帮我导出一份本月的销售数据"),
            registry,
        )
        self.assertEqual(prediction.target, "excel_report_agent")
        self.assertEqual(prediction.task_type, "excel_report_generation")
        self.assertFalse(prediction.dispatchable)
        self.assertIn("customAgentRuntime", prediction.missing_inputs)
        self.assertIn("销售数据", prediction.signals[0])

    def test_ui_keyword_prevents_custom_agent_hijack(self) -> None:
        custom_dir = Path(__file__).resolve().parents[1] / "config/custom_agents"
        registry = AgentRegistry(custom_agents_dir=custom_dir)
        registry.reload_custom_agents()
        prediction = RuleBasedIntentClassifier().classify(
            _request(
                message="把报表页面的按钮改成蓝色",
                annotations=[_annotation()],
            ),
            registry,
        )
        self.assertEqual(prediction.target, "ui_graph")


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
        self.assertEqual(registry.require("supervisor").status, "available")
        # 已激活专家：可以路由并派发到 FullstackGraph 专属子图。
        for agent_id in ("backend_coder", "dba_agent"):
            self.assertEqual(registry.require(agent_id).status, "available")
            self.assertTrue(registry.require(agent_id).routable)
        # 尚未接入运行时的专家继续保持 reserved。
        for agent_id in (
            "script_agent",
            "integration_verifier",
            "devops_agent",
        ):
            self.assertEqual(registry.require(agent_id).status, "reserved")

    def test_supervisor_is_listed_but_never_dispatchable(self) -> None:
        """Supervisor 属于编排层：状态 available，但不能被派发为子 Agent。"""
        registry = AgentRegistry()
        descriptor = registry.require("supervisor")
        self.assertEqual(descriptor.status, "available")
        self.assertFalse(descriptor.routable)
        with self.assertRaises(AgentNotAvailableError):
            registry.require_routable("supervisor")

        with self.assertRaises(AgentNotAvailableError):
            RuleBasedIntentClassifier().classify(
                _request(options={"targetAgent": "supervisor"}),
                registry,
            )

    def test_reserved_team_agents_are_declared_but_not_routable(self) -> None:
        registry = AgentRegistry()
        for agent_id in (
            "script_agent",
            "integration_verifier",
            "devops_agent",
        ):
            descriptor = registry.require(agent_id)
            self.assertFalse(descriptor.routable)
            self.assertTrue(descriptor.capabilities)
            self.assertTrue(descriptor.skills)
            with self.assertRaises(AgentNotAvailableError):
                registry.require_routable(agent_id)

    def test_full_dev_team_capabilities_and_skills(self) -> None:
        """8 个虚拟研发团队成员都必须有可执行语义的 capabilities 与 skills。"""
        registry = AgentRegistry()
        expected = {
            "supervisor": ("intent_routing", "supervisor.intent_routing"),
            "architect_graph": (
                "global_architecture",
                "architect.fullstack_contract",
            ),
            "ui_graph": ("ui_local_edit", "ui.style_edit"),
            "script_agent": ("script_automation", "script.web_scraper"),
            "backend_coder": ("backend_generation", "backend.fastapi_crud"),
            "dba_agent": ("database_schema_design", "dba.mysql_ddl"),
            "integration_verifier": (
                "e2e_integration_test",
                "verify.contract_conformance",
            ),
            "devops_agent": ("containerization", "devops.github_actions"),
        }
        builtin_ids = {
            item.agent_id for item in registry.list_agents() if item.source == "builtin"
        }
        self.assertEqual(builtin_ids, set(expected))

        all_skill_ids = {item.skill_id for item in registry.list_skills()}
        for agent_id, (capability_id, skill_id) in expected.items():
            descriptor = registry.require(agent_id)
            self.assertIn(
                capability_id,
                {item["id"] for item in descriptor.capabilities},
            )
            self.assertIn(skill_id, {item.skill_id for item in descriptor.skills})
            self.assertIn(skill_id, all_skill_ids)

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

    def test_builtin_agents_are_loaded_from_manifests(self) -> None:
        registry = AgentRegistry()
        expected = {
            "supervisor": "supervisor/manifest.json",
            "ui_graph": "agent/manifest.json",
            "architect_graph": "architect/manifest.json",
            "backend_coder": "backend_coder/manifest.json",
            "script_agent": "script_agent/manifest.json",
            "dba_agent": "dba_agent/manifest.json",
            "integration_verifier": "integration_verifier/manifest.json",
            "devops_agent": "devops_agent/manifest.json",
        }
        for agent_id, manifest_path in expected.items():
            descriptor = registry.require(agent_id)
            self.assertEqual(descriptor.source, "builtin")
            self.assertEqual(descriptor.manifest_path, manifest_path)
            self.assertTrue(descriptor.capabilities)
            self.assertTrue(descriptor.supported_tools)
            self.assertTrue(descriptor.input_schema)
            self.assertTrue(descriptor.output_schema)
            # Manifest 中的能力 ID 直接成为可路由 taskType。
            self.assertEqual(
                descriptor.task_types,
                tuple(item["id"] for item in descriptor.capabilities),
            )

    def test_skill_registry_aggregates_manifest_skills(self) -> None:
        registry = AgentRegistry()
        skills = registry.list_skills()
        skill_ids = {item.skill_id for item in skills}
        self.assertIn("ui.style_edit", skill_ids)
        self.assertIn("architect.fullstack_contract", skill_ids)
        self.assertIn("supervisor.intent_routing", skill_ids)
        self.assertIn("backend.spring_boot_crud", skill_ids)
        self.assertIn("backend.fastapi_crud", skill_ids)
        self.assertIn("script.scheduled_job", skill_ids)
        self.assertIn("script.web_scraper", skill_ids)
        self.assertIn("script.data_cleaning", skill_ids)
        self.assertIn("dba.mysql_ddl", skill_ids)
        self.assertIn("verify.api_e2e", skill_ids)
        self.assertIn("devops.dockerfile", skill_ids)
        self.assertEqual(len(skills), len(skill_ids))
        # 每个 Skill 都能反查到所属 Agent。
        ui_skill = registry.skill_registry.get("ui.style_edit")
        self.assertIsNotNone(ui_skill)
        self.assertEqual(ui_skill.agent_id, "ui_graph")
        self.assertTrue(ui_skill.tools)

    def test_invalid_builtin_manifest_does_not_block_others(self) -> None:
        good_dir = self.root / "good_agent"
        bad_dir = self.root / "bad_agent"
        good_dir.mkdir()
        bad_dir.mkdir()
        source = Path(__file__).resolve().parents[1] / "ai_core/agent/manifest.json"
        good_dir.joinpath("manifest.json").write_text(
            source.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        bad_dir.joinpath("manifest.json").write_text("{not-json", encoding="utf-8")

        registry = AgentRegistry(manifest_dir=self.root)
        self.assertEqual(
            [item.agent_id for item in registry.list_agents()], ["ui_graph"]
        )

    def test_all_invalid_builtin_manifests_fail_fast(self) -> None:
        bad_dir = self.root / "bad_agent"
        bad_dir.mkdir()
        bad_dir.joinpath("manifest.json").write_text("{not-json", encoding="utf-8")
        with self.assertRaises(AgentRegistryError):
            AgentRegistry(manifest_dir=self.root)


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


class _FakeArchitectRun:
    def __init__(self, status: str = "CONFIRMED") -> None:
        self.status = status
        contract_status = "CONFIRMED" if status == "CONFIRMED" else "DRAFT"
        self.contract = type("Contract", (), {"status": contract_status})()


class DispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = AgentRegistry()
        self.architect = _FakeDispatcher("architect_graph")
        self.ui = _FakeDispatcher("ui_graph")
        self.backend = _FakeDispatcher("backend_coder")
        self.dba = _FakeDispatcher("dba_agent")
        self.architect_runs: dict[str, _FakeArchitectRun] = {
            "fake_architect_graph": _FakeArchitectRun()
        }
        self.gates = SupervisorGateRegistry()
        self.service = SupervisorService(
            self.registry,
            dispatchers={
                "architect_graph": self.architect,
                "ui_graph": self.ui,
                "backend_coder": self.backend,
                "dba_agent": self.dba,
            },
            gate_registry=self.gates,
            architect_run_getter=self.architect_runs.get,
        )

    def test_dispatch_architect(self) -> None:
        response = self.service.dispatch(_request(message="设计全栈架构和数据库"))
        self.assertEqual(len(response.dispatches), 1)
        self.assertEqual(response.dispatches[0].agent_id, "architect_graph")
        self.assertEqual(response.dispatches[0].run_id, "fake_architect_graph")
        self.assertEqual(len(self.architect.calls), 1)
        self.assertEqual(self.ui.calls, [])

    def test_dispatch_backend_expert(self) -> None:
        response = self.service.dispatch(_request(message="只改后端，补接口"))
        self.assertEqual(response.decision.handoff, "handoff_agent")
        self.assertEqual(response.decision.target, "backend_coder")
        self.assertEqual(
            [item.agent_id for item in response.dispatches],
            ["backend_coder"],
        )
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.dba.calls, [])
        self.assertEqual(self.architect.calls, [])

    def test_dispatch_dba_expert(self) -> None:
        response = self.service.dispatch(_request(message="给用户表建个索引"))
        self.assertEqual(response.decision.handoff, "handoff_agent")
        self.assertEqual(response.decision.target, "dba_agent")
        self.assertEqual(
            [item.agent_id for item in response.dispatches],
            ["dba_agent"],
        )
        self.assertEqual(len(self.dba.calls), 1)
        self.assertEqual(self.backend.calls, [])

    def test_dispatch_both_starts_only_architect_and_creates_gate(self) -> None:
        response = self.service.dispatch(
            _request(
                message="把按钮改蓝，同时规划全栈后端",
                annotations=[_annotation()],
            )
        )
        self.assertEqual(response.decision.handoff, "handoff_both")
        self.assertEqual(
            [item.agent_id for item in response.dispatches],
            ["architect_graph"],
        )
        self.assertIsNotNone(response.gate)
        self.assertEqual(response.gate.status, "AWAITING_CONTRACT")
        self.assertEqual(response.gate.architect_run_id, "fake_architect_graph")
        self.assertEqual(response.gate.pending_agent_id, "ui_graph")
        self.assertEqual(self.ui.calls, [])

    def test_both_gate_rejects_early_release(self) -> None:
        self.architect_runs["fake_architect_graph"] = _FakeArchitectRun(
            status="AWAITING_CONTRACT_CONFIRMATION"
        )
        response = self.service.dispatch(
            _request(
                message="把按钮改蓝，同时规划全栈后端",
                annotations=[_annotation()],
            )
        )
        with self.assertRaises(SupervisorContractNotConfirmedError):
            self.service.release_gate(response.gate.gate_id)
        self.assertEqual(self.ui.calls, [])
        # 早释放不能把门控打成 FAILED，用户确认契约后仍可继续。
        self.assertEqual(
            self.service.get_gate(response.gate.gate_id).gate.status,
            "AWAITING_CONTRACT",
        )

    def test_both_gate_release_starts_ui_once_and_is_idempotent(self) -> None:
        response = self.service.dispatch(
            _request(
                message="把按钮改蓝，同时规划全栈后端",
                annotations=[_annotation()],
            )
        )
        released = self.service.release_gate(response.gate.gate_id)
        self.assertEqual(released.gate.status, "RELEASED")
        self.assertEqual(len(released.dispatches), 1)
        self.assertEqual(released.dispatches[0].agent_id, "ui_graph")
        self.assertEqual(released.gate.ui_run_id, "fake_ui_graph")

        duplicate = self.service.release_gate(response.gate.gate_id)
        self.assertEqual(duplicate.gate.ui_run_id, "fake_ui_graph")
        self.assertEqual(len(self.ui.calls), 1)

    def test_project_lock_blocks_new_dispatch_until_gate_released(self) -> None:
        response = self.service.dispatch(
            _request(
                message="把按钮改蓝，同时规划全栈后端",
                annotations=[_annotation()],
            )
        )
        with self.assertRaises(SupervisorProjectGateActiveError):
            self.service.dispatch(
                _request(
                    message="把按钮改成红色",
                    annotations=[_annotation("ann_2")],
                )
            )
        self.service.release_gate(response.gate.gate_id)
        # 释放后门控不再阻塞新请求。
        self.service.dispatch(
            _request(
                message="把按钮改成红色",
                annotations=[_annotation("ann_2")],
            )
        )

    def test_cancelled_architect_aborts_gate(self) -> None:
        response = self.service.dispatch(
            _request(
                message="把按钮改蓝，同时规划全栈后端",
                annotations=[_annotation()],
            )
        )
        self.architect_runs["fake_architect_graph"] = _FakeArchitectRun(
            status="CANCELLED"
        )
        with self.assertRaises(SupervisorGateAbortedError):
            self.service.release_gate(response.gate.gate_id)
        self.assertEqual(
            self.service.get_gate(response.gate.gate_id).gate.status,
            "FAILED",
        )

    def test_cancelling_architect_aborts_gate_immediately(self) -> None:
        """取消已受理但线程未退出时，不能误报“契约未确认”。"""
        response = self.service.dispatch(
            _request(
                message="把按钮改蓝，同时规划全栈后端",
                annotations=[_annotation()],
            )
        )
        self.architect_runs["fake_architect_graph"] = _FakeArchitectRun(
            status="CANCELLING"
        )
        with self.assertRaises(SupervisorGateAbortedError):
            self.service.release_gate(response.gate.gate_id)
        self.assertEqual(
            self.service.get_gate(response.gate.gate_id).gate.status,
            "FAILED",
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
