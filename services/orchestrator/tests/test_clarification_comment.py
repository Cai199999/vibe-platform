"""HITL 补充说明（comment）回归测试。

开发者工具形态下，澄清不再用弹窗打断：主输入栏保持可用，用户可以把大段
补充说明随答案一起提交。服务端必须把 ``comment`` 当作一条伪回答并入
上下文，否则前端写了也不会被 Planner / Coder 看到。
"""

from __future__ import annotations

import unittest

from ai_core.agent.graph import _augment_context_with_answers
from ai_core.agent.service import RunExecutionService
from ai_core.domain.agent import (
    Clarification,
    ClarificationAnswersRequest,
    ClarificationQuestion,
)

QUESTION = ClarificationQuestion.model_validate(
    {
        "questionId": "q1",
        "type": "single_choice",
        "prompt": "改成什么颜色？",
        "required": True,
        "options": [
            {"value": "red", "label": "红色"},
            {"value": "blue", "label": "蓝色"},
        ],
    }
)


def _clarification() -> Clarification:
    return Clarification(
        clarification_id="clar_comment",
        questions=[QUESTION],
    )


class ClarificationCommentTest(unittest.TestCase):
    def test_comment_is_appended_without_breaking_required_check(self) -> None:
        request = ClarificationAnswersRequest.model_validate(
            {
                "answers": [{"questionId": "q1", "value": "blue"}],
                "comment": "按钮要圆角，悬停时加深颜色",
            }
        )
        answers = RunExecutionService._normalize_answers(_clarification(), request)
        self.assertEqual(answers[0]["questionId"], "q1")
        self.assertEqual(answers[-1]["questionId"], "__developer_comment__")
        self.assertEqual(answers[-1]["value"], "按钮要圆角，悬停时加深颜色")

    def test_comment_alone_cannot_satisfy_required_question(self) -> None:
        request = ClarificationAnswersRequest.model_validate(
            {"comment": "随便，你决定"}
        )
        # 必填题未回答时即使写了 comment 也必须报错，避免模型在缺失
        # 关键槽位时继续执行。
        with self.assertRaises(Exception):
            RunExecutionService._normalize_answers(_clarification(), request)

    def test_comment_is_merged_into_coder_context(self) -> None:
        context = {
            "annotations": [
                {
                    "annotation": {
                        "payload": {"text": "把按钮改成蓝色"},
                        "intent": {"ui": {"summary": "把按钮改成蓝色"}},
                    }
                }
            ]
        }
        answers = [
            {"questionId": "q1", "value": "blue", "text": None},
            {
                "questionId": "__developer_comment__",
                "value": "按钮要圆角，悬停时加深颜色",
                "text": None,
            },
        ]
        updated = _augment_context_with_answers(context, answers)
        payload = updated["annotations"][0]["annotation"]["payload"]
        self.assertIn("blue", payload["text"])
        self.assertIn("按钮要圆角", payload["text"])
        # 原始上下文不能被就地修改（LangGraph state 共享）。
        self.assertEqual(
            context["annotations"][0]["annotation"]["payload"]["text"], "把按钮改成蓝色"
        )


if __name__ == "__main__":
    unittest.main()
