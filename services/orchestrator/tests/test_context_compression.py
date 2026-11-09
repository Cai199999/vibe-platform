"""分层上下文压缩、项目记忆与工具结果落盘测试。

运行：
    cd services/orchestrator
    PYTHONPATH=. python -m unittest tests.test_context_compression -v
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from ai_core.agent.memory import (
    IntentClassifier,
    compress_annotation_context,
    compress_architect_history,
    count_tokens,
    heuristic_high_priority,
)
from ai_core.config import Settings
from ai_core.context_memory import (
    ContextSummarizer,
    ProjectMemory,
    get_project_memory,
    refresh_global_memory,
)


class _FixedClassifier:
    """只把指定下标标为高优，用于验证分层压缩顺序。"""

    def __init__(self, high_indexes: set[int]) -> None:
        self.high_indexes = high_indexes
        self.calls = 0

    def classify(self, texts: list[str]) -> list[bool]:
        self.calls += 1
        return [index in self.high_indexes for index in range(len(texts))]


class _FakeSummarizer:
    def global_summary(self, local_markdown: str, max_chars: int = 1200) -> str:
        return "全局：目标/架构/重大变更"

    def user_profile(self, material: str, max_chars: int = 600) -> str:
        return "### 技术栈偏好\nReact + FastAPI\n\n### 协作约束\n- 必须写中文注释"


class LayeredCompressionTest(unittest.TestCase):
    def test_count_tokens_handles_cjk(self) -> None:
        self.assertGreater(count_tokens("中文内容" * 100), 0)
        self.assertGreater(
            count_tokens("中文内容" * 100),
            count_tokens("abcdefgh" * 100),
        )

    def test_keeps_recent_turns_and_constraint_text(self) -> None:
        messages = []
        for index in range(12):
            messages.append(
                {
                    "role": "user",
                    "content": f"第 {index} 轮：请把按钮改成红色。" + "细节" * 80,
                }
            )
            messages.append({"role": "assistant", "content": "已处理"})
        context = {
            "annotations": [
                {
                    "annotation": {
                        "threadContext": {
                            "chatHistory": messages,
                            "previousRuns": [{"runId": "r1", "patch": "x" * 500}],
                        }
                    }
                }
            ]
        }
        classifier = _FixedClassifier({0})
        summarizer_calls: list[str] = []

        def summarize(text: str, max_chars: int) -> str:
            summarizer_calls.append(text)
            return "旧需求：统一按钮样式"

        result = compress_annotation_context(
            context,
            summarize,
            threshold=50,
            max_chars=120,
            recent_turns=2,
            classifier=classifier,
            encoding="cl100k_base",
        )
        self.assertTrue(result.compressed)
        self.assertEqual(classifier.calls, 1)
        self.assertTrue(summarizer_calls)
        thread = result.value["annotations"][0]["annotation"]["threadContext"]
        # system 摘要 + 最近 2 轮（4 条）
        self.assertEqual(len(thread["chatHistory"]), 5)
        block = thread["chatHistory"][0]["content"]
        self.assertIn("历史摘要", block)
        self.assertIn("用户明确要求（逐字保留", block)
        self.assertIn("第 0 轮", block)
        # 最近 2 条 previousRuns 仍保留，旧的（这里只有 1 条）不会被删除。
        self.assertEqual(len(thread["previousRuns"]), 1)
        self.assertEqual(thread["chatHistory"][-1]["content"], "已处理")

    def test_architect_history_keeps_recent_messages(self) -> None:
        history = []
        for index in range(10):
            history.append(
                {"role": "user", "content": f"需求 {index}：" + "很长的描述" * 60}
            )
            history.append({"role": "assistant", "content": f"回答 {index}"})
        result = compress_architect_history(
            history,
            lambda text, max_chars: "历史需求摘要",
            threshold=50,
            max_chars=120,
            recent_turns=2,
            classifier=_FixedClassifier({0}),
        )
        self.assertTrue(result.compressed)
        self.assertEqual(result.value[0]["role"], "system")
        self.assertEqual(len(result.value), 5)
        self.assertEqual(result.value[-1]["content"], "回答 9")

    def test_below_threshold_is_identity(self) -> None:
        context = {
            "annotations": [{"annotation": {"threadContext": {"chatHistory": []}}}]
        }
        result = compress_annotation_context(
            context,
            lambda text, max_chars: "摘要",
            threshold=1000,
            max_chars=50,
        )
        self.assertFalse(result.compressed)
        self.assertIs(result.value, context)

    def test_heuristic_high_priority(self) -> None:
        self.assertTrue(heuristic_high_priority("必须保持蓝色，不要改成红色"))
        self.assertFalse(heuristic_high_priority("好的，谢谢"))
        self.assertTrue(
            IntentClassifier(Settings(deepseek_api_key="")).classify(
                ["必须保持中文注释"]
            )[0]
        )


class ProjectMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            context_memory_dir=self.temp.name,
            tool_output_preview_chars=20,
        )
        self.memory = get_project_memory(
            self.settings,
            "tenant_a",
            "../../escape",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_scope_is_sanitized(self) -> None:
        self.assertNotIn("..", str(self.memory.directory))
        self.assertTrue(str(self.memory.directory).startswith(self.temp.name))

    def test_local_summary_is_idempotent(self) -> None:
        self.assertTrue(
            self.memory.append_local_summary("第一段摘要", turn=10, run_id="r1")
        )
        self.assertFalse(
            self.memory.append_local_summary("重复摘要", turn=10, run_id="r2")
        )
        text = self.memory.read_local_summary()
        self.assertIn("第一段摘要", text)
        self.assertNotIn("重复摘要", text)
        self.assertTrue(self.memory.has_local_summary(10))

    def test_global_summary_and_profile_from_local(self) -> None:
        self.memory.append_local_summary(
            "需求：实现登录；技术栈：React + FastAPI",
            turn=10,
        )
        changed = refresh_global_memory(
            self.memory,
            summarizer=_FakeSummarizer(),
            settings=self.settings,
        )
        self.assertTrue(changed)
        self.assertIn("全局", self.memory.read_global_summary())
        self.assertIn("React", self.memory.read_user_profile())
        prompt = self.memory.prompt_context()
        self.assertIn("用户画像", prompt)
        self.assertIn("项目全局摘要", prompt)

    def test_refresh_without_local_summary_is_noop(self) -> None:
        self.assertFalse(
            refresh_global_memory(self.memory, summarizer=_FakeSummarizer())
        )
        self.assertFalse(self.memory.global_summary_path.exists())

    def test_refresh_uses_latest_changes_before_first_local_summary(self) -> None:
        """首次保存快照没有 local_summary 时，也必须能生成全局摘要。"""
        changed = refresh_global_memory(
            self.memory,
            summarizer=_FakeSummarizer(),
            settings=self.settings,
            latest_changes="新增登录按钮，并拖拽到卡片右上角",
        )
        self.assertTrue(changed)
        self.assertIn("全局", self.memory.read_global_summary())
        self.assertTrue(self.memory.user_profile_path.exists())

    def test_tool_output_preview_and_hash(self) -> None:
        payload = "搜索结果" * 500
        ref = self.memory.save_tool_output("search_web", payload, preview_chars=20)
        self.assertEqual(ref.chars, len(payload))
        self.assertEqual(ref.preview, payload[:20])
        self.assertEqual(
            ref.sha256,
            hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        )
        self.assertIsNotNone(ref.path)
        self.assertTrue(Path(ref.path).exists())
        self.assertEqual(self.memory.read_tool_output(ref.sha256), payload)
        prompt = ref.prompt_text()
        self.assertIn(ref.sha256, prompt)
        self.assertIn(ref.preview, prompt)
        self.assertNotIn(payload, prompt)

    def test_summarizer_falls_back_without_api_key(self) -> None:
        summarizer = ContextSummarizer(Settings(deepseek_api_key=""))
        local = summarizer.local_summary(
            "用户要求：按钮必须为红色\n中间代码\n不要改成蓝色",
            max_chars=80,
        )
        self.assertIn("必须", local)
        profile = summarizer.user_profile(
            "技术栈使用 React + FastAPI；必须保持中文注释",
            max_chars=200,
        )
        self.assertIn("React", profile)


if __name__ == "__main__":
    unittest.main()
