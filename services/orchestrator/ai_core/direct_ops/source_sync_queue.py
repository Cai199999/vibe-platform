"""SourceSync 补偿队列。

BFF / MinIO 短暂不可用时不丢用户代码：失败任务落盘到 JSON 文件，
后台线程指数退避重试。队列只存“同步任务”，不存源码正文；重试时
从沙盒重新读取最新文件，避免覆盖用户后续修改。

这是轻量级本地补偿，不引入 Redis 依赖；data/ 目录随 AI 容器持久化。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ai_core.direct_ops.source_sync import SourceSnapshotSync
from ai_core.ports.sandbox import SandboxProvider

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 6
BACKOFF_SECONDS = (15, 30, 60, 180, 600, 1800)


class SourceSyncQueue:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key_for(task: dict[str, Any]) -> str:
        return (
            f"{task.get('projectId')}:{task.get('sandboxId')}:"
            f"{task.get('commitHash') or task.get('touchedFile')}"
        )

    def add(self, task: dict[str, Any]) -> str:
        key = self.key_for(task)
        now = time.time()
        with self._lock:
            tasks = self._load()
            existing = tasks.get(key)
            if existing is None:
                tasks[key] = {
                    **task,
                    "key": key,
                    "attempts": 0,
                    "nextAt": now,
                    "lastError": "",
                    "createdAt": now,
                }
            else:
                existing.update(task)
            self._save(tasks)
        return key

    def due(self, now: float | None = None) -> list[dict[str, Any]]:
        current = time.time() if now is None else now
        with self._lock:
            tasks = self._load()
            return [
                dict(item)
                for item in tasks.values()
                if float(item.get("nextAt") or 0) <= current
            ]

    def remove(self, key: str) -> None:
        with self._lock:
            tasks = self._load()
            tasks.pop(key, None)
            self._save(tasks)

    def reschedule(self, key: str, error: str) -> bool:
        """返回 True 表示仍在队列，False 表示超过上限已丢弃。"""
        with self._lock:
            tasks = self._load()
            task = tasks.get(key)
            if task is None:
                return False
            attempts = int(task.get("attempts") or 0) + 1
            if attempts >= MAX_ATTEMPTS:
                tasks.pop(key, None)
                self._save(tasks)
                return False
            delay = BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS) - 1)]
            task["attempts"] = attempts
            task["nextAt"] = time.time() + delay
            task["lastError"] = str(error)[:300]
            self._save(tasks)
            return True

    def size(self) -> int:
        with self._lock:
            return len(self._load())

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(key): value for key, value in data.items() if isinstance(value, dict)
        }

    def _save(self, tasks: dict[str, dict[str, Any]]) -> None:
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(tasks, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self._path)


class SourceSyncRetryWorker:
    def __init__(
        self,
        queue: SourceSyncQueue,
        *,
        sync_factory: Callable[[], SourceSnapshotSync],
        sandbox_provider: SandboxProvider,
        interval_seconds: float = 15.0,
    ) -> None:
        self._queue = queue
        self._sync_factory = sync_factory
        self._sandbox = sandbox_provider
        self._interval = max(1.0, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop,
            name="source-sync-retry",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> None:
        for task in self._queue.due():
            key = SourceSyncQueue.key_for(task)
            try:
                result = self._sync_factory().sync(
                    project_id=str(task.get("projectId") or ""),
                    sandbox_id=str(task.get("sandboxId") or ""),
                    tenant_id=task.get("tenantId"),
                    touched_file=str(task.get("touchedFile") or ""),
                    sandbox=self._sandbox,
                )
            except Exception as exc:  # noqa: BLE001 - 队列必须继续运行
                result = {"status": "FAILED", "message": str(exc)}
            status = str(result.get("status") or "")
            if status in {"SYNCED", "SKIPPED", "DISABLED"}:
                self._queue.remove(key)
                logger.info("source sync retry succeeded key=%s status=%s", key, status)
            else:
                remains = self._queue.reschedule(
                    key,
                    str(result.get("message") or result),
                )
                logger.warning(
                    "source sync retry failed key=%s remains=%s error=%s",
                    key,
                    remains,
                    result.get("message"),
                )

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.run_once()
            except Exception:  # pragma: no cover - 后台线程必须自愈
                logger.exception("source sync retry worker crashed")


_QUEUES: dict[str, SourceSyncQueue] = {}
_QUEUE_LOCK = threading.Lock()
_WORKER: SourceSyncRetryWorker | None = None
_WORKER_LOCK = threading.Lock()


def get_source_sync_queue(path: str | Path) -> SourceSyncQueue:
    """同进程共享同一个队列实例，避免多请求并发写坏 JSON。"""
    key = str(Path(path).resolve())
    with _QUEUE_LOCK:
        queue = _QUEUES.get(key)
        if queue is None:
            queue = SourceSyncQueue(key)
            _QUEUES[key] = queue
        return queue


def ensure_source_sync_retry_worker(
    queue: SourceSyncQueue,
    *,
    sync_factory: Callable[[], SourceSnapshotSync],
    sandbox_provider: SandboxProvider,
    interval_seconds: float = 15.0,
) -> SourceSyncRetryWorker:
    """整个进程只启动一个后台补偿线程。"""
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None:
            _WORKER = SourceSyncRetryWorker(
                queue,
                sync_factory=sync_factory,
                sandbox_provider=sandbox_provider,
                interval_seconds=interval_seconds,
            )
            _WORKER.start()
        return _WORKER


__all__ = [
    "SourceSyncQueue",
    "SourceSyncRetryWorker",
    "get_source_sync_queue",
    "ensure_source_sync_retry_worker",
]
