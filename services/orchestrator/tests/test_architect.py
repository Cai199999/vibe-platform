"""M2.5 Architect：应用形态、动态槽位、分级契约与决策状态机测试。

运行：

    cd services/orchestrator
    PYTHONPATH=. python -m unittest tests.test_architect -v

全部使用 ``MockArchitectLLM``，不依赖 DeepSeek Key、MySQL 或 Docker。
"""

from __future__ import annotations

import unittest
import uuid
from typing import Any

from ai_core.agent.checkpoint import MemoryCheckpointerProvider
from ai_core.agent.events import RunEventBuffer
from ai_core.architect.confidence import (
    APP_SHAPE_PROMPT,
    apply_confidence_guard,
)
from ai_core.architect.contract import validate_draft
from ai_core.architect.graph import ArchitectGraphRunner
from ai_core.architect.llm import (
    ArchitectContext,
    ArchitectLLMError,
    ContractDraft,
    DeepSeekArchitectLLM,
    MockArchitectLLM,
)
from ai_core.architect.schemas import (
    ArchitectAnalysis,
    ArchitectApiRoute,
    ArchitectImplementationPhase,
)
from ai_core.config import Settings

SHAPES = ("lightweight_web", "script_automation", "full_stack_system")


def _initial_state(run_id: str, *, max_rounds: int = 5) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "project_id": f"project_{run_id}",
        "project_name": "M2.5 测试项目",
        "source_files": {
            "src/App.jsx": (
                "export default function App() { return <main>Todo</main> }"
            )
        },
        "source_hash": "source_hash_for_test",
        "source_origin": "request",
        "source_warnings": [],
        "ui_history": [{"role": "user", "content": "先做一个可用的页面"}],
        "threshold": 0.85,
        "max_rounds": max_rounds,
        "round": 0,
        "status": "INGESTING",
        "app_shape": "",
        "slots": {},
        "confidence_score": 0.0,
        "missing_slots": [],
        "questions": [],
        "analysis": {},
        "retrieved_chunks": [],
        "clarification": {},
        "contract": {},
        "contract_draft": {},
        "contract_issues": [],
        "contract_id": "",
        "contract_decision": "",
        "contract_feedback": "",
        "repair_count": 0,
        "repair_limit": 1,
        "contract_revision_count": 0,
        "asked_slots": [],
        "messages": [],
        "rag_provider": "markdown",
        "rag_top_k": 3,
        "error": "",
        "error_code": "",
    }


def _answer_for(question: dict[str, Any], shape: str) -> dict[str, Any]:
    slot = str(question.get("slot") or "")
    if slot == "app.shape":
        value: Any = shape
    elif question.get("options"):
        value = question["options"][0]["value"]
    else:
        value = {
            "ui.primary_flow": "输入待办 -> 勾选完成 -> 保存到 localStorage",
            "script.input": "一个 CSV 文件路径",
            "script.output": "按日期汇总的 JSON 文件",
            "script.logic": "解析 CSV、去重并按日期聚合，异常返回非零退出码",
            "business.key_flows": "创建订单并扣减库存",
            "data.entities": "Order(id, userId, total, status) 与 User(id, name)",
        }.get(slot, f"{slot} 的测试取值")
    return {"questionId": question["questionId"], "value": value}


class ArchitectHarness:
    def __init__(self, *, max_rounds: int = 5) -> None:
        self.run_id = f"test_{uuid.uuid4().hex[:12]}"
        self.buffer = RunEventBuffer(self.run_id)
        self.runner = ArchitectGraphRunner(
            MockArchitectLLM(),
            event_sink=self.buffer,
            checkpointer_provider=MemoryCheckpointerProvider(),
        )
        self.state = _initial_state(self.run_id, max_rounds=max_rounds)
        self.outcome = self.runner.run(self.state)

    def answer(self, payload: dict[str, Any], shape: str) -> dict[str, Any]:
        questions = payload.get("questions") or []
        answers = [_answer_for(question, shape) for question in questions]
        self.outcome = self.runner.run(
            {},
            run_id=self.run_id,
            resume={"answers": answers},
        )
        return self.outcome.interrupt or {}

    def drive_to_contract(self, shape: str) -> dict[str, Any]:
        payload = self.outcome.interrupt or {}
        for _ in range(8):
            if payload.get("kind") == "contract_confirmation":
                return payload
            self.assert_shape_question_or_slots(payload, shape)
            payload = self.answer(payload, shape)
        raise AssertionError("未能在预期轮次内生成契约")

    def assert_shape_question_or_slots(
        self,
        payload: dict[str, Any],
        shape: str,
    ) -> None:
        questions = payload.get("questions") or []
        if not questions:
            raise AssertionError(f"澄清 payload 缺少 questions: {payload}")
        if not payload.get("appShape"):
            if questions[0].get("slot") != "app.shape":
                raise AssertionError("第一轮必须先询问 app.shape")
            if questions[0].get("prompt") != APP_SHAPE_PROMPT:
                raise AssertionError("应用形态问题文案不符合约定")

    def decide(self, decision: str, comment: str = "") -> None:
        self.outcome = self.runner.run(
            {},
            run_id=self.run_id,
            resume={"decision": decision, "comment": comment},
        )


class ArchitectFlowTest(unittest.TestCase):
    def test_first_round_always_asks_app_shape(self) -> None:
        harness = ArchitectHarness()
        self.assertTrue(harness.outcome.interrupted)
        payload = harness.outcome.interrupt or {}
        self.assertEqual(payload.get("reason"), "app_shape_required")
        question = payload["questions"][0]
        self.assertEqual(question["slot"], "app.shape")
        self.assertEqual(question["prompt"], APP_SHAPE_PROMPT)
        self.assertEqual(
            {item["value"] for item in question["options"]},
            set(SHAPES),
        )
        self.assertLessEqual(payload["confidenceScore"], 0.42)
        event_types = [event.type for event in harness.buffer.snapshot()]
        self.assertIn("architect.rag.retrieved", event_types)
        self.assertIn("architect.clarification.required", event_types)

    def test_lightweight_never_requires_backend_or_database(self) -> None:
        harness = ArchitectHarness()
        payload = harness.drive_to_contract("lightweight_web")
        contract = payload["contract"]
        self.assertEqual(contract["appShape"], "lightweight_web")
        self.assertEqual(contract["apiRoutes"], [])
        self.assertEqual(contract["databaseDdl"], "")
        self.assertTrue(
            contract["uiLogic"]
            or contract["frontendDataModel"]
            or contract["componentInteractions"]
        )
        self.assertEqual(contract["scriptSpec"], {})
        self.assertIsNone(contract["techStack"].get("backend"))
        self.assertIsNone(contract["techStack"].get("database"))
        self.assertGreaterEqual(contract["confidenceScore"], 0.85)
        self.assertTrue(contract["citations"])

        harness.decide("CONFIRM")
        self.assertEqual(harness.outcome.state["status"], "CONFIRMED")
        self.assertFalse(harness.outcome.interrupted)
        event_types = [event.type for event in harness.buffer.snapshot()]
        self.assertIn("architect.contract.confirmed", event_types)

    def test_script_never_requires_frontend(self) -> None:
        harness = ArchitectHarness()
        payload = harness.drive_to_contract("script_automation")
        contract = payload["contract"]
        self.assertEqual(contract["appShape"], "script_automation")
        self.assertEqual(contract["apiRoutes"], [])
        self.assertEqual(contract["databaseDdl"], "")
        self.assertEqual(contract["uiLogic"], [])
        self.assertEqual(contract["componentInteractions"], [])
        self.assertEqual(contract["frontendDataModel"], [])
        spec = contract["scriptSpec"]
        for key in ("runtime", "entry", "dependencies", "input", "output", "exitCodes"):
            self.assertIn(key, spec)
        self.assertNotIn("backend", contract["techStack"])
        self.assertNotIn("database", contract["techStack"])

        harness.decide("CANCEL", "暂时不生成脚本")
        self.assertEqual(harness.outcome.state["status"], "CANCELLED")

    def test_full_stack_enforces_api_and_ddl(self) -> None:
        harness = ArchitectHarness()
        payload = harness.drive_to_contract("full_stack_system")
        contract = payload["contract"]
        self.assertEqual(contract["appShape"], "full_stack_system")
        self.assertTrue(contract["apiRoutes"])
        self.assertTrue(
            all(route["path"].startswith("/api/") for route in contract["apiRoutes"])
        )
        self.assertIn("CREATE TABLE", contract["databaseDdl"])
        self.assertNotIn("FOREIGN KEY", contract["databaseDdl"].upper())
        self.assertTrue(contract["componentInteractions"])
        self.assertTrue(contract["techStack"].get("backend"))
        self.assertTrue(contract["techStack"].get("database"))

        harness.decide("CONFIRM")
        self.assertEqual(harness.outcome.state["status"], "CONFIRMED")

    def test_revise_returns_new_contract_then_confirm(self) -> None:
        harness = ArchitectHarness()
        first = harness.drive_to_contract("lightweight_web")
        first_id = first["contract"]["contractId"]

        harness.decide("REVISE", "增加导出 JSON 的入口")
        revised = harness.outcome.interrupt or {}
        self.assertEqual(revised.get("kind"), "contract_confirmation")
        contract = revised["contract"]
        self.assertEqual(contract["contractId"], first_id)
        self.assertIn("修订记录", contract["contractMarkdown"])
        self.assertIn("增加导出 JSON 的入口", contract["contractMarkdown"])
        event_types = [event.type for event in harness.buffer.snapshot()]
        self.assertIn("architect.contract.revision_requested", event_types)

        harness.decide("CONFIRM")
        self.assertEqual(harness.outcome.state["status"], "CONFIRMED")

    def test_round_limit_exhausts_confidence(self) -> None:
        harness = ArchitectHarness(max_rounds=1)
        first = harness.outcome.interrupt or {}
        self.assertEqual(first.get("reason"), "app_shape_required")

        harness.answer(first, "full_stack_system")
        self.assertEqual(harness.outcome.state["status"], "FAILED")
        self.assertEqual(
            (
                harness.outcome.state["errorCode"]
                if "errorCode" in harness.outcome.state
                else harness.outcome.state["error_code"]
            ),
            "CONFIDENCE_EXHAUSTED",
        )
        event_types = [event.type for event in harness.buffer.snapshot()]
        self.assertIn("architect.failed", event_types)


class ArchitectJsonRepairTest(unittest.TestCase):
    """business_flows 宽容解析 + 校验失败回传重试。"""

    class _SequenceClient:
        def __init__(self, outputs: list[str]) -> None:
            self.outputs = list(outputs)
            self.calls: list[tuple[str, str, str]] = []

        def generate(self, task: str, system: str, human: str) -> str:
            self.calls.append((task, system, human))
            return self.outputs.pop(0)

        def generate_json(
            self,
            task: str,
            system: str,
            human: str,
            *,
            json_schema: dict[str, Any] | None = None,
        ) -> str:
            self.calls.append((task, system, human))
            return self.outputs.pop(0)

    @staticmethod
    def _context() -> ArchitectContext:
        return ArchitectContext(
            project_id="p1",
            project_name="测试项目",
            source_files={},
            ui_history=[],
            slots={},
            app_shape=None,
            round=0,
            threshold=0.85,
        )

    def test_business_flows_accepts_string_and_mixed_list(self) -> None:
        analysis = ArchitectAnalysis.model_validate(
            {
                "business_flows": ["用户登录", {"step": "创建订单", "actor": "用户"}],
                "entities": ["订单", {"name": "用户"}],
            }
        )
        self.assertEqual(analysis.business_flows[0], {"step": "用户登录"})
        self.assertEqual(analysis.business_flows[1]["step"], "创建订单")
        self.assertEqual(analysis.entities[0], {"name": "订单"})

        bare = ArchitectAnalysis.model_validate({"business_flows": "下单 -> 支付"})
        self.assertEqual(bare.business_flows, [{"step": "下单 -> 支付"}])

    def test_validation_error_is_fed_back_and_retried(self) -> None:
        client = self._SequenceClient(
            [
                # reasoning 必须是字符串；对象会在 Pydantic 校验时报错。
                '{"reasoning": {"bad": true}}',
                '{"business_flows": ["登录", "下单"], "confidence_score": 0.4}',
            ]
        )
        llm = DeepSeekArchitectLLM(
            Settings(deepseek_api_key="test-key", architect_json_retry_limit=2),
            client=client,
        )
        analysis = llm.analyze(self._context())
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(analysis.confidence_score, 0.4)
        self.assertEqual(analysis.business_flows[0], {"step": "登录"})
        # 第二次请求必须携带上一轮校验错误，模型才知道要修什么。
        self.assertIn("校验错误", client.calls[1][2])

    def test_retry_limit_exhausted_raises_readable_error(self) -> None:
        client = self._SequenceClient(
            ['{"reasoning": {"bad": true}}'] * 3,
        )
        llm = DeepSeekArchitectLLM(
            Settings(deepseek_api_key="test-key", architect_json_retry_limit=2),
            client=client,
        )
        with self.assertRaises(ArchitectLLMError) as captured:
            llm.analyze(self._context())
        self.assertEqual(len(client.calls), 3)
        self.assertIn("连续 3 次不合法", str(captured.exception))


class ConfidenceGuardTest(unittest.TestCase):
    def test_missing_shape_caps_confidence(self) -> None:
        guarded, result = apply_confidence_guard(
            ArchitectAnalysis(app_shape=None, confidence_score=0.99),
            {},
            0.85,
        )
        self.assertEqual(result.missing_slots, ["app.shape"])
        self.assertLessEqual(guarded.confidence_score, 0.42)
        self.assertEqual(guarded.questions[0].slot, "app.shape")

    def test_lightweight_only_needs_ui_flow(self) -> None:
        guarded, result = apply_confidence_guard(
            ArchitectAnalysis(
                app_shape="lightweight_web",
                confidence_score=0.99,
                slots={},
            ),
            {},
            0.85,
        )
        self.assertEqual(result.missing_slots, ["ui.primary_flow"])
        self.assertEqual(guarded.confidence_score, 0.72)
        self.assertFalse(
            any(
                slot.startswith(("backend.", "database."))
                for slot in result.missing_slots
            )
        )

    def test_script_does_not_require_ui(self) -> None:
        _, result = apply_confidence_guard(
            ArchitectAnalysis(
                app_shape="script_automation",
                confidence_score=0.99,
                slots={},
            ),
            {},
            0.85,
        )
        self.assertEqual(
            result.missing_slots,
            ["script.runtime", "script.input", "script.output", "script.logic"],
        )
        self.assertFalse(any("ui." in slot for slot in result.missing_slots))

    def test_full_stack_requires_backend_and_database(self) -> None:
        guarded, result = apply_confidence_guard(
            ArchitectAnalysis(
                app_shape="full_stack_system",
                confidence_score=0.99,
                slots={},
            ),
            {},
            0.85,
        )
        self.assertEqual(
            result.missing_slots,
            [
                "backend.framework",
                "database.engine",
                "business.key_flows",
                "data.entities",
            ],
        )
        self.assertLess(guarded.confidence_score, 0.85)
        self.assertEqual(guarded.confidence_score, 0.60)

    def test_assumptions_cap_complete_analysis(self) -> None:
        guarded, _ = apply_confidence_guard(
            ArchitectAnalysis(
                app_shape="lightweight_web",
                confidence_score=0.99,
                slots={"ui.primary_flow": "输入 -> 勾选 -> 保存"},
                assumptions=["假设浏览器支持 localStorage"],
            ),
            {},
            0.85,
        )
        self.assertEqual(guarded.confidence_score, 0.84)
        self.assertTrue(guarded.questions)
        self.assertLess(guarded.confidence_score, 0.85)


class ContractTierValidationTest(unittest.TestCase):
    def _context(self, shape: str) -> ArchitectContext:
        return ArchitectContext(
            project_id="p1",
            project_name="校验项目",
            source_files={"src/App.jsx": "export default () => null"},
            ui_history=[],
            slots={},
            app_shape=shape,
            round=1,
            threshold=0.85,
        )

    def test_mock_contracts_are_valid_for_all_shapes(self) -> None:
        llm = MockArchitectLLM()
        for shape in SHAPES:
            draft = llm.draft_contract(self._context(shape))
            validation = validate_draft(shape, draft)
            self.assertTrue(validation.valid, f"{shape}: {validation.issues}")

    def test_lightweight_rejects_api_and_ddl(self) -> None:
        llm = MockArchitectLLM()
        draft = llm.draft_contract(self._context("lightweight_web"))
        polluted = ContractDraft(
            contract_markdown=draft.contract_markdown,
            tech_stack=draft.tech_stack,
            api_routes=[ArchitectApiRoute(method="GET", path="/api/v1/items")],
            database_ddl="CREATE TABLE items (id INT);",
            component_interactions=draft.component_interactions,
            ui_logic=draft.ui_logic,
            frontend_data_model=draft.frontend_data_model,
            implementation_path=draft.implementation_path,
        )
        validation = validate_draft("lightweight_web", polluted)
        self.assertFalse(validation.valid)
        self.assertTrue(any("REST API" in issue for issue in validation.issues))
        self.assertTrue(any("DDL" in issue for issue in validation.issues))
        # 装配前应裁剪，防止错误形态的内容泄漏到契约。
        self.assertEqual(validation.draft.api_routes, [])
        self.assertEqual(validation.draft.database_ddl, "")

    def test_script_requires_spec_and_rejects_ui(self) -> None:
        llm = MockArchitectLLM()
        draft = llm.draft_contract(self._context("script_automation"))
        broken = ContractDraft(
            contract_markdown=draft.contract_markdown,
            tech_stack=draft.tech_stack,
            ui_logic=[{"flow": "打开页面"}],
            script_spec={"runtime": "python"},
            implementation_path=draft.implementation_path,
        )
        validation = validate_draft("script_automation", broken)
        self.assertFalse(validation.valid)
        self.assertTrue(any("UI" in issue for issue in validation.issues))
        self.assertTrue(
            any("script_spec 缺少字段" in issue for issue in validation.issues)
        )
        self.assertEqual(validation.draft.ui_logic, [])

    def test_full_stack_rejects_foreign_keys_and_missing_api(self) -> None:
        llm = MockArchitectLLM()
        draft = llm.draft_contract(self._context("full_stack_system"))
        broken = ContractDraft(
            contract_markdown=draft.contract_markdown,
            tech_stack={"frontend": "React"},
            api_routes=[],
            database_ddl=(
                "CREATE TABLE orders (id INT, user_id INT, "
                "FOREIGN KEY (user_id) REFERENCES users(id));"
            ),
            component_interactions=[],
            implementation_path=draft.implementation_path,
        )
        validation = validate_draft("full_stack_system", broken)
        self.assertFalse(validation.valid)
        joined = "；".join(validation.issues)
        self.assertIn("必须包含 API", joined)
        self.assertIn("物理外键", joined)
        self.assertIn("backend", joined)
        self.assertIn("database", joined)

    def test_unknown_shape_is_rejected(self) -> None:
        draft = ContractDraft(
            contract_markdown="x" * 300,
            implementation_path=[ArchitectImplementationPhase(phase="1", title="阶段")],
        )
        validation = validate_draft("mobile_app", draft)
        self.assertFalse(validation.valid)
        self.assertTrue(any("未知应用形态" in issue for issue in validation.issues))


if __name__ == "__main__":
    unittest.main()
