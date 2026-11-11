"""Token 降本三项改造的单元测试。

覆盖：
1. ModelRouter 能力分级与任务级覆盖优先级；
2. UI / Architect 历史压缩；
3. 构建错误精准截断与错误指纹；
4. Evaluator 只接收精简错误、同错终止（见 test_run_options）。
"""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from typing import Any

from ai_core.agent.error_context import (
    extract_error_context,
    extract_error_fingerprint,
    extract_error_location,
)
from ai_core.agent.graph import (
    CODER_TIMEOUT_MESSAGE,
    AgentGraphRunner,
    CoderTimeoutError,
    _repair_feedback,
    _repair_payload,
)
from ai_core.agent.memory import (
    compress_annotation_context,
    compress_architect_history,
    estimate_tokens,
)
from ai_core.config import Settings
from ai_core.model_router import ModelRouter
from ai_core.observability.streaming import (
    TokenStreamEmitter,
    collect_tokens,
    get_token_sink,
)


class ModelRouterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            deepseek_model="base-model",
            model_fast_name="fast-model",
            model_strong_name="strong-model",
            model_fast_temperature=0.0,
            model_strong_temperature=0.6,
            model_coder_name="coder-override",
        )
        self.router = ModelRouter(self.settings)

    def test_fast_tasks_use_fast_tier(self) -> None:
        for task in ("routing", "validation", "summary", "planner"):
            profile = self.router.resolve(task)
            self.assertEqual(profile.model, "fast-model", task)
            self.assertEqual(profile.capability, "fast")
            # Planner 是长 JSON 决策节点，必须用确定性温度；
            # 其余快速任务仍沿用模型档位默认值。
            expected = 0.1 if task == "planner" else 0.0
            self.assertEqual(profile.temperature, expected, task)

    def test_strong_tasks_use_strong_tier(self) -> None:
        profile = self.router.resolve("architect_contract")
        self.assertEqual(profile.model, "strong-model")
        self.assertEqual(profile.capability, "strong")
        # Architect 契约生成同样固定为 0.1，消除随机反问。
        self.assertEqual(profile.temperature, 0.1)
        # 未声明任务级温度覆盖的强模型任务保持档位默认值。
        coder = self.router.resolve("coder")
        self.assertEqual(coder.temperature, 0.6)

    def test_task_override_wins_over_tier(self) -> None:
        profile = self.router.resolve("coder")
        self.assertEqual(profile.model, "coder-override")
        self.assertEqual(profile.capability, "strong")

    def test_empty_tier_falls_back_to_base_model(self) -> None:
        router = ModelRouter(
            Settings(
                deepseek_model="only-model",
                model_fast_name="",
                model_strong_name="",
            )
        )
        self.assertEqual(router.resolve("planner").model, "only-model")
        self.assertEqual(router.resolve("coder").model, "only-model")

    def test_unknown_task_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.router.resolve("unknown_task")

    def test_describe_covers_all_tasks(self) -> None:
        described = self.router.describe()
        self.assertIn("summary", described)
        self.assertEqual(described["coder"]["model"], "coder-override")

    def test_client_cache_separates_temperatures(self) -> None:
        router = ModelRouter(
            Settings(
                deepseek_api_key="test-key",
                deepseek_model="deepseek-chat",
                model_fast_name="same-model",
                model_strong_name="same-model",
            )
        )
        planner = router.build("planner")
        coder = router.build("coder")
        # 同名模型但温度不同，必须拿到不同客户端。
        self.assertIsNot(planner, coder)
        self.assertEqual(len(router._clients), 2)
        # 流式调用必须要求网关回传 usage，保证 token 成本不丢失。
        self.assertTrue(planner.stream_usage)
        self.assertTrue(coder.stream_usage)


class HistoryCompressionTest(unittest.TestCase):
    @staticmethod
    def _context(text: str) -> dict:
        return {
            "annotations": [
                {
                    "annotation": {
                        "annotationId": "a1",
                        "threadContext": {
                            "chatHistory": [
                                {"role": "user", "content": text},
                                {"role": "assistant", "content": "好的，已修改"},
                            ],
                            "previousRuns": [{"runId": "r1", "patch": text}],
                        },
                    }
                }
            ]
        }

    def test_annotation_context_compressed_over_threshold(self) -> None:
        # 不含“必须 / 改成”等高优关键词，确保旧消息可以完全折叠为摘要；
        # 高优关键词的逐字保留由 test_context_compression 单独覆盖。
        long_text = "请处理这个按钮的样式细节。" * 200
        result = compress_annotation_context(
            self._context(long_text),
            lambda text, max_chars: "按钮必须为红色，禁止改成蓝色",
            threshold=100,
            max_chars=50,
            recent_turns=0,
        )
        self.assertTrue(result.compressed)
        self.assertLess(result.tokens_after, result.tokens_before)
        thread = result.value["annotations"][0]["annotation"]["threadContext"]
        self.assertEqual(len(thread["chatHistory"]), 1)
        self.assertIn("历史摘要", thread["chatHistory"][0]["content"])
        self.assertEqual(thread["previousRuns"], [])

    def test_annotation_context_untouched_below_threshold(self) -> None:
        context = self._context("把按钮改成红色")
        result = compress_annotation_context(
            context,
            lambda text, max_chars: "摘要",
            threshold=100,
            max_chars=50,
        )
        self.assertFalse(result.compressed)
        self.assertIs(result.value, context)

    def test_summarizer_failure_degrades_to_fallback(self) -> None:
        long_text = "用户要求：按钮必须为红色。\n中间代码\n" * 120

        def boom(text: str, max_chars: int) -> str:
            raise RuntimeError("model down")

        result = compress_annotation_context(
            self._context(long_text),
            boom,
            threshold=10,
            max_chars=80,
        )
        # 摘要有收益才替换；即使模型失败也不能抛异常。
        self.assertIn(result.compressed, (True, False))
        self.assertGreater(result.tokens_before, 0)

    def test_architect_history_compressed(self) -> None:
        history = [
            {"role": "user", "content": "做一个订单管理系统。" * 100},
            {"role": "assistant", "content": "请问需要数据库吗？"},
        ]
        result = compress_architect_history(
            history,
            lambda text, max_chars: "核心需求：订单管理；约束：必须使用 MySQL",
            threshold=50,
            max_chars=60,
            recent_turns=0,
        )
        self.assertTrue(result.compressed)
        self.assertEqual(len(result.value), 1)
        self.assertEqual(result.value[0]["role"], "system")

    def test_token_estimate_prefers_cjk_density(self) -> None:
        self.assertGreater(
            estimate_tokens("中文内容" * 100),
            estimate_tokens("abcdefgh" * 100),
        )


class ErrorContextTest(unittest.TestCase):
    STDERR = (
        "vite v5.4.0 building for production...\n"
        "✓ 42 modules transformed.\n"
        "error during build:\n"
        'src/App.jsx:12:5: ERROR: Expected "}" but found "}"\n'
        "    at failureErrorWithLog (node:internal)\n"
    )

    @staticmethod
    def _code(lines: int = 30) -> str:
        return "\n".join(f"const line{i} = {i};" for i in range(1, lines + 1))

    def test_location_extracted(self) -> None:
        location = extract_error_location(self.STDERR)
        self.assertIsNotNone(location)
        self.assertEqual(location.file, "src/App.jsx")
        self.assertEqual(location.line, 12)
        self.assertEqual(location.column, 5)

    def test_context_only_keeps_error_window(self) -> None:
        context = extract_error_context(
            self.STDERR,
            self._code(),
            context_lines=2,
            max_chars=4000,
        )
        self.assertIn("src/App.jsx:12:5", context)
        self.assertIn("Expected", context)
        self.assertIn(">   12 |", context)
        self.assertIn("   10 |", context)
        self.assertIn("   14 |", context)
        self.assertNotIn("    9 |", context)
        self.assertNotIn("   15 |", context)

    def test_fingerprint_stable_across_line_numbers(self) -> None:
        first = 'src/App.jsx:12:5: ERROR: Expected "}" but found "}"'
        second = 'src/Other.jsx:99:1: ERROR: Expected "}" but found "}"'
        self.assertEqual(
            extract_error_fingerprint(first),
            extract_error_fingerprint(second),
        )

    def test_fingerprint_changes_for_different_error(self) -> None:
        first = 'src/App.jsx:12:5: ERROR: Expected "}" but found "}"'
        second = "src/App.jsx:12:5: ERROR: Cannot find module './api'"
        self.assertNotEqual(
            extract_error_fingerprint(first),
            extract_error_fingerprint(second),
        )

    def test_no_location_falls_back_to_tail(self) -> None:
        context = extract_error_context("something failed badly", "", context_lines=2)
        self.assertIn("[stderr-tail]", context)
        self.assertIn("something failed badly", context)

    def test_short_logs_preferred_over_expanded_context(self) -> None:
        logs = "x" * 1000
        feedback = _repair_feedback(
            {
                "success": False,
                "error_logs": logs,
                "error_context": logs + "y" * 500,
            }
        )
        self.assertEqual(feedback, logs)

    def test_long_logs_use_truncated_context(self) -> None:
        logs = "x" * 20000
        context = "[error] Expected }"
        feedback = _repair_feedback(
            {
                "success": False,
                "error_logs": logs,
                "error_context": context,
            }
        )
        self.assertEqual(feedback, context)

    def test_repair_payload_drops_full_logs(self) -> None:
        payload = _repair_payload(
            {
                "success": False,
                "exit_code": 1,
                "error_logs": "x" * 100000,
                "error_context": "[error] Expected }",
                "error_fingerprint": "abc",
            }
        )
        self.assertEqual(
            payload,
            {
                "success": False,
                "exit_code": 1,
                "error_context": "[error] Expected }",
            },
        )


class _SummaryHarness(AgentGraphRunner):
    def __init__(self, llm) -> None:  # noqa: ANN001
        self._llm = llm
        self._settings = Settings(
            history_summary_token_threshold=50,
            history_summary_max_chars=40,
        )
        self.events: list[tuple[str, dict]] = []

    def _emit(self, event_type: str, payload: dict, **kwargs) -> None:  # noqa: ANN003
        self.events.append((event_type, payload))


class _LLMStub:
    def summarize_history(self, text: str, max_chars: int = 200) -> str:
        return "核心需求：红色按钮；约束：不得改布局"


class GraphSummaryNodeTest(unittest.TestCase):
    def test_summarize_node_compresses_context(self) -> None:
        runner = _SummaryHarness(_LLMStub())
        context = HistoryCompressionTest._context("按钮改成红色。" * 200)
        update = runner._summarize_history(
            {"run_id": "run1", "annotation_context": context}
        )
        self.assertLess(
            update["history_tokens_after"],
            update["history_tokens_before"],
        )
        self.assertIn("核心需求", update["history_summary"])
        thread = update["annotation_context"]["annotations"][0]["annotation"][
            "threadContext"
        ]
        # system 摘要 + 最近一轮原文（user + assistant）
        self.assertEqual(len(thread["chatHistory"]), 3)
        self.assertIn("核心需求", thread["chatHistory"][0]["content"])
        self.assertTrue(runner.events)


class _SlowCoderLLM:
    """模拟真实模型调用：阻塞时间超过节点硬超时。"""

    def __init__(self, delay: float) -> None:
        self.delay = delay

    def generate(self, **kwargs: Any) -> Any:  # noqa: ARG002
        time.sleep(self.delay)
        return SimpleNamespace(files={}, patch="", summary="late")


class TokenStreamingTest(unittest.TestCase):
    def test_context_sink_receives_model_deltas(self) -> None:
        events: list[tuple[str, str]] = []
        emitter = TokenStreamEmitter(
            lambda text, task: events.append((task, text)),
            min_interval_ms=0,
            max_chars=1,
        )
        with collect_tokens(emitter.feed):
            sink = get_token_sink()
            self.assertIsNotNone(sink)
            sink("coder", "你")
            sink("coder", "好")
        emitter.flush()
        self.assertEqual(events, [("coder", "你"), ("coder", "好")])

    def test_close_drops_late_tokens_after_timeout(self) -> None:
        events: list[tuple[str, str]] = []
        emitter = TokenStreamEmitter(
            lambda text, task: events.append((task, text)),
            min_interval_ms=0,
            max_chars=1,
        )
        emitter.feed("coder", "first")
        emitter.close()
        # Coder 超时后后台 HTTP 线程仍可能收到 chunk，必须被丢弃。
        emitter.feed("coder", "late")
        emitter.flush()
        self.assertEqual(events, [("coder", "first")])

    def test_collect_tokens_resets_after_node(self) -> None:
        emitter = TokenStreamEmitter(lambda text, task: None, max_chars=1)
        with collect_tokens(emitter.feed):
            self.assertIsNotNone(get_token_sink())
        self.assertIsNone(get_token_sink())


class CoderCircuitBreakerTest(unittest.TestCase):
    @staticmethod
    def _runner(llm: Any) -> AgentGraphRunner:
        # 只测试超时辅助方法，跳过 LLM / sandbox / checkpoint 初始化。
        runner = object.__new__(AgentGraphRunner)
        runner._settings = SimpleNamespace(agent_coder_timeout_seconds=1.0)
        runner._llm = llm
        return runner

    def test_coder_timeout_raises_with_actionable_message(self) -> None:
        runner = self._runner(_SlowCoderLLM(delay=1.4))
        started = time.monotonic()
        with self.assertRaises(CoderTimeoutError) as ctx:
            runner._generate_with_coder_timeout(annotations=[], plan="")
        elapsed = time.monotonic() - started
        self.assertEqual(str(ctx.exception), CODER_TIMEOUT_MESSAGE)
        self.assertLess(elapsed, 1.35, "超时熔断必须立即返回，不能等模型收尾")

    def test_fast_coder_returns_normally(self) -> None:
        result = self._runner(_SlowCoderLLM(delay=0))._generate_with_coder_timeout(
            annotations=[], plan=""
        )
        self.assertEqual(result.summary, "late")

    def test_coder_fatal_error_routes_directly_to_failed(self) -> None:
        runner = object.__new__(AgentGraphRunner)
        self.assertEqual(
            runner._route_after_coder({"fatal_error": True, "apply_failed": True}),
            "failed",
        )
        self.assertEqual(
            runner._route_after_coder({"apply_failed": True, "next_action": "retry"}),
            "evaluator",
        )


if __name__ == "__main__":
    unittest.main()
