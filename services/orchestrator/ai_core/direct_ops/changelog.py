"""Direct Ops ChangeLog：内存索引 + JSONL 落盘。

默认写入 ``data/changelog.jsonl``（容器内 data/ 已挂载到宿主机），
服务重启后重新加载，Architect 仍能读取最近 10 条历史操作。
只保存摘要与哈希，不保存完整源码。
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from pathlib import Path

from ai_core.config import get_settings
from ai_core.domain.direct_ops import ChangeLogEntry

logger = logging.getLogger(__name__)


class MemoryChangeLogStore:
    def __init__(self, max_entries: int | None = None) -> None:
        self._limit = max_entries or get_settings().direct_ops_changelog_limit
        self._lock = threading.RLock()
        self._entries: dict[str, deque[ChangeLogEntry]] = {}

    def append(self, entry: ChangeLogEntry) -> None:
        with self._lock:
            project_entries = self._entries.setdefault(
                entry.project_id, deque(maxlen=max(1, self._limit))
            )
            project_entries.append(entry)

    def list_for_project(
        self, project_id: str, limit: int = 20
    ) -> list[ChangeLogEntry]:
        with self._lock:
            items = list(self._entries.get(project_id, ()))
        if limit <= 0:
            return []
        return items[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class JsonlChangeLogStore(MemoryChangeLogStore):
    """追加写 JSONL；启动时加载，单进程内线程安全。"""

    def __init__(
        self,
        path: str | Path,
        max_entries: int | None = None,
    ) -> None:
        super().__init__(max_entries=max_entries)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        loaded = 0
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        entry = ChangeLogEntry.model_validate_json(raw)
                    except Exception:  # noqa: BLE001 - 单行损坏不影响其它记录
                        logger.warning("skip invalid changelog line")
                        continue
                    MemoryChangeLogStore.append(self, entry)
                    loaded += 1
        except OSError:
            logger.warning("failed to load changelog path=%s", self.path, exc_info=True)
            return
        logger.info("loaded %d direct changelog entrie(s) from %s", loaded, self.path)

    def append(self, entry: ChangeLogEntry) -> None:
        super().append(entry)
        try:
            line = entry.model_dump_json(by_alias=True)
            with self._lock:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
        except OSError:
            # 落盘失败不能阻断已经成功的代码写入；下次启动最多丢失该条。
            logger.warning(
                "failed to persist changelog path=%s", self.path, exc_info=True
            )

    def clear(self) -> None:
        super().clear()
        try:
            self.path.write_text("", encoding="utf-8")
        except OSError:
            logger.warning(
                "failed to clear changelog path=%s", self.path, exc_info=True
            )


def _build_store() -> MemoryChangeLogStore:
    settings = get_settings()
    try:
        return JsonlChangeLogStore(
            settings.direct_ops_changelog_path,
            max_entries=settings.direct_ops_changelog_limit,
        )
    except Exception:  # noqa: BLE001 - 落盘不可用时退回内存
        logger.warning("changelog persistence unavailable; using memory", exc_info=True)
        return MemoryChangeLogStore(settings.direct_ops_changelog_limit)


_change_log_store: MemoryChangeLogStore = _build_store()


def get_change_log_store() -> MemoryChangeLogStore:
    return _change_log_store


__all__ = [
    "JsonlChangeLogStore",
    "MemoryChangeLogStore",
    "get_change_log_store",
]
