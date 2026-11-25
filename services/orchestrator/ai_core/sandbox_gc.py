"""孤儿草稿后台回收线程。

草稿登记表是进程内存态：python-ai 重启后，容器内的 git worktree 与
草稿预览进程仍在，但已经没有任何审批会再引用它们。这里在服务启动时
立即执行一次回收，之后按配置间隔周期执行。

设计约束：
  - 纯后台 daemon 线程，绝不阻塞 FastAPI 启动 / 健康检查；
  - 任何异常只记录日志，不向主流程冒泡；
  - 可通过 ``SANDBOX_DRAFT_GC_ENABLED=false`` 或 TTL<=0 完全停用。
"""

from __future__ import annotations

import logging
import threading

from ai_core.config import Settings
from ai_core.ports.sandbox import SandboxProvider

logger = logging.getLogger(__name__)


class DraftGarbageCollector:
    """可停止的周期回收器。"""

    def __init__(self, provider: SandboxProvider, settings: Settings) -> None:
        self._provider = provider
        self._settings = settings
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="vibe-draft-gc",
            daemon=True,
        )

    def start(self) -> "DraftGarbageCollector":
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        # 最低 60 秒，防止误配成 0 导致 Docker API 被轮询打满。
        interval = max(60, self._settings.sandbox_draft_gc_interval_seconds)
        while not self._stop_event.is_set():
            try:
                reclaimed = self._provider.reap_orphan_drafts()
                if reclaimed:
                    logger.info(
                        "orphan draft gc reclaimed %d draft(s): %s",
                        len(reclaimed),
                        ", ".join(reclaimed),
                    )
            except Exception:  # pragma: no cover - 防御性兜底
                logger.warning("orphan draft gc failed", exc_info=True)
            self._stop_event.wait(interval)


def start_draft_gc(
    provider: SandboxProvider,
    settings: Settings,
) -> DraftGarbageCollector | None:
    """按配置启动回收器；关闭时返回 ``None``。"""
    if not settings.sandbox_draft_gc_enabled:
        logger.info("orphan draft gc disabled by config")
        return None
    return DraftGarbageCollector(provider, settings).start()


class SandboxContainerGarbageCollector:
    """回收长时间停止的沙盒容器。

    与草稿回收独立：只处理 ``vibe.managed=true`` 且已退出、超过 TTL、
    不在当前进程登记表中的容器。运行中的预览永远不碰。
    """

    def __init__(self, provider: SandboxProvider, settings: Settings) -> None:
        self._provider = provider
        self._settings = settings
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="vibe-sandbox-container-gc",
            daemon=True,
        )

    def start(self) -> "SandboxContainerGarbageCollector":
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        interval = max(
            60,
            int(
                getattr(self._settings, "sandbox_container_gc_interval_seconds", 21_600)
            ),
        )
        while not self._stop_event.is_set():
            try:
                reclaimed = self._provider.reap_stopped_sandboxes()
                if reclaimed:
                    logger.info(
                        "stopped sandbox gc reclaimed %d container(s): %s",
                        len(reclaimed),
                        ", ".join(reclaimed),
                    )
            except Exception:  # pragma: no cover - 防御性兜底
                logger.warning("stopped sandbox gc failed", exc_info=True)
            self._stop_event.wait(interval)


def start_container_gc(
    provider: SandboxProvider,
    settings: Settings,
) -> SandboxContainerGarbageCollector | None:
    """按配置启动停止容器回收器；关闭时返回 ``None``。"""
    if not getattr(settings, "sandbox_container_gc_enabled", False):
        logger.info("stopped sandbox gc disabled by config")
        return None
    return SandboxContainerGarbageCollector(provider, settings).start()
