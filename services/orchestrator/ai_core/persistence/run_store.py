"""Run 事件的持久化存储。

目标不是替代 LangGraph checkpoint，而是先解决三类“重启即失”的问题：

  1. 事件历史：客户端重连 / 服务重启后仍能按 afterSeq 回放；
  2. Run 快照：GET /runs/{runId} 仍能返回最后状态；
  3. 已确认契约：Fullstack 在 Architect 进程重启后仍能按 contractId 启动。

默认使用进程内实现（测试与无 Redis 环境零依赖）；``RUN_STORE_BACKEND=redis``
时写入 Redis，Redis 暂时不可用则自动回退到内存，绝不阻塞主流程。

事件写入异步批量落盘；快照 / 契约是低频写，直接同步写入但捕获所有异常。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Any, Protocol

from ai_core.config import Settings, get_settings

logger = logging.getLogger(__name__)


class RunStore(Protocol):
    """事件 / 快照 / 契约的最小持久化接口。"""

    def append_event(self, run_id: str, event: dict[str, Any]) -> None: ...

    def list_events(
        self,
        run_id: str,
        after_seq: int = 0,
        limit: int = 2000,
    ) -> list[dict[str, Any]]: ...

    def save_snapshot(
        self,
        kind: str,
        run_id: str,
        snapshot: dict[str, Any],
    ) -> None: ...

    def load_snapshot(self, kind: str, run_id: str) -> dict[str, Any] | None: ...

    def save_contract(self, contract_id: str, contract: dict[str, Any]) -> None: ...

    def load_contract(self, contract_id: str) -> dict[str, Any] | None: ...

    def close(self) -> None: ...


class MemoryRunStore:
    """进程内实现：行为与旧版完全一致，只是多了一份可回放副本。"""

    backend = "memory"

    def __init__(self, settings: Settings | None = None) -> None:
        config = settings or get_settings()
        self._max_events = max(1, int(config.run_event_max))
        self._lock = threading.RLock()
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._contracts: dict[str, dict[str, Any]] = {}

    def append_event(self, run_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            events = self._events.setdefault(run_id, [])
            events.append(event)
            if len(events) > self._max_events:
                del events[: len(events) - self._max_events]

    def list_events(
        self,
        run_id: str,
        after_seq: int = 0,
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        with self._lock:
            events = list(self._events.get(run_id, []))
        selected = [item for item in events if int(item.get("seq") or 0) > after_seq]
        return selected[-max(1, limit) :]

    def save_snapshot(
        self,
        kind: str,
        run_id: str,
        snapshot: dict[str, Any],
    ) -> None:
        with self._lock:
            self._snapshots[f"{kind}:{run_id}"] = dict(snapshot)

    def load_snapshot(self, kind: str, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._snapshots.get(f"{kind}:{run_id}")
            return dict(value) if value is not None else None

    def save_contract(self, contract_id: str, contract: dict[str, Any]) -> None:
        with self._lock:
            self._contracts[contract_id] = dict(contract)

    def load_contract(self, contract_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._contracts.get(contract_id)
            return dict(value) if value is not None else None

    def close(self) -> None:
        return None


class RedisRunStore:
    """Redis 实现：事件异步批量写，快照 / 契约同步写。"""

    backend = "redis"

    def __init__(self, settings: Settings | None = None) -> None:
        import redis

        config = settings or get_settings()
        self._settings = config
        self._prefix = config.run_store_redis_prefix.rstrip(":")
        self._max_events = max(1, int(config.run_event_max))
        self._event_ttl = max(60, int(config.run_event_ttl_seconds))
        self._snapshot_ttl = max(60, int(config.run_snapshot_ttl_seconds))
        self._client = redis.Redis(
            host=config.redis_host,
            port=config.redis_port,
            password=config.redis_password or None,
            db=config.redis_db,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
            health_check_interval=30,
        )
        self._queue: queue.Queue[tuple[str, dict[str, Any]] | None] = queue.Queue(
            maxsize=10_000
        )
        self._closed = threading.Event()
        self._dropped = 0
        self._writer = threading.Thread(
            target=self._write_loop,
            name="vibe-run-store-writer",
            daemon=True,
        )
        self._writer.start()

    # --------------------------------------------------------
    # 事件
    # --------------------------------------------------------
    def append_event(self, run_id: str, event: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait((run_id, event))
        except queue.Full:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 1000 == 0:
                logger.warning(
                    "run event persistence queue full, dropped=%s run_id=%s",
                    self._dropped,
                    run_id,
                )

    def list_events(
        self,
        run_id: str,
        after_seq: int = 0,
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        try:
            raw = self._client.lrange(self._event_key(run_id), 0, -1)
        except Exception:  # noqa: BLE001 - 读失败按“无历史”处理
            logger.warning("run event load failed run_id=%s", run_id, exc_info=True)
            return []
        events: list[dict[str, Any]] = []
        for item in raw:
            try:
                parsed = json.loads(item)
            except (TypeError, ValueError):
                continue
            if int(parsed.get("seq") or 0) > after_seq:
                events.append(parsed)
        return events[-max(1, limit) :]

    def _write_loop(self) -> None:
        while not self._closed.is_set():
            try:
                first = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if first is None:
                break
            batch = [first]
            deadline = time.monotonic() + 0.2
            while len(batch) < 200:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = self._queue.get(timeout=remaining)
                except queue.Empty:
                    break
                if item is None:
                    self._closed.set()
                    break
                batch.append(item)
            self._flush_events(batch)

    def _flush_events(self, batch: list[tuple[str, dict[str, Any]]]) -> None:
        if not batch:
            return
        try:
            pipeline = self._client.pipeline(transaction=False)
            for run_id, event in batch:
                key = self._event_key(run_id)
                pipeline.rpush(
                    key,
                    json.dumps(
                        event, ensure_ascii=False, separators=(",", ":"), default=str
                    ),
                )
                pipeline.ltrim(key, -self._max_events, -1)
                pipeline.expire(key, self._event_ttl)
            pipeline.execute()
        except Exception:  # noqa: BLE001 - 持久化失败不能影响 Run 主流程
            logger.warning(
                "run event persist failed batch=%s",
                len(batch),
                exc_info=True,
            )

    # --------------------------------------------------------
    # 快照 / 契约
    # --------------------------------------------------------
    def save_snapshot(
        self,
        kind: str,
        run_id: str,
        snapshot: dict[str, Any],
    ) -> None:
        self._set_json(
            f"{self._prefix}:snapshot:{kind}:{run_id}",
            snapshot,
            self._snapshot_ttl,
        )

    def load_snapshot(self, kind: str, run_id: str) -> dict[str, Any] | None:
        return self._get_json(f"{self._prefix}:snapshot:{kind}:{run_id}")

    def save_contract(self, contract_id: str, contract: dict[str, Any]) -> None:
        self._set_json(
            f"{self._prefix}:contract:{contract_id}",
            contract,
            self._snapshot_ttl,
        )

    def load_contract(self, contract_id: str) -> dict[str, Any] | None:
        return self._get_json(f"{self._prefix}:contract:{contract_id}")

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception:  # noqa: BLE001
            return False

    def close(self) -> None:
        # 先让 writer 排空队列再退出；队列满时最多等 join 超时。
        try:
            self._queue.put(None, timeout=0.5)
        except queue.Full:
            pass
        if self._writer.is_alive():
            self._writer.join(timeout=2.0)
        self._closed.set()

    # --------------------------------------------------------
    # 内部
    # --------------------------------------------------------
    def _event_key(self, run_id: str) -> str:
        return f"{self._prefix}:events:{run_id}"

    def _set_json(self, key: str, value: dict[str, Any], ttl: int) -> None:
        try:
            self._client.setex(
                key,
                ttl,
                json.dumps(
                    value, ensure_ascii=False, separators=(",", ":"), default=str
                ),
            )
        except Exception:  # noqa: BLE001
            logger.warning("run store write failed key=%s", key, exc_info=True)

    def _get_json(self, key: str) -> dict[str, Any] | None:
        try:
            raw = self._client.get(key)
        except Exception:  # noqa: BLE001
            logger.warning("run store read failed key=%s", key, exc_info=True)
            return None
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None


_STORE: RunStore | None = None
_STORE_LOCK = threading.Lock()


def get_run_store() -> RunStore:
    """按配置惰性创建；Redis 不可用时回退到内存。"""
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                settings = get_settings()
                backend = (settings.run_store_backend or "memory").strip().lower()
                if backend in {"redis", "auto"}:
                    try:
                        candidate = RedisRunStore(settings)
                        if candidate.ping():
                            _STORE = candidate
                            logger.info(
                                "run store backend=redis host=%s db=%s",
                                settings.redis_host,
                                settings.redis_db,
                            )
                        else:
                            candidate.close()
                            logger.warning(
                                "run store redis unavailable, fallback to memory"
                            )
                            _STORE = MemoryRunStore(settings)
                    except Exception:  # noqa: BLE001 - 缺依赖 / 连接失败
                        logger.warning(
                            "run store redis init failed, fallback to memory",
                            exc_info=True,
                        )
                        _STORE = MemoryRunStore(settings)
                else:
                    _STORE = MemoryRunStore(settings)
    return _STORE


def reset_run_store_for_tests(store: RunStore | None = None) -> RunStore:
    """测试辅助：替换全局 store，避免用例相互污染。"""
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None and store is not _STORE:
            try:
                _STORE.close()
            except Exception:  # noqa: BLE001
                pass
        _STORE = store or MemoryRunStore()
    return _STORE


__all__ = [
    "MemoryRunStore",
    "RedisRunStore",
    "RunStore",
    "get_run_store",
    "reset_run_store_for_tests",
]
