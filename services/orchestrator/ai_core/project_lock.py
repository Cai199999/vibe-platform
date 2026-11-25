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

import logging
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

from ai_core.config import get_settings

logger = logging.getLogger(__name__)

LOCK_KEY_PREFIX = "vibe:lock:project:"
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""
_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""


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
    _lock: threading.Lock | None = field(default=None, repr=False)
    backend: str = "local"
    redis_client: Any | None = field(default=None, repr=False)
    redis_key: str = ""
    redis_token: str = ""
    redis_renew_stop: threading.Event | None = field(default=None, repr=False)
    _renew_thread: threading.Thread | None = field(default=None, repr=False)


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
        self._redis_client: Any | None = None
        self._redis_checked = False

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
            self._waits += 1

        redis_client = self._get_redis_client()
        if redis_client is not None:
            redis_lease = self._acquire_redis(
                redis_client,
                project,
                owner,
                timeout=float(timeout),
            )
            if redis_lease is not None:
                with self._guard:
                    self._owners[project] = owner
                return redis_lease

        with self._guard:
            lock = self._locks.setdefault(project, threading.Lock())
        acquired = lock.acquire(timeout=max(0.0, float(timeout)))
        if not acquired:
            with self._guard:
                self._timeouts += 1
            raise ProjectBusyError(project, owner)

        lease = ProjectWriteLease(
            project_id=project,
            owner=owner,
            _lock=lock,
            backend="local",
        )
        with self._guard:
            self._owners[project] = owner
        return lease

    def release(self, lease: ProjectWriteLease) -> None:
        if lease.backend == "redis":
            self._release_redis(lease)
        with self._guard:
            if lease.released:
                return
            lease.released = True
            self._owners.pop(lease.project_id, None)
            hold_ms = (time.monotonic() - lease.started_monotonic) * 1000
            self._max_hold_ms = max(self._max_hold_ms, hold_ms)
        if lease._lock is not None:
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
                "backend": "redis" if self._redis_client is not None else "local",
            }

    # --------------------------------------------------------
    # Redis 分布式锁
    # --------------------------------------------------------
    def _get_redis_client(self) -> Any | None:
        """惰性创建 Redis 客户端；不可用时回退本地锁。"""
        if self._redis_checked:
            return self._redis_client
        with self._guard:
            if self._redis_checked:
                return self._redis_client
            self._redis_checked = True
            settings = get_settings()
            if not settings.distributed_lock_enabled:
                return None
            try:
                import redis

                client = redis.Redis(
                    host=settings.redis_host,
                    port=settings.redis_port,
                    password=settings.redis_password or None,
                    db=settings.redis_db,
                    decode_responses=True,
                    socket_connect_timeout=1.0,
                    socket_timeout=1.0,
                )
                client.ping()
                self._redis_client = client
                logger.info(
                    "project write lock backend=redis host=%s",
                    settings.redis_host,
                )
            except Exception:  # noqa: BLE001 - 无 Redis 时保持单机可用
                logger.warning(
                    "distributed project lock unavailable, fallback to local",
                    exc_info=True,
                )
                self._redis_client = None
            return self._redis_client

    def _acquire_redis(
        self,
        client: Any,
        project_id: str,
        owner: str,
        *,
        timeout: float,
    ) -> ProjectWriteLease | None:
        settings = get_settings()
        ttl_ms = max(60, int(settings.distributed_lock_ttl_seconds)) * 1000
        key = f"{LOCK_KEY_PREFIX}{project_id}"
        token = f"{owner}:{uuid.uuid4().hex}"
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                acquired = client.set(key, token, nx=True, px=ttl_ms)
            except Exception:  # noqa: BLE001 - 连接失败回退本地锁
                logger.warning(
                    "redis project lock unavailable, fallback to local for %s",
                    project_id,
                    exc_info=True,
                )
                with self._guard:
                    self._redis_client = None
                return None
            if acquired:
                stop = threading.Event()
                lease = ProjectWriteLease(
                    project_id=project_id,
                    owner=owner,
                    backend="redis",
                    redis_client=client,
                    redis_key=key,
                    redis_token=token,
                    redis_renew_stop=stop,
                )
                renew = threading.Thread(
                    target=self._renew_redis,
                    args=(client, key, token, ttl_ms, stop),
                    name=f"vibe-lock-renew-{project_id}",
                    daemon=True,
                )
                lease._renew_thread = renew
                renew.start()
                return lease
            if time.monotonic() >= deadline:
                with self._guard:
                    self._timeouts += 1
                raise ProjectBusyError(project_id, owner)
            time.sleep(0.05)

    @staticmethod
    def _renew_redis(
        client: Any,
        key: str,
        token: str,
        ttl_ms: int,
        stop: threading.Event,
    ) -> None:
        # TTL/3 续期：进程崩溃后最多 TTL 秒自动释放，不会永久占锁。
        interval = max(1.0, ttl_ms / 3000.0)
        while not stop.wait(interval):
            try:
                renewed = client.eval(_RENEW_SCRIPT, 1, key, token, ttl_ms)
            except Exception:  # noqa: BLE001 - 网络抖动下一轮再试
                logger.warning(
                    "redis project lock renew failed key=%s", key, exc_info=True
                )
                continue
            if not renewed:
                logger.warning("redis project lock lost key=%s", key)
                stop.set()
                return

    @staticmethod
    def _release_redis(lease: ProjectWriteLease) -> None:
        client = lease.redis_client
        if client is None:
            return
        if lease.redis_renew_stop is not None:
            lease.redis_renew_stop.set()
        if lease._renew_thread is not None and lease._renew_thread.is_alive():
            lease._renew_thread.join(timeout=1.0)
        try:
            client.eval(_RELEASE_SCRIPT, 1, lease.redis_key, lease.redis_token)
        except Exception:  # noqa: BLE001 - TTL 兜底，不让清理流程崩溃
            logger.warning(
                "redis project lock release failed project=%s",
                lease.project_id,
                exc_info=True,
            )


project_write_lock = ProjectWriteLock()


__all__ = [
    "ProjectBusyError",
    "ProjectWriteLease",
    "ProjectWriteLock",
    "project_write_lock",
]
