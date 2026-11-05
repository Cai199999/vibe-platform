"""RAGProvider 抽象。

M2.5 只依赖最小检索接口，不把 Markdown / Chroma / pgvector 的实现细节
泄漏到 Architect Graph。未来切换重型向量库时，只需要注册新的 Provider。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RetrievedChunk:
    """一条检索结果。

    ``content`` 会进入 LLM Prompt 和契约引用，必须控制长度；完整知识库
    内容不应通过 SSE 下发给前端。
    """

    doc_id: str
    title: str
    section: str
    path: str
    content: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_citation(self) -> dict[str, Any]:
        return {
            "docId": self.doc_id,
            "title": self.title,
            "section": self.section,
            "path": self.path,
            "score": round(float(self.score), 4),
        }


class RAGProvider(ABC):
    """知识检索抽象。"""

    name: str = "base"

    @abstractmethod
    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        """按查询返回最相关的知识块，按 score 降序。"""


class RAGProviderError(Exception):
    """RAG Provider 配置或检索失败。"""
