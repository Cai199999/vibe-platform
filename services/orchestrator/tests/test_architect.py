"""M2.5 Architect：应用形态、动态槽位、分级契约与决策状态机测试。

运行：

    cd services/orchestrator
    PYTHONPATH=. python -m unittest tests.test_architect -v

全部使用 ``MockArchitectLLM``，不依赖 DeepSeek Key、MySQL 或 Docker。
"""

from __future__ import annotations

import json
import unittest
import uuid
from dataclasses import replace
from typing import Any
from unittest.mock import patch

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
from ai_core.architect.skeleton import (
    build_skeleton_draft,
    is_abstract_brief,
    should_use_skeleton,
)
from ai_core.architect.slots import infer_app_shape, infer_slots
from ai_core.domain.visual_document import VisualDocument, VisualNode
from ai_core.architect import service as architect_service_module
from ai_core.architect.service import (
    ActiveArchitectRun,
    ArchitectService,
    ArchitectVisualRequiredError,
)
from ai_core.architect.schemas import (
    ArchitectAnalysis,
    ArchitectApiRoute,
    ArchitectImplementationPhase,
)
from ai_core.config import Settings
from ai_core.domain.visual_document import validate_visual_document
from ai_core.domain.architect import (
    ArchitectComponentInteraction,
    ArchitectConflictDecisionRequest,
    ArchitectContractConflict,
    ArchitectHelperRequest,
    ArchitectQuestion,
    ArchitectRunRequest,
    ArchitectTableRelation,
)
from ai_core.ports.model import ModelClient, ModelClientError

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
    def __init__(self, *, max_rounds: int = 5, llm: Any | None = None) -> None:
        self.run_id = f"test_{uuid.uuid4().hex[:12]}"
        self.buffer = RunEventBuffer(self.run_id)
        self.runner = ArchitectGraphRunner(
            llm or MockArchitectLLM(),
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

    def decide(
        self,
        decision: str,
        comment: str = "",
        quality_tier: str = "",
    ) -> None:
        resume: dict[str, Any] = {"decision": decision, "comment": comment}
        if quality_tier:
            resume["qualityTier"] = quality_tier
        self.outcome = self.runner.run(
            {},
            run_id=self.run_id,
            resume=resume,
        )

    def append_requirement(self, comment: str) -> None:
        """模拟服务端“已确认契约后追加需求”的初始状态。

        真实服务会把同一份状态写入同一个 checkpoint thread，并通过
        contract_decision=REVISE 直接路由到 revise_contract。
        """
        state = dict(self.outcome.state)
        history = list(state.get("ui_history") or [])
        history.append({"role": "user", "content": comment})
        revision_count = int(state.get("contract_revision_count") or 0)
        state.update(
            {
                "ui_history": history,
                "contract_decision": "REVISE",
                "contract_feedback": comment,
                "contract_revision_count": revision_count + 1,
                "contract_issues": [],
                "repair_count": 0,
                "status": "REVISING_CONTRACT",
            }
        )
        self.outcome = self.runner.run(state, run_id=self.run_id)


class ArchitectFlowTest(unittest.TestCase):
    def test_first_round_always_asks_app_shape(self) -> None:
        harness = ArchitectHarness()
        self.assertTrue(harness.outcome.interrupted)
        payload = harness.outcome.interrupt or {}
        self.assertEqual(payload.get("reason"), "app_shape_required")
        question = payload["questions"][0]
        self.assertEqual(question["slot"], "app.shape")
        self.assertEqual(question["prompt"], APP_SHAPE_PROMPT)
        # 对话前置：形态不再用三选一卡片，而是让用户自然语言描述目标。
        self.assertEqual(question["type"], "free_text")
        self.assertFalse(question.get("options"))
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
        # Architect → BackendCoder 的反向 A2A 回执必须随确认一起发出。
        a2a_events = [
            event
            for event in harness.buffer.snapshot()
            if event.type == "a2a.feedback"
        ]
        self.assertTrue(a2a_events)
        self.assertEqual(a2a_events[-1].payload["fromAgent"], "Architect")
        self.assertEqual(a2a_events[-1].payload["toAgent"], "BackendCoder")
        self.assertEqual(
            a2a_events[-1].payload["requestedAction"],
            "GENERATE_WITH_CONTRACT",
        )

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

    def test_contract_gate_requires_quality_tier_question(self) -> None:
        """风险自适应门禁：契约确认 payload 必须携带不可跳过的分级问题。"""
        harness = ArchitectHarness()
        payload = harness.drive_to_contract("full_stack_system")
        self.assertTrue(payload.get("qualityTierRequired"))
        self.assertIn("请选择您对本次生成的质量要求", payload["qualityTierPrompt"])
        options = payload.get("qualityTierOptions") or []
        self.assertEqual(
            [item["value"] for item in options],
            ["L1_PROTOTYPE", "L2_STANDARD", "L3_PRODUCTION"],
        )
        # 前端未选择时不得把 L2 伪装成“用户已选择”。
        self.assertEqual(payload.get("qualityTier"), "L2_STANDARD")

        harness.decide("CONFIRM", quality_tier="L3_PRODUCTION")
        state = harness.outcome.state
        self.assertEqual(state["status"], "CONFIRMED")
        self.assertEqual(state["quality_tier"], "L3_PRODUCTION")
        self.assertEqual(state["contract"]["qualityTier"], "L3_PRODUCTION")
        confirmed = [
            event
            for event in harness.buffer.snapshot()
            if event.type == "architect.contract.confirmed"
        ][-1]
        self.assertEqual(confirmed.payload["contract"]["qualityTier"], "L3_PRODUCTION")

    def test_quality_tier_survives_contract_revision(self) -> None:
        """修订重算契约时必须沿用用户已选分级，不能被重置回默认 L2。"""
        harness = ArchitectHarness()
        harness.drive_to_contract("full_stack_system")
        harness.decide("REVISE", "增加导出入口", quality_tier="L1_PROTOTYPE")
        revised = harness.outcome.interrupt or {}
        self.assertEqual(revised.get("kind"), "contract_confirmation")
        self.assertEqual(revised["contract"]["qualityTier"], "L1_PROTOTYPE")
        self.assertEqual(revised.get("qualityTier"), "L1_PROTOTYPE")

        # 旧客户端二次确认不传分级：沿用上一次选择，而不是回退 L2。
        harness.decide("CONFIRM")
        self.assertEqual(harness.outcome.state["status"], "CONFIRMED")
        self.assertEqual(
            harness.outcome.state["contract"]["qualityTier"], "L1_PROTOTYPE"
        )

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

    def test_confirmed_contract_accepts_appended_requirements(self) -> None:
        """契约确认后仍可追加需求：不重新澄清，直接修订同一份契约。"""
        harness = ArchitectHarness()
        first = harness.drive_to_contract("full_stack_system")
        contract_id = first["contract"]["contractId"]
        harness.decide("CONFIRM")
        self.assertEqual(harness.outcome.state["status"], "CONFIRMED")

        harness.append_requirement("账单页再加一个导出按钮")
        revised = harness.outcome.interrupt or {}
        self.assertEqual(revised.get("kind"), "contract_confirmation")
        contract = revised["contract"]
        self.assertEqual(contract["contractId"], contract_id)
        self.assertIn("账单页再加一个导出按钮", contract["contractMarkdown"])
        self.assertEqual(
            harness.outcome.state["contract_revision_count"],
            1,
        )
        # 修订后仍可再次确认，说明面板可以持续在线。
        harness.decide("CONFIRM")
        self.assertEqual(harness.outcome.state["status"], "CONFIRMED")
        # 第二次 CONFIRMED 必须作为新事件送达；否则前端会一直停在
        # “确认中”，追加需求后的闭环无法完成。
        confirmed_events = [
            event
            for event in harness.buffer.snapshot()
            if event.type == "architect.contract.confirmed"
        ]
        self.assertEqual(len(confirmed_events), 2)
        self.assertEqual(
            confirmed_events[-1].payload["contract"]["contractId"],
            contract_id,
        )

    def test_comment_answer_backfills_slots_deterministically(self) -> None:
        harness = ArchitectHarness()
        harness.answer(harness.outcome.interrupt or {}, "full_stack_system")
        outcome = harness.runner.run(
            {},
            run_id=harness.run_id,
            resume={"comment": "后端用 Java Spring Boot，数据库用 MySQL。"},
        )
        slots = outcome.state["slots"]
        self.assertEqual(slots["backend.framework"], "java_spring_boot")
        self.assertEqual(slots["database.engine"], "mysql")

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

    def test_mock_extracts_slots_from_free_text_history(self) -> None:
        # 用户自由输入“Java + MySQL”“订单和商品表”时，Mock 也必须动态
        # 填槽并提升分数，而不是永远停在 0.90 / 0.70。
        context = ArchitectContext(
            project_id="p1",
            project_name="秒杀",
            source_files={},
            ui_history=[
                {"role": "user", "content": "点击后扣除库存并跳转"},
                {
                    "role": "user",
                    "content": "后端用 Java Spring Boot，数据库用 MySQL。",
                },
                {"role": "user", "content": "核心实体是订单和商品表。"},
            ],
            slots={"app.shape": "full_stack_system", "ui.layout": "topnav_cards"},
            app_shape="full_stack_system",
            round=2,
            threshold=0.85,
        )
        analysis = MockArchitectLLM().analyze(context)
        self.assertEqual(analysis.missing_slots, [])
        self.assertGreaterEqual(analysis.confidence_score, 0.85)
        self.assertEqual(analysis.slots["backend.framework"], "java_spring_boot")
        self.assertEqual(analysis.slots["database.engine"], "mysql")
        self.assertIn("订单", analysis.slots["data.entities"])

    def test_natural_language_goal_infers_shape_without_three_choice(self) -> None:
        """用户只描述目标时，Mock 应直接推断形态并继续追问缺失槽位。"""
        goal = "我要做一个团队任务看板，成员可以登录、创建任务并拖拽状态。"
        self.assertEqual(infer_app_shape(goal), "full_stack_system")
        slots = infer_slots(goal)
        self.assertEqual(slots.get("app.shape"), "full_stack_system")

        context = ArchitectContext(
            project_id="p1",
            project_name="团队任务看板",
            source_files={},
            ui_history=[{"role": "user", "content": goal}],
            slots={},
            app_shape=None,
            round=1,
            threshold=0.85,
        )
        analysis = MockArchitectLLM().analyze(context)
        self.assertEqual(analysis.app_shape, "full_stack_system")
        self.assertNotIn("app.shape", analysis.missing_slots)

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
        self.assertEqual(result.missing_slots, ["ui.primary_flow", "ui.layout"])
        self.assertEqual(guarded.confidence_score, 0.70)
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
                "ui.layout",
            ],
        )
        self.assertLess(guarded.confidence_score, 0.85)
        self.assertEqual(guarded.confidence_score, 0.60)

    def test_assumptions_do_not_block_complete_analysis(self) -> None:
        # 用户给出默认技术栈后，LLM 会把默认字段写入 assumptions；
        # 只要必需槽位齐全，就应允许进入契约，而不是卡在 0.84。
        guarded, _ = apply_confidence_guard(
            ArchitectAnalysis(
                app_shape="lightweight_web",
                confidence_score=0.99,
                slots={
                    "ui.primary_flow": "输入 -> 勾选 -> 保存",
                    "ui.layout": "topnav_cards",
                },
                assumptions=["假设浏览器支持 localStorage"],
            ),
            {},
            0.85,
        )
        self.assertEqual(guarded.confidence_score, 0.90)
        self.assertFalse(guarded.questions)

    def test_optional_slots_never_block_completion(self) -> None:
        # LLM 自报 auth.required / api.style 缺失时，不能把它们当成必需槽位。
        guarded, result = apply_confidence_guard(
            ArchitectAnalysis(
                app_shape="full_stack_system",
                confidence_score=0.93,
                missing_slots=["auth.required", "api.style"],
                slots={
                    "backend.framework": "java_spring_boot",
                    "database.engine": "mysql",
                    "business.key_flows": "点击 -> 扣库存 -> 跳转",
                    "data.entities": "Order, Product",
                    "ui.layout": "topnav_cards",
                },
            ),
            {},
            0.85,
        )
        self.assertEqual(result.missing_slots, [])
        self.assertEqual(guarded.confidence_score, 0.93)
        self.assertFalse(guarded.questions)

    def test_required_question_is_not_crowded_out_by_optional_ones(self) -> None:
        # LLM 一次返回 3 个可选问题，但 data.entities 仍缺失；必须优先问它。
        raw = ArchitectAnalysis(
            app_shape="full_stack_system",
            confidence_score=0.90,
            missing_slots=["data.entities"],
            slots={
                "backend.framework": "java_spring_boot",
                "database.engine": "mysql",
                "business.key_flows": "点击 -> 扣库存 -> 跳转",
            },
            questions=[
                ArchitectQuestion(
                    question_id="q_auth",
                    slot="auth.required",
                    type="single_choice",
                    prompt="是否需要登录？",
                ),
                ArchitectQuestion(
                    question_id="q_api",
                    slot="api.style",
                    type="free_text",
                    prompt="API 风格？",
                ),
                ArchitectQuestion(
                    question_id="q_deploy",
                    slot="deployment.target",
                    type="free_text",
                    prompt="部署目标？",
                ),
            ],
        )
        guarded, _ = apply_confidence_guard(raw, {}, 0.85)
        slots = [question.slot for question in guarded.questions]
        self.assertIn("data.entities", slots)
        self.assertLessEqual(len(slots), 3)


class _NoBusinessLLM(MockArchitectLLM):
    """若骨架闸门失效、错误地调用 LLM，测试立即失败。"""

    def analyze(self, context: ArchitectContext):  # noqa: ANN001
        raise AssertionError("抽象需求不应调用 analyze")

    def draft_contract(self, context: ArchitectContext):  # noqa: ANN001
        raise AssertionError("抽象需求不应调用 LLM 起草契约")

    def revise_contract(self, context, previous, feedback):  # noqa: ANN001
        raise AssertionError("骨架契约不应交给 LLM 修订")


class SkeletonModeTest(unittest.TestCase):
    """“做个工作台”必须走确定性骨架，禁止脑补业务组件。"""

    def test_abstract_brief_detection(self) -> None:
        self.assertTrue(is_abstract_brief("做个工作台"))
        self.assertTrue(is_abstract_brief("帮我搭建一个管理后台"))
        self.assertTrue(is_abstract_brief("做一个简单的网站"))
        self.assertFalse(is_abstract_brief("做一个任务管理系统"))
        self.assertFalse(is_abstract_brief("做个工作台，要能登录并上传简历"))
        self.assertFalse(is_abstract_brief("做一个数据分析看板"))
        self.assertFalse(is_abstract_brief("你好"))
        self.assertFalse(is_abstract_brief("随便看看"))

    def test_skeleton_gate_requires_blank_project(self) -> None:
        history = [{"role": "user", "content": "做个工作台"}]
        self.assertTrue(
            should_use_skeleton(
                history=history,
                slots={},
                source_files={},
                project_summary="",
                latest_changes="",
            )
        )
        self.assertFalse(
            should_use_skeleton(
                history=history,
                slots={},
                source_files={"src/App.jsx": "export default () => null"},
            )
        )
        self.assertFalse(
            should_use_skeleton(
                history=history,
                slots={"business.key_flows": "创建订单"},
                source_files={},
            )
        )
        # 追问后用户仍然没说出业务：继续保持骨架模式，禁止模型借机脑补。
        self.assertTrue(
            should_use_skeleton(
                history=history + [{"role": "user", "content": "没想好，先这样"}],
                slots={},
                source_files={},
            )
        )

    def test_abstract_brief_stops_before_llm_and_returns_skeleton(self) -> None:
        run_id = f"test_{uuid.uuid4().hex[:12]}"
        buffer = RunEventBuffer(run_id)
        runner = ArchitectGraphRunner(
            _NoBusinessLLM(),
            event_sink=buffer,
            checkpointer_provider=MemoryCheckpointerProvider(),
        )
        state = _initial_state(run_id)
        state.update(
            {
                "source_files": {},
                "source_hash": "",
                "ui_history": [{"role": "user", "content": "做个工作台"}],
            }
        )
        outcome = runner.run(state)

        self.assertTrue(outcome.interrupted)
        payload = outcome.interrupt or {}
        self.assertEqual(payload.get("kind"), "contract_confirmation")
        contract = payload["contract"]
        self.assertTrue(contract["skeletonMode"])
        self.assertEqual(contract["appShape"], "lightweight_web")
        self.assertEqual(len(contract["components"]), 1)
        self.assertEqual(contract["components"][0]["name"], "待定区域")
        self.assertTrue(contract["components"][0]["isPlaceholder"])
        regions = [item["region"] for item in contract["pageLayout"]]
        self.assertEqual(regions, ["header", "sidebar", "main"])
        # 业务幻觉关键词不允许出现在组件清单里。
        component_blob = str(contract["components"])
        for forbidden in ("LoginForm", "ResumeUploader", "JDdetail", "登录", "简历"):
            self.assertNotIn(forbidden, component_blob)

        visual = contract["visualDocument"]
        self.assertEqual(visual["schemaVersion"], "1.0")
        self.assertEqual(visual["root"]["type"], "page")
        visual_report = validate_visual_document(visual)
        self.assertTrue(visual_report.ok, visual_report.errors)
        self.assertGreaterEqual(visual_report.node_count, 5)
        self.assertNotIn("<", json.dumps(visual, ensure_ascii=False))

        event_types = [event.type for event in buffer.snapshot()]
        self.assertIn("architect.skeleton.detected", event_types)
        self.assertIn("architect.skeleton.contract", event_types)
        self.assertNotIn("architect.clarification.required", event_types)

    def test_skeleton_draft_validates_and_keeps_placeholder_flag(self) -> None:
        draft = build_skeleton_draft(project_name="工作台", goal="做个工作台")
        validation = validate_draft("lightweight_web", draft)
        self.assertTrue(validation.valid, validation.issues)
        self.assertIsNotNone(validation.draft)
        assert validation.draft is not None
        self.assertTrue(validation.draft.skeleton_mode)
        self.assertEqual(len(validation.draft.components), 1)
        self.assertTrue(validation.draft.components[0]["isPlaceholder"])
        self.assertTrue(validation.draft.visual_document)
        self.assertTrue(validate_visual_document(validation.draft.visual_document).ok)

    def test_backend_binding_contract_reuses_frozen_visual_document(self) -> None:
        run_id = f"test_{uuid.uuid4().hex[:12]}"
        runner = ArchitectGraphRunner(
            MockArchitectLLM(),
            checkpointer_provider=MemoryCheckpointerProvider(),
        )
        frozen = VisualDocument(
            docId="vdoc_frozen",
            projectId=f"project_{run_id}",
            root=VisualNode(id="nd_page0001", type="page", name="页面"),
        ).model_dump(mode="json", by_alias=True)
        state = _initial_state(run_id)
        state.update(
            {
                "output_phase": "BACKEND_BINDING",
                "source_files": {},
                "source_hash": "",
                "visual_document": frozen,
                "visual_delta": {"added": [{"id": "nd_new"}]},
                "slots": {
                    "app.shape": "full_stack_system",
                    "backend.framework": "java_spring_boot",
                    "database.engine": "mysql",
                    "business.key_flows": ["查看订单", "创建订单"],
                    "data.entities": ["orders(id, title)"],
                },
                "ui_history": [
                    {
                        "role": "user",
                        "content": "为冻结界面补充订单接口与数据库",
                    }
                ],
            }
        )
        outcome = runner.run(state, run_id=run_id)
        self.assertEqual(
            (outcome.interrupt or {}).get("kind"),
            "contract_confirmation",
            outcome.interrupt,
        )
        contract = outcome.state.get("contract") or {}
        self.assertEqual(contract.get("outputPhase"), "BACKEND_BINDING")
        self.assertEqual(
            (contract.get("visualDocument") or {}).get("docId"),
            "vdoc_frozen",
        )
        self.assertTrue(contract.get("apiRoutes"))
        self.assertTrue(contract.get("databaseDdl"))

    def test_visual_v0_stops_before_contract_confirmation(self) -> None:
        run_id = f"test_{uuid.uuid4().hex[:12]}"
        runner = ArchitectGraphRunner(
            MockArchitectLLM(),
            checkpointer_provider=MemoryCheckpointerProvider(),
        )
        state = _initial_state(run_id)
        state.update(
            {
                "output_phase": "VISUAL_V0",
                "source_files": {},
                "source_hash": "",
                "slots": {
                    "app.shape": "full_stack_system",
                    "ui.layout": "topnav_cards",
                    "ui.primary_flow": "查看订单列表 -> 点击新建订单 -> 保存",
                },
                "ui_history": [
                    {
                        "role": "user",
                        "content": "订单管理后台，需要订单列表和新建订单按钮",
                    }
                ],
            }
        )
        outcome = runner.run(state, run_id=run_id)
        for _ in range(6):
            payload = outcome.interrupt or {}
            questions = payload.get("questions") or []
            if not questions:
                break
            answers = [
                _answer_for(question, "full_stack_system") for question in questions
            ]
            outcome = runner.run(
                {},
                run_id=run_id,
                resume={"answers": answers},
            )

        self.assertEqual(
            outcome.state.get("status"),
            "AWAITING_VISUAL_EDIT",
            outcome.interrupt,
        )
        contract = outcome.state.get("contract") or {}
        self.assertEqual(contract.get("outputPhase"), "VISUAL_V0")
        self.assertEqual(contract.get("apiRoutes"), [])
        self.assertEqual(contract.get("databaseDdl"), "")
        self.assertTrue(contract.get("visualDocument"))

    def test_semantic_review_packet_forces_first_round_business_question(self) -> None:
        run_id = f"test_{uuid.uuid4().hex[:12]}"
        runner = ArchitectGraphRunner(
            MockArchitectLLM(),
            checkpointer_provider=MemoryCheckpointerProvider(),
        )
        state = _initial_state(run_id)
        state.update(
            {
                "source_files": {},
                "source_hash": "",
                "project_summary": "虚拟界面已冻结",
                "ui_history": [
                    {
                        "role": "system",
                        "content": (
                            "【SEMANTIC_REVIEW_PACKET】\n"
                            '{"nodes":[{"name":"登录按钮"}],'
                            '"unresolvedInteractions":[{"intent":"登录"}]}'
                        ),
                    },
                    {
                        "role": "user",
                        "content": "请开始业务审查",
                    },
                ],
            }
        )
        outcome = runner.run(state, run_id=run_id)
        self.assertTrue(outcome.interrupted)
        payload = outcome.interrupt or {}
        self.assertEqual(payload.get("reason"), "missing_required_slots")
        questions = payload.get("questions") or []
        self.assertTrue(questions)
        self.assertTrue(
            any("鉴权" in str(item.get("prompt") or "") for item in questions)
        )


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

    def test_mock_architect_reads_semantic_review_packet(self) -> None:
        context = ArchitectContext(
            project_id="p_review",
            project_name="登录工作台",
            source_files={},
            ui_history=[
                {
                    "role": "system",
                    "content": (
                        "【SEMANTIC_REVIEW_PACKET】\n"
                        '{"nodes":[{"name":"登录按钮"}],'
                        '"unresolvedInteractions":[{"intent":"登录"}]}'
                    ),
                },
                {"role": "user", "content": "请开始业务审查"},
            ],
            slots={},
            app_shape=None,
            round=1,
            threshold=0.85,
        )
        analysis = MockArchitectLLM().analyze(context)
        self.assertEqual(analysis.app_shape, "full_stack_system")
        self.assertEqual(analysis.missing_slots, ["ui.interactions"])
        self.assertEqual(len(analysis.questions), 1)
        self.assertEqual(analysis.questions[0].slot, "ui.interactions")
        self.assertIn("鉴权", analysis.questions[0].prompt)

    def test_invalid_visual_document_is_rejected_before_confirmation(self) -> None:
        llm = MockArchitectLLM()
        draft = llm.draft_contract(self._context("lightweight_web"))
        broken = replace(
            draft,
            visual_document={
                "schemaVersion": "1.0",
                "docId": "vdoc_bad",
                "root": {"id": "bad_node_id", "type": "page"},
            },
        )
        validation = validate_draft("lightweight_web", broken)
        self.assertFalse(validation.valid)
        self.assertTrue(any("visual_document" in issue for issue in validation.issues))

    def test_visual_v0_requires_visual_document(self) -> None:
        draft = MockArchitectLLM().draft_contract(self._context("lightweight_web"))
        empty = replace(draft, visual_document={})
        validation = validate_draft(
            "lightweight_web",
            empty,
            output_phase="VISUAL_V0",
        )
        self.assertFalse(validation.valid)
        self.assertTrue(
            any("visual_document" in issue for issue in validation.issues),
            validation.issues,
        )

    def test_visual_v0_invalid_visual_document_is_reported(self) -> None:
        draft = MockArchitectLLM().draft_contract(self._context("lightweight_web"))
        broken = replace(
            draft,
            visual_document={
                "schemaVersion": "1.0",
                "docId": "vdoc_bad",
                "root": {"id": "bad_node_id", "type": "page"},
            },
        )
        validation = validate_draft(
            "lightweight_web",
            broken,
            output_phase="VISUAL_V0",
        )
        self.assertFalse(validation.valid)
        self.assertTrue(
            any(
                "visual_document" in issue and "Schema" in issue
                for issue in validation.issues
            ),
            validation.issues,
        )

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

    def test_full_stack_derives_master_detail_relation_from_ddl(self) -> None:
        """LLM 漏写 relations 时，按 <parent>_id 确定性推导，而不是退化成单表。"""
        draft = MockArchitectLLM().draft_contract(self._context("full_stack_system"))
        with_tables = replace(
            draft,
            database_ddl=(
                "CREATE TABLE projects (id INT, name TEXT);"
                "CREATE TABLE tasks (id INT, project_id INT, title TEXT);"
            ),
        )
        validation = validate_draft("full_stack_system", with_tables)
        self.assertTrue(validation.valid, validation.issues)
        self.assertEqual(len(validation.draft.relations), 1)
        relation = validation.draft.relations[0]
        self.assertEqual(relation.parent_table, "projects")
        self.assertEqual(relation.child_table, "tasks")
        self.assertEqual(relation.foreign_key, "project_id")

    def test_full_stack_rejects_relation_to_missing_table(self) -> None:
        """关联引用不存在的表属于契约自相矛盾，必须在确认前修复。"""
        draft = MockArchitectLLM().draft_contract(self._context("full_stack_system"))
        broken = replace(
            draft,
            database_ddl="CREATE TABLE tasks (id INT, title TEXT);",
            relations=[
                ArchitectTableRelation(
                    parentTable="users",
                    childTable="tasks",
                    foreignKey="user_id",
                )
            ],
        )
        validation = validate_draft("full_stack_system", broken)
        self.assertFalse(validation.valid)
        joined = "；".join(validation.issues)
        self.assertIn("users", joined)
        self.assertIn("不存在", joined)

    def test_unknown_shape_is_rejected(self) -> None:
        draft = ContractDraft(
            contract_markdown="x" * 300,
            implementation_path=[ArchitectImplementationPhase(phase="1", title="阶段")],
        )
        validation = validate_draft("mobile_app", draft)
        self.assertFalse(validation.valid)
        self.assertTrue(any("未知应用形态" in issue for issue in validation.issues))


class _PromptCaptureClient(ModelClient):
    """记录 Architect 实际发送的 system prompt，然后主动失败。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def generate(self, task: str, system: str, human: str) -> str:
        self.calls.append((task, system, human))
        raise ModelClientError("prompt captured")

    def generate_json(
        self,
        task: str,
        system: str,
        human: str,
        *,
        json_schema: dict | None = None,
    ) -> str:
        self.calls.append((task, system, human))
        raise ModelClientError("prompt captured")


class ArchitectSubresourcePromptTest(unittest.TestCase):
    """嵌套子资源必须有显式表与外键，不能靠 Coder 隐式猜测。"""

    def _llm(self) -> tuple[DeepSeekArchitectLLM, _PromptCaptureClient]:
        settings = Settings(
            deepseek_api_key="test-key",
            architect_json_retry_limit=0,
        )
        client = _PromptCaptureClient()
        return DeepSeekArchitectLLM(settings, client=client), client

    @staticmethod
    def _context() -> ArchitectContext:
        return ArchitectContext(
            project_id="p1",
            project_name="用户中心",
            source_files={"src/App.jsx": "export default () => null"},
            ui_history=[],
            slots={},
            app_shape="full_stack_system",
            round=1,
            threshold=0.85,
        )

    def test_draft_prompt_requires_explicit_child_table(self) -> None:
        llm, client = self._llm()
        with self.assertRaises(ArchitectLLMError):
            llm.draft_contract(self._context())

        self.assertEqual(len(client.calls), 1)
        task, system, _ = client.calls[0]
        self.assertEqual(task, "architect_contract")
        self.assertIn("子资源契约铁律", system)
        self.assertIn("/users/{user_id}/profile", system)
        self.assertIn("profiles", system)
        self.assertIn("relations", system)
        self.assertIn("禁止", system)
        # 资源 / 动作分离：认证接口不得被建模为独立集合与独立表。
        self.assertIn("资源与动作分离铁律", system)
        self.assertIn("/api/v1/users/login", system)
        self.assertIn("禁止把 /auth", system)

    def test_prompt_injects_recent_direct_changelog(self) -> None:
        llm, client = self._llm()
        context = replace(
            self._context(),
            direct_changelog=[
                {
                    "action": "UPDATE_TEXT",
                    "summary": "将 App 的文案改为“我的任务”",
                    "target": {"file": "src/App.jsx", "component": "App"},
                    "createdAt": "2026-11-16T01:56:00Z",
                }
            ],
        )
        with self.assertRaises(ArchitectLLMError):
            llm.analyze(context)

        system = client.calls[0][1]
        self.assertIn("用户最近直接操作", system)
        self.assertIn("我的任务", system)
        self.assertIn("不要当作新需求", system)

    def test_revise_prompt_requires_explicit_child_table(self) -> None:
        llm, client = self._llm()
        previous = MockArchitectLLM().draft_contract(self._context())
        with self.assertRaises(ArchitectLLMError):
            llm.revise_contract(self._context(), previous, "补充 /users/{id}/profile")

        self.assertEqual(len(client.calls), 1)
        task, system, human = client.calls[0]
        self.assertEqual(task, "architect_contract")
        self.assertIn("/users/{user_id}/profile", system)
        self.assertIn("databaseDdl", system)
        self.assertIn("禁止依赖隐式推断", system)
        self.assertIn("必须区分资源与动作", system)
        self.assertIn("禁止把 /auth", system)
        self.assertIn("补充 /users/{id}/profile", human)


class ArchitectHelperTest(unittest.TestCase):
    """影子 LLM：只读问答，绝不触碰主架构状态机。"""

    def _register_active_run(self) -> ActiveArchitectRun:
        run_id = f"helper_{uuid.uuid4().hex[:10]}"
        request = ArchitectRunRequest.model_validate(
            {
                "projectId": "project_helper_test",
                "projectName": "影子助手测试项目",
                "options": {
                    "llmProvider": "mock",
                    "ragProvider": "markdown",
                },
            }
        )
        active = ActiveArchitectRun(
            run_id=run_id,
            fingerprint="helper-test",
            request=request,
            llm=MockArchitectLLM(),
            buffer=RunEventBuffer(run_id),
            status="AWAITING_CLARIFICATION",
            state={
                "slots": {
                    "app.shape": "full_stack_system",
                    "backend.framework": "Java Spring Boot",
                    "database.engine": "MySQL",
                },
                "rag_provider": "markdown",
                "rag_top_k": 3,
                "ui_history": [{"role": "user", "content": "做一个订单管理系统"}],
            },
            source_hash="helper_source_hash",
            app_shape="full_stack_system",
            round=1,
            max_rounds=5,
            confidence_score=0.72,
            threshold=0.85,
        )
        with architect_service_module._ACTIVE_LOCK:
            architect_service_module._ACTIVE_RUNS[run_id] = active
        self.addCleanup(lambda: architect_service_module._ACTIVE_RUNS.pop(run_id, None))
        return active

    def test_helper_answers_without_mutating_state(self) -> None:
        active = self._register_active_run()
        service = ArchitectService(llm=MockArchitectLLM())
        before_state = dict(active.state)
        before_clarification = active.clarification
        before_contract = active.contract
        request = ArchitectHelperRequest.model_validate(
            {
                "question": "为什么要用 Node.js？",
                "history": [
                    {"role": "user", "content": "当前用什么后端？"},
                    {"role": "assistant", "content": "当前记录的是 Java。"},
                ],
                # 关闭知识库，避免测试依赖 RAG 实现。
                "useKnowledgeBase": False,
            }
        )

        response = service.helper(active.run_id, request)

        self.assertTrue(response.answer.strip())
        self.assertTrue(response.read_only)
        self.assertEqual(response.run_id, active.run_id)
        # 只读铁律：状态、状态机字段与契约引用都必须原样保留。
        self.assertEqual(active.state, before_state)
        self.assertEqual(active.status, "AWAITING_CLARIFICATION")
        self.assertIs(active.clarification, before_clarification)
        self.assertIs(active.contract, before_contract)

    def test_helper_requires_existing_run(self) -> None:
        service = ArchitectService(llm=MockArchitectLLM())
        request = ArchitectHelperRequest.model_validate(
            {"question": "为什么这样分层？", "useKnowledgeBase": False}
        )
        with self.assertRaises(Exception):
            service.helper("arc_missing_helper_run", request)


class _NamingConflictLLM(MockArchitectLLM):
    """模拟“自动修复耗尽后仍有集合根 ↔ 表名矛盾”的契约。"""

    def draft_contract(self, context: ArchitectContext) -> ContractDraft:
        base = super().draft_contract(context)
        routes = [
            ArchitectApiRoute(
                method="GET",
                path="/api/v1/orders",
                response="订单列表",
                description="查询订单",
            ),
            ArchitectApiRoute(
                method="POST",
                path="/api/v1/orders",
                request="订单字段",
                response="新订单",
                description="创建订单",
            ),
            ArchitectApiRoute(
                method="GET",
                path="/api/v1/orders/{id}",
                response="订单详情",
                description="订单详情",
            ),
        ]
        interactions = [
            ArchitectComponentInteraction(
                component="OrderList",
                event="mount",
                api_route="GET /api/v1/orders",
                logic="加载订单列表",
            ),
            ArchitectComponentInteraction(
                component="OrderForm",
                event="submit",
                api_route="POST /api/v1/orders",
                logic="提交新订单",
            ),
        ]
        ddl = (
            "CREATE TABLE users (id BIGINT PRIMARY KEY, name VARCHAR(64));\n"
            "CREATE TABLE resumes (id BIGINT PRIMARY KEY, user_id BIGINT);"
        )
        return replace(
            base,
            api_routes=routes,
            component_interactions=interactions,
            database_ddl=ddl,
        )

    def revise_contract(
        self,
        context: ArchitectContext,
        previous: ContractDraft,
        feedback: str,
    ) -> ContractDraft:
        # 模拟模型“修不动”：自动维修一次后仍是同一份矛盾契约。
        return self.draft_contract(context)


class ContractConflictDecisionTest(unittest.TestCase):
    """自动修复失败后必须停下来向用户要决策，而不是直接 FAILED。"""

    def _drive_to_conflict(self) -> ArchitectHarness:
        harness = ArchitectHarness(llm=_NamingConflictLLM())
        payload = harness.outcome.interrupt or {}
        for _ in range(10):
            if payload.get("kind") == "contract_naming_conflict":
                return harness
            if payload.get("kind") == "contract_confirmation":
                self.fail("矛盾契约不应直接进入确认")
            harness.assert_shape_question_or_slots(payload, "full_stack_system")
            payload = harness.answer(payload, "full_stack_system")
        self.fail(f"未在预期轮次进入冲突决策: {payload}")

    def _resume(self, harness: ArchitectHarness, option_id: str) -> Any:
        harness.outcome = harness.runner.run(
            {},
            run_id=harness.run_id,
            resume={"optionId": option_id},
        )
        return harness.outcome

    def test_repair_exhausted_pauses_for_user_decision(self) -> None:
        harness = self._drive_to_conflict()
        payload = harness.outcome.interrupt or {}

        self.assertEqual(payload.get("kind"), "contract_naming_conflict")
        self.assertEqual(payload.get("apiCollection"), "/api/v1/orders")
        self.assertEqual(payload.get("resource"), "orders")
        actions = {item.get("action") for item in payload.get("options") or []}
        self.assertIn("MAP_TO_TABLE", actions)
        self.assertIn("CREATE_TABLE", actions)
        self.assertIn("CANCEL", actions)
        self.assertEqual(payload.get("status"), "PENDING")
        # 状态必须是等待决策，而不是终态失败。
        self.assertEqual(harness.outcome.state.get("status"), "AWAITING_DECISION")

    def test_create_table_resolution_reaches_contract_confirmation(self) -> None:
        harness = self._drive_to_conflict()

        outcome = self._resume(harness, "create_table")

        payload = outcome.interrupt or {}
        self.assertTrue(outcome.interrupted)
        self.assertEqual(payload.get("kind"), "contract_confirmation")
        contract = payload.get("contract") or {}
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS orders", contract.get("databaseDdl", "")
        )
        self.assertTrue(
            any(
                item.get("path") == "/api/v1/orders"
                for item in contract.get("apiRoutes") or []
            )
        )

    def test_map_to_table_resolution_rewrites_collection_root(self) -> None:
        harness = self._drive_to_conflict()
        conflict = harness.outcome.interrupt or {}
        map_option = next(
            item
            for item in conflict.get("options") or []
            if item.get("action") == "MAP_TO_TABLE"
        )

        outcome = self._resume(harness, map_option["optionId"])

        payload = outcome.interrupt or {}
        self.assertEqual(payload.get("kind"), "contract_confirmation")
        contract = payload.get("contract") or {}
        target = map_option.get("targetTable")
        self.assertTrue(
            any(
                str(item.get("path") or "") == f"/api/v1/{target}"
                for item in contract.get("apiRoutes") or []
            ),
            contract.get("apiRoutes"),
        )

    def test_cancel_resolution_ends_run_without_contract(self) -> None:
        harness = self._drive_to_conflict()

        outcome = self._resume(harness, "cancel")

        self.assertFalse(outcome.interrupted)
        self.assertEqual(outcome.state.get("status"), "CANCELLED")


class _KebabNestedLLM(MockArchitectLLM):
    """复现 kebab-case 路由 + snake_case DDL 的契约。"""

    def draft_contract(self, context: ArchitectContext) -> ContractDraft:
        base = super().draft_contract(context)
        routes = [
            ArchitectApiRoute(
                method="GET", path="/api/v1/users", description="用户列表"
            ),
            ArchitectApiRoute(
                method="GET",
                path="/api/v1/users/{user_id}/job-descriptions",
                description="用户职位描述",
            ),
        ]
        interactions = [
            ArchitectComponentInteraction(
                component="UserList",
                event="mount",
                api_route="GET /api/v1/users",
                logic="加载用户列表",
            )
        ]
        ddl = (
            "CREATE TABLE users (id BIGINT PRIMARY KEY, name VARCHAR(64));\n"
            "CREATE TABLE job_descriptions (\n"
            "  id BIGINT PRIMARY KEY,\n"
            "  user_id BIGINT NOT NULL,\n"
            "  title VARCHAR(128) NOT NULL\n"
            ");"
        )
        return replace(
            base,
            api_routes=routes,
            component_interactions=interactions,
            database_ddl=ddl,
        )

    def revise_contract(
        self,
        context: ArchitectContext,
        previous: ContractDraft,
        feedback: str,
    ) -> ContractDraft:
        return self.draft_contract(context)


class KebabNestedAuditTest(unittest.TestCase):
    def test_kebab_nested_route_reaches_confirmation(self) -> None:
        harness = ArchitectHarness(llm=_KebabNestedLLM())
        payload = harness.outcome.interrupt or {}
        for _ in range(10):
            if payload.get("kind") == "contract_confirmation":
                break
            harness.assert_shape_question_or_slots(payload, "full_stack_system")
            payload = harness.answer(payload, "full_stack_system")
        self.assertEqual(payload.get("kind"), "contract_confirmation")

    def test_repair_feedback_mentions_naming_convention(self) -> None:
        from types import SimpleNamespace

        feedback = ArchitectGraphRunner._repair_feedback(
            ["嵌套路由引用了 DDL 中不存在的子表 job-descriptions"],
            [
                SimpleNamespace(
                    code="nested_table_missing",
                    hint="在 databaseDdl 中创建 snake_case 子表 job_descriptions",
                )
            ],
        )

        self.assertIn("kebab-case", feedback)
        self.assertIn("snake_case", feedback)
        self.assertIn("job_descriptions", feedback)


class ContractConflictServiceTest(unittest.TestCase):
    """服务层只接受服务端下发的 optionId，并正确推进状态。"""

    def _register_active(self) -> ActiveArchitectRun:
        run_id = f"conflict_{uuid.uuid4().hex[:10]}"
        request = ArchitectRunRequest.model_validate(
            {
                "projectId": "project_conflict_test",
                "projectName": "冲突决策测试项目",
                "options": {"llmProvider": "mock", "ragProvider": "markdown"},
            }
        )
        conflict = ArchitectContractConflict.model_validate(
            {
                "conflictId": f"ccf_{run_id}",
                "runId": run_id,
                "contractId": "ctr_conflict",
                "appShape": "full_stack_system",
                "message": "API 集合 /api/v1/orders 对应的业务表 orders 不在 DDL 中",
                "apiCollection": "/api/v1/orders",
                "resource": "orders",
                "missingTable": "orders",
                "suggestedTable": "resumes",
                "tables": ["users", "resumes"],
                "options": [
                    {
                        "optionId": "map_to_table",
                        "action": "MAP_TO_TABLE",
                        "label": "映射到 resumes",
                        "targetTable": "resumes",
                    },
                    {
                        "optionId": "create_table",
                        "action": "CREATE_TABLE",
                        "label": "新建 orders 表",
                        "targetTable": "orders",
                    },
                    {
                        "optionId": "cancel",
                        "action": "CANCEL",
                        "label": "取消",
                    },
                ],
            }
        )
        active = ActiveArchitectRun(
            run_id=run_id,
            fingerprint="conflict-test",
            request=request,
            llm=MockArchitectLLM(),
            buffer=RunEventBuffer(run_id),
            status="AWAITING_DECISION",
            conflict=conflict,
        )
        with architect_service_module._ACTIVE_LOCK:
            architect_service_module._ACTIVE_RUNS[run_id] = active
        self.addCleanup(lambda: architect_service_module._ACTIVE_RUNS.pop(run_id, None))
        return active

    def test_valid_option_clears_conflict_and_resumes_graph(self) -> None:
        active = self._register_active()
        service = ArchitectService(llm=MockArchitectLLM())
        request = ArchitectConflictDecisionRequest.model_validate(
            {"optionId": "create_table", "comment": "保留现有接口"}
        )

        with patch.object(ArchitectService, "_resume_worker", lambda *args: None):
            response = service.decide_conflict(
                active.run_id, active.conflict.conflict_id, request
            )

        self.assertEqual(response.conflict.status, "ANSWERED")
        self.assertEqual(active.status, "ANALYZING")
        self.assertIsNone(active.conflict)

    def test_invalid_option_is_rejected(self) -> None:
        active = self._register_active()
        service = ArchitectService(llm=MockArchitectLLM())
        request = ArchitectConflictDecisionRequest.model_validate(
            {"optionId": "drop_database"}
        )

        with self.assertRaises(Exception):
            service.decide_conflict(active.run_id, active.conflict.conflict_id, request)
        self.assertEqual(active.status, "AWAITING_DECISION")
        self.assertIsNotNone(active.conflict)


class BackendBindingPreconditionTest(unittest.TestCase):
    """BACKEND_BINDING 缺少冻结树时必须 fail-fast，不能产出不可用契约。"""

    def test_backend_binding_without_visual_document_is_rejected(self) -> None:
        run_id = f"arc_{uuid.uuid4().hex[:12]}"
        request = ArchitectRunRequest(
            run_id=run_id,
            project_id="p_backend_binding",
            output_phase="BACKEND_BINDING",
            ui_history=[{"role": "user", "content": "为冻结界面补充订单接口"}],
        )
        service = ArchitectService(llm=MockArchitectLLM())
        with self.assertRaises(ArchitectVisualRequiredError):
            service.start_stream(request)
        # 拒绝必须发生在注册 Run 之前：不得留下半成品状态。
        self.assertIsNone(architect_service_module.get_active_architect_run(run_id))

    def test_backend_binding_with_visual_document_is_accepted(self) -> None:
        run_id = f"arc_{uuid.uuid4().hex[:12]}"
        document = VisualDocument(
            docId=f"vdoc_{uuid.uuid4().hex[:8]}",
            projectId="p_backend_binding",
            root=VisualNode(id="nd_page0001", type="page", name="页面"),
        )
        request = ArchitectRunRequest(
            run_id=run_id,
            project_id="p_backend_binding",
            output_phase="BACKEND_BINDING",
            visualDocument=document,
            ui_history=[{"role": "user", "content": "为冻结界面补充订单接口"}],
        )
        service = ArchitectService(llm=MockArchitectLLM())
        with patch.object(ArchitectService, "_run_worker", lambda *args: None):
            active = service.start_stream(request)
        self.addCleanup(lambda: architect_service_module._ACTIVE_RUNS.pop(run_id, None))
        self.assertEqual(active.output_phase, "BACKEND_BINDING")
        self.assertIsNotNone(active.request.visual_document)


if __name__ == "__main__":
    unittest.main()
