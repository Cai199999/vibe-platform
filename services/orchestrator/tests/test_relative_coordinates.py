"""ADD 相对坐标契约回归测试。

背景：画布圈选 ADD 时，旧的 ``relativeTarget`` 只有归一化 x/y/width/height。
模型无法知道圈选区域在父容器中的真实像素位置，只能按比例猜测插入顺序，
导致“在空白处画圈加按钮”最终落点与圈不一致。

本文件锁定升级后的契约：
  1. 领域模型与上下文同时保留归一化坐标与像素坐标；
  2. 导出上下文 VIBE_CONTEXT.md 渲染像素字段；
  3. Planner / Coder Prompt 明确要求按像素坐标落位，并提供 flex/grid/
     absolute / margin 的实现指引。
"""

from __future__ import annotations

import json
import unittest

from ai_core.agent.context_doc import render_vibe_context
from ai_core.agent.llm import DeepSeekAgentLLM
from ai_core.agent.service import RunExecutionService
from ai_core.config import Settings
from ai_core.domain.agent import ExecuteRunRequest
from ai_core.ports.model import ModelClient

RELATIVE_TARGET = {
    "x": 0.25,
    "y": 0.5,
    "width": 0.2,
    "height": 0.1,
    "relativeX": 320.5,
    "relativeY": 180.25,
    "widthPx": 256.0,
    "heightPx": 72.5,
    "parentWidth": 1280.0,
    "parentHeight": 360.0,
    "coordinateSpace": "parent-relative",
    "parentSelector": '[data-source="card"]',
    "parentMarker": "card",
}


def _request() -> ExecuteRunRequest:
    return ExecuteRunRequest.model_validate(
        {
            "runId": "run_relative",
            "projectId": "project_relative",
            "annotations": [
                {
                    "annotationId": "ann_relative",
                    "action": "ADD",
                    "target": {
                        "mode": "source",
                        "source": {"file": "src/App.jsx", "line": 5},
                        "selector": '[data-source="card"]',
                    },
                    "intent": {
                        "kind": "UI_ONLY",
                        "ui": {"summary": "在卡片中间加一个按钮"},
                    },
                    "payload": {"text": "在卡片中间加一个按钮"},
                    "relativeTarget": RELATIVE_TARGET,
                }
            ],
            "files": {
                "src/App.jsx": (
                    "export default function App() {\n"
                    '  return <main className="page" data-source="page">\n'
                    '    <section data-source="card" data-source-kind="container" />\n'
                    "  </main>\n"
                    "}\n"
                )
            },
        }
    )


class _CaptureClient(ModelClient):
    """记录 system / human Prompt，返回可解析的固定 JSON。"""

    def __init__(self, response: str) -> None:
        self.response = response
        self.system = ""
        self.human = ""

    def generate(self, task: str, system: str, human: str) -> str:
        self.system = system
        self.human = human
        return self.response


class RelativeCoordinateContextTest(unittest.TestCase):
    def test_annotation_context_keeps_pixel_fields(self) -> None:
        request = _request()
        annotation = request.effective_annotations[0]
        context = RunExecutionService._build_annotation_context(
            request,
            ["src/App.jsx"],
            {},
            request.files,
        )
        payload = context["annotations"][0]["annotation"]["relativeTarget"]

        # 归一化字段保持兼容。
        self.assertEqual(payload["x"], 0.25)
        self.assertEqual(payload["y"], 0.5)
        # 像素字段必须原样进入模型上下文。
        self.assertEqual(payload["relativeX"], 320.5)
        self.assertEqual(payload["relativeY"], 180.25)
        self.assertEqual(payload["widthPx"], 256.0)
        self.assertEqual(payload["heightPx"], 72.5)
        self.assertEqual(payload["parentWidth"], 1280.0)
        self.assertEqual(payload["parentHeight"], 360.0)
        self.assertEqual(payload["coordinateSpace"], "parent-relative")
        # 整个上下文必须可 JSON 序列化（历史 datetime 500 的同类防线）。
        json.dumps(context, ensure_ascii=False)

    def test_vibe_context_renders_pixel_fields(self) -> None:
        request = _request()
        markdown = render_vibe_context(
            request,
            run_id="run_relative",
            sandbox_id="sandbox_relative",
            target_files=["src/App.jsx"],
        )
        self.assertIn("relativeX", markdown)
        self.assertIn("320.5", markdown)
        self.assertIn("relativeY", markdown)
        self.assertIn("180.25", markdown)
        self.assertIn("parentWidth", markdown)
        self.assertIn("1280.0", markdown)
        self.assertIn("parentHeight", markdown)
        self.assertIn("360.0", markdown)


class RelativeCoordinatePromptTest(unittest.TestCase):
    def _llm(self, response: str) -> tuple[DeepSeekAgentLLM, _CaptureClient]:
        client = _CaptureClient(response)
        return (
            DeepSeekAgentLLM(Settings(deepseek_api_key="test-key"), client=client),
            client,
        )

    def test_planner_prompt_mentions_pixel_contract(self) -> None:
        llm, client = self._llm(
            json.dumps({"plan": "在卡片中间加按钮", "confidence_score": 0.95})
        )
        llm.plan(
            [{"annotationId": "ann_relative", "relativeTarget": RELATIVE_TARGET}],
            {"src/App.jsx": "<main />"},
        )
        # 像素数值随标注透传给 Planner。
        self.assertIn('"relativeX": 320.5', client.human)
        self.assertIn('"parentHeight": 360.0', client.human)
        self.assertIn("relativeX", client.system)
        self.assertIn("parentWidth", client.system)

    def test_coder_prompt_requires_transform_move_and_relative_hints(self) -> None:
        llm, client = self._llm(
            json.dumps(
                {
                    "mode": "files",
                    "files": {
                        "src/App.jsx": (
                            "export default function App() {\n"
                            '  return <main className="page">\n'
                            "    <section data-source=\"card\" style={{position:'relative'}}>\n"
                            "      <button style={{position:'absolute',left:320.5,top:180.25}}>提交</button>\n"
                            "    </section>\n"
                            "  </main>\n"
                            "}\n"
                        )
                    },
                    "summary": "定位插入按钮",
                }
            )
        )
        llm.generate(
            [{"annotationId": "ann_relative", "relativeTarget": RELATIVE_TARGET}],
            plan="在卡片中间加按钮",
            current_files={"src/App.jsx": "<main />"},
            target_files=["src/App.jsx"],
            verify_result={"ok": False},
            retry_count=0,
        )
        # 相对坐标只用于插入顺序；移动必须使用 transform，禁止改宽高。
        self.assertIn("relativeX", client.system)
        self.assertIn("flex/grid", client.system)
        self.assertIn("transform: translate3d(dx, dy, 0)", client.system)
        self.assertIn("禁止修改 width / height", client.system)
        self.assertIn('"relativeX": 320.5', client.human)


if __name__ == "__main__":
    unittest.main()
