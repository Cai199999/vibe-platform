"""VISUAL_V0 契约：虚拟树由确定性骨架生成，避免模型输出超限。"""

from __future__ import annotations

import unittest

from ai_core.architect.llm import (
    ArchitectContext,
    ContractDraft,
    DeepSeekArchitectLLM,
)
from ai_core.domain.visual_document import validate_visual_document


def _context(output_phase: str = "VISUAL_V0") -> ArchitectContext:
    return ArchitectContext(
        project_id="proj_v0",
        project_name="AI 求职助理",
        source_files={},
        ui_history=[],
        slots={},
        app_shape="full_stack_system",
        round=0,
        threshold=0.85,
        output_phase=output_phase,
    )


def _draft() -> ContractDraft:
    return ContractDraft(
        contract_markdown="# V0 原型",
        page_layout=[
            {"region": "header", "title": "顶部导航"},
            {"region": "main", "title": "主内容区"},
        ],
        components=[
            {
                "name": "简历列表",
                "role": "list",
                "section": "主内容区",
                "description": "展示已上传简历与 JD 匹配结果",
            },
            {
                "name": "定制简历草稿",
                "role": "container",
                "section": "主内容区",
                "description": "展示针对某个 JD 的定制简历草稿",
            },
        ],
        frontend_routes=[{"path": "/", "name": "求职工作台"}],
    )


class VisualV0DeterministicTest(unittest.TestCase):
    def test_v0_builds_valid_visual_document(self) -> None:
        result = DeepSeekArchitectLLM._ensure_v0_visual(  # type: ignore[arg-type]
            object(), _context(), _draft()
        )

        self.assertTrue(result.visual_document)
        self.assertEqual(result.api_routes, [])
        self.assertEqual(result.database_ddl, "")
        self.assertEqual(result.relations, [])
        self.assertEqual(result.component_interactions, [])
        report = validate_visual_document(result.visual_document)
        self.assertTrue(report.ok, report.errors)
        root = result.visual_document.get("root") or {}
        self.assertEqual(root.get("type"), "page")

    def test_non_v0_phase_is_untouched(self) -> None:
        draft = _draft()
        result = DeepSeekArchitectLLM._ensure_v0_visual(  # type: ignore[arg-type]
            object(), _context("FULL"), draft
        )
        self.assertIs(result, draft)


if __name__ == "__main__":
    unittest.main()
