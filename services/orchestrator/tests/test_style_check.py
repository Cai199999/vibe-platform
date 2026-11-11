"""样式澄清（style_check）的单元测试。

覆盖用户要求的关键闭环：

    画圈新增按钮 -> 系统追问样式 -> 用户回答“和登录按钮一样”
    -> 回答写回标注上下文 -> Coder 看到明确样式约束

运行：

    docker exec vibe-python-ai python -m unittest tests.test_style_check -v
"""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from ai_core.adapters.checkpoint import MemoryCheckpointerProvider
from ai_core.agent import graph as graph_module
from ai_core.agent.graph import (
    AgentGraphRunner,
    _augment_style_answers,
    _has_style_hints,
    _style_targets,
)
from ai_core.agent.llm import MockAgentLLM
from ai_core.ports.sandbox import SandboxProvider, VerifyResult


def _annotation(text: str, action: str = "CREATE_NEW") -> dict:
    return {
        "annotationId": "ann_add_1",
        "threadId": "thread_add_1",
        "action": action,
        "type": "visual-draw",
        "intent": {
            "kind": "UI_ONLY",
            "ui": {"summary": text, "acceptance": []},
        },
        "payload": {"text": text},
        "target": {"selector": "main", "source": {"file": "src/App.jsx"}},
    }


def _context(annotation: dict) -> dict:
    return {
        "count": 1,
        "annotations": [
            {
                "annotation_id": annotation["annotationId"],
                "thread_id": annotation.get("threadId"),
                "annotation": annotation,
                "instruction": annotation["payload"]["text"],
                "ui_summary": annotation["payload"]["text"],
            }
        ],
    }


def _layout_context(text: str = "加个按钮") -> dict:
    nested = _annotation(text)
    nested["annotationId"] = "ann_add_nested"
    nested["threadId"] = "thread_nested"
    nested["intent"]["ui"]["summary"] = text
    layout = {
        "annotationId": "ann_layout_1",
        "action": "BATCH_LAYOUT",
        "payload": {
            "moves": [],
            "additions": [
                {
                    "annotationId": "ann_add_nested",
                    "intent": text,
                    "componentType": "button",
                    "annotation": nested,
                }
            ],
        },
    }
    return {
        "count": 1,
        "annotations": [
            {
                "annotation_id": "ann_layout_1",
                "thread_id": "thread_layout",
                "annotation": layout,
                "instruction": "统一提交画布变更",
                "ui_summary": "统一提交画布变更",
            }
        ],
    }


def _state(context: dict, **overrides) -> dict:
    base = {
        "run_id": "run_style",
        "annotation_context": context,
        "clarification_mode": "auto",
        "clarification_round": 0,
        "max_clarification_rounds": 3,
        "clarification_ttl_hours": 24,
        "plan": "新增一个按钮",
    }
    base.update(overrides)
    return base


class StyleHintTest(unittest.TestCase):
    def test_detects_only_explicit_style(self) -> None:
        self.assertFalse(_has_style_hints("加个按钮"))
        self.assertFalse(_has_style_hints("加一个导出按钮"))
        self.assertTrue(_has_style_hints("加一个红色按钮"))
        self.assertTrue(_has_style_hints("和登录按钮一样"))
        self.assertTrue(_has_style_hints("圆角和提交按钮保持一致"))
        self.assertTrue(_has_style_hints("用 bg-blue-600 rounded-lg"))

    def test_targets_include_nested_layout_additions(self) -> None:
        targets = _style_targets(_layout_context())
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["annotationId"], "ann_add_nested")
        self.assertEqual(targets[0]["text"], "加个按钮")


class StyleCheckNodeTest(unittest.TestCase):
    def _runner(self) -> tuple[AgentGraphRunner, list[tuple]]:
        runner = AgentGraphRunner.__new__(AgentGraphRunner)
        events: list[tuple] = []
        runner._emit = lambda event, payload, **kwargs: events.append(  # type: ignore[method-assign]
            (event, payload, kwargs)
        )
        return runner, events

    @patch.object(graph_module, "interrupt")
    def test_asks_style_then_merges_answer(self, interrupt_mock) -> None:
        interrupt_mock.return_value = [
            {"questionId": "style.appearance", "value": "和登录按钮一样"}
        ]
        runner, events = self._runner()
        state = _state(_context(_annotation("加个按钮")))

        update = runner._style_check(state)

        self.assertEqual(update["style_check_outcome"], "proceed")
        self.assertEqual(update["clarification_round"], 1)
        payload = next(
            payload for event, payload, _ in events if event == "clarification.required"
        )
        self.assertEqual(payload["reason"], "style_detail_missing")
        self.assertEqual(payload["annotationId"], "ann_add_1")
        prompt = payload["questions"][0]["prompt"]
        self.assertIn("主色还是次要色", prompt)
        self.assertIn("提交按钮一致", prompt)
        # 用户答案必须写回 annotation_context，Coder 才能看到。
        merged = update["annotation_context"]["annotations"][0]["annotation"]
        self.assertIn("和登录按钮一样", merged["payload"]["text"])
        self.assertIn("和登录按钮一样", merged["intent"]["ui"]["summary"])
        self.assertIn("和登录按钮一样", update["plan"])

    @patch.object(graph_module, "interrupt")
    def test_skips_when_style_is_already_clear(self, interrupt_mock) -> None:
        runner, _ = self._runner()
        update = runner._style_check(
            _state(_context(_annotation("加一个主色按钮，和登录按钮一样")))
        )
        self.assertEqual(update["style_check_outcome"], "skipped")
        interrupt_mock.assert_not_called()

    @patch.object(graph_module, "interrupt")
    def test_layout_addition_merges_into_nested_envelope(self, interrupt_mock) -> None:
        interrupt_mock.return_value = [
            {"questionId": "style.appearance", "value": "和现有主按钮一致"}
        ]
        runner, events = self._runner()
        update = runner._style_check(_state(_layout_context("加个按钮")))

        payload = next(
            payload for event, payload, _ in events if event == "clarification.required"
        )
        # 问题投递到嵌套新增项自己的圈，而不是整批布局信封。
        self.assertEqual(payload["annotationId"], "ann_add_nested")
        self.assertEqual(payload["threadId"], "thread_nested")

        layout = update["annotation_context"]["annotations"][0]["annotation"]
        addition = layout["payload"]["additions"][0]
        self.assertIn("和现有主按钮一致", addition["intent"])
        nested_text = addition["annotation"]["payload"]["text"]
        self.assertIn("和现有主按钮一致", nested_text)
        self.assertIn(
            "和现有主按钮一致",
            addition["annotation"]["intent"]["ui"]["summary"],
        )

    @patch.object(graph_module, "interrupt")
    def test_mode_off_never_interrupts(self, interrupt_mock) -> None:
        runner, _ = self._runner()
        update = runner._style_check(
            _state(_context(_annotation("加个按钮")), clarification_mode="off")
        )
        self.assertEqual(update["style_check_outcome"], "skipped")
        interrupt_mock.assert_not_called()

    @patch.object(graph_module, "interrupt")
    def test_exhausted_after_max_rounds(self, interrupt_mock) -> None:
        runner, _ = self._runner()
        update = runner._style_check(
            _state(_context(_annotation("加个按钮")), clarification_round=3)
        )
        self.assertEqual(update["style_check_outcome"], "exhausted")
        self.assertEqual(update["status"], "FAILED")
        self.assertEqual(update["error_code"], "STYLE_CLARIFICATION_LIMIT")
        interrupt_mock.assert_not_called()

    def test_routing(self) -> None:
        runner = AgentGraphRunner.__new__(AgentGraphRunner)
        self.assertEqual(
            runner._route_after_style_check({"style_check_outcome": "exhausted"}),
            "failed",
        )
        self.assertEqual(
            runner._route_after_style_check({"style_check_outcome": "proceed"}),
            "coder",
        )
        self.assertEqual(runner._route_after_clarify({}), "style_check")


class AugmentStyleAnswerTest(unittest.TestCase):
    def test_only_touched_targets_are_augmented(self) -> None:
        first = _context(_annotation("加个按钮"))
        second = _context(_annotation("加个按钮"))
        second["annotations"][0]["annotation_id"] = "ann_other"
        second["annotations"][0]["annotation"]["annotationId"] = "ann_other"
        first["annotations"].extend(second["annotations"])

        updated = _augment_style_answers(
            first,
            [{"questionId": "style.appearance", "value": "次要按钮"}],
            {"ann_add_1"},
        )
        touched = updated["annotations"][0]["annotation"]["payload"]["text"]
        untouched = updated["annotations"][1]["annotation"]["payload"]["text"]
        self.assertIn("次要按钮", touched)
        self.assertEqual(untouched, "加个按钮")


UI_SOURCE = """export default function App() {
  return (
    <main>
      <button className="primary">登录</button>
    </main>
  );
}
"""


class _MemorySandbox(SandboxProvider):
    """内存沙盒：E2E 只验证 Graph 编排，不构建 Docker / 不跑 npm。"""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = dict(files)
        self._lock = threading.Lock()

    def create(self, project_id, files, template="react-vite"):  # type: ignore[override]
        raise NotImplementedError

    def apply_patch(
        self,
        sandbox_id: str,
        files: dict[str, str] | None = None,
        patch: str | None = None,
        deleted_paths: list[str] | None = None,
    ) -> list[str]:
        with self._lock:
            incoming = dict(files or {})
            self.files.update(incoming)
            for path in deleted_paths or []:
                self.files.pop(path, None)
            return sorted(incoming)

    def verify(self, sandbox_id: str, check: str = "build") -> VerifyResult:
        return VerifyResult(
            success=True,
            exit_code=0,
            timed_out=False,
            duration_ms=1,
            logs="ok",
            logs_truncated=False,
        )

    def read_file(self, sandbox_id: str, path: str) -> str:
        return self.files[path]

    def export_archive(self, sandbox_id: str) -> bytes:
        return b""

    def destroy(self, sandbox_id: str) -> None:
        return None


class _CapturingLLM(MockAgentLLM):
    """复用 Mock 的确定性按钮生成，同时记录 Coder 实际收到的输入。"""

    def __init__(self) -> None:
        super().__init__(scenario="add_button")
        self.generate_calls: list[dict] = []

    def generate(self, **kwargs):  # type: ignore[override]
        self.generate_calls.append(kwargs)
        return super().generate(**kwargs)


def _e2e_state() -> dict:
    """构造一次“画圈新增按钮”的初始 State。"""
    annotation = _annotation("加个按钮")
    annotation["annotationId"] = "ann_e2e_button"
    annotation["threadId"] = "thread_e2e_button"
    return {
        "run_id": "run_style_e2e",
        "tenant_id": "tenant_e2e",
        "project_id": "project_e2e",
        "sandbox_id": "sbx_e2e",
        "target_file": "src/App.jsx",
        "target_files": ["src/App.jsx"],
        "annotation_context": _context(annotation),
        "messages": [],
        "current_code": UI_SOURCE,
        "current_files": {"src/App.jsx": UI_SOURCE},
        "plan": "",
        "generated_patch": "",
        "generated_files": {},
        "verify_result": {},
        "apply_failed": False,
        "retry_count": 0,
        "max_retries": 0,
        "history_summary": "",
        "clarification_mode": "auto",
        "max_clarification_rounds": 3,
        "confidence_threshold": 0.7,
        "clarification_round": 0,
        "clarification_answers": [],
        "clarification": None,
        "approval_required": False,
        "status": "RUNNING",
        "error": None,
        "error_code": None,
    }


class StyleClarificationE2ETest(unittest.TestCase):
    """验收场景：画圈加按钮 -> 追问 -> 用户回答 -> Coder 拿到样式约束。"""

    def test_pause_resume_merges_style_answer_into_coder_input(self) -> None:
        sandbox = _MemorySandbox({"src/App.jsx": UI_SOURCE})
        llm = _CapturingLLM()
        runner = AgentGraphRunner(
            sandbox=sandbox,
            llm=llm,
            max_retries=0,
            checkpointer_provider=MemoryCheckpointerProvider(),
        )
        state = _e2e_state()
        thread_id = "thread_style_e2e"

        # 第一段：Graph 必须在 Coder 之前停在 style_check。
        first = runner.run(state, thread_id=thread_id)
        self.assertTrue(first.interrupted)
        self.assertEqual(llm.generate_calls, [])
        payload = first.interrupt or {}
        self.assertEqual(payload.get("reason"), "style_detail_missing")
        self.assertEqual(payload.get("annotationId"), "ann_e2e_button")
        self.assertIn("主色还是次要色", payload["questions"][0]["prompt"])

        # 第二段：用户回答“和登录按钮一样”，恢复同一 Checkpoint。
        resumed = runner.run(
            state,
            thread_id=thread_id,
            resume=[{"questionId": "style.appearance", "value": "和登录按钮一样"}],
        )
        self.assertFalse(resumed.interrupted)
        self.assertEqual(resumed.state.get("status"), "SUCCEEDED")

        # Coder 收到的标注文本与 Plan 都必须带用户回答。
        call = llm.generate_calls[-1]
        annotation_text = call["annotations"][0]["payload"]["text"]
        self.assertIn("和登录按钮一样", annotation_text)
        self.assertIn("和登录按钮一样", call["plan"])
        # 新增按钮真实落盘，并带拖拽锚点。
        self.assertIn(
            'data-vibe-created="ann_e2e_button"',
            sandbox.files["src/App.jsx"],
        )


if __name__ == "__main__":
    unittest.main()
