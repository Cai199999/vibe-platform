"""可视化选型（Visual Reference）回归测试。

锁定三条契约：
  1. SDK 提取的参考组件类名 / computed CSS 必须原样穿过澄清答案；
  2. 参考样式必须写回标注 payload，并生成结构化的 reference_styles；
  3. 沙盒项目的 Tailwind / CSS 设计令牌必须能静态提取，供 Coder 复用。
"""

from __future__ import annotations

import json
import unittest

from ai_core.agent.design_system import (
    extract_design_system,
    render_design_system,
)
from ai_core.agent.graph import _augment_context_with_answers
from ai_core.agent.service import RunExecutionService
from ai_core.agent.ui_context import compact_annotations
from ai_core.domain.agent import (
    Clarification,
    ClarificationAnswersRequest,
    ClarificationQuestion,
)

REFERENCE = {
    "kind": "visual_reference",
    "label": "登录按钮",
    "tag": "button",
    "classes": ["rounded-md", "bg-blue-600", "px-4", "py-2", "text-white"],
    "styles": {
        "backgroundColor": "rgb(37, 99, 235)",
        "color": "rgb(255, 255, 255)",
        "borderRadius": "6px",
        "fontSize": "14px",
        "padding": "8px 16px",
    },
    "source": {
        "file": "src/components/LoginForm.jsx",
        "line": 42,
        "column": 9,
        "component": "LoginForm",
        "instance": 1,
        "instanceCount": 2,
    },
    "capturedAt": "2026-11-11T10:00:00Z",
}


class ClarificationReferenceTest(unittest.TestCase):
    def test_request_parsing_keeps_visual_reference(self) -> None:
        request = ClarificationAnswersRequest.model_validate(
            {
                "answers": [
                    {
                        "questionId": "style.appearance",
                        "value": "和登录按钮一样",
                        "reference": REFERENCE,
                    }
                ]
            }
        )
        self.assertEqual(request.answers[0].reference.label, "登录按钮")
        self.assertIn("bg-blue-600", request.answers[0].reference.classes)

    def test_normalize_answers_propagates_reference(self) -> None:
        clarification = Clarification(
            clarification_id="clr_1",
            questions=[
                ClarificationQuestion(
                    question_id="style.appearance",
                    type="single_choice",
                    prompt="按钮用哪种样式？",
                    required=True,
                )
            ],
        )
        request = ClarificationAnswersRequest.model_validate(
            {
                "answers": [
                    {
                        "questionId": "style.appearance",
                        "value": "和登录按钮一样",
                        "reference": REFERENCE,
                    }
                ]
            }
        )
        answers = RunExecutionService._normalize_answers(clarification, request)
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0]["reference"]["label"], "登录按钮")
        self.assertEqual(
            answers[0]["reference"]["styles"]["backgroundColor"],
            "rgb(37, 99, 235)",
        )

    def test_augment_context_writes_reference_and_summary(self) -> None:
        context = {
            "annotations": [
                {
                    "annotation": {
                        "annotationId": "ann_1",
                        "payload": {"text": "在卡片底部加一个登录按钮"},
                    }
                }
            ]
        }
        updated = _augment_context_with_answers(
            context,
            [
                {
                    "questionId": "style.appearance",
                    "value": "和登录按钮一样",
                    "text": None,
                    "reference": REFERENCE,
                }
            ],
        )
        annotation = updated["annotations"][0]["annotation"]
        payload = annotation["payload"]
        self.assertEqual(payload["reference"]["classes"][1], "bg-blue-600")
        intent = payload["finalIntent"]
        self.assertIn("在卡片底部加一个登录按钮", intent)
        self.assertIn("参考「登录按钮」", intent)
        self.assertIn("bg-blue-600", intent)
        # 原始上下文不能被就地修改，避免污染历史检查点。
        self.assertNotIn(
            "reference", context["annotations"][0]["annotation"]["payload"]
        )

    def test_compact_annotations_emits_reference_styles(self) -> None:
        compacted = compact_annotations(
            [
                {
                    "annotationId": "ann_1",
                    "action": "ADD",
                    "target": {
                        "selector": '[data-source="card"]',
                        "source": {
                            "file": "src/App.jsx",
                            "component": "App",
                            "styleSignature": {
                                "backgroundColor": "rgb(15, 23, 42)",
                                "borderRadius": "12px",
                                "tailwindClasses": ["rounded-xl", "bg-slate-900"],
                            },
                        },
                    },
                    "payload": {
                        "finalIntent": "加一个提交按钮",
                        "reference": REFERENCE,
                    },
                }
            ]
        )
        item = compacted[0]
        self.assertEqual(
            item["reference_styles"]["backgroundColor"], "rgb(37, 99, 235)"
        )
        self.assertEqual(item["reference_styles"]["borderRadius"], "6px")
        self.assertIn("bg-blue-600", item["reference_styles"]["tailwindClasses"])
        self.assertEqual(item["reference"]["label"], "登录按钮")
        self.assertEqual(
            item["reference"]["source"]["file"], "src/components/LoginForm.jsx"
        )
        # 目标元素当前样式同样必须暴露，模型不能凭空推算“更圆 / 更大”。
        self.assertEqual(
            item["current_styles"]["borderRadius"],
            "12px",
        )
        self.assertIn("rounded-xl", item["current_styles"]["tailwindClasses"])
        self.assertNotIn("capturedAt", json.dumps(item, ensure_ascii=False))


class DesignSystemTest(unittest.TestCase):
    def test_extract_tailwind_colors_and_css_variables(self) -> None:
        design = extract_design_system(
            {
                "tailwind.config.js": (
                    "module.exports = { theme: { extend: { colors: { "
                    "primary: '#2563eb', "
                    "secondary: { DEFAULT: '#0f172a', foreground: '#ffffff' } "
                    "} } } }"
                ),
                "src/index.css": (
                    "@tailwind base;\n"
                    ":root { --primary: #2563eb; --secondary: #64748b; "
                    "--space-2: 8px; }\n"
                ),
            }
        )
        self.assertTrue(design["tailwind"])
        self.assertEqual(design["colors"]["primary"], "#2563eb")
        self.assertEqual(design["colors"]["secondary"], "#0f172a")
        self.assertEqual(design["colors"]["secondary-foreground"], "#ffffff")
        self.assertEqual(design["cssVariables"]["--primary"], "#2563eb")
        # 非颜色变量不能混入 Prompt。
        self.assertNotIn("--space-2", design["cssVariables"])
        self.assertIn("bg-primary", design["suggestedClasses"])
        self.assertIn("text-[var(--primary)]", design["suggestedClasses"])

    def test_empty_design_system_returns_empty_text(self) -> None:
        self.assertEqual(extract_design_system({}), {})
        self.assertEqual(render_design_system({}), "")
        text = render_design_system(
            {
                "tailwind": True,
                "colors": {"primary": "#2563eb"},
                "cssVariables": {},
                "suggestedClasses": ["bg-primary"],
            }
        )
        self.assertIn("主题色: primary=#2563eb", text)
        self.assertIn("语义类名: bg-primary", text)


if __name__ == "__main__":
    unittest.main()
