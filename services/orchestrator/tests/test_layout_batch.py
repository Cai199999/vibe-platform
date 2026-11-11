"""BATCH_LAYOUT（画布视觉暂存统一提交）的后端单元测试。

覆盖：
  1. 协议解析：位移 + 嵌套 CREATE_NEW 信封；
  2. 非法新增项必须整体拒绝，不能悄悄丢用户草稿；
  3. 内存版 unified diff 合并（容忍行号偏移、多文件、上下文不匹配拒绝）；
  4. Coder 只产出一份合并补丁，新增 JSX 与 transform 位移同时落地；
  5. Verifier 能穿透 BATCH_LAYOUT 校验每个 data-vibe-created 标记。

运行：

    docker exec vibe-python-ai python -m unittest \
        tests.test_layout_batch -v
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field

from ai_core.agent.graph import (
    AgentGraphRunner,
    _apply_unified_patch,
    _explicit_layout_batch,
    _is_explicit_layout_batch,
)
from ai_core.agent.llm import CodeResult
from ai_core.config import Settings
from ai_core.tools import build_default_toolbox

APP = (
    "export default function App() {\n"
    "  return (\n"
    "    <main>\n"
    '      <button data-source="cta">开始</button>\n'
    "    </main>\n"
    "  )\n"
    "}\n"
)


def _addition(annotation_id: str = "ann_add_1") -> dict:
    return {
        "annotationId": annotation_id,
        "intent": "加一个导出按钮",
        "componentType": "button",
        "annotation": {
            "annotationId": annotation_id,
            "action": "CREATE_NEW",
            "type": "visual-draw",
            "target": {
                "selector": "main",
                "source": {"file": "src/App.jsx"},
            },
            "intent": {
                "kind": "UI_ONLY",
                "ui": {"summary": "加一个导出按钮", "acceptance": []},
            },
            "payload": {"text": "加一个导出按钮"},
        },
    }


def _layout(**overrides) -> dict:
    base = {
        "annotationId": "ann_layout_1",
        "action": "BATCH_LAYOUT",
        "payload": {
            "moves": [
                {
                    "source": "src/App.jsx:4",
                    "file": "src/App.jsx",
                    "selector": '[data-source="cta"]',
                    "dx": 10,
                    "dy": 20,
                }
            ],
            "additions": [_addition()],
        },
    }
    base.update(overrides)
    return base


@dataclass
class _FakeLLM:
    """只返回固定文件内容的 Coder 替身。"""

    files: dict[str, str] = field(default_factory=dict)
    calls: int = 0

    def generate(self, **kwargs) -> CodeResult:
        self.calls += 1
        return CodeResult(summary="新增导出按钮", files=dict(self.files))


class _FakeSandbox:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    def read_file(self, _sandbox_id: str, path: str) -> str:
        return self.files[path]


class LayoutBatchProtocolTest(unittest.TestCase):
    def test_parse_moves_and_additions(self) -> None:
        layout = _explicit_layout_batch(_layout())
        self.assertIsNotNone(layout)
        assert layout is not None
        self.assertEqual(len(layout["moves"]), 1)
        self.assertEqual(layout["moves"][0]["dx"], 10)
        self.assertEqual(len(layout["additions"]), 1)
        nested = layout["additions"][0]["annotation"]
        self.assertEqual(nested["action"], "CREATE_NEW")
        self.assertEqual(nested["intent"]["ui"]["summary"], "加一个导出按钮")

    def test_additions_only_is_accepted(self) -> None:
        layout = _explicit_layout_batch(_layout(payload={"additions": [_addition()]}))
        self.assertIsNotNone(layout)
        assert layout is not None
        self.assertEqual(layout["moves"], [])
        self.assertTrue(_is_explicit_layout_batch([_layout()]))

    def test_invalid_addition_rejects_whole_batch(self) -> None:
        broken = _addition()
        broken["annotation"] = {"target": {}}
        self.assertIsNone(
            _explicit_layout_batch(_layout(payload={"additions": [broken]}))
        )
        # 空批次没有任何可执行内容，直接回退普通流程。
        self.assertIsNone(_explicit_layout_batch(_layout(payload={})))
        # 非 BATCH_LAYOUT 不参与该分支。
        self.assertIsNone(_explicit_layout_batch(_layout(action="BATCH_MOVE")))


class UnifiedPatchTest(unittest.TestCase):
    def test_applies_hunk_with_line_offset(self) -> None:
        files = {"src/App.jsx": "a\nb\nc\nd\n"}
        patch = (
            "--- a/src/App.jsx\n"
            "+++ b/src/App.jsx\n"
            "@@ -10,2 +10,3 @@\n"
            " b\n"
            "+x\n"
            " c\n"
        )
        merged = _apply_unified_patch(files, patch)
        self.assertIsNotNone(merged)
        assert merged is not None
        self.assertEqual(merged["src/App.jsx"], "a\nb\nx\nc\nd\n")
        # 原文件表不能被就地修改，Applier 仍以返回值写盘。
        self.assertEqual(files["src/App.jsx"], "a\nb\nc\nd\n")

    def test_applies_multiple_files(self) -> None:
        files = {"a.jsx": "one\ntwo\n", "b.jsx": "red\ngreen\n"}
        patch = (
            "--- a/a.jsx\n"
            "+++ b/a.jsx\n"
            "@@ -1,2 +1,2 @@\n"
            "-one\n"
            "+ONE\n"
            " two\n"
            "--- a/b.jsx\n"
            "+++ b/b.jsx\n"
            "@@ -1,2 +1,3 @@\n"
            " red\n"
            "+blue\n"
            " green\n"
        )
        merged = _apply_unified_patch(files, patch)
        self.assertIsNotNone(merged)
        assert merged is not None
        self.assertEqual(merged["a.jsx"], "ONE\ntwo\n")
        self.assertEqual(merged["b.jsx"], "red\nblue\ngreen\n")

    def test_context_mismatch_returns_none(self) -> None:
        files = {"src/App.jsx": "a\nb\n"}
        patch = (
            "--- a/src/App.jsx\n"
            "+++ b/src/App.jsx\n"
            "@@ -1,2 +1,2 @@\n"
            "-not-here\n"
            "+x\n"
            " b\n"
        )
        self.assertIsNone(_apply_unified_patch(files, patch))


class CoderLayoutBatchTest(unittest.TestCase):
    def _runner(self, files: dict[str, str]) -> tuple[AgentGraphRunner, object]:
        runner = AgentGraphRunner.__new__(AgentGraphRunner)
        runner._toolbox = build_default_toolbox(_settings())
        runner._llm = _FakeLLM(files=files)
        events: list[tuple] = []
        runner._emit = lambda event, payload, **kwargs: events.append(  # type: ignore[method-assign]
            (event, payload, kwargs)
        )
        return runner, runner._llm

    def test_generates_one_patch_with_additions_and_moves(self) -> None:
        added = APP.replace(
            "    </main>",
            '      <button data-vibe-created="ann_add_1">导出</button>\n' "    </main>",
        )
        runner, llm = self._runner({"src/App.jsx": added})
        layout = _explicit_layout_batch(_layout())
        assert layout is not None
        state = {
            "run_id": "run_layout",
            "target_files": ["src/App.jsx"],
            "annotation_context": {"annotations": [{"annotation": _layout()}]},
            "plan": "统一提交画布变更",
            "clarification_round": 0,
            "coder_timeout_seconds": 5,
        }
        result = runner._coder_layout_batch(state, layout, {"src/App.jsx": APP}, {}, 0)
        self.assertEqual(llm.calls, 1)
        generated = result["generated_files"]
        self.assertEqual(list(generated), ["src/App.jsx"])
        content = generated["src/App.jsx"]
        self.assertIn('data-vibe-created="ann_add_1"', content)
        self.assertIn("translate3d(10.00px,20.00px,0)", content)
        self.assertTrue(result["generated_patch"])

    def test_moves_only_does_not_call_llm(self) -> None:
        runner, llm = self._runner({})
        layout = _explicit_layout_batch(
            _layout(payload={"moves": _layout()["payload"]["moves"]})
        )
        assert layout is not None
        state = {
            "run_id": "run_layout_move",
            "target_files": ["src/App.jsx"],
            "annotation_context": {"annotations": [{"annotation": _layout()}]},
            "plan": "",
            "clarification_round": 0,
        }
        result = runner._coder_layout_batch(state, layout, {"src/App.jsx": APP}, {}, 0)
        self.assertEqual(llm.calls, 0)
        content = result["generated_files"]["src/App.jsx"]
        self.assertIn("translate3d(10.00px,20.00px,0)", content)
        self.assertNotIn("data-vibe-created", content)


class MissingCreatedMarkerTest(unittest.TestCase):
    def _runner(self, files: dict[str, str]) -> AgentGraphRunner:
        runner = AgentGraphRunner.__new__(AgentGraphRunner)
        runner._sandbox = _FakeSandbox(files)
        return runner

    def test_checks_every_nested_addition(self) -> None:
        state = {
            "sandbox_id": "sbx_test",
            "target_files": ["src/App.jsx"],
            "annotation_context": {"annotations": [{"annotation": _layout()}]},
        }
        missing = self._runner({"src/App.jsx": APP})._missing_created_markers(state)
        self.assertEqual(
            missing, [{"annotationId": "ann_add_1", "file": "src/App.jsx"}]
        )

    def test_passes_when_marker_exists(self) -> None:
        state = {
            "sandbox_id": "sbx_test",
            "target_files": ["src/App.jsx"],
            "annotation_context": {"annotations": [{"annotation": _layout()}]},
        }
        content = APP.replace(
            "    </main>",
            '      <button data-vibe-created="ann_add_1">导出</button>\n' "    </main>",
        )
        self.assertEqual(
            self._runner({"src/App.jsx": content})._missing_created_markers(state),
            [],
        )


def _settings() -> Settings:
    """隔离 .env / 环境变量，保证测试结果可重复。"""
    return Settings(
        _env_file=None,
        tool_shell_enabled=False,
        mcp_servers=[],
        tool_web_search_enabled=False,
    )


if __name__ == "__main__":
    unittest.main()
