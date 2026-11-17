"""Architect 模型瞬态连接错误的重试测试。"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from ai_core.architect.llm import (
    ArchitectLLMError,
    DeepSeekArchitectLLM,
    _project_mode_block,
)
from ai_core.config import Settings
from ai_core.error_envelope import envelope_from_code
from ai_core.observability.streaming import collect_tokens, reset_token_stream
from ai_core.ports.model import ModelClientError


class _FlakyModelClient:
    def __init__(self, failures: int, error: str) -> None:
        self.failures = failures
        self.error = error
        self.calls = 0

    def generate_json(self, task: str, system: str, human: str, **_kwargs) -> str:
        self.calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise ModelClientError(self.error)
        return json.dumps({"ok": True, "task": task})


def _llm(client: _FlakyModelClient, *, transport_retries: int = 2):
    llm = DeepSeekArchitectLLM.__new__(DeepSeekArchitectLLM)
    llm.settings = Settings(
        deepseek_api_key="test-key",
        architect_json_retry_limit=0,
        architect_llm_retry_limit=transport_retries,
    )
    llm._client = client
    return llm


class ArchitectLLMRetryTests(unittest.TestCase):
    def test_transient_stream_interruption_is_retried(self) -> None:
        client = _FlakyModelClient(
            failures=2,
            error=(
                "DeepSeek 流式调用中断: peer closed connection without "
                "sending complete message body (incomplete chunked read)"
            ),
        )
        llm = _llm(client)
        with (
            patch("ai_core.architect.llm.time.sleep"),
            patch("ai_core.architect.llm.reset_token_stream") as reset,
        ):
            result = llm._generate_with_repair(
                task="architect_analyze",
                system="system",
                human="human",
                json_schema=None,
                parse=json.loads,
                label="analyze",
            )
        self.assertTrue(result["ok"])
        self.assertEqual(client.calls, 3)
        # 每次重试前都要清空前端半截流式文本。
        self.assertEqual(reset.call_count, 2)

    def test_non_transient_error_is_not_retried(self) -> None:
        client = _FlakyModelClient(failures=5, error="401 unauthorized invalid api key")
        llm = _llm(client)
        with (
            patch("ai_core.architect.llm.time.sleep"),
            patch("ai_core.architect.llm.reset_token_stream"),
        ):
            with self.assertRaises(ArchitectLLMError):
                llm._generate_with_repair(
                    task="architect_analyze",
                    system="system",
                    human="human",
                    json_schema=None,
                    parse=json.loads,
                    label="analyze",
                )
        self.assertEqual(client.calls, 1)

    def test_retry_budget_is_bounded(self) -> None:
        client = _FlakyModelClient(failures=10, error="502 bad gateway")
        llm = _llm(client, transport_retries=2)
        with (
            patch("ai_core.architect.llm.time.sleep"),
            patch("ai_core.architect.llm.reset_token_stream"),
        ):
            with self.assertRaises(ArchitectLLMError):
                llm._generate_with_repair(
                    task="architect_analyze",
                    system="system",
                    human="human",
                    json_schema=None,
                    parse=json.loads,
                    label="analyze",
                )
        self.assertEqual(client.calls, 3)


class TokenStreamResetTests(unittest.TestCase):
    def test_reset_callback_is_scoped_to_collect_tokens(self) -> None:
        events: list[str] = []
        with collect_tokens(
            lambda _task, delta: events.append(delta),
            lambda: events.append("<reset>"),
        ):
            reset_token_stream()
        self.assertEqual(events, ["<reset>"])
        # 退出上下文后不应再触发旧回调。
        reset_token_stream()
        self.assertEqual(events, ["<reset>"])


class ProjectModePromptTests(unittest.TestCase):
    def test_existing_mode_forbids_visual_document_and_full_code(self) -> None:
        block = _project_mode_block("EXISTING_SOURCE")
        self.assertIn("严禁输出 visual_document", block)
        self.assertIn("整页 JSX", block)
        self.assertIn("后端增量", block)
        self.assertEqual(_project_mode_block("NEW_PROJECT"), "")


class ArchitectErrorEnvelopeTests(unittest.TestCase):
    def test_llm_error_has_friendly_retryable_message(self) -> None:
        envelope = envelope_from_code(
            "ARCHITECT_LLM_ERROR",
            technical_message="DeepSeek 流式调用中断",
            run_id="arc_retry",
        )
        self.assertEqual(envelope.code, "ARCHITECT_LLM_ERROR")
        self.assertIn("架构师模型连接中断", envelope.user_message)
        self.assertTrue(envelope.retryable)
        self.assertNotIn("未预期问题", envelope.user_message)


if __name__ == "__main__":
    unittest.main()
