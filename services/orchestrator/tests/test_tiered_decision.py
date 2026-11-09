"""分级决策 / 一键澄清 / Reflection 偏好账本单元测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_core.agent.llm import MockAgentLLM
from ai_core.agent.preferences import (
    CORE_BUSINESS,
    IMPLEMENTATION_DETAIL,
    build_clarification_options,
    extract_preference_observations,
    infer_decision_impact,
    normalize_decision_impact,
    should_interrupt,
)
from ai_core.context_memory import ProjectMemory


def annotation(text: str) -> dict:
    return {
        "annotationId": "ann-1",
        "action": "ADD",
        "payload": {"text": text},
        "intent": {"ui": {"summary": text}},
    }


class DecisionImpactTest(unittest.TestCase):
    def test_login_ui_is_implementation_detail(self) -> None:
        self.assertEqual(
            infer_decision_impact([annotation("加个登录框，做得漂亮一点")]),
            IMPLEMENTATION_DETAIL,
        )

    def test_password_validation_is_core_business(self) -> None:
        self.assertEqual(
            infer_decision_impact(
                [annotation("加个登录框，密码提交时调用后端密码校验接口")]
            ),
            CORE_BUSINESS,
        )

    def test_destructive_operation_is_core_business(self) -> None:
        self.assertEqual(
            infer_decision_impact([annotation("删除整个任务列表组件")]),
            CORE_BUSINESS,
        )

    def test_unknown_model_value_falls_back_to_none(self) -> None:
        self.assertIsNone(normalize_decision_impact("WHATEVER"))
        self.assertEqual(normalize_decision_impact("ui_detail"), IMPLEMENTATION_DETAIL)


class InterruptPolicyTest(unittest.TestCase):
    def test_detail_never_interrupts_even_when_confidence_is_low(self) -> None:
        self.assertFalse(
            should_interrupt(
                decision_impact=IMPLEMENTATION_DETAIL,
                needs_clarification=True,
                confidence_score=0.1,
                threshold=0.7,
            )
        )

    def test_core_interrupts_on_missing_information(self) -> None:
        self.assertTrue(
            should_interrupt(
                decision_impact=CORE_BUSINESS,
                needs_clarification=True,
                confidence_score=0.9,
                threshold=0.7,
            )
        )

    def test_core_interrupts_on_low_confidence(self) -> None:
        self.assertTrue(
            should_interrupt(
                decision_impact=CORE_BUSINESS,
                needs_clarification=False,
                confidence_score=0.4,
                threshold=0.7,
            )
        )

    def test_off_disables_interrupt(self) -> None:
        self.assertFalse(
            should_interrupt(
                decision_impact=CORE_BUSINESS,
                needs_clarification=True,
                confidence_score=0.1,
                threshold=0.7,
                mode="off",
            )
        )

    def test_always_only_forces_core(self) -> None:
        self.assertTrue(
            should_interrupt(
                decision_impact=CORE_BUSINESS,
                needs_clarification=False,
                confidence_score=1.0,
                threshold=0.7,
                mode="always",
            )
        )
        self.assertFalse(
            should_interrupt(
                decision_impact=IMPLEMENTATION_DETAIL,
                needs_clarification=False,
                confidence_score=1.0,
                threshold=0.7,
                mode="always",
            )
        )


class MockPlannerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.llm = MockAgentLLM(scenario="none")

    def test_pretty_login_box_does_not_interrupt(self) -> None:
        result = self.llm.plan([annotation("加个登录框，做得漂亮一点")], {})
        self.assertEqual(result.decision_impact, IMPLEMENTATION_DETAIL)
        self.assertFalse(result.needs_clarification)
        self.assertIsNone(result.clarification)

    def test_backend_password_check_interrupts_with_quick_options(self) -> None:
        result = self.llm.plan(
            [annotation("加个登录框，密码提交时调用后端密码校验接口")], {}
        )
        self.assertEqual(result.decision_impact, CORE_BUSINESS)
        self.assertTrue(result.needs_clarification)
        self.assertIsNotNone(result.clarification)
        payload = result.clarification or {}
        self.assertEqual(payload.get("reason"), "core_business_decision")
        self.assertEqual(payload.get("decisionImpact"), CORE_BUSINESS)
        options = payload.get("clarificationOptions") or []
        self.assertEqual(
            [item["value"] for item in options],
            ["ui_only", "backend_api", "existing_auth"],
        )
        recommended = [item for item in options if item.get("recommended")]
        self.assertEqual(recommended[0]["value"], "backend_api")

    def test_answered_clarification_replans_without_asking_again(self) -> None:
        result = self.llm.plan(
            [annotation("加个登录框，密码提交时调用后端密码校验接口")],
            {},
            clarification_answers=[
                {"questionId": "q_password_check", "value": "backend_api"}
            ],
        )
        self.assertFalse(result.needs_clarification)
        self.assertIsNone(result.clarification)


class ClarificationOptionsTest(unittest.TestCase):
    def test_only_first_single_choice_and_three_options(self) -> None:
        questions = [
            {"questionId": "q_text", "type": "free_text", "prompt": "补充"},
            {
                "questionId": "q_scope",
                "type": "single_choice",
                "options": [
                    {"value": "a", "label": "A"},
                    {"value": "b", "label": "B", "description": "B 方案"},
                    {"value": "c", "label": "C"},
                    {"value": "d", "label": "D"},
                ],
                "defaultValue": "b",
            },
        ]
        options = build_clarification_options(questions)
        self.assertEqual([item["value"] for item in options], ["a", "b", "c"])
        self.assertTrue(options[1]["recommended"])
        self.assertEqual(options[1]["questionId"], "q_scope")


class PreferenceLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.memory = ProjectMemory(self._tmp.name, "tenant", "project")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _record(self, text: str) -> None:
        observations = extract_preference_observations([annotation(text)], run_id="run")
        self.assertTrue(observations)
        self.memory.record_preferences(observations, min_evidence=2)

    def test_single_modification_is_not_stable(self) -> None:
        self._record("把主色改成紫色")
        self.assertEqual(self.memory.stable_preferences(min_evidence=2), {})
        self.assertEqual(self.memory.preference_context(min_evidence=2), "")

    def test_two_consistent_modifications_become_default(self) -> None:
        self._record("把主色改成紫色")
        self._record("主色调调整为紫色")
        stable = self.memory.stable_preferences(min_evidence=2)
        self.assertEqual(stable["primary_color"]["value"], "purple")
        self.assertIn("主色调：紫色", self.memory.preference_context(min_evidence=2))

    def test_single_new_color_does_not_override_two_votes(self) -> None:
        self._record("把主色改成紫色")
        self._record("主色调调整为紫色")
        self._record("把主色改成蓝色")
        stable = self.memory.stable_preferences(min_evidence=2)
        self.assertEqual(stable["primary_color"]["value"], "purple")


if __name__ == "__main__":
    unittest.main()
