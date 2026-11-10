"""Token 用量核算与结构化输出自修复回归测试。

覆盖三条防线：

1. LangChain / OpenAI 兼容网关两种 usage 结构都能换算费用；
2. ContextVar 采集器按节点隔离，不串账；
3. Planner / Coder 遇到真实模型格式漂移时，会把「原始输出 + 校验错误」
   回传模型自修复，而不是直接 500。
"""

from __future__ import annotations

import json
import unittest

from ai_core.agent.llm import DeepSeekAgentLLM
from ai_core.config import Settings
from ai_core.domain.state import _merge_token_usage
from ai_core.observability.token_usage import (
    TokenUsage,
    collect_usage,
    extract_usage,
    record_model_response,
)
from ai_core.ports.model import ModelClient


class _FakeResponse:
    def __init__(
        self,
        *,
        usage_metadata: dict | None = None,
        response_metadata: dict | None = None,
    ) -> None:
        self.usage_metadata = usage_metadata
        self.response_metadata = response_metadata or {}


class _SequenceClient(ModelClient):
    """按顺序返回预设输出，并记录每次 Human Prompt。"""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.humans: list[str] = []
        self.tasks: list[str] = []

    def generate(self, task: str, system: str, human: str) -> str:
        self.tasks.append(task)
        self.humans.append(human)
        if not self.responses:
            raise AssertionError("模型调用次数超出预期")
        return self.responses.pop(0)


def _settings(**overrides) -> Settings:
    return Settings(deepseek_api_key="test-key", **overrides)


class ExtractUsageTest(unittest.TestCase):
    def test_langchain_usage_metadata_with_cache(self) -> None:
        response = _FakeResponse(
            usage_metadata={
                "input_tokens": 1000,
                "output_tokens": 500,
                "input_token_details": {"cache_read": 400},
            }
        )
        usage = extract_usage(response, model="deepseek-chat")
        self.assertEqual(usage.input_tokens, 1000)
        self.assertEqual(usage.output_tokens, 500)
        self.assertEqual(usage.cached_input_tokens, 400)
        # 600 * 2 / 1M + 400 * 0.5 / 1M + 500 * 8 / 1M
        self.assertAlmostEqual(usage.cost, 0.0054, places=6)
        self.assertEqual(usage.currency, "CNY")

    def test_openai_compatible_token_usage(self) -> None:
        response = _FakeResponse(
            response_metadata={
                "token_usage": {
                    "prompt_tokens": 200,
                    "completion_tokens": 100,
                    "prompt_cache_hit_tokens": 50,
                }
            }
        )
        usage = extract_usage(response, model="deepseek-chat")
        self.assertEqual(usage.input_tokens, 200)
        self.assertEqual(usage.cached_input_tokens, 50)
        # 150 * 2 / 1M + 50 * 0.5 / 1M + 100 * 8 / 1M
        self.assertAlmostEqual(usage.cost, 0.001125, places=6)

    def test_price_override(self) -> None:
        response = _FakeResponse(
            usage_metadata={"input_tokens": 1_000_000, "output_tokens": 0}
        )
        usage = extract_usage(
            response,
            model="custom-model",
            pricing={"custom-model": {"input": 1.0, "output": 2.0}},
        )
        self.assertAlmostEqual(usage.cost, 1.0, places=6)

    def test_collector_accumulates_without_leaking(self) -> None:
        response = _FakeResponse(
            usage_metadata={"input_tokens": 100, "output_tokens": 20}
        )
        with collect_usage() as collector:
            record_model_response(
                task="planner", model="deepseek-chat", response=response
            )
            record_model_response(
                task="coder", model="deepseek-chat", response=response
            )
        summary = collector.snapshot()
        self.assertEqual(summary.calls, 2)
        self.assertEqual(summary.input_tokens, 200)
        self.assertEqual(summary.output_tokens, 40)

        # 离开 with 后无激活采集器：不影响主流程，也不写入旧采集器。
        self.assertIsNone(
            record_model_response(
                task="summary", model="deepseek-chat", response=response
            )
        )
        self.assertEqual(collector.snapshot().calls, 2)

    def test_merge_token_usage_reducer(self) -> None:
        first = TokenUsage(input_tokens=10, output_tokens=5, calls=1, cost=0.1)
        second = TokenUsage(
            input_tokens=20, cached_input_tokens=3, output_tokens=8, calls=1, cost=0.2
        )
        merged = _merge_token_usage(first.as_dict(), second.as_dict())
        self.assertEqual(merged["inputTokens"], 30)
        self.assertEqual(merged["cachedInputTokens"], 3)
        self.assertEqual(merged["outputTokens"], 13)
        self.assertEqual(merged["totalTokens"], 43)
        self.assertEqual(merged["calls"], 2)
        self.assertAlmostEqual(merged["cost"], 0.3, places=6)


class AgentSelfRepairTest(unittest.TestCase):
    def test_coder_repairs_missing_target_file(self) -> None:
        client = _SequenceClient(
            [
                # 第一次：缺目标文件，必须触发自修复而不是直接失败。
                json.dumps(
                    {
                        "mode": "files",
                        "files": {
                            "src/Other.jsx": "export default function Other() {}"
                        },
                        "summary": "改错了文件",
                    }
                ),
                json.dumps(
                    {
                        "mode": "files",
                        "files": {
                            "src/App.jsx": "export default function App() { return <main /> }"
                        },
                        "summary": "修复目标文件",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        llm = DeepSeekAgentLLM(_settings(), client=client)
        result = llm.generate(
            annotations=[{"annotationId": "ann_1", "payload": {"text": "改标题"}}],
            plan="修改 src/App.jsx",
            current_files={"src/App.jsx": "<main />"},
            target_files=["src/App.jsx"],
            verify_result={"success": False, "error_context": "build failed"},
            retry_count=0,
        )
        self.assertEqual(client.tasks, ["coder", "coder"])
        self.assertIn("src/App.jsx", result.files)
        # 重试 Prompt 必须携带上一轮原始输出与具体校验错误。
        self.assertIn("【校验错误】", client.humans[1])
        self.assertIn("src/App.jsx", client.humans[1])
        self.assertIn("src/Other.jsx", client.humans[1])

    def test_coder_raises_after_retry_exhausted(self) -> None:
        invalid = json.dumps({"mode": "diff", "patch": "", "summary": "空补丁"})
        client = _SequenceClient([invalid, invalid, invalid])
        llm = DeepSeekAgentLLM(_settings(agent_json_retry_limit=2), client=client)
        with self.assertRaises(Exception) as ctx:
            llm.generate(
                annotations=[{"annotationId": "ann_1"}],
                plan="计划",
                current_files={"src/App.jsx": "<main />"},
                target_files=["src/App.jsx"],
                verify_result={},
                retry_count=0,
            )
        self.assertIn("连续 3 次输出不合法", str(ctx.exception))
        self.assertEqual(len(client.humans), 3)

    def test_planner_repairs_invalid_json_then_succeeds(self) -> None:
        client = _SequenceClient(
            [
                "我建议把标题改成蓝色，但这不是 JSON。",
                json.dumps(
                    {
                        "plan": "把标题改成蓝色",
                        "confidence_score": 0.95,
                        "decision_impact": "IMPLEMENTATION_DETAIL",
                        "needs_clarification": False,
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        llm = DeepSeekAgentLLM(_settings(), client=client)
        result = llm.plan(
            [{"annotationId": "ann_1", "payload": {"text": "改蓝色"}}],
            {"src/App.jsx": "<h1>标题</h1>"},
        )
        self.assertEqual(result.plan, "把标题改成蓝色")
        self.assertFalse(result.needs_clarification)
        self.assertIn("【上一次输出】", client.humans[1])
        self.assertIn("不要输出 Markdown 围栏", client.humans[1])

    def test_planner_degrades_to_text_after_retries(self) -> None:
        client = _SequenceClient(["纯文本计划 A", "纯文本计划 B", "纯文本计划 C"])
        llm = DeepSeekAgentLLM(_settings(agent_json_retry_limit=2), client=client)
        result = llm.plan(
            [{"annotationId": "ann_1", "payload": {"text": "改颜色"}}],
            {"src/App.jsx": "<main />"},
        )
        # 自修复耗尽后仍按文本计划继续，不把每个请求都变成澄清。
        self.assertEqual(result.plan, "纯文本计划 C")
        self.assertEqual(len(client.humans), 3)


if __name__ == "__main__":
    unittest.main()
