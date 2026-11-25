"""G4 上下文：多轮对话必须读取 project summary，不重复问已确认决策。"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from ai_core.architect.llm import (
    ArchitectContext,
    DeepSeekArchitectLLM,
    _analysis_human,
    _contract_human,
)
from ai_core.ports.model import ModelClient


class _CapturingClient(ModelClient):
    def __init__(self, payload: str):
        self.payload = payload
        self.human = ""
        self.system = ""

    def generate(self, task: str, system: str, human: str) -> str:
        self.system = system
        self.human = human
        return self.payload

    def generate_json(
        self,
        task: str,
        system: str,
        human: str,
        *,
        json_schema: dict | None = None,
    ) -> str:
        self.system = system
        self.human = human
        return self.payload


ANALYSIS_PAYLOAD = json.dumps(
    {
        "app_shape": "full_stack_system",
        "confidence_score": 0.95,
        "missing_slots": [],
        "questions": [],
        "slots": {
            "database.engine": "mysql",
            "backend.framework": "java_spring_boot",
            "business.key_flows": "项目 -> 任务 -> 成果 -> 质检",
            "data.entities": "Project;Task;Deliverable;QCRecord",
        },
        "tech_stack": {},
        "business_flows": [],
        "entities": [],
        "assumptions": ["数据库已确认 MySQL 8，不再重复询问"],
        "reasoning": "需求与目的优先；数据库已确认。",
    },
    ensure_ascii=False,
)

SUMMARY = (
    "已确认：数据库 MySQL 8；作业员只能看自己任务；"
    "不要重复询问数据库或权限模型。"
)


def _context() -> ArchitectContext:
    return ArchitectContext(
        project_id="proj_g4",
        project_name="测绘任务平台",
        source_files={},
        ui_history=[
            {"role": "user", "content": "数据库用 MySQL，不要再问数据库。"},
            {"role": "assistant", "content": "已记录：MySQL 8，不再追问。"},
            {"role": "user", "content": "再加一个成果导出按钮。"},
        ],
        slots={"database.engine": "mysql"},
        app_shape="full_stack_system",
        round=2,
        threshold=0.85,
        project_summary=SUMMARY,
        latest_changes="新增成果导出按钮",
        project_mode="NEW_PROJECT",
        output_phase="BACKEND_BINDING",
    )


class G4ContextTests(unittest.TestCase):
    def test_analysis_prompt_contains_project_summary(self) -> None:
        context = _context()
        human = _analysis_human(context)
        self.assertIn(SUMMARY, human)
        self.assertIn("成果导出按钮", human)

    def test_contract_prompt_contains_project_summary(self) -> None:
        context = _context()
        human = _contract_human(context)
        self.assertIn(SUMMARY, human)
        self.assertIn("MySQL", human)

    def test_analyze_does_not_reask_confirmed_database(self) -> None:
        client = _CapturingClient(ANALYSIS_PAYLOAD)
        llm = DeepSeekArchitectLLM(
            settings=SimpleNamespace(
                deepseek_api_key="test-key",
                architect_json_retry_limit=0,
                architect_llm_retry_limit=0,
            ),
            client=client,
        )
        analysis = llm.analyze(_context())
        self.assertIn(SUMMARY, client.human)
        self.assertEqual(analysis.questions, [])
        self.assertEqual(analysis.slots.get("database.engine"), "mysql")
        self.assertTrue(
            any("MySQL" in item for item in analysis.assumptions),
            analysis.assumptions,
        )


if __name__ == "__main__":
    unittest.main()
