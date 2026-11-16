"""项目级写锁（单进程）。

AI Run 与 Direct Ops 可能同时写同一沙盒主工作树；仅靠前端禁用按钮
无法阻止服务端并发。这里用 ``project_id`` 粒度的互斥锁串行化所有
主工作树写入：

  - AI Run：草稿可并发生成，但 promote + git commit 必须持锁；
  - Fullstack：生成过程持续写主工作树，整段持锁；
  - Direct Ops：resolve 之前不写，execute + commit 持锁；
  - Rollback：git reset --hard 持锁。

当前为进程内实现；未来多副本时替换为 Redis 分布式锁，接口不变。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator


class ProjectBusyError(Exception):
    """项目写锁在超时时间内未获取到。"""

    code = "DIRECT_PROJECT_BUSY"
    status_code = 409

    def __init__(self, project_id: str, owner: str) -> None:
        super().__init__(f"项目 {project_id} 正在被 {owner} 写入，请稍后重试")
        self.project_id = project_id
        self.owner = owner


@dataclass
class ProjectWriteLease:
    """一次写锁租约；必须由获取它的线程释放。"""

    project_id: str
    owner: str
    acquired_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    released: bool = False
    started_monotonic: float = field(default_factory=time.monotonic, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class ProjectWriteLock:
    """按 project_id 分桶的互斥锁。"""

    def __init__(self) -> None:
        self._guard = threading.RLock()
        self._locks: dict[str, threading.Lock] = {}
        self._owners: dict[str, str] = {}
        # 观测：等待次数 / 超时次数 / 最大持锁时长（毫秒）。
        self._waits = 0
        self._timeouts = 0
        self._max_hold_ms = 0.0

    def acquire(
        self,
        project_id: str,
        *,
        owner: str,
        timeout: float,
    ) -> ProjectWriteLease:
        project = str(project_id or "").strip()
        if not project:
            raise ValueError("project_id 不能为空")
        with self._guard:
            lock = self._locks.setdefault(project, threading.Lock())
            self._waits += 1

        acquired = lock.acquire(timeout=max(0.0, float(timeout)))
        if not acquired:
            with self._guard:
                self._timeouts += 1
            raise ProjectBusyError(project, owner)

        lease = ProjectWriteLease(
            project_id=project,
            owner=owner,
            _lock=lock,
        )
        with self._guard:
            self._owners[project] = owner
        return lease

    def release(self, lease: ProjectWriteLease) -> None:
        with self._guard:
            if lease.released:
                return
            lease.released = True
            self._owners.pop(lease.project_id, None)
            hold_ms = (time.monotonic() - lease.started_monotonic) * 1000
            self._max_hold_ms = max(self._max_hold_ms, hold_ms)
        try:
            lease._lock.release()
        except RuntimeError:
            # 重复释放或跨线程释放：只记录，不让清理流程崩溃。
            pass

    @contextmanager
    def lease(
        self,
        project_id: str,
        *,
        owner: str,
        timeout: float = 30.0,
    ) -> Iterator[ProjectWriteLease]:
        lease = self.acquire(project_id, owner=owner, timeout=timeout)
        try:
            yield lease
        finally:
            self.release(lease)

    def snapshot(self) -> dict[str, object]:
        with self._guard:
            return {
                "held": dict(self._owners),
                "waits": self._waits,
                "timeouts": self._timeouts,
                "maxHoldMs": round(self._max_hold_ms, 2),
            }


project_write_lock = ProjectWriteLock()


__all__ = [
    "ProjectBusyError",
    "ProjectWriteLease",
    "ProjectWriteLock",
    "project_write_lock",
]
