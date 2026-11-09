"""兼容层：RAG 工厂已迁移至 ``ai_core.adapters.rag``。"""

from ai_core.adapters.rag import (
    ChromaRAGProvider,
    MarkdownRAGProvider,
    RAGProvider,
    RAGProviderError,
    get_rag_provider,
    reset_rag_provider,
)
from ai_core.ports.rag import RetrievedChunk

__all__ = [
    "RAGProvider",
    "RAGProviderError",
    "RetrievedChunk",
    "MarkdownRAGProvider",
    "ChromaRAGProvider",
    "get_rag_provider",
    "reset_rag_provider",
]
