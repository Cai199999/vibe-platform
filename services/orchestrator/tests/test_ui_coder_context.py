"""FrontendCoder 职责解耦与上下文降级回归测试。

锁定三条契约：
  1. UI Coder 只接收标注附近的 JSX 窗口，绝不整文件倾倒；
  2. 业务 / API / DDL 字段不得进入 UI Coder 上下文；
  3. 上下文被裁剪后强制 mode=diff；超时先切换 FAST 模型精简重试，
     仍失败则返回可恢复的 ``CODER_TIMEOUT_RECOVERABLE`` 供前端转后台。
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any

from ai_core.agent.graph import (
    CODER_FALLBACK_PROGRESS,
    AgentGraphRunner,
    CoderTimeoutError,
)
from ai_core.agent.llm import CodeResult, DeepSeekAgentLLM
from ai_core.agent.ui_context import (
    build_ui_coder_context,
    compact_annotations,
    extract_jsx_window,
)
from ai_core.config import Settings
from ai_core.ports.model import ModelClient

APP_PATH = "src/App.jsx"


def _big_source() -> str:
    """构造超过整文件阈值、目标位于中段的 JSX。"""
    padding_top = "\n".join(
        f'      <p className="noise-top-{index}">顶部噪声内容顶部噪声内容</p>'
        for index in range(220)
    )
    padding_bottom = "\n".join(
        f'      <p className="noise-bottom-{index}">底部噪声内容底部噪声内容</p>'
        for index in range(220)
    )
    return (
        "export default function App() {\n"
        '  return <div className="page" data-source="page">\n'
        '    <div className="far-away">FAR_AWAY_MARKER</div>\n'
        '    <main className="shell" data-source="shell">\n'
        f"{padding_top}\n"
        '      <section className="board" data-source="board">\n'
        '        <div className="card" data-source="card">\n'
        "          <h2>卡片标题</h2>\n"
        "          <p>卡片说明</p>\n"
        "        </div>\n"
        '        <div className="sibling" data-source="sibling">兄弟节点</div>\n'
        "      </section>\n"
        f"{padding_bottom}\n"
        "    </main>\n"
        "  </div>\n"
        "}\n"
    )


class UiContextWindowTest(unittest.TestCase):
    def test_small_file_is_sent_in_full(self) -> None:
        source = (
            "export default function App() {\n"
            '  return <main data-source="card"><h1>标题</h1></main>\n'
            "}\n"
        )
        context = build_ui_coder_context(
            {APP_PATH: source},
            [APP_PATH],
            [
                {
                    "annotationId": "ann_1",
                    "action": "MODIFY_EXISTING",
                    "target": {
                        "selector": '[data-source="card"]',
                        "source": {"file": APP_PATH},
                    },
                    "payload": {"text": "标题改红"},
                }
            ],
        )
        self.assertFalse(context.force_diff)
        self.assertIn("标题", context.files_text)
        self.assertEqual(context.included_files, (APP_PATH,))

    def test_large_file_keeps_target_ancestors_and_siblings_only(self) -> None:
        source = _big_source()
        window = extract_jsx_window(
            source,
            selector='[data-source="card"]',
            max_lines=90,
            max_tokens=1200,
        )
        self.assertTrue(window.truncated)
        # 目标、父容器与同层兄弟必须保留。
        self.assertIn('data-source="card"', window.text)
        self.assertIn("兄弟节点", window.text)
        self.assertIn('data-source="board"', window.text)
        # 远离目标的噪声与文件头标记必须被裁掉。
        self.assertNotIn("FAR_AWAY_MARKER", window.text)
        self.assertLess(
            window.end_line - window.start_line + 1, len(source.splitlines())
        )

    def test_empty_selector_falls_back_to_main_content(self) -> None:
        """真实画布 selector 为空时必须锚定主内容区，而不是文件头。"""
        source = (
            "import { Plus } from 'lucide-react'\n"
            + "".join(
                f'const HEADER_{index} = "文件头噪声内容文件头噪声内容{index}"\n'
                for index in range(320)
            )
            + "export default function App() {\n"
            '  return <div className="page" data-source="page">\n'
            '    <main className="shell" data-source="app-main">\n'
            "      <h1>工作台</h1>\n"
            "    </main>\n"
            "  </div>\n"
            "}\n"
        )
        window = extract_jsx_window(source, selector="", max_lines=90)
        self.assertIn('data-source="app-main"', window.text)
        self.assertNotIn("HEADER_0", window.text)

    def test_explicit_line_wins_over_structural_fallback(self) -> None:
        """SDK 精确行号必须优先于 selector / 结构兜底。"""
        source = _big_source()
        window = extract_jsx_window(
            source,
            selector="",
            line=50,
            max_lines=40,
            max_tokens=1200,
        )
        self.assertLessEqual(window.start_line, 50)
        self.assertGreaterEqual(window.end_line, 50)
        self.assertNotIn('data-source="card"', window.text)

    def test_large_file_forces_local_diff(self) -> None:
        context = build_ui_coder_context(
            {APP_PATH: _big_source()},
            [APP_PATH],
            [
                {
                    "annotationId": "ann_1",
                    "action": "MODIFY_EXISTING",
                    "target": {
                        "selector": '[data-source="card"]',
                        "source": {"file": APP_PATH},
                    },
                    "payload": {"text": "把卡片标题改成红色"},
                }
            ],
            budget_tokens=2600,
            full_file_token_limit=900,
            diff_token_threshold=1800,
        )
        self.assertTrue(context.force_diff)
        self.assertNotIn("noise-bottom-219", context.files_text)


class UiContextBusinessIsolationTest(unittest.TestCase):
    def test_compact_annotations_drops_business_payload(self) -> None:
        compacted = compact_annotations(
            [
                {
                    "annotationId": "ann_1",
                    "action": "ADD",
                    "target": {
                        "selector": '[data-source="card"]',
                        "source": {"file": APP_PATH, "component": "Card"},
                    },
                    "payload": {"text": "加一个提交按钮"},
                    "intent": {
                        "kind": "UI_ONLY",
                        "ui": {
                            "summary": "卡片底部加提交按钮",
                            "acceptance": ["按钮可见"],
                        },
                        "business": {"flow": "订单提交"},
                    },
                    "business": {"rule": "库存扣减"},
                    "apiContract": {"path": "/api/orders", "method": "POST"},
                    "ddl": "CREATE TABLE orders (id INT);",
                    "relativeTarget": {"relativeX": 12.5, "parentWidth": 320},
                }
            ]
        )
        text = json.dumps(compacted, ensure_ascii=False)
        self.assertIn("加一个提交按钮", text)
        self.assertIn("relativeX", text)
        for forbidden in ("/api/orders", "CREATE TABLE", "库存扣减", "订单提交"):
            self.assertNotIn(forbidden, text)


class _CaptureClient(ModelClient):
    def __init__(self, response: str) -> None:
        self.response = response
        self.tasks: list[str] = []
        self.system = ""
        self.human = ""

    def generate(self, task: str, system: str, human: str) -> str:
        self.tasks.append(task)
        self.system = system
        self.human = human
        return self.response


class UiCoderPromptTest(unittest.TestCase):
    def _llm(self, response: str) -> tuple[DeepSeekAgentLLM, _CaptureClient]:
        client = _CaptureClient(response)
        settings = Settings(deepseek_api_key="test-key", agent_json_retry_limit=0)
        return DeepSeekAgentLLM(settings, client=client), client

    def test_prompt_is_visual_only_and_uses_placeholder_click(self) -> None:
        llm, client = self._llm(
            json.dumps(
                {
                    "mode": "files",
                    "files": {
                        APP_PATH: (
                            "export default function App() {\n"
                            '  return <main><button data-vibe-created="ann_1" '
                            "onClick={() => "
                            "console.log('待绑定逻辑')}>提交</button></main>\n"
                            "}\n"
                        )
                    },
                    "summary": "新增提交按钮视觉占位",
                },
                ensure_ascii=False,
            )
        )
        result = llm.generate(
            annotations=[
                {
                    "annotationId": "ann_1",
                    "action": "ADD",
                    "target": {
                        "selector": '[data-source="card"]',
                        "source": {"file": APP_PATH},
                    },
                    "payload": {"text": "加一个提交按钮"},
                    "apiContract": {"path": "/api/orders"},
                }
            ],
            plan="在卡片底部加按钮",
            current_files={
                APP_PATH: (
                    "export default function App() {\n"
                    '  return <main data-source="card"><h2>标题</h2></main>\n'
                    "}\n"
                )
            },
            target_files=[APP_PATH],
            verify_result={},
            retry_count=0,
        )
        self.assertEqual(result.files[APP_PATH].count("待绑定逻辑"), 1)
        self.assertIn("视觉与 DOM", client.system)
        self.assertIn("TODO(interaction)", client.system)
        self.assertIn("待绑定逻辑", client.system)
        # 业务契约绝不能进入 UI Coder 的上下文。
        self.assertNotIn("/api/orders", client.human)
        self.assertNotIn("/api/orders", client.system)

    def test_create_patch_with_only_import_is_rejected(self) -> None:
        """复现真实事故：ADD 只返回 import，必须判为未完成。"""
        llm, _ = self._llm(
            json.dumps(
                {
                    "mode": "diff",
                    "patch": (
                        "--- a/src/App.jsx\n"
                        "+++ b/src/App.jsx\n"
                        "@@ -1,3 +1,4 @@\n"
                        " import { useMemo, useState } from 'react'\n"
                        "+import { Plus } from 'lucide-react'\n"
                        " \n"
                        " export default function App() {\n"
                    ),
                    "summary": "已应用：添加按钮",
                },
                ensure_ascii=False,
            )
        )
        with self.assertRaises(Exception) as ctx:
            llm.generate(
                annotations=[
                    {
                        "annotationId": "ann_b870aabe750a",
                        "action": "ADD",
                        "target": {
                            "selector": "",
                            "source": {"file": APP_PATH},
                        },
                        "payload": {"text": "添加按钮"},
                    }
                ],
                plan="在页面添加按钮",
                current_files={
                    APP_PATH: (
                        "import { useMemo, useState } from 'react'\n"
                        "\n"
                        "export default function App() {\n"
                        "  return <main>页面</main>\n"
                        "}\n"
                    )
                },
                target_files=[APP_PATH],
                verify_result={},
                retry_count=0,
                compact=True,
                prefer_fast=True,
            )
        self.assertIn("data-vibe-created", str(ctx.exception))

    def test_compact_mode_selects_fast_model_and_forbids_full_files(self) -> None:
        llm, client = self._llm(
            json.dumps(
                {
                    "mode": "files",
                    "files": {APP_PATH: "<main />"},
                    "summary": "试图整文件重写",
                }
            )
        )
        with self.assertRaises(Exception) as ctx:
            llm.generate(
                annotations=[
                    {
                        "annotationId": "ann_1",
                        "payload": {"text": "加按钮"},
                        "target": {"source": {"file": APP_PATH}},
                    }
                ],
                plan="加按钮",
                current_files={APP_PATH: "<main />"},
                target_files=[APP_PATH],
                verify_result={},
                retry_count=0,
                compact=True,
                prefer_fast=True,
            )
        self.assertIn("只允许 mode=diff", str(ctx.exception))
        # 降级重试必须走 FAST 模型，且强制 diff 规则进入 Prompt。
        self.assertEqual(client.tasks, ["coder_fast"])
        self.assertIn("只允许 mode=diff", client.system)


class _TimeoutPlan:
    """记录 generate kwargs，按预设序列抛超时 / 返回结果。"""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class CoderFallbackTest(unittest.TestCase):
    @staticmethod
    def _runner() -> AgentGraphRunner:
        runner = object.__new__(AgentGraphRunner)
        runner._settings = SimpleNamespace(
            agent_coder_timeout_seconds=30.0,
            agent_coder_retry_timeout_seconds=20.0,
            agent_coder_background_timeout_seconds=240.0,
        )
        runner._toolbox = {}
        runner.events = []

        def emit(event_type: str, payload: dict, **kwargs: Any) -> None:
            runner.events.append((event_type, payload))

        runner._emit = emit  # type: ignore[method-assign]
        return runner

    @staticmethod
    def _state(**overrides: Any) -> dict[str, Any]:
        state = {
            "run_id": "run_1",
            "annotation_context": {
                "annotations": [
                    {
                        "annotation": {
                            "annotationId": "ann_1",
                            "action": "MODIFY_EXISTING",
                            "payload": {"text": "标题改红"},
                            "target": {"source": {"file": APP_PATH}},
                        }
                    }
                ]
            },
            "current_files": {APP_PATH: "<main />"},
            "target_files": [APP_PATH],
            "verify_result": {},
            "retry_count": 0,
            "coder_timeout_seconds": 30.0,
            "background_run": False,
            "clarification_round": 0,
        }
        state.update(overrides)
        return state

    def test_first_timeout_retries_with_compact_fast_context(self) -> None:
        runner = self._runner()
        plan = _TimeoutPlan(
            [
                CoderTimeoutError("timeout"),
                CodeResult(files={}, patch="", summary="降级成功"),
            ]
        )
        runner._generate_with_coder_timeout = plan  # type: ignore[method-assign]
        update = runner._coder(self._state())

        self.assertTrue(update["coder_fallback_used"])
        self.assertFalse(update["apply_failed"])
        self.assertEqual(len(plan.calls), 2)
        self.assertFalse(plan.calls[0]["compact"])
        self.assertFalse(plan.calls[0]["prefer_fast"])
        self.assertEqual(plan.calls[0]["timeout_seconds"], 30.0)
        # 第二次使用精简上下文 + FAST 模型 + 更短预算。
        self.assertTrue(plan.calls[1]["compact"])
        self.assertTrue(plan.calls[1]["prefer_fast"])
        self.assertEqual(plan.calls[1]["timeout_seconds"], 20.0)
        self.assertTrue(
            any(
                event_type == "node.progress"
                and payload.get("message") == CODER_FALLBACK_PROGRESS
                for event_type, payload in runner.events
            )
        )

    def test_double_timeout_returns_recoverable_background_option(self) -> None:
        runner = self._runner()
        plan = _TimeoutPlan(
            [CoderTimeoutError("timeout"), CoderTimeoutError("timeout again")]
        )
        runner._generate_with_coder_timeout = plan  # type: ignore[method-assign]
        update = runner._coder(self._state())

        self.assertEqual(update["error_code"], "CODER_TIMEOUT_RECOVERABLE")
        self.assertTrue(update["background_available"])
        self.assertTrue(update["fatal_error"])
        self.assertEqual(update["status"], "FAILED")
        self.assertEqual(len(plan.calls), 2)

    def test_background_run_uses_long_timeout_without_fast_retry(self) -> None:
        runner = self._runner()
        plan = _TimeoutPlan([CoderTimeoutError("timeout")])
        runner._generate_with_coder_timeout = plan  # type: ignore[method-assign]
        update = runner._coder(
            self._state(
                background_run=True,
                coder_timeout_seconds=240.0,
            )
        )

        self.assertEqual(plan.calls[0]["timeout_seconds"], 240.0)
        self.assertEqual(len(plan.calls), 1)
        self.assertTrue(update["background_available"])
        self.assertEqual(update["error_code"], "CODER_TIMEOUT_RECOVERABLE")


class VerifierCreatedMarkerTest(unittest.TestCase):
    """构建通过但新增组件缺失时，Verifier 必须判失败。"""

    @staticmethod
    def _runner(content: str) -> AgentGraphRunner:
        from ai_core.ports.sandbox import VerifyResult

        runner = object.__new__(AgentGraphRunner)
        runner.events = []
        runner._settings = SimpleNamespace(
            error_context_lines=3,
            error_context_max_chars=2000,
        )

        def emit(event_type: str, payload: dict, **kwargs: Any) -> None:
            runner.events.append((event_type, payload))

        runner._emit = emit  # type: ignore[method-assign]

        class _Sandbox:
            def verify(self, sandbox_id: str, check: str = "build") -> Any:
                return VerifyResult(
                    success=True,
                    exit_code=0,
                    timed_out=False,
                    duration_ms=900,
                    logs="build ok",
                    logs_truncated=False,
                )

            def read_file(self, sandbox_id: str, path: str) -> str:
                return content

        runner._sandbox = _Sandbox()  # type: ignore[assignment]
        return runner

    @staticmethod
    def _state() -> dict[str, Any]:
        return {
            "run_id": "run_1",
            "sandbox_id": "sbx_1",
            "retry_count": 0,
            "clarification_round": 0,
            "target_files": [APP_PATH],
            "annotation_context": {
                "annotations": [
                    {
                        "annotation": {
                            "annotationId": "ann_b870aabe750a",
                            "action": "ADD",
                            "target": {"source": {"file": APP_PATH}},
                            "payload": {"text": "添加按钮"},
                        }
                    }
                ]
            },
        }

    def test_build_success_without_created_marker_fails(self) -> None:
        runner = self._runner(
            "import { Plus } from 'lucide-react'\n"
            "export default function App() { return <main /> }\n"
        )
        update = runner._verifier(self._state())

        self.assertFalse(update["verify_result"]["success"])
        self.assertIn("新增组件未写入文件", update["verify_result"]["error_logs"])
        self.assertTrue(
            any(event_type == "verify.failed" for event_type, _ in runner.events)
        )

    def test_build_success_with_created_marker_passes(self) -> None:
        runner = self._runner(
            "export default function App() { return <main>"
            '<button data-vibe-created="ann_b870aabe750a">按钮</button>'
            "</main> }\n"
        )
        update = runner._verifier(self._state())

        self.assertTrue(update["verify_result"]["success"])
        self.assertTrue(
            any(event_type == "verify.passed" for event_type, _ in runner.events)
        )


if __name__ == "__main__":
    unittest.main()
