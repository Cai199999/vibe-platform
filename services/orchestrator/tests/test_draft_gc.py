"""孤儿草稿回收单元测试。

锁定三条契约：
  1. 只回收“未被当前进程登记 + 目录 mtime 超过 TTL”的草稿；
  2. 活跃草稿即使超过 TTL 也绝不回收；
  3. 后台回收线程启动即执行一次，关闭开关时完全不启动。
"""

from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from typing import Any

from docker.errors import DockerException

from ai_core.adapters.sandbox.local_docker import (
    LABEL_MANAGED,
    LABEL_SANDBOX_ID,
    LocalDockerSandbox,
)
from ai_core.sandbox_gc import start_draft_gc

EXPIRED_ID = "drf_0123456789abcdef"
FRESH_ID = "drf_fedcba9876543210"
ACTIVE_ID = "drf_aaaabbbbccccdddd"
DAY = 86_400


def _fake_sandbox(drafts: dict[str, Any] | None = None) -> tuple[Any, dict[str, Any]]:
    sandbox = object.__new__(LocalDockerSandbox)
    sandbox.settings = SimpleNamespace(  # type: ignore[assignment]
        sandbox_workdir="/app",
        sandbox_draft_root="/app-drafts",
        sandbox_draft_port=5174,
        sandbox_preview_host="127.0.0.1",
        sandbox_draft_orphan_ttl_days=7,
        sandbox_draft_gc_interval_seconds=60,
        sandbox_draft_gc_enabled=True,
    )
    sandbox._drafts = drafts or {}
    sandbox._lock = threading.RLock()
    sandbox._git_lock = threading.RLock()
    container = SimpleNamespace(
        labels={LABEL_MANAGED: "true", LABEL_SANDBOX_ID: "sbx_1"},
    )
    state: dict[str, Any] = {"stops": [], "removed": [], "commands": []}

    sandbox._require_client = lambda: SimpleNamespace(  # type: ignore[method-assign]
        containers=SimpleNamespace(list=lambda filters=None: [container])
    )
    sandbox._stop_draft_preview = (  # type: ignore[method-assign]
        lambda _container, draft_id: state["stops"].append(draft_id)
    )
    sandbox._remove_worktree = (  # type: ignore[method-assign]
        lambda _container, path, remove_dir=True: state["removed"].append(
            (path, remove_dir)
        )
    )
    return sandbox, state


def _list_output(entries: list[tuple[str, float]]) -> bytes:
    return "".join(f"{draft_id} {int(mtime)}\n" for draft_id, mtime in entries).encode()


class OrphanDraftReclaimTest(unittest.TestCase):
    def test_reclaims_only_expired_unregistered_drafts(self) -> None:
        now = time.time()
        sandbox, state = _fake_sandbox()
        sandbox._exec = (  # type: ignore[method-assign]
            lambda _container, argv, workdir=None, **_: (
                state["commands"].append(argv),
                (
                    0,
                    _list_output(
                        [
                            (EXPIRED_ID, now - 8 * DAY),
                            (FRESH_ID, now - 6 * DAY),
                            # 非法名称必须在解析阶段被丢弃。
                            ("drf_not-hex", now - 30 * DAY),
                        ]
                    ),
                ),
            )[1]
        )

        reclaimed = sandbox.reap_orphan_drafts()

        self.assertEqual(reclaimed, [EXPIRED_ID])
        self.assertEqual(state["stops"], [EXPIRED_ID])
        self.assertEqual(state["removed"], [(f"/app-drafts/{EXPIRED_ID}", True)])
        self.assertEqual(sandbox._drafts, {})

    def test_active_draft_is_never_reclaimed(self) -> None:
        now = time.time()
        sandbox, state = _fake_sandbox(
            {
                ACTIVE_ID: {
                    "sandbox_id": "sbx_1",
                    "workdir": f"/app-drafts/{ACTIVE_ID}",
                }
            }
        )
        sandbox._exec = (  # type: ignore[method-assign]
            lambda *_args, **_kwargs: (0, _list_output([(ACTIVE_ID, now - 30 * DAY)]))
        )

        reclaimed = sandbox.reap_orphan_drafts()

        self.assertEqual(reclaimed, [])
        self.assertEqual(state["stops"], [])
        self.assertEqual(state["removed"], [])
        self.assertIn(ACTIVE_ID, sandbox._drafts)

    def test_non_positive_ttl_disables_reclaim(self) -> None:
        sandbox, state = _fake_sandbox()
        sandbox._exec = lambda *_args, **_kwargs: (0, b"")  # type: ignore[method-assign]

        self.assertEqual(sandbox.reap_orphan_drafts(ttl_days=0), [])
        self.assertEqual(state["stops"], [])

    def test_docker_unavailable_returns_empty(self) -> None:
        sandbox, _state = _fake_sandbox()

        def boom() -> Any:
            raise DockerException("daemon down")

        sandbox._require_client = boom  # type: ignore[method-assign]

        self.assertEqual(sandbox.reap_orphan_drafts(), [])

    def test_failure_on_one_draft_does_not_abort_others(self) -> None:
        now = time.time()
        second_id = "drf_1111222233334444"
        sandbox, state = _fake_sandbox()
        sandbox._exec = (  # type: ignore[method-assign]
            lambda *_args, **_kwargs: (
                0,
                _list_output(
                    [(EXPIRED_ID, now - 30 * DAY), (second_id, now - 30 * DAY)]
                ),
            )
        )

        def stop(_container: Any, draft_id: str) -> None:
            if draft_id == EXPIRED_ID:
                raise DockerException("stop failed")
            state["stops"].append(draft_id)

        sandbox._stop_draft_preview = stop  # type: ignore[method-assign]

        reclaimed = sandbox.reap_orphan_drafts()

        self.assertEqual(reclaimed, [second_id])
        self.assertEqual(state["stops"], [second_id])


class BeginDraftReclaimsStaleWorktreeTest(unittest.TestCase):
    """重启后旧 worktree 会占住 5174 端口，新草稿创建前必须主动回收。"""

    def test_begin_draft_reclaims_orphan_before_new_worktree(self) -> None:
        now = time.time()
        stale_id = "drf_9999888877776666"
        sandbox, state = _fake_sandbox()
        container = SimpleNamespace(labels={LABEL_SANDBOX_ID: "sbx_1"})
        state["starts"] = []

        def exec_(
            _container: Any, argv: list[str], workdir: str | None = None, **_: Any
        ) -> tuple[int, bytes]:
            joined = " ".join(argv)
            state["commands"].append(argv)
            if "for d in" in joined:
                return 0, _list_output([(stale_id, now - 3600)])
            if "rev-parse HEAD" in joined:
                return 0, b"a" * 40 + b"\n"
            if "status --porcelain" in joined:
                return 0, b""
            return 0, b""

        sandbox._exec = exec_  # type: ignore[method-assign]
        sandbox._get_container = lambda _sandbox_id: container  # type: ignore[method-assign]
        sandbox._ensure_git_repo = lambda _container: None  # type: ignore[method-assign]
        sandbox._published_port = lambda _container, _port: 55074  # type: ignore[method-assign]
        sandbox._start_draft_preview = (  # type: ignore[method-assign]
            lambda _container, draft_id, path, port: state["starts"].append(draft_id)
        )

        draft = sandbox.begin_draft("sbx_1")

        self.assertNotEqual(draft.draft_id, stale_id)
        self.assertEqual(state["stops"], [stale_id])
        self.assertEqual(state["removed"], [(f"/app-drafts/{stale_id}", True)])
        self.assertEqual(state["starts"], [draft.draft_id])


class _FakeGcProvider:
    def __init__(self) -> None:
        self.called = threading.Event()

    def reap_orphan_drafts(self, ttl_days: int | None = None) -> list[str]:
        self.called.set()
        return []


class DraftGcSchedulerTest(unittest.TestCase):
    def test_disabled_config_does_not_start(self) -> None:
        settings = SimpleNamespace(sandbox_draft_gc_enabled=False)
        self.assertIsNone(start_draft_gc(_FakeGcProvider(), settings))  # type: ignore[arg-type]

    def test_start_runs_immediately_and_stops(self) -> None:
        provider = _FakeGcProvider()
        settings = SimpleNamespace(
            sandbox_draft_gc_enabled=True,
            sandbox_draft_gc_interval_seconds=3600,
        )

        collector = start_draft_gc(provider, settings)  # type: ignore[arg-type]
        self.assertIsNotNone(collector)
        self.assertTrue(provider.called.wait(timeout=2))
        assert collector is not None
        collector.stop()
        self.assertFalse(collector._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
