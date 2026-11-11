"""实例作用域与最终意图回归测试。

覆盖本轮修复的三个关键缺陷：

1. Applier 必须拒绝“为单个实例改基础组件”的补丁；
2. 多轮澄清只保留最后一轮意图（红色→蓝色不得并存）；
3. Coder Prompt 只看到 finalIntent，历史聊天记录不得进入 UI 上下文。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from ai_core.agent.graph import (
    AgentGraphRunner,
    _augment_context_with_answers,
    _augment_style_answers,
    _instance_scope_violation,
    _latest_answers,
)
from ai_core.agent.ui_context import build_ui_coder_context

APP_FILE = "src/App.jsx"
BUTTON_FILE = "src/components/Button.jsx"


def _numbered(lines: int) -> str:
    return "\n".join(f"// line {index}" for index in range(1, lines + 1))


def _replace_line(source: str, line_no: int, content: str) -> str:
    lines = source.splitlines()
    lines[line_no - 1] = content
    return "\n".join(lines)


def _context(annotation: dict, *, wrapped: bool = True) -> dict:
    row = {
        "annotation_id": annotation.get("annotationId"),
        "thread_id": annotation.get("threadId"),
        "annotation": annotation,
        "instruction": ((annotation.get("payload") or {}).get("text") or ""),
        "ui_summary": ((annotation.get("payload") or {}).get("text") or ""),
    }
    return {"count": 1, "annotations": [row if wrapped else row["annotation"]]}


def _annotation(
    *,
    source_file: str,
    source_line: int,
    action: str = "MODIFY_EXISTING",
    instance: int = 1,
    instance_count: int = 1,
    call_site_file: str | None = None,
    call_site_line: int = 0,
    instance_key: str | None = None,
    instance_props: dict | None = None,
    text: str = "把按钮改成蓝色",
) -> dict:
    source = {
        "file": source_file,
        "line": source_line,
        "column": 3,
        "component": "Button",
        "instance": instance,
        "instanceCount": instance_count,
        "instanceId": f"{source_file}:{source_line}:3#{instance}",
    }
    if instance_key:
        source["instanceKey"] = instance_key
    if instance_props:
        source["instanceProps"] = instance_props
    if call_site_file:
        source["callSite"] = {
            "file": call_site_file,
            "line": call_site_line,
            "column": 5,
            "component": "Dashboard",
        }
    return {
        "annotationId": "ann_instance_1",
        "threadId": "thread_instance_1",
        "action": action,
        "target": {"selector": "[data-source='button']", "source": source},
        "payload": {"text": text},
        "intent": {"ui": {"summary": text, "acceptance": []}},
    }


def _state(annotation: dict, current_files: dict[str, str]) -> dict:
    return {
        "run_id": "run_instance",
        "annotation_context": _context(annotation),
        "current_files": current_files,
    }


class InstanceScopeGuardTest(unittest.TestCase):
    def test_rejects_violation_when_base_component_is_edited(self) -> None:
        old_button = _numbered(12)
        new_button = _replace_line(old_button, 4, "// 新样式：只有第 2 个实例需要")
        annotation = _annotation(
            source_file=BUTTON_FILE,
            source_line=4,
            instance=2,
            instance_count=2,
            call_site_file=APP_FILE,
            call_site_line=18,
        )
        state = _state(annotation, {BUTTON_FILE: old_button, APP_FILE: _numbered(30)})

        violation = _instance_scope_violation(state, {BUTTON_FILE: new_button}, None)

        self.assertIsNotNone(violation)
        assert violation is not None
        self.assertTrue(violation["apply_failed"])
        self.assertEqual(violation["error_code"], "INSTANCE_SCOPE_VIOLATION")
        self.assertIn("callSite", violation["messages"][0]["content"])

    def test_single_instance_base_edit_is_still_rejected(self) -> None:
        old_button = _numbered(12)
        new_button = _replace_line(old_button, 4, "// 唯一的卡片实例")
        annotation = _annotation(
            source_file=BUTTON_FILE,
            source_line=4,
            instance=1,
            instance_count=1,
            call_site_file=APP_FILE,
            call_site_line=18,
        )
        state = _state(annotation, {BUTTON_FILE: old_button, APP_FILE: _numbered(30)})

        violation = _instance_scope_violation(state, {BUTTON_FILE: new_button}, None)
        self.assertIsNotNone(violation)

    def test_allows_call_site_parent_edit_with_instance_guard(self) -> None:
        old_app = _numbered(30)
        new_app = _replace_line(
            old_app,
            18,
            "      {item.id === 'card-2' && <Button variant=\"primary\" />}",
        )
        annotation = _annotation(
            source_file=BUTTON_FILE,
            source_line=4,
            instance=2,
            instance_count=2,
            call_site_file=APP_FILE,
            call_site_line=18,
            instance_props={"item.id": "card-2"},
        )
        state = _state(annotation, {BUTTON_FILE: _numbered(12), APP_FILE: old_app})

        violation = _instance_scope_violation(state, {APP_FILE: new_app}, None)

        self.assertIsNone(violation)

    def test_rejects_unguarded_map_callsite_insertion(self) -> None:
        """在共享 map 返回体里无条件插按钮，会让所有容器都出现按钮。"""
        old_app = _numbered(30)
        new_app = _replace_line(old_app, 18, '      <Button variant="primary" />')
        annotation = _annotation(
            source_file=BUTTON_FILE,
            source_line=4,
            action="CREATE_NEW",
            instance=2,
            instance_count=2,
            call_site_file=APP_FILE,
            call_site_line=18,
            instance_props={"item.id": "card-2"},
        )
        state = _state(annotation, {BUTTON_FILE: _numbered(12), APP_FILE: old_app})

        violation = _instance_scope_violation(state, {APP_FILE: new_app}, None)

        self.assertIsNotNone(violation)
        assert violation is not None
        self.assertTrue(violation["apply_failed"])
        self.assertEqual(violation["error_code"], "INSTANCE_GUARD_MISSING")
        self.assertIn("card-2", violation["messages"][0]["content"])

    def test_rejects_unguarded_diff_mode_insertion(self) -> None:
        """diff 模式（generated_files 为空）也必须读取 + 行做守卫校验。"""
        old_app = _numbered(30)
        annotation = _annotation(
            source_file=BUTTON_FILE,
            source_line=4,
            action="CREATE_NEW",
            instance=2,
            instance_count=2,
            call_site_file=APP_FILE,
            call_site_line=18,
            instance_props={"item.id": "card-2"},
        )
        state = _state(annotation, {BUTTON_FILE: _numbered(12), APP_FILE: old_app})
        patch = (
            "--- a/src/App.jsx\n"
            "+++ b/src/App.jsx\n"
            "@@ -18,1 +18,1 @@\n"
            '-      <Button variant="secondary" />\n'
            '+      <Button variant="primary" />\n'
        )

        violation = _instance_scope_violation(state, {}, patch)

        self.assertIsNotNone(violation)
        assert violation is not None
        self.assertEqual(violation["error_code"], "INSTANCE_GUARD_MISSING")

    def test_allows_map_callsite_insertion_guarded_by_index(self) -> None:
        old_app = _numbered(30)
        new_app = _replace_line(
            old_app,
            18,
            '      {index === 1 && <Button variant="primary" />}',
        )
        annotation = _annotation(
            source_file=BUTTON_FILE,
            source_line=4,
            action="CREATE_NEW",
            instance=2,
            instance_count=2,
            call_site_file=APP_FILE,
            call_site_line=18,
        )
        state = _state(annotation, {BUTTON_FILE: _numbered(12), APP_FILE: old_app})

        violation = _instance_scope_violation(state, {APP_FILE: new_app}, None)

        self.assertIsNone(violation)

    def test_same_file_only_call_site_line_is_allowed(self) -> None:
        old_app = _numbered(40)
        annotation = _annotation(
            source_file=APP_FILE,
            source_line=20,
            instance=2,
            instance_count=2,
            call_site_file=APP_FILE,
            call_site_line=8,
        )
        state = _state(annotation, {APP_FILE: old_app})

        # 改 callSite 附近且带实例守卫：允许（局部变体 / 条件渲染）。
        near_call_site = _replace_line(
            old_app, 8, '  {index === 1 && <Button variant="outline" />}'
        )
        self.assertIsNone(
            _instance_scope_violation(state, {APP_FILE: near_call_site}, None)
        )

        # 改组件主体：拒绝（会影响所有实例）。
        body_edit = _replace_line(old_app, 20, "  const shared = 'global-style'")
        violation = _instance_scope_violation(state, {APP_FILE: body_edit}, None)
        self.assertIsNotNone(violation)

    def test_nested_layout_addition_is_checked(self) -> None:
        nested = _annotation(
            source_file=BUTTON_FILE,
            source_line=4,
            action="CREATE_NEW",
            instance=1,
            instance_count=2,
            call_site_file=APP_FILE,
            call_site_line=18,
        )
        layout = {
            "annotationId": "ann_layout_1",
            "action": "BATCH_LAYOUT",
            "payload": {
                "moves": [],
                "additions": [
                    {
                        "annotationId": "ann_instance_1",
                        "intent": "加个按钮",
                        "annotation": nested,
                    }
                ],
            },
        }
        old_button = _numbered(12)
        state = {
            "run_id": "run_layout",
            "annotation_context": _context(layout),
            "current_files": {BUTTON_FILE: old_button, APP_FILE: _numbered(30)},
        }

        violation = _instance_scope_violation(
            state,
            {BUTTON_FILE: _replace_line(old_button, 4, "// 改动基础按钮")},
            None,
        )

        self.assertIsNotNone(violation)

    def test_plain_list_item_without_call_site_requires_guard(self) -> None:
        """根组件直接 map 渲染时 callSite 为空，但多实例仍需实例守卫。"""
        old_app = _numbered(30)
        annotation = _annotation(
            source_file=APP_FILE,
            source_line=12,
            instance=3,
            instance_count=5,
            call_site_file=None,
        )
        state = _state(annotation, {APP_FILE: old_app})

        edit = _replace_line(old_app, 12, "        <li>条件渲染后的文本</li>")
        violation = _instance_scope_violation(state, {APP_FILE: edit}, None)
        self.assertIsNotNone(violation)
        assert violation is not None
        self.assertEqual(violation["error_code"], "INSTANCE_GUARD_MISSING")

        guarded = _replace_line(
            old_app,
            12,
            '        <li>{index === 2 && "条件渲染后的文本"}</li>',
        )
        self.assertIsNone(_instance_scope_violation(state, {APP_FILE: guarded}, None))


class FinalIntentTest(unittest.TestCase):
    def test_latest_answer_wins_for_same_question(self) -> None:
        answers = [
            {"questionId": "q_color", "value": "红色"},
            {"questionId": "q_size", "value": "大一点"},
            {"questionId": "q_color", "value": "蓝色"},
        ]
        latest = _latest_answers(answers)
        self.assertEqual(len(latest), 2)
        self.assertEqual(latest[0]["value"], "蓝色")
        self.assertEqual(latest[1]["value"], "大一点")

    def test_clarification_overwrites_old_color(self) -> None:
        annotation = _annotation(
            source_file=APP_FILE,
            source_line=12,
            text="把按钮改成红色",
        )
        context = _context(annotation)

        updated = _augment_context_with_answers(
            context,
            [
                {"questionId": "q_color", "value": "红色"},
                {"questionId": "q_color", "value": "蓝色"},
            ],
        )

        payload = updated["annotations"][0]["annotation"]["payload"]
        self.assertNotIn("红色", payload["text"])
        self.assertIn("蓝色", payload["text"])
        self.assertEqual(
            payload["finalIntent"],
            updated["annotations"][0]["annotation"]["intent"]["ui"]["summary"],
        )
        # 原始上下文保持不可变（LangGraph 共享 state）。
        self.assertIn(
            "红色", context["annotations"][0]["annotation"]["payload"]["text"]
        )

    def test_style_answers_do_not_stack(self) -> None:
        annotation = _annotation(
            source_file=APP_FILE,
            source_line=12,
            action="CREATE_NEW",
            text="加个按钮（样式要求：红色）",
        )
        context = _context(annotation)

        updated = _augment_style_answers(
            context,
            [
                {"questionId": "style.appearance", "value": "红色"},
                {"questionId": "style.appearance", "value": "蓝色"},
            ],
            {"ann_instance_1"},
        )

        text = updated["annotations"][0]["annotation"]["payload"]["text"]
        self.assertIn("蓝色", text)
        self.assertNotIn("红色", text)

    def test_style_check_plan_replaces_previous_constraint(self) -> None:
        annotation = _annotation(
            source_file=APP_FILE,
            source_line=12,
            action="CREATE_NEW",
            text="加个按钮",
        )
        state = {
            **_state(annotation, {APP_FILE: _numbered(30)}),
            "plan": "新增按钮\n\n视觉样式约束（必须严格复用现有组件类名）：红色",
            "clarification_mode": "auto",
            "clarification_round": 0,
            "max_clarification_rounds": 3,
        }
        runner = AgentGraphRunner.__new__(AgentGraphRunner)
        runner._emit = lambda *args, **kwargs: None

        with patch(
            "ai_core.agent.graph.interrupt",
            return_value=[{"questionId": "style.appearance", "value": "蓝色"}],
        ):
            update = runner._style_check(state)

        self.assertIn("蓝色", update["plan"])
        self.assertNotIn("红色", update["plan"])


class CoderHistoryIsolationTest(unittest.TestCase):
    def test_coder_context_without_history_drops_old_messages(self) -> None:
        annotation = {
            "annotationId": "ann_1",
            "action": "MODIFY_EXISTING",
            "target": {
                "selector": "main",
                "source": {
                    "file": APP_FILE,
                    "line": 3,
                    "column": 1,
                    "instance": 2,
                    "instanceCount": 3,
                    "instanceId": f"{APP_FILE}:3:1#2",
                    "callSite": {"file": APP_FILE, "line": 8, "component": "App"},
                },
            },
            "payload": {"text": "改成蓝色", "finalIntent": "改成蓝色"},
            "threadContext": {
                "chatHistory": [
                    {"role": "user", "content": "把按钮改成红色"},
                    {"role": "assistant", "content": "已改成红色"},
                ]
            },
        }
        source = "export default function App() {\n  return <main />\n}\n"

        compact = build_ui_coder_context(
            {APP_FILE: source},
            [APP_FILE],
            [annotation],
            include_history=False,
        )

        self.assertNotIn("红色", compact.annotations_text)
        self.assertNotIn("chatHistory", compact.annotations_text)
        self.assertIn("蓝色", compact.annotations_text)
        self.assertIn("instanceCount", compact.annotations_text)
        self.assertIn("callSite", compact.annotations_text)

        with_history = build_ui_coder_context(
            {APP_FILE: source},
            [APP_FILE],
            [annotation],
            include_history=True,
        )
        self.assertIn("把按钮改成红色", with_history.annotations_text)


if __name__ == "__main__":
    unittest.main()
