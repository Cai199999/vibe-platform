"""混合 RAG 单元测试：Chroma 本地索引、双路召回与 RRF 融合。

不依赖外部 Embedding 服务：测试注入确定性 Hash Embedding，避免下载
ONNX 模型；真实模型与演示文档由 ``demo/rag_hybrid_demo.py`` 验证。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from ai_core.adapters.rag.chroma import ChromaRAGProvider
from ai_core.adapters.rag.hybrid import HybridRAGProvider
from ai_core.adapters.rag.markdown import MarkdownRAGProvider
from ai_core.config import Settings
from ai_core.ports.rag import RAGProvider, RAGProviderError, RetrievedChunk


class _HashEmbedding:
    """确定性字符哈希向量，仅用于测试 Chroma 索引链路。"""

    DIM = 32

    def __call__(self, input: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in input:
            vector = [0.0] * self.DIM
            for char in text:
                vector[ord(char) % self.DIM] += 1.0
            norm = sum(value * value for value in vector) ** 0.5 or 1.0
            vectors.append([value / norm for value in vector])
        return vectors

    @staticmethod
    def name() -> str:
        return "test-hash"

    @staticmethod
    def get_config() -> dict[str, Any]:
        return {}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> "_HashEmbedding":
        return _HashEmbedding()

    @staticmethod
    def validate_config(config: dict[str, Any]) -> None:
        return None

    def max_tokens(self) -> int:
        return 256


def _chunk(
    key: str,
    content: str,
    *,
    path: str = "doc.md",
    score: float = 1.0,
) -> RetrievedChunk:
    return RetrievedChunk(
        doc_id=key,
        title="测试文档",
        section=key,
        path=path,
        content=content,
        score=score,
        metadata={"category": "test"},
    )


class _FakeProvider(RAGProvider):
    def __init__(
        self,
        name: str,
        chunks: list[RetrievedChunk] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.name = name
        self._chunks = chunks or []
        self._error = error
        self.calls: list[tuple[str, int]] = []

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        self.calls.append((query, top_k))
        if self._error:
            raise self._error
        return self._chunks[:top_k]


class ChromaProviderTest(unittest.TestCase):
    def test_local_index_and_vector_retrieve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "docs"
            root.mkdir()
            (root / "security.md").write_text(
                "# 安全规范\n\n"
                "## API 鉴权\n\n"
                "对外 API 使用 OAuth 2.1，内部服务使用 JWT Bearer Token。\n\n"
                "## JWT 有效期\n\n"
                "JWT 访问令牌过期时间 15 分钟，刷新令牌 7 天。\n",
                encoding="utf-8",
            )
            provider = ChromaRAGProvider(
                root,
                persist_dir=Path(tmp) / "chroma",
                embedding_function=_HashEmbedding(),
                max_chunk_chars=900,
            )

            chunks = provider.retrieve("JWT 过期时间 15 分钟", top_k=3)

            self.assertTrue(chunks)
            self.assertTrue(
                any("15 分钟" in chunk.content for chunk in chunks),
                [chunk.content for chunk in chunks],
            )
            self.assertEqual(chunks[0].metadata["retrievalSource"], "chroma")
            self.assertEqual(chunks[0].path, "security.md")

    def test_reopen_persistent_index_does_not_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "docs"
            root.mkdir()
            (root / "knowledge.md").write_text(
                "# 知识\n\n## 规范\n\n所有接口必须使用 HTTPS。\n",
                encoding="utf-8",
            )
            kwargs = {
                "persist_dir": Path(tmp) / "chroma",
                "embedding_function": _HashEmbedding(),
            }
            first = ChromaRAGProvider(root, **kwargs)
            self.assertTrue(first.retrieve("HTTPS 接口", top_k=2))

            second = ChromaRAGProvider(root, **kwargs)
            chunks = second.retrieve("HTTPS 接口", top_k=2)
            self.assertEqual(len(chunks), 1)
            self.assertIn("HTTPS", chunks[0].content)

    def test_empty_knowledge_dir_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = ChromaRAGProvider(
                Path(tmp) / "missing",
                persist_dir=Path(tmp) / "chroma",
                embedding_function=_HashEmbedding(),
            )
            with self.assertRaises(RAGProviderError):
                provider.retrieve("任意问题", top_k=1)


class HybridRRFTest(unittest.TestCase):
    def test_rrf_prefers_chunk_found_by_both_channels(self) -> None:
        shared = _chunk("shared", "两路都命中的 JWT 规范")
        sparse_only = _chunk("sparse", "仅 BM25 命中的规范")
        dense_only = _chunk("dense", "仅向量命中的长文档")
        sparse = _FakeProvider("bm25", [sparse_only, shared])
        dense = _FakeProvider("chroma", [shared, dense_only])
        hybrid = HybridRAGProvider(sparse, dense, rrf_k=60)

        results = hybrid.retrieve("JWT", top_k=3)

        self.assertEqual(results[0].doc_id, "shared")
        self.assertEqual(results[0].metadata["retrievalSources"], ["bm25", "chroma"])
        self.assertEqual(results[0].metadata["bm25Rank"], 2)
        self.assertEqual(results[0].metadata["chromaRank"], 1)
        self.assertAlmostEqual(expected := 1 / 62 + 1 / 61, results[0].score)
        self.assertTrue(expected > results[1].score)
        # 两路并行检索，候选集按 top_k * multiplier 扩张。
        self.assertEqual(sparse.calls[0][1], 10)
        self.assertEqual(dense.calls[0][1], 10)

    def test_single_source_failure_degrades_gracefully(self) -> None:
        sparse = _FakeProvider("bm25", [_chunk("a", "BM25 结果")])
        dense = _FakeProvider("chroma", error=RAGProviderError("模型不可用"))
        hybrid = HybridRAGProvider(sparse, dense, rrf_k=60)

        results = hybrid.retrieve("问题", top_k=3)

        self.assertEqual([chunk.doc_id for chunk in results], ["a"])
        self.assertEqual(results[0].metadata["retrievalSources"], ["bm25"])
        self.assertIsNone(results[0].metadata["chromaRank"])

    def test_both_sources_failure_raises(self) -> None:
        hybrid = HybridRAGProvider(
            _FakeProvider("bm25", error=RAGProviderError("bm25 down")),
            _FakeProvider("chroma", error=RAGProviderError("chroma down")),
            rrf_k=60,
        )
        with self.assertRaises(RAGProviderError):
            hybrid.retrieve("问题", top_k=3)

    def test_empty_query_skips_providers(self) -> None:
        sparse = _FakeProvider("bm25", [_chunk("a", "x")])
        dense = _FakeProvider("chroma", [_chunk("b", "y")])
        hybrid = HybridRAGProvider(sparse, dense)
        self.assertEqual(hybrid.retrieve("   ", top_k=3), [])
        self.assertEqual(sparse.calls, [])
        self.assertEqual(dense.calls, [])

    def test_factory_returns_hybrid(self) -> None:
        from ai_core.adapters.rag import get_rag_provider, reset_rag_provider

        reset_rag_provider()
        provider = get_rag_provider("hybrid")
        self.assertIsInstance(provider, HybridRAGProvider)
        self.assertIsInstance(provider.sparse_provider, MarkdownRAGProvider)
        self.assertIsInstance(provider.dense_provider, ChromaRAGProvider)
        reset_rag_provider()

    def test_architect_default_uses_hybrid(self) -> None:
        from ai_core.domain.architect import ArchitectRunOptions

        self.assertEqual(ArchitectRunOptions().rag_provider, "hybrid")


class PlannerKnowledgeTest(unittest.TestCase):
    def test_planner_retrieval_builds_query_and_citations(self) -> None:
        from ai_core.agent.graph import AgentGraphRunner

        fake = _FakeProvider(
            "hybrid",
            [
                _chunk(
                    "jwt",
                    "JWT 访问令牌过期时间 15 分钟。",
                    path="enterprise/security.md",
                )
            ],
        )
        runner = AgentGraphRunner.__new__(AgentGraphRunner)
        runner._settings = Settings()
        runner._rag_provider_factory = lambda name: fake

        query, context, citations = runner._retrieve_planner_knowledge(
            {"rag_provider": "hybrid", "rag_top_k": 3},
            [{"payload": {"text": "登录接口需要使用公司统一鉴权"}}],
        )

        self.assertIn("统一鉴权", query)
        self.assertIn("15 分钟", context)
        self.assertIn("enterprise/security.md", context)
        self.assertEqual(citations[0]["path"], "enterprise/security.md")
        self.assertEqual(fake.calls[0][1], 3)


if __name__ == "__main__":
    unittest.main()
