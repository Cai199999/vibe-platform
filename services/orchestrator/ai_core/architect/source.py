"""Architect 源码采集。

优先级：
  1. 有 ``sandboxId`` 时，从沙盒导出当前项目 tar.gz，读取真实源码；
  2. 沙盒不可用或未提供时，回退到请求里的 ``files``。

不能使用前端 ``project.files`` 作为主来源：它通常停在初始模板，经过多轮
局部修改后与沙盒真实代码不一致。采集器只读取文本源码，并设置文件数 /
字节上限，避免把依赖、构建产物或二进制内容送入模型。
"""

from __future__ import annotations

import hashlib
import io
import logging
import tarfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from ai_core.config import Settings, get_settings
from ai_core.sandbox.base import SandboxError, SandboxProvider

logger = logging.getLogger(__name__)

TEXT_SUFFIXES = {
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".css",
    ".scss",
    ".less",
    ".html",
    ".htm",
    ".json",
    ".md",
    ".py",
    ".java",
    ".kt",
    ".go",
    ".rs",
    ".sql",
    ".yml",
    ".yaml",
    ".toml",
    ".txt",
    ".vue",
    ".svelte",
}
IGNORED_NAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "node_modules",
    "dist",
    "build",
    ".git",
    ".venv",
    "__pycache__",
}
IGNORED_PREFIXES = (
    "node_modules/",
    "dist/",
    "build/",
    ".git/",
    ".venv/",
    "__pycache__/",
    "coverage/",
    ".next/",
)
# 优先级越高越先进入预算。业务源码 > 页面入口 > 配置 > 文档。
PRIORITY_PREFIXES = (
    "src/",
    "app/",
    "pages/",
    "components/",
    "index.html",
    "package.json",
    "vite.config.",
    "README",
)
MAX_FILE_BYTES = 40_000


@dataclass
class SourceBundle:
    files: dict[str, str]
    source_hash: str
    origin: str = "request"
    warnings: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(len(value.encode("utf-8")) for value in self.files.values())


def collect_source(
    sandbox_id: str | None,
    fallback_files: dict[str, str] | None = None,
    *,
    sandbox: SandboxProvider | None = None,
    settings: Settings | None = None,
) -> SourceBundle:
    """采集架构分析所需的源码快照。"""
    resolved = settings or get_settings()
    max_files = max(1, resolved.architect_source_max_files)
    max_bytes = max(1_000, resolved.architect_source_max_bytes)

    warnings: list[str] = []
    if sandbox_id:
        try:
            provider = sandbox
            if provider is None:
                from ai_core.sandbox import get_sandbox_provider

                provider = get_sandbox_provider()
            archive = provider.export_archive(sandbox_id)
            files = _read_archive(archive, max_files=max_files, max_bytes=max_bytes)
            if files:
                return SourceBundle(
                    files=files,
                    source_hash=hash_source(files),
                    origin="sandbox",
                    warnings=warnings,
                )
            warnings.append("沙盒源码为空，已回退到请求中的 files")
        except SandboxError as exc:
            logger.warning("architect source export failed: %s", exc)
            warnings.append(f"沙盒源码不可用，已回退: {exc}")
        except (tarfile.TarError, OSError) as exc:
            logger.warning("architect source archive invalid: %s", exc)
            warnings.append(f"沙盒源码包解析失败，已回退: {exc}")

    files = _normalize_request_files(
        fallback_files or {},
        max_files=max_files,
        max_bytes=max_bytes,
    )
    return SourceBundle(
        files=files,
        source_hash=hash_source(files),
        origin="request",
        warnings=warnings,
    )


def hash_source(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(files[path].encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def format_source(files: dict[str, str], limit: int = 48_000) -> str:
    """把源码拼接为带路径的文本块，供 Prompt 使用。"""
    parts: list[str] = []
    used = 0
    for path in _ordered_paths(files):
        content = files[path]
        block = f"\n--- {path} ---\n{content}\n"
        if used + len(block) > limit:
            remaining = max(0, limit - used)
            if remaining > 200:
                parts.append(block[:remaining])
            break
        parts.append(block)
        used += len(block)
    return "".join(parts) or "（无源码，这是一个新项目）"


def _read_archive(
    data: bytes,
    *,
    max_files: int,
    max_bytes: int,
) -> dict[str, str]:
    files: dict[str, str] = {}
    used = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = sorted(
            (member for member in archive.getmembers() if member.isfile()),
            key=lambda member: _priority(member.name),
        )
        for member in members:
            path = _safe_path(member.name)
            if path is None or not _is_text_path(path):
                continue
            if len(files) >= max_files or used >= max_bytes:
                break
            if member.size > MAX_FILE_BYTES:
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            raw = handle.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                continue
            content = raw.decode("utf-8", errors="ignore")
            if not content.strip():
                continue
            if b"\x00" in raw[:1024]:
                continue
            encoded = content.encode("utf-8")
            remaining = max_bytes - used
            if remaining <= 0:
                break
            if len(encoded) > remaining:
                content = encoded[:remaining].decode("utf-8", errors="ignore")
                encoded = content.encode("utf-8")
            files[path] = content
            used += len(encoded)
    return files


def _normalize_request_files(
    raw_files: dict[str, Any],
    *,
    max_files: int,
    max_bytes: int,
) -> dict[str, str]:
    files: dict[str, str] = {}
    used = 0
    for raw_path, raw_content in sorted(
        raw_files.items(), key=lambda item: _priority(item[0])
    ):
        path = _safe_path(str(raw_path))
        if path is None or not _is_text_path(path):
            continue
        if not isinstance(raw_content, str) or not raw_content.strip():
            continue
        if len(files) >= max_files or used >= max_bytes:
            break
        content = raw_content[:MAX_FILE_BYTES]
        encoded = content.encode("utf-8")
        remaining = max_bytes - used
        if len(encoded) > remaining:
            content = encoded[:remaining].decode("utf-8", errors="ignore")
            encoded = content.encode("utf-8")
        files[path] = content
        used += len(encoded)
    return files


def _safe_path(name: str) -> str | None:
    normalized = name.replace("\\", "/").lstrip("./")
    if not normalized or normalized.startswith("/"):
        return None
    path = PurePosixPath(normalized)
    if any(part in {"..", ""} for part in path.parts):
        return None
    if any(part in IGNORED_NAMES for part in path.parts):
        return None
    lowered = normalized.lower()
    if any(lowered.startswith(prefix) for prefix in IGNORED_PREFIXES):
        return None
    if ".env" in path.name.lower() and path.name != ".env.example":
        return None
    return normalized


def _is_text_path(path: str) -> bool:
    suffix = PurePosixPath(path).suffix.lower()
    return suffix in TEXT_SUFFIXES


def _ordered_paths(files: dict[str, str]) -> list[str]:
    return sorted(files, key=lambda path: (_priority(path), path))


def _priority(path: str) -> int:
    normalized = path.replace("\\", "/").lstrip("./")
    for index, prefix in enumerate(PRIORITY_PREFIXES):
        if normalized.startswith(prefix):
            return index
    return len(PRIORITY_PREFIXES)
