"""局部 VisualNode Agent：严格 JSON、ID 重写与 Schema 校验测试。"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any

from ai_core.domain.visual_agent import (
    VisualNodeGenerationError,
    VisualNodeGenerationRequest,
)
from ai_core.domain.visual_document import VisualDocument, VisualNode
from ai_core.ports.model import ModelClientError
from ai_core.visual_agent.service import VisualNodeGenerator


class _FakeModel:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, str]] = []

    def generate(self, task: str, system: str, human: str) -> str:
        self.calls.append({"task": task, "system": system, "human": human})
        if not self.responses:
            return "[]"
        return self.responses.pop(0)


class _Settings:
    model_fast_name = "fake-fast"
    deepseek_model = "fake-deepseek"


def _request(**overrides: Any) -> VisualNodeGenerationRequest:
    payload = {
        "projectId": "proj_visual_agent",
        "documentId": "vdoc_agent",
        "nodeId": "nd_target",
        "instruction": "生成一个登录表单",
        "context": {
            "id": "nd_target",
            "type": "container",
            "name": "登录卡片",
            "children": [],
        },
        "existingIds": ["nd_target"],
        "maxNodes": 40,
    }
    payload.update(overrides)
    return VisualNodeGenerationRequest.model_validate(payload)


FORM_ARRAY = json.dumps(
    [
        {
            "id": "model_will_be_rewritten",
            "type": "form",
            "name": "登录表单",
            "layout": {"display": "flex", "direction": "column", "gap": "12px"},
            "children": [
                {"type": "text", "props": {"text": "账号登录"}},
                {
                    "type": "input",
                    "props": {"placeholder": "用户名"},
                    "a11y": {"label": "用户名"},
                },
                {
                    "type": "input",
                    "props": {"placeholder": "密码"},
                    "a11y": {"label": "密码"},
                },
                {
                    "type": "button",
                    "props": {"text": "登录", "variant": "primary"},
                },
            ],
        }
    ],
    ensure_ascii=False,
)


class VisualNodeAgentTest(unittest.TestCase):
    def test_generates_schema_valid_nodes_and_rewrites_ids(self) -> None:
        model = _FakeModel([FORM_ARRAY])
        service = VisualNodeGenerator(client=model, settings=_Settings())  # type: ignore[arg-type]

        response = service.generate(_request())

        self.assertEqual(len(response.nodes), 1)
        form = response.nodes[0]
        self.assertEqual(form["type"], "form")
        self.assertEqual(len(form["children"]), 4)
        self.assertTrue(form["id"].startswith("nd_"))
        self.assertNotEqual(form["id"], "model_will_be_rewritten")
        ids = [form["id"]] + [child["id"] for child in form["children"]]
        self.assertEqual(len(ids), len(set(ids)))

        document = VisualDocument.model_validate(
            {
                "schemaVersion": "1.0",
                "docId": "vdoc_check",
                "root": VisualNode(
                    id="nd_root",
                    type="page",
                    children=[
                        VisualNode.model_validate(node) for node in response.nodes
                    ],
                ).model_dump(by_alias=True),
            }
        )
        self.assertEqual(document.root.children[0].type, "form")
        self.assertIn("严禁输出 JSX", model.calls[0]["system"])
        self.assertEqual(model.calls[0]["task"], "visual_node")

    def test_jsx_or_markdown_output_is_rejected(self) -> None:
        model = _FakeModel(
            [
                "```jsx\n<Button>提交</Button>\n```",
                "```jsx\n<Button>提交</Button>\n```",
            ]
        )
        service = VisualNodeGenerator(client=model, settings=_Settings())  # type: ignore[arg-type]

        with self.assertRaises(VisualNodeGenerationError) as ctx:
            service.generate(_request())

        self.assertEqual(ctx.exception.code, "VISUAL_NODE_INVALID")
        self.assertEqual(len(model.calls), 2)

    def test_upstream_model_outage_is_retryable_503(self) -> None:
        class _DownModel:
            def __init__(self) -> None:
                self.calls = 0

            def generate(self, task: str, system: str, human: str) -> str:
                self.calls += 1
                raise ModelClientError("DeepSeek 调用失败: Connection error.")

        model = _DownModel()
        service = VisualNodeGenerator(client=model, settings=_Settings())  # type: ignore[arg-type]

        with self.assertRaises(VisualNodeGenerationError) as ctx:
            service.generate(_request())

        self.assertEqual(ctx.exception.code, "VISUAL_LLM_UNAVAILABLE")
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(model.calls, 2)

    def test_page_root_and_absolute_coordinates_are_rejected(self) -> None:
        for payload in (
            [{"type": "page", "children": []}],
            [
                {
                    "type": "container",
                    "x": 20,
                    "children": [{"type": "text", "props": {"text": "x"}}],
                }
            ],
            [
                {
                    "type": "container",
                    "style": {"tailwind": ["absolute", "top-4"]},
                    "children": [],
                }
            ],
        ):
            model = _FakeModel([json.dumps(payload), json.dumps(payload)])
            service = VisualNodeGenerator(client=model, settings=_Settings())  # type: ignore[arg-type]
            with self.assertRaises(VisualNodeGenerationError):
                service.generate(_request())
            self.assertEqual(len(model.calls), 2)

    def test_repairs_after_first_invalid_output(self) -> None:
        bad = json.dumps([{"type": "text", "children": [{"type": "text"}]}])
        model = _FakeModel([bad, FORM_ARRAY])
        service = VisualNodeGenerator(client=model, settings=_Settings())  # type: ignore[arg-type]

        response = service.generate(_request())

        self.assertTrue(response.repaired)
        self.assertEqual(len(model.calls), 2)
        self.assertIn("上一次输出不合法", model.calls[1]["human"])

    def test_accepts_nodes_wrapper(self) -> None:
        model = _FakeModel([json.dumps({"nodes": json.loads(FORM_ARRAY)})])
        service = VisualNodeGenerator(client=model, settings=_Settings())  # type: ignore[arg-type]
        response = service.generate(_request())
        self.assertEqual(len(response.nodes), 1)
        self.assertEqual(len(response.nodes[0]["children"]), 4)

    def test_enforces_max_nodes(self) -> None:
        model = _FakeModel([FORM_ARRAY, FORM_ARRAY])
        service = VisualNodeGenerator(client=model, settings=_Settings())  # type: ignore[arg-type]
        with self.assertRaises(VisualNodeGenerationError):
            service.generate(_request(maxNodes=3))


if __name__ == "__main__":
    unittest.main()
