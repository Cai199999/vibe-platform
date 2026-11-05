"""Architect RAG（Markdown + BM25）单元测试。

运行：

    cd services/orchestrator
    PYTHONPATH=. python -m unittest tests.test_rag -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_core.rag import RAGProviderError, get_rag_provider
from ai_core.rag.markdown import MarkdownRAGProvider


class MarkdownRAGTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1] / "knowledge" / "fullstack"
        self.provider = MarkdownRAGProvider(self.root)

    def test_retrieve_rest_api_norms(self) -> None:
        chunks = self.provider.retrieve("REST API 分页 接口 规范", top_k=3)
        self.assertTrue(chunks)
        self.assertTrue(all(chunk.content.strip() for chunk in chunks))
        self.assertEqual(
            [chunk.score for chunk in chunks],
            sorted((chunk.score for chunk in chunks), reverse=True),
        )
        self.assertTrue(any("rest" in chunk.path.lower() for chunk in chunks))

    def test_retrieve_database_schema(self) -> None:
        chunks = self.provider.retrieve("MySQL CREATE TABLE 外键 索引", top_k=3)
        self.assertTrue(chunks)
        joined = "\n".join(chunk.content for chunk in chunks)
        self.assertTrue("CREATE TABLE" in joined or "索引" in joined or "DDL" in joined)

    def test_citation_shape(self) -> None:
        chunk = self.provider.retrieve("全栈脚手架 目录结构", top_k=1)[0]
        citation = chunk.to_citation()
        self.assertEqual(
            set(citation),
            {"docId", "title", "section", "path", "score"},
        )
        self.assertGreaterEqual(citation["score"], 0)

    def test_empty_knowledge_dir_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = MarkdownRAGProvider(tmp)
            with self.assertRaises(RAGProviderError):
                provider.retrieve("REST")

    def test_factory_rejects_unknown_provider(self) -> None:
        with self.assertRaises(RAGProviderError):
            get_rag_provider("pgvector")


if __name__ == "__main__":
    unittest.main()
