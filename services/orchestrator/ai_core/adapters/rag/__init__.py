"""RAG 适配器与组合根。

应用层只依赖 ``RAGProvider`` 端口；本模块按配置选择 Markdown BM25 或
Chroma 实现。
"""

from __future__ import annotations

from functools import lru_cache

from ai_core.adapters.rag.chroma import ChromaRAGProvider
from ai_core.adapters.rag.markdown import MarkdownRAG, MarkdownRAGProvider
from ai_core.config import get_settings
from ai_core.ports.rag import RAGProvider, RAGProviderError

__all__ = [
    "ChromaRAGProvider",
    "MarkdownRAG",
    "MarkdownRAGProvider",
    "RAGProvider",
    "RAGProviderError",
    "get_rag_provider",
    "reset_rag_provider",
]


@lru_cache
def get_rag_provider(name: str | None = None) -> RAGProvider:
    """按配置或调用方指定名称返回进程级 RAG Provider。"""
    provider = (name or get_settings().architect_rag_provider).strip().lower()
    if provider == "chroma":
        return ChromaRAGProvider()
    if provider not in {"", "markdown"}:
        raise RAGProviderError(f"未知 ARCHITECT_RAG_PROVIDER: {provider}")
    return MarkdownRAGProvider()


def reset_rag_provider() -> None:
    """测试 / 热更新知识库时清空缓存。"""
    get_rag_provider.cache_clear()
