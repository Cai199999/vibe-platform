"""Direct Ops ChangeLog：进程内环形存储。

Phase 1 只保证“成功动作可回顾”；跨重启持久化与 Architect 注入在后续
阶段接入。这里只保存摘要与哈希，不保存完整源码。
"""

from __future__ import annotations

import threading
from collections import deque

from ai_core.config import get_settings
from ai_core.domain.direct_ops import ChangeLogEntry


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


_change_log_store = MemoryChangeLogStore()


def get_change_log_store() -> MemoryChangeLogStore:
    return _change_log_store


__all__ = ["MemoryChangeLogStore", "get_change_log_store"]
