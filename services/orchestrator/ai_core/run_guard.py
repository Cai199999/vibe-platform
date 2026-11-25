"""全局 Run 并发舱壁。

LLM 调用、沙盒构建和 npm 校验都会长时间占用线程与外部资源。没有上限时，
一次流量峰值会把 python-ai 的线程与容器配额打满，进而拖垮所有请求。

这里给“真正执行 Graph 的后台线程”加一个进程级信号量：超出的 Run 在
自己的线程里等待，HTTP 接口仍然立即返回 202。等待中的 Run 不会占用
LLM / 沙盒资源，也不会拒绝用户请求，因此不改变既有交互语义。

多副本部署时每个进程各自限额；后续可替换为 Redis 分布式信号量。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator


class RunConcurrencyGuard:
    """基于 BoundedSemaphore 的进程内并发闸门。"""

    def __init__(self, limit: int) -> None:
        self._limit = max(1, int(limit))
        self._slots = threading.BoundedSemaphore(self._limit)
        self._lock = threading.Lock()
        self._in_flight = 0

    @property
    def limit(self) -> int:
        return self._limit

    def wrap(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """包装后台 worker：进入 Graph 前拿槽位，退出时归还。"""

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            with self.slot():
                return fn(*args, **kwargs)

        wrapped.__name__ = getattr(fn, "__name__", "guarded_worker")
        wrapped.__doc__ = getattr(fn, "__doc__")
        return wrapped

    @contextmanager
    def slot(self) -> Iterator[None]:
        self._slots.acquire()
        with self._lock:
            self._in_flight += 1
        try:
            yield
        finally:
            with self._lock:
                self._in_flight -= 1
            self._slots.release()

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            in_flight = self._in_flight
        return {
            "limit": self._limit,
            "inFlight": in_flight,
        }


_GUARD: RunConcurrencyGuard | None = None
_GUARD_LOCK = threading.Lock()


def get_run_guard() -> RunConcurrencyGuard:
    """按配置惰性创建全局闸门。"""
    global _GUARD
    if _GUARD is None:
        with _GUARD_LOCK:
            if _GUARD is None:
                from ai_core.config import get_settings

                _GUARD = RunConcurrencyGuard(get_settings().max_concurrent_runs)
    return _GUARD


def reset_run_guard_for_tests(limit: int | None = None) -> RunConcurrencyGuard:
    """测试辅助：替换全局闸门，避免用例之间互相影响。"""
    global _GUARD
    with _GUARD_LOCK:
        _GUARD = RunConcurrencyGuard(limit or 8)
    return _GUARD


__all__ = [
    "RunConcurrencyGuard",
    "get_run_guard",
    "reset_run_guard_for_tests",
]
