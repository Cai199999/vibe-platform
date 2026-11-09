"""JSON 序列化回归测试。

背景：前端 AnnotationEnvelope 携带 ``createdAt``（以及 chatHistory[].createdAt）
时，Pydantic 默认 ``model_dump()`` 会保留 ``datetime`` 对象；该对象随后进入
Planner / Coder Prompt 的 ``json.dumps`` 或 SSE 帧，直接触发：

    Object of type datetime is not JSON serializable

本文件锁定三条防线：
  1. 领域模型 field_serializer：即使 python mode 也输出 ISO 8601；
  2. 应用层上下文构造显式使用 ``mode="json"``；
  3. Prompt / SSE 的 ``json.dumps`` 带 ``default=str`` 兜底。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from ai_core.agent.events import RunEvent
from ai_core.agent.llm import DeepSeekAgentLLM
from ai_core.agent.service import RunExecutionService
from ai_core.agent.thread_memory import ThreadMemoryStore
from ai_core.config import Settings
from ai_core.domain.agent import ExecuteRunRequest
from ai_core.ports.model import ModelClient

CREATED_AT = "2026-11-09T10:00:00Z"


class _CaptureClient(ModelClient):
    """只记录 Prompt，不访问网络。"""

    def __init__(self) -> None:
        self.human = ""

    def generate(self, task: str, system: str, human: str) -> str:
        self.human = human
        return json.dumps(
            {"plan": "生成按钮", "confidence_score": 0.95},
            ensure_ascii=False,
        )


def _request_with_datetime() -> ExecuteRunRequest:
    return ExecuteRunRequest.model_validate(
        {
            "runId": "run_json",
            "projectId": "project_json",
            "annotations": [
                {
                    "annotationId": "ann_json",
                    "threadId": "thread_json",
                    "createdAt": CREATED_AT,
                    "target": {
                        "mode": "source",
                        "source": {"file": "src/App.jsx", "line": 2},
                        "selector": "button.primary",
                    },
                    "intent": {"kind": "UI_ONLY", "ui": {"summary": "生成按钮"}},
                    "payload": {"text": "生成按钮"},
                    "chatHistory": [
                        {
                            "role": "user",
                            "content": "生成按钮",
                            "createdAt": CREATED_AT,
                        }
                    ],
                }
            ],
            "files": {
                "src/App.jsx": (
                    "export default function App() {\n"
                    '  return <main className="page"><button>提交</button></main>\n'
                    "}\n"
                )
            },
        }
    )


class AnnotationSerializationTest(unittest.TestCase):
    def test_annotation_context_is_json_serializable(self) -> None:
        request = _request_with_datetime()
        store = ThreadMemoryStore()
        annotation = request.effective_annotations[0]
        scope = "scope_json"
        thread_context = store.merge_context(
            annotation.thread_id or "",
            annotation.chat_history,
            scope=scope,
        )

        context = RunExecutionService._build_annotation_context(
            request,
            ["src/App.jsx"],
            {(annotation.thread_id or ""): thread_context},
            request.files,
        )

        # 根因回归：这里过去会抛 TypeError。
        serialized = json.dumps(context, ensure_ascii=False)
        self.assertIn("2026-11-09T10:00:00", serialized)

        payload = context["annotations"][0]["annotation"]
        self.assertIsInstance(payload["createdAt"], str)
        self.assertEqual(payload["createdAt"], "2026-11-09T10:00:00.000Z")
        history = payload["threadContext"]["chatHistory"]
        self.assertIsInstance(history[0]["createdAt"], str)
        self.assertEqual(history[0]["createdAt"], "2026-11-09T10:00:00.000Z")

    def test_thread_memory_normalizes_datetime_to_iso(self) -> None:
        store = ThreadMemoryStore()
        raw = [
            {
                "role": "user",
                "content": "生成按钮",
                "createdAt": datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc),
            }
        ]
        context = store.merge_context("thread_dt", raw, scope="scope_dt")
        created_at = context["chatHistory"][0]["createdAt"]
        self.assertIsInstance(created_at, str)
        self.assertEqual(created_at, "2026-11-09T10:00:00.000Z")


class PromptSerializationTest(unittest.TestCase):
    def test_planner_prompt_accepts_datetime_payload(self) -> None:
        client = _CaptureClient()
        llm = DeepSeekAgentLLM(
            Settings(deepseek_api_key="test-key"),
            client=client,
        )
        # 即使上游漏了 mode="json"，Prompt 序列化也不能让整个 Run 失败。
        annotations = [
            {
                "annotationId": "ann_dt",
                "createdAt": datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc),
                "payload": {"text": "生成按钮"},
            }
        ]
        result = llm.plan(annotations, {"src/App.jsx": "<main />"})
        self.assertEqual(result.plan, "生成按钮")
        self.assertIn("2026-11-09 10:00:00+00:00", client.human)


class EventSerializationTest(unittest.TestCase):
    def test_sse_frame_accepts_datetime_payload(self) -> None:
        event = RunEvent(
            event_id="evt_dt",
            run_id="run_dt",
            seq=1,
            type="run.failed",
            version=1,
            ts="2026-11-09T10:00:00Z",
            payload={
                "message": "failed",
                "failedAt": datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc),
            },
        )
        frame = event.to_sse()
        self.assertIn("data: {", frame)
        self.assertIn("2026-11-09 10:00:00+00:00", frame)
        # 3 行字段 + 末尾空行；data 本身必须是单行 JSON，避免 SSE 拼接歧义。
        self.assertEqual(frame.count("\n"), 4)


if __name__ == "__main__":
    unittest.main()
