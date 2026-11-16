"""Phase 1：全局错误信封 / SSE 终态测试。

模拟节点抛错，验证前端实际收到的 HTTP 响应与 SSE 帧都包含：
code / userMessage / errorOptions / traceId，且不泄漏技术细节。
"""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from ai_core.error_envelope import (
    envelope_from_code,
    install_error_handlers,
    stream_event_frames,
    translate_exception,
)


class _Body(BaseModel):
    name: str


def _app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom")
    def boom() -> dict[str, str]:
        raise RuntimeError("postgres password leaked at /home/secret/db.py")

    @app.get("/contract")
    def contract() -> dict[str, str]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CONTRACT_VALIDATION_FAILED",
                "message": "internal stack trace /home/secret",
            },
        )

    @app.post("/validate")
    def validate(body: _Body) -> dict[str, str]:
        return {"name": body.name}

    return app


class GlobalEnvelopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(_app(), raise_server_exceptions=False)

    def test_unhandled_error_is_translated_without_leaking_details(self) -> None:
        response = self.client.get("/boom", headers={"X-Trace-Id": "trc_test_001"})

        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertEqual(body["code"], "INTERNAL_ERROR")
        self.assertIn("userMessage", body)
        self.assertEqual(body["errorOptions"], [])
        self.assertEqual(body["traceId"], "trc_test_001")
        self.assertEqual(response.headers["X-Trace-Id"], "trc_test_001")
        serialized = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("/home/secret", serialized)
        self.assertNotIn("password", serialized)

    def test_known_code_uses_business_message(self) -> None:
        response = self.client.get("/contract")

        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["code"], "CONTRACT_VALIDATION_FAILED")
        self.assertEqual(body["severity"], "DECISION_REQUIRED")
        self.assertIn("数据库表", body["userMessage"])
        self.assertEqual(body["errorOptions"], [])
        self.assertTrue(body["traceId"].startswith("trc_"))
        self.assertNotIn("/home/secret", json.dumps(body, ensure_ascii=False))

    def test_validation_error_is_friendly_and_field_scoped(self) -> None:
        response = self.client.post("/validate", json={})

        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["code"], "REQUEST_VALIDATION_FAILED")
        self.assertIn("提交内容", body["userMessage"])
        self.assertEqual(body["diagnostic"]["fields"][0]["loc"], "body.name")


class _BrokenBuffer:
    run_id = "run_sse_1"
    trace_id = "trc_sse_1"
    last_seq = 7

    async def subscribe(self, after_seq: int):  # noqa: ANN201
        yield SimpleNamespace(
            type="node.started",
            to_sse=lambda: (
                'id: 1\nevent: run.event\ndata: {"seq":1,"type":"node.started"}\n\n'
            ),
        )
        raise RuntimeError("coder node exploded at /home/secret/coder.py")

    def emit(self, *args: Any, **kwargs: Any) -> Any:
        # 模拟缓冲区同时不可用：生成器必须合成终态帧，绝不能挂起。
        raise RuntimeError("buffer unavailable")


class SseTerminalTest(unittest.TestCase):
    def test_stream_error_emits_terminal_envelope(self) -> None:
        async def collect() -> list[str]:
            return [
                frame
                async for frame in stream_event_frames(
                    _BrokenBuffer(),  # type: ignore[arg-type]
                    0,
                    terminal_event_type="run.failed",
                )
            ]

        frames = asyncio.run(collect())

        self.assertTrue(frames)
        terminal = [item for item in frames if '"type":"run.failed"' in item]
        self.assertEqual(len(terminal), 1, frames)
        data_line = next(
            line for line in terminal[0].splitlines() if line.startswith("data: ")
        )
        event = json.loads(data_line[len("data: ") :])
        payload = event["payload"]
        self.assertEqual(payload["status"], "FAILED")
        self.assertEqual(
            payload["userMessage"], "系统遇到未预期问题，已记录诊断编号，请稍后重试。"
        )
        self.assertEqual(payload["errorOptions"], [])
        self.assertEqual(payload["traceId"], "trc_sse_1")
        self.assertEqual(payload["errorEnvelope"]["code"], "INTERNAL_ERROR")
        self.assertNotIn("/home/secret", json.dumps(event, ensure_ascii=False))


class EnvelopeBuilderTest(unittest.TestCase):
    def test_translate_exception_keeps_code_but_hides_message(self) -> None:
        envelope = translate_exception(
            RuntimeError("connection refused 127.0.0.1:3306"),
            trace_id="trc_unit",
            run_id="run_unit",
            stage="unit",
        )

        self.assertEqual(envelope.code, "INTERNAL_ERROR")
        self.assertNotIn("3306", envelope.user_message)
        self.assertEqual(envelope.trace_id, "trc_unit")
        self.assertEqual(envelope.error_options, ())

    def test_known_code_builder_exposes_contract_severity(self) -> None:
        envelope = envelope_from_code(
            "CONTRACT_VALIDATION_FAILED",
            trace_id="trc_contract",
            run_id="run_contract",
        )
        body = envelope.to_dict()

        self.assertEqual(body["code"], "CONTRACT_VALIDATION_FAILED")
        self.assertEqual(body["severity"], "DECISION_REQUIRED")
        self.assertIn("errorOptions", body)
        self.assertEqual(body["detail"]["code"], "CONTRACT_VALIDATION_FAILED")


if __name__ == "__main__":
    unittest.main()
