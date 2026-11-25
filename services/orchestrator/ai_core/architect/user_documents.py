"""用户上传文档的轻量 RAG。

设计取舍：
- 不做持久化、不建向量库：文档只在本次 Architect Run 内使用；
- 复用 Markdown 分节思路，按标题 / 段落切块；
- 用确定性的关键词打分（中文 bigram + ASCII 词）排序，避免额外依赖；
- 结果转换成 ``RetrievedChunk``，与全局 RAG 走同一条 Prompt / 引用链路。
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Iterable

from ai_core.ports.rag import RetrievedChunk

MAX_DOCS = 3
MAX_CHUNK_CHARS = 1200
MAX_TOTAL_CHARS = 240_000
DEFAULT_TOP_K = 3

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}")
_CJK_RUN_RE = re.compile(r"[一-鿿]+")
_PARAGRAPH_RE = re.compile(r"\n\s*\n")


def _split_sections(content: str) -> list[tuple[str, str]]:
    """按 Markdown 标题切节；没有标题时整篇作为“正文”。"""
    sections: list[tuple[str, str]] = []
    heading = "正文"
    buffer: list[str] = []

    def flush() -> None:
        text = "\n".join(buffer).strip()
        buffer.clear()
        if text:
            sections.extend(_split_long_section(heading, text))

    for line in content.splitlines():
        match = _HEADING_RE.match(line.strip())
        if match:
            flush()
            heading = match.group(2).strip() or "正文"
        else:
            buffer.append(line)
    flush()
    return sections


def _split_long_section(heading: str, text: str) -> list[tuple[str, str]]:
    if len(text) <= MAX_CHUNK_CHARS:
        return [(heading, text)]
    chunks: list[tuple[str, str]] = []
    buffer = ""
    for paragraph in _PARAGRAPH_RE.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(buffer) + len(paragraph) + 2 <= MAX_CHUNK_CHARS:
            buffer = f"{buffer}\n\n{paragraph}".strip()
            continue
        if buffer:
            chunks.append((heading, buffer))
            buffer = ""
        if len(paragraph) <= MAX_CHUNK_CHARS:
            buffer = paragraph
        else:
            for index in range(0, len(paragraph), MAX_CHUNK_CHARS):
                chunks.append((heading, paragraph[index : index + MAX_CHUNK_CHARS]))
    if buffer:
        chunks.append((heading, buffer))
    return chunks


def _query_tokens(query: str) -> set[str]:
    tokens: set[str] = set()
    for token in _ASCII_TOKEN_RE.findall(query.lower()):
        tokens.add(token)
    for run in _CJK_RUN_RE.findall(query):
        if len(run) == 1:
            tokens.add(run)
            continue
        for index in range(len(run) - 1):
            tokens.add(run[index : index + 2])
    return tokens


def _score(tokens: set[str], text: str, name: str) -> float:
    if not tokens:
        return 0.0
    lower = text.lower()
    hits = sum(1 for token in tokens if token in lower)
    title_hits = sum(1 for token in tokens if token in name.lower())
    # 长文档不能因为词多就天然占优；用 sqrt 归一化后再加标题命中加权。
    return (hits / math.sqrt(max(1, len(text)))) + title_hits * 0.05


def _iter_documents(documents: Iterable[Any]) -> Iterable[tuple[str, str]]:
    for document in list(documents)[:MAX_DOCS]:
        if isinstance(document, dict):
            name = str(document.get("name") or "上传文档")
            content = str(document.get("content") or "")
        else:
            name = str(getattr(document, "name", "") or "上传文档")
            content = str(getattr(document, "content", "") or "")
        yield name, content


def retrieve_user_document_chunks(
    documents: Iterable[Any],
    query: str,
    *,
    top_k: int = DEFAULT_TOP_K,
) -> list[RetrievedChunk]:
    """从用户上传文档中检索最相关的片段；空输入返回空列表。"""
    if top_k <= 0:
        return []
    tokens = _query_tokens(query)
    candidates: list[RetrievedChunk] = []
    total_chars = 0

    for name, content in _iter_documents(documents):
        content = content.strip()
        if not content:
            continue
        remaining = MAX_TOTAL_CHARS - total_chars
        if remaining <= 0:
            break
        content = content[:remaining]
        total_chars += len(content)

        for index, (section, text) in enumerate(_split_sections(content)):
            if not text.strip():
                continue
            digest = hashlib.sha1(
                f"{name}\x00{index}\x00{text}".encode("utf-8")
            ).hexdigest()[:12]
            candidates.append(
                RetrievedChunk(
                    doc_id=f"doc-user-{digest}",
                    title=name,
                    section=section,
                    path=name,
                    content=text,
                    score=_score(tokens, text, name),
                    metadata={"source": "user_document", "documentName": name},
                )
            )

    if not candidates:
        return []
    candidates.sort(key=lambda chunk: chunk.score, reverse=True)
    return candidates[:top_k]
