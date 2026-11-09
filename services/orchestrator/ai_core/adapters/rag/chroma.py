"""Chroma RAG 适配器占位。

M2.5 默认使用 ``MarkdownRAGProvider``。后续接入 Chroma 时只需实现本类
并在 ``get_rag_provider`` 中按配置返回，Architect Graph 无需改动。

约束：
  - 文档 ID 使用 ``{doc_id}:{section}``，与 Markdown Provider 的 citation
    结构保持一致；
  - embedding 模型通过环境变量配置，禁止把 API Key 写入向量 metadata；
  - 检索结果仍转换为 ``RetrievedChunk``，不向上层暴露 Chroma 类型。
"""

from __future__ import annotations

from typing import Any

from ai_core.ports.rag import RAGProvider, RAGProviderError, RetrievedChunk


class ChromaRAGProvider(RAGProvider):
    name = "chroma"

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise RAGProviderError(
            "ChromaRAGProvider 尚未启用；M2.5 请设置 ARCHITECT_RAG_PROVIDER=markdown"
        )

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        raise RAGProviderError("ChromaRAGProvider 尚未启用")
