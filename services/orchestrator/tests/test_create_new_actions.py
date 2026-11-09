"""CREATE_NEW / MODIFY_EXISTING 与拖拽定位回写的回归测试。

覆盖链路：
  1. 旧 ADD / MODIFY 输入归一化为新动作语义；
  2. CREATE_NEW 只生成组件主体，不写死绝对坐标，并打上
     data-vibe-created="<annotationId>" 标记；
  3. 用户拖拽确认后的 payload.position 由 Coder 写成 absolute 定位，
     同时给父容器补 position:relative。
"""

from __future__ import annotations

import unittest

from ai_core.agent.llm import MockAgentLLM
from ai_core.domain.actions import (
    CREATE_NEW,
    MODIFY_EXISTING,
    is_create_action,
    normalize_action,
)
from ai_core.domain.agent import AnnotationEnvelope

SOURCE = (
    "export default function App() {\n"
    '  return <main className="page" data-source="page">\n'
    '    <section data-source="card" data-source-kind="container" />\n'
    "  </main>\n"
    "}\n"
)


def _annotation(**overrides):
    payload = {
        "annotationId": "ann_new",
        "action": CREATE_NEW,
        "target": {
            "mode": "source",
            "source": {"file": "src/App.jsx", "line": 3},
            "selector": '[data-source="card"]',
        },
        "intent": {
            "kind": "UI_ONLY",
            "ui": {"summary": "在卡片里加一个立即购买按钮"},
        },
        "payload": {"text": "在卡片里加一个立即购买按钮"},
    }
    payload.update(overrides)
    return payload


class ActionSemanticsTest(unittest.TestCase):
    def test_legacy_actions_are_normalized(self) -> None:
        self.assertEqual(normalize_action("ADD"), CREATE_NEW)
        self.assertEqual(normalize_action("CREATE_NEW"), CREATE_NEW)
        self.assertEqual(normalize_action("MODIFY"), MODIFY_EXISTING)
        self.assertEqual(normalize_action("MODIFY_EXISTING"), MODIFY_EXISTING)
        self.assertTrue(is_create_action("add"))

    def test_annotation_envelope_exposes_canonical_intent(self) -> None:
        annotation = AnnotationEnvelope.model_validate(
            {
                "annotationId": "ann_new",
                "action": "ADD",
                "target": {
                    "mode": "source",
                    "source": {"file": "src/App.jsx", "line": 1},
                },
            }
        )
        self.assertEqual(annotation.intent_action, CREATE_NEW)
        self.assertTrue(annotation.is_create_new)
        self.assertFalse(annotation.is_modify_existing)


class MockCreateNewFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.llm = MockAgentLLM(scenario="none")

    def test_create_new_renders_body_with_marker_not_absolute_position(self) -> None:
        result = self.llm._apply_instruction(SOURCE, _annotation())

        self.assertIn('data-vibe-created="ann_new"', result)
        # CREATE_NEW 阶段不得写死 left/top；位置等用户拖拽后再回写。
        self.assertNotIn("left:", result)
        self.assertNotIn("top:", result)

    def test_drag_position_is_written_as_absolute_patch(self) -> None:
        generated = self.llm._apply_instruction(SOURCE, _annotation())
        patched = self.llm._apply_instruction(
            generated,
            _annotation(
                action=MODIFY_EXISTING,
                intent={
                    "kind": "UI_ONLY",
                    "ui": {"summary": "将新增组件定位到拖拽位置"},
                },
                payload={
                    "text": "将新增组件定位到拖拽位置",
                    "position": {
                        "x": 120,
                        "y": 64,
                        "selector": '[data-vibe-created="ann_new"]',
                        "parentSelector": '[data-source="card"]',
                    },
                },
            ),
        )

        self.assertIn('[data-source="card"]{position:relative !important;}', patched)
        self.assertIn(
            '[data-vibe-created="ann_new"]{position:absolute !important;',
            patched,
        )
        self.assertIn("left:120.00px !important", patched)
        self.assertIn("top:64.00px !important", patched)


if __name__ == "__main__":
    unittest.main()
