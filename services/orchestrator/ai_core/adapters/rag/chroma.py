"""基于 Chroma PersistentClient 的本地向量检索。

设计约束：
  - 不引入独立向量数据库服务：数据落在本地目录，进程重启后自动复用；
  - 只读取 ``knowledge/enterprise_docs`` 下的长文档，硬性代码规范仍由
    Markdown Skill / BM25 负责；
  - Embedding 默认为 Chroma 内置的 ONNX MiniLM（无 API Key、无 PyTorch），
    镜像构建阶段预热模型，运行期离线可用；
  - 对上层只暴露 ``RetrievedChunk``，不泄漏 Chroma 类型。

切块规则复用 ``MarkdownRAGProvider`` 的解析函数，保证同一段落在 BM25
与向量两路里的 ``path + section`` 完全一致，RRF 才能正确去重。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path
from typing import Any, Protocol, Sequence

from ai_core.adapters.rag.markdown import parse_markdown
from ai_core.config import get_settings
from ai_core.ports.rag import RAGProvider, RAGProviderError, RetrievedChunk

logger = logging.getLogger(__name__)


class _EmbeddingFunction(Protocol):
    """Chroma EmbeddingFunction 的最小结构化契约。"""

    def __call__(self, input: Sequence[str]) -> list[list[float]]: ...


def _default_embedding_function() -> _EmbeddingFunction:
    """延迟导入 chromadb，未安装依赖时给出可读错误而不是模块导入崩溃。"""
    try:
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
    except ImportError as exc:  # pragma: no cover - 取决于部署环境
        raise RAGProviderError(
            "ChromaRAGProvider 需要 chromadb；请安装 requirements.txt 后重试"
        ) from exc
    # DefaultEmbeddingFunction 内部使用 ONNX all-MiniLM-L6-v2，
    # 不依赖 sentence-transformers / PyTorch。
    return DefaultEmbeddingFunction()


def _default_enterprise_dir() -> Path:
    configured = get_settings().enterprise_knowledge_dir.strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    candidates.append(Path("/app/knowledge/enterprise_docs"))
    candidates.append(
        Path(__file__).resolve().parents[3] / "knowledge" / "enterprise_docs"
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _default_persist_dir() -> Path:
    configured = get_settings().chroma_persist_dir.strip()
    if configured:
        return Path(configured)
    return Path("data/chroma")


def _chunk_id(path: str, section: str, content: str) -> str:
    """内容寻址 ID：文档变化时自然生成新 ID，旧 Chunk 由增量同步删除。"""
    digest = hashlib.sha1(
        f"{path}\x00{section}\x00{content}".encode("utf-8")
    ).hexdigest()
    return f"doc-{digest}"


class ChromaRAGProvider(RAGProvider):
    """本地 Chroma 向量检索器。"""

    name = "chroma"

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        persist_dir: str | Path | None = None,
        collection_name: str | None = None,
        embedding_function: _EmbeddingFunction | None = None,
        max_chunk_chars: int = 900,
        content_limit: int = 2400,
    ) -> None:
        self._root = Path(root) if root else _default_enterprise_dir()
        self._persist_dir = Path(persist_dir) if persist_dir else _default_persist_dir()
        self._collection_name = (
            collection_name or get_settings().chroma_collection_name
        ).strip()
        self._embedding_function = embedding_function
        self._max_chunk_chars = max(300, max_chunk_chars)
        self._content_limit = max(400, content_limit)
        self._lock = threading.Lock()
        self._collection: Any | None = None

    @property
    def root(self) -> Path:
        return self._root

    @property
    def persist_dir(self) -> Path:
        return self._persist_dir

    # --------------------------------------------------------
    # 索引
    # --------------------------------------------------------
    def _load_chunks(self) -> list[dict[str, Any]]:
        if not self._root.exists():
            raise RAGProviderError(f"企业知识库目录不存在: {self._root}")
        chunks: list[dict[str, Any]] = []
        for path in sorted(self._root.rglob("*.md")):
            for chunk in parse_markdown(path, self._root, self._max_chunk_chars):
                chunks.append(
                    {
                        "id": _chunk_id(chunk.path, chunk.section, chunk.content),
                        "doc_id": chunk.doc_id,
                        "title": chunk.title,
                        "section": chunk.section,
                        "path": chunk.path,
                        "content": chunk.content,
                        "metadata": dict(chunk.metadata),
                    }
                )
        if not chunks:
            raise RAGProviderError(f"企业知识库没有可用 Markdown: {self._root}")
        return chunks

    def _ensure_collection(self) -> Any:
        """首次检索时建库；只 upsert 新增 Chunk，删除已下线文档。"""
        if self._collection is not None:
            return self._collection
        with self._lock:
            if self._collection is not None:
                return self._collection
            try:
                import chromadb
                from chromadb.config import Settings as ChromaSettings
            except ImportError as exc:  # pragma: no cover - 取决于部署环境
                raise RAGProviderError(
                    "ChromaRAGProvider 需要 chromadb；请安装 requirements.txt"
                ) from exc

            chunks = self._load_chunks()
            self._persist_dir.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(
                path=str(self._persist_dir),
                settings=ChromaSettings(anonymized_telemetry=False),
            )
            embedding_function = (
                self._embedding_function or _default_embedding_function()
            )
            collection = client.get_or_create_collection(
                name=self._collection_name,
                embedding_function=embedding_function,  # type: ignore[arg-type]
                metadata={"hnsw:space": "cosine"},
            )

            expected_ids = {item["id"] for item in chunks}
            existing_ids: set[str] = set()
            offset = 0
            # Chroma get 有分页上限；显式翻页，避免知识库增长后漏删旧文档。
            while True:
                page = collection.get(include=[], limit=500, offset=offset)
                page_ids = list(page.get("ids") or [])
                existing_ids.update(page_ids)
                if len(page_ids) < 500:
                    break
                offset += len(page_ids)
            new_items = [item for item in chunks if item["id"] not in existing_ids]
            stale_ids = sorted(existing_ids - expected_ids)

            # 分批写入，避免单次请求携带过多文本 / 向量；upsert 保证
            # 进程重启或并发初始化时不会产生重复 Chunk。
            for start in range(0, len(new_items), 64):
                batch = new_items[start : start + 64]
                collection.upsert(
                    ids=[item["id"] for item in batch],
                    documents=[item["content"] for item in batch],
                    metadatas=[
                        {
                            "doc_id": item["doc_id"],
                            "title": item["title"],
                            "section": item["section"],
                            "path": item["path"],
                            "category": str(item["metadata"].get("category", "")),
                            "source": str(item["metadata"].get("source", "")),
                        }
                        for item in batch
                    ],
                )
            if stale_ids:
                collection.delete(ids=stale_ids)

            logger.info(
                "chroma index ready root=%s persist=%s total=%s added=%s removed=%s",
                self._root,
                self._persist_dir,
                len(chunks),
                len(new_items),
                len(stale_ids),
            )
            self._collection = collection
            return collection

    # --------------------------------------------------------
    # 检索
    # --------------------------------------------------------
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
        collection = self._ensure_collection()
        total = int(collection.count())
        if total <= 0:
            return []

        where: dict[str, Any] | None = None
        allowed = [
            item
            for item in (filters or {}).get("categories", [])
            if isinstance(item, str)
        ]
        if allowed:
            where = {"category": {"$in": allowed}}

        result = collection.query(
            query_texts=[query],
            n_results=min(max(1, top_k), total),
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]

        chunks: list[RetrievedChunk] = []
        for document, metadata, distance in zip(documents, metadatas, distances):
            if not document or not metadata:
                continue
            # 集合使用 cosine 距离（0~2），转换为越大越相关的相似度。
            similarity = max(0.0, 1.0 - float(distance))
            chunks.append(
                RetrievedChunk(
                    doc_id=str(metadata.get("doc_id", "")),
                    title=str(metadata.get("title", "")),
                    section=str(metadata.get("section", "")),
                    path=str(metadata.get("path", "")),
                    content=str(document)[: self._content_limit],
                    score=similarity,
                    metadata={
                        "category": str(metadata.get("category", "")),
                        "source": str(metadata.get("source", "")),
                        "distance": float(distance),
                        "retrievalSource": "chroma",
                    },
                )
            )
        return chunks


__all__ = ["ChromaRAGProvider"]
