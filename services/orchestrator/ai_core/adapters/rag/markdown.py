"""基于本地 Markdown 的轻量 RAG。

设计目标：
  - 零外部服务、零 API Key、确定性输出，便于 Mock / E2E；
  - 按标题切块，保留章节语义；
  - 使用 BM25 做中文友好的词项检索，后续可无缝替换 ChromaProvider。

这不是“语义向量检索”的替代品，而是 M2.5 的最小可用实现。知识库规范
变化时只需更新 Markdown，不需要重建索引。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ai_core.config import get_settings
from ai_core.ports.rag import RAGProvider, RAGProviderError, RetrievedChunk

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_ASCII_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_.+\-]*")
_CJK_RE = re.compile(r"[一-鿿]+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])")


@dataclass(frozen=True)
class _KnowledgeChunk:
    doc_id: str
    title: str
    section: str
    path: str
    content: str
    metadata: dict[str, Any]


def _tokenize(text: str) -> list[str]:
    """中文按单字 + 二元组，英文/数字按单词切分。

    中文没有空格，直接按空格切词会退化成整段匹配；二元组能覆盖
    “库存扣减”“订单创建”这类领域短语，同时保留单字兜底。
    """
    lowered = text.lower()
    tokens: list[str] = _ASCII_WORD_RE.findall(lowered)
    for sequence in _CJK_RE.findall(lowered):
        tokens.extend(sequence)
        tokens.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return tokens


def _split_long_text(text: str, max_chars: int) -> list[str]:
    """按句子 / 段落切分超长章节，避免单块超过 Prompt 预算。"""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    current = ""
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if not sentence:
            continue
        if len(current) + len(sentence) <= max_chars:
            current += sentence
            continue
        if current:
            chunks.append(current)
        while len(sentence) > max_chars:
            chunks.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
        current = sentence
    if current:
        chunks.append(current)
    return chunks


def _parse_markdown(path: Path, root: Path, max_chars: int) -> list[_KnowledgeChunk]:
    """按标题切块；每个块继承最近的标题路径。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RAGProviderError(f"读取知识库文件失败: {path}: {exc}") from exc

    rel_path = str(path.relative_to(root))
    doc_id = path.stem
    title = path.stem
    heading_stack: list[tuple[int, str]] = []
    current_lines: list[str] = []
    chunks: list[_KnowledgeChunk] = []

    def flush() -> None:
        nonlocal current_lines
        content = "\n".join(current_lines).strip()
        # 只有标题 / 文档元信息（引用块）的片段不单独成块：
        #  - h1 后立刻跟 h2 时，标题语义已由 heading_stack 继承；
        #  - 开头 > 文档编号 / 生效日期 等元信息不构成可检索知识。
        body_lines = [
            line.strip()
            for line in current_lines
            if line.strip() and not _HEADING_RE.match(line.strip())
        ]
        has_body = any(not line.startswith(">") for line in body_lines)
        if not content or not has_body:
            current_lines = []
            return
        section = " / ".join(item[1] for item in heading_stack) or title
        for index, part in enumerate(_split_long_text(content, max_chars), start=1):
            suffix = f" #{index}" if index > 1 else ""
            chunks.append(
                _KnowledgeChunk(
                    doc_id=doc_id,
                    title=title,
                    section=section + suffix,
                    path=rel_path,
                    content=part.strip(),
                    metadata={
                        "category": path.parent.name,
                        "source": str(path),
                    },
                )
            )
        current_lines = []

    for line in raw.splitlines():
        match = _HEADING_RE.match(line)
        if not match:
            current_lines.append(line)
            continue
        flush()
        level = len(match.group(1))
        heading = match.group(2).strip()
        if not heading_stack:
            title = heading
        heading_stack = [item for item in heading_stack if item[0] < level]
        heading_stack.append((level, heading))
        current_lines.append(line)

    flush()
    return chunks


def parse_markdown(
    path: Path,
    root: Path,
    max_chars: int = 2400,
) -> list[_KnowledgeChunk]:
    """公开的 Markdown 切块入口。

    Chroma Provider 与 BM25 Provider 必须使用完全一致的切块规则，
    否则 RRF 融合时同一个语义单元会被当成两个不同 Chunk。因此这里
    只暴露函数，不复制实现。
    """
    return _parse_markdown(path, root, max_chars)


class _BM25Index:
    """极简 BM25，避免引入 rank_bm25 依赖。"""

    def __init__(self, chunks: list[_KnowledgeChunk]) -> None:
        self.chunks = chunks
        self.doc_tokens = [
            _tokenize(chunk.content + " " + chunk.section) for chunk in chunks
        ]
        self.doc_freqs: Counter[str] = Counter()
        for tokens in self.doc_tokens:
            self.doc_freqs.update(set(tokens))
        self.doc_len = [len(tokens) for tokens in self.doc_tokens]
        self.avgdl = (sum(self.doc_len) / len(self.doc_len)) if self.doc_len else 0.0

    def search(self, query: str, top_k: int, limit: int) -> list[RetrievedChunk]:
        query_terms = list(dict.fromkeys(_tokenize(query)))
        if not query_terms or not self.chunks:
            return []
        total_docs = len(self.chunks)
        k1 = 1.5
        b = 0.75
        scored: list[tuple[float, int]] = []
        for index, tokens in enumerate(self.doc_tokens):
            frequencies = Counter(tokens)
            doc_len = self.doc_len[index] or 1
            score = 0.0
            for term in query_terms:
                df = self.doc_freqs.get(term, 0)
                if df == 0:
                    continue
                idf = math.log(1.0 + (total_docs - df + 0.5) / (df + 0.5))
                tf = frequencies.get(term, 0)
                denominator = tf + k1 * (1.0 - b + b * doc_len / (self.avgdl or 1.0))
                score += idf * (tf * (k1 + 1.0) / denominator)
            if score > 0:
                scored.append((score, index))
        scored.sort(key=lambda item: item[0], reverse=True)
        results: list[RetrievedChunk] = []
        for score, index in scored[:top_k]:
            chunk = self.chunks[index]
            results.append(
                RetrievedChunk(
                    doc_id=chunk.doc_id,
                    title=chunk.title,
                    section=chunk.section,
                    path=chunk.path,
                    content=chunk.content[:limit],
                    score=score,
                    metadata=dict(chunk.metadata),
                )
            )
        return results


class MarkdownRAGProvider(RAGProvider):
    """本地 Markdown + BM25 检索器。"""

    name = "markdown"

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        max_chunk_chars: int = 2400,
        content_limit: int = 2400,
    ) -> None:
        self._root = Path(root) if root else _default_knowledge_dir()
        self._max_chunk_chars = max(400, max_chunk_chars)
        self._content_limit = max(400, content_limit)
        self._index: _BM25Index | None = None

    @property
    def root(self) -> Path:
        return self._root

    def _ensure_index(self) -> _BM25Index:
        if self._index is not None:
            return self._index
        if not self._root.exists():
            raise RAGProviderError(f"知识库目录不存在: {self._root}")
        chunks: list[_KnowledgeChunk] = []
        for path in sorted(self._root.rglob("*.md")):
            chunks.extend(_parse_markdown(path, self._root, self._max_chunk_chars))
        if not chunks:
            raise RAGProviderError(f"知识库没有可用 Markdown: {self._root}")
        self._index = _BM25Index(chunks)
        return self._index

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        index = self._ensure_index()
        # 先按 metadata 过滤，再检索；当前只有 category 过滤，保留扩展位。
        if filters:
            allowed = {
                item for item in filters.get("categories", []) if isinstance(item, str)
            }
            if allowed:
                filtered = [
                    chunk
                    for chunk in index.chunks
                    if chunk.metadata.get("category") in allowed
                ]
                index = _BM25Index(filtered)
        return index.search(query, max(1, top_k), self._content_limit)


def _default_knowledge_dir() -> Path:
    """兼容容器 /app 与本地源码树两种运行方式。"""
    configured = get_settings().architect_knowledge_dir.strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    candidates.append(Path("/app/knowledge/fullstack"))
    candidates.append(Path(__file__).resolve().parents[2] / "knowledge" / "fullstack")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _all_documents(root: Path) -> Iterable[Path]:
    return sorted(root.rglob("*.md"))


# 文档 / 架构图中的简写；实现类仍保留 Provider 后缀。
MarkdownRAG = MarkdownRAGProvider

__all__ = ["MarkdownRAG", "MarkdownRAGProvider"]
