"""GeoAI 复用目录已接入 Architect RAG 的确定性测试。"""

from __future__ import annotations

import unittest
from pathlib import Path

from ai_core.adapters.rag.markdown import MarkdownRAGProvider
from ai_core.architect.llm import ArchitectContext, _contract_human

REPO_ROOT = Path(__file__).resolve().parents[3]
KNOWLEDGE_ROOT = REPO_ROOT / "services" / "orchestrator" / "knowledge"
CATALOG = KNOWLEDGE_ROOT / "geoai_reuse" / "catalog.md"


class GeoAIReuseCatalogTests(unittest.TestCase):
    def test_catalog_covers_geoai_projects(self) -> None:
        text = CATALOG.read_text(encoding="utf-8")
        for name in (
            "opengeos/geoai",
            "decision-labs/geoai.js",
            "azavea/raster-vision",
            "sentinel-hub/eo-learn",
            "MEKXH/golem",
            "microsoft/torchgeo",
        ):
            self.assertIn(name, text)

    def test_architect_rag_retrieves_catalog_for_gis_query(self) -> None:
        provider = MarkdownRAGProvider(KNOWLEDGE_ROOT)
        chunks = provider.retrieve("遥感测绘 GIS GeoAI 任务管理 复用方案", top_k=5)
        self.assertTrue(chunks)
        self.assertTrue(
            any(chunk.path == "geoai_reuse/catalog.md" for chunk in chunks),
            [(chunk.path, chunk.title) for chunk in chunks],
        )

    def test_catalog_marks_reuse_decision(self) -> None:
        text = CATALOG.read_text(encoding="utf-8")
        self.assertIn("REUSE / ADAPT / BUILD", text)
        self.assertIn("不会自动安装", text)

    def test_contract_prompt_carries_catalog_chunks(self) -> None:
        provider = MarkdownRAGProvider(KNOWLEDGE_ROOT)
        chunks = provider.retrieve("遥感测绘 GIS GeoAI 任务管理 复用方案", top_k=5)
        context = ArchitectContext(
            project_id="prj_test",
            project_name="测绘任务平台",
            source_files={},
            ui_history=[],
            slots={},
            app_shape="full_stack_system",
            round=1,
            threshold=0.6,
            retrieved_chunks=[
                {
                    "docId": chunk.path,
                    "title": chunk.title,
                    "section": chunk.section,
                    "content": chunk.content,
                    "metadata": {"source": "knowledge"},
                }
                for chunk in chunks
            ],
        )
        human = _contract_human(context)
        self.assertIn("geoai_reuse/catalog.md", human)
        self.assertIn("TorchGeo", human)


if __name__ == "__main__":
    unittest.main()
