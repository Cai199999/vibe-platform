"""混合检索：BM25 稀疏召回 + Chroma 向量召回 + RRF 融合。

两路查询并行发起（``ThreadPoolExecutor``），各自取 candidate_k 条结果，
再用 Reciprocal Rank Fusion（RRF）合并：

    rrf(d) = Σ_source 1 / (k + rank_source(d))

RRF 只依赖排名、不依赖两路分数量纲，因此不需要把 BM25 分数与余弦相似度
强行归一化。最终返回 Top-K，并在 metadata 中保留每一路的名次，便于调试
与评估。

降级策略：单路失败不影响另一路；两路都失败才抛出 RAGProviderError。
RAG 是增强能力，不能成为 Architect / Planner 的单点故障。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_core.adapters.rag.chroma import ChromaRAGProvider
from ai_core.adapters.rag.markdown import MarkdownRAGProvider
from ai_core.config import get_settings
from ai_core.ports.rag import RAGProvider, RAGProviderError, RetrievedChunk

logger = logging.getLogger(__name__)


@dataclass
class _FusedChunk:
    """融合过程中的可变累计器。"""

    chunk: RetrievedChunk
    rrf_score: float = 0.0
    ranks: dict[str, int] = field(default_factory=dict)
    raw_scores: dict[str, float] = field(default_factory=dict)


class HybridRAGProvider(RAGProvider):
    """BM25 + Chroma 的倒数排名融合检索器。"""

    name = "hybrid"

    def __init__(
        self,
        sparse_provider: RAGProvider | None = None,
        dense_provider: RAGProvider | None = None,
        *,
        rrf_k: int | None = None,
        candidate_multiplier: int | None = None,
    ) -> None:
        settings = get_settings()
        # 两路必须索引同一语料，RRF 的排名才可比较。knowledge/ 同时包含
        # fullstack 开发规范与 enterprise_docs 企业长文档：
        #   - BM25 保证接口名、字段名、规范条款等关键词精确命中；
        #   - Chroma 负责“鉴权方式”“过期时间”这类语义近似召回。
        corpus_root = Path(settings.rag_knowledge_root)
        self._sparse = sparse_provider or MarkdownRAGProvider(corpus_root)
        self._dense = dense_provider or ChromaRAGProvider(
            corpus_root,
            collection_name=settings.chroma_hybrid_collection_name,
        )
        self._rrf_k = max(1, int(rrf_k or settings.rag_hybrid_rrf_k))
        self._candidate_multiplier = max(
            1, int(candidate_multiplier or settings.rag_hybrid_candidate_multiplier)
        )

    @property
    def sparse_provider(self) -> RAGProvider:
        return self._sparse

    @property
    def dense_provider(self) -> RAGProvider:
        return self._dense

    # --------------------------------------------------------
    # 两路召回
    # --------------------------------------------------------
    def _retrieve_source(
        self,
        label: str,
        provider: RAGProvider,
        query: str,
        candidate_k: int,
        filters: dict[str, Any] | None,
    ) -> tuple[str, list[RetrievedChunk], str | None]:
        try:
            chunks = provider.retrieve(query, top_k=candidate_k, filters=filters)
            return label, chunks, None
        except Exception as exc:  # noqa: BLE001 - RAG 降级必须吞掉单路异常
            logger.warning("hybrid rag source failed source=%s: %s", label, exc)
            return label, [], str(exc)

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        query = (query or "").strip()
        if not query:
            return []
        top_k = max(1, top_k)
        candidate_k = max(10, top_k * self._candidate_multiplier)

        # 显式并行：BM25 与向量检索互不阻塞；任一慢查询不会串行叠加延迟。
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="hybrid-rag") as pool:
            futures = [
                pool.submit(
                    self._retrieve_source,
                    "bm25",
                    self._sparse,
                    query,
                    candidate_k,
                    filters,
                ),
                pool.submit(
                    self._retrieve_source,
                    "chroma",
                    self._dense,
                    query,
                    candidate_k,
                    filters,
                ),
            ]
            outputs = [future.result() for future in futures]

        errors = [error for _, _, error in outputs if error]
        if len(errors) == 2:
            raise RAGProviderError("混合检索两路均失败: " + " | ".join(errors))

        fused: dict[str, _FusedChunk] = {}
        for label, chunks, error in outputs:
            if error:
                continue
            for rank, chunk in enumerate(chunks, start=1):
                # path + section 是两路共用的稳定语义键；同一段落只保留一条。
                key = f"{chunk.path}\x00{chunk.section}"
                entry = fused.get(key)
                if entry is None:
                    entry = _FusedChunk(chunk=chunk)
                    fused[key] = entry
                elif len(chunk.content) > len(entry.chunk.content):
                    entry.chunk = chunk
                entry.rrf_score += 1.0 / (self._rrf_k + rank)
                entry.ranks[label] = rank
                entry.raw_scores[label] = float(chunk.score)

        ranked = sorted(
            fused.values(),
            key=lambda item: (
                -item.rrf_score,
                min(item.ranks.values()),
                item.chunk.path,
                item.chunk.section,
            ),
        )[:top_k]

        results: list[RetrievedChunk] = []
        for item in ranked:
            metadata = dict(item.chunk.metadata)
            metadata.update(
                {
                    "retrievalSources": sorted(item.ranks),
                    "rrfScore": round(item.rrf_score, 6),
                    "rrfK": self._rrf_k,
                    "bm25Rank": item.ranks.get("bm25"),
                    "chromaRank": item.ranks.get("chroma"),
                    "bm25Score": item.raw_scores.get("bm25"),
                    "chromaScore": item.raw_scores.get("chroma"),
                }
            )
            results.append(
                RetrievedChunk(
                    doc_id=item.chunk.doc_id,
                    title=item.chunk.title,
                    section=item.chunk.section,
                    path=item.chunk.path,
                    content=item.chunk.content,
                    score=item.rrf_score,
                    metadata=metadata,
                )
            )
        return results


__all__ = ["HybridRAGProvider"]
