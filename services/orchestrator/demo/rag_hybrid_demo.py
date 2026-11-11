"""混合 RAG 离线演示：BM25 + Chroma 向量检索 + RRF 融合。

运行（在 services/orchestrator 下）：

    PYTHONPATH=. python demo/rag_hybrid_demo.py

默认使用临时 Chroma 目录，不污染 data/chroma；可通过 --persist 指定
持久化目录以复用已有索引。Embedding 为本地 ONNX MiniLM，不调用任何
外部 API。
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from ai_core.adapters.rag.chroma import ChromaRAGProvider
from ai_core.adapters.rag.hybrid import HybridRAGProvider
from ai_core.adapters.rag.markdown import MarkdownRAGProvider
from ai_core.ports.rag import RetrievedChunk

QUESTIONS = (
    "我们公司的 API 用什么鉴权方式？",
    "JWT 过期时间设多久？",
)


def _print_chunks(label: str, chunks: list[RetrievedChunk]) -> None:
    print(f"\n  {label}:")
    if not chunks:
        print("    （无结果）")
        return
    for rank, chunk in enumerate(chunks[:3], start=1):
        content = " ".join(chunk.content.split())
        print(
            f"    #{rank} score={chunk.score:.4f} " f"[{chunk.path} / {chunk.section}]"
        )
        # 展示足够长的正文，确保关键条款（如“15 分钟”）出现在输出中。
        print(f"        {content[:520]}...")


def main() -> int:
    parser = argparse.ArgumentParser(description="Hybrid RAG 检索演示")
    parser.add_argument(
        "--persist",
        default="",
        help="Chroma 持久化目录；留空使用临时目录",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1] / "knowledge" / "enterprise_docs"
    if not root.exists():
        raise SystemExit(f"企业文档目录不存在: {root}")

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if args.persist:
        persist_dir = Path(args.persist)
    else:
        temp_dir = tempfile.TemporaryDirectory(prefix="vibe-chroma-demo-")
        persist_dir = Path(temp_dir.name)

    sparse = MarkdownRAGProvider(root, max_chunk_chars=900)
    dense = ChromaRAGProvider(root, persist_dir=persist_dir, max_chunk_chars=900)
    hybrid = HybridRAGProvider(
        sparse_provider=sparse,
        dense_provider=dense,
        rrf_k=60,
        candidate_multiplier=3,
    )

    print(f"知识库: {root}")
    print(f"向量库: {persist_dir}（本地 Chroma PersistentClient）")
    print("融合算法: RRF，k=60；每路召回 top_k×3 后合并重排")

    try:
        for question in QUESTIONS:
            print("\n" + "=" * 72)
            print(f"问题：{question}")
            bm25_chunks = sparse.retrieve(question, top_k=3)
            vector_chunks = dense.retrieve(question, top_k=3)
            fused_chunks = hybrid.retrieve(question, top_k=3)

            _print_chunks("BM25 Top-3", bm25_chunks)
            _print_chunks("Chroma 向量 Top-3", vector_chunks)
            _print_chunks("RRF 融合 Top-3", fused_chunks)

            for rank, chunk in enumerate(fused_chunks[:3], start=1):
                sources = ",".join(chunk.metadata.get("retrievalSources", []))
                print(
                    f"    融合 #{rank}: {chunk.section} "
                    f"(sources={sources or '-'}, "
                    f"bm25Rank={chunk.metadata.get('bm25Rank')}, "
                    f"chromaRank={chunk.metadata.get('chromaRank')})"
                )
        print("\n" + "=" * 72)
        print("混合检索演示完成。")
        return 0
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
