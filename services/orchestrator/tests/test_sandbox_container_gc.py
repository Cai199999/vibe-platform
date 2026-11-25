"""停止沙盒容器回收测试。

只允许清理“已退出 + 超过 TTL + 未在当前进程登记”的容器；
运行中的预览、当前登记容器、未超时的容器都必须保留。
"""

from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from ai_core.adapters.sandbox.local_docker import (
    LABEL_MANAGED,
    LABEL_SANDBOX_ID,
    LocalDockerSandbox,
)
from ai_core.sandbox_gc import start_container_gc


def _iso(days_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f000Z")


def _container(
    sandbox_id: str,
    *,
    cid: str,
    status: str = "exited",
    finished_days_ago: float = 8,
    removed: list[str] | None = None,
) -> SimpleNamespace:
    def remove(force: bool = False) -> None:
        if removed is not None:
            removed.append(sandbox_id)

    return SimpleNamespace(
        id=cid,
        status=status,
        labels={LABEL_MANAGED: "true", LABEL_SANDBOX_ID: sandbox_id},
        attrs={
            "State": {
                "Status": status,
                "FinishedAt": _iso(finished_days_ago),
            },
            "Created": _iso(finished_days_ago + 1),
        },
        remove=remove,
    )


def _fake_sandbox(
    containers: list[SimpleNamespace],
) -> tuple[LocalDockerSandbox, list[str]]:
    sandbox = object.__new__(LocalDockerSandbox)
    sandbox.settings = SimpleNamespace(  # type: ignore[assignment]
        sandbox_container_orphan_ttl_days=7,
        sandbox_container_gc_enabled=True,
        sandbox_container_gc_interval_seconds=60,
    )
    sandbox._sandboxes = {"sbx_live": "cid_live"}
    sandbox._lock = threading.RLock()
    removed: list[str] = []
    for container in containers:
        container.remove = lambda force=False, _id=container.labels[LABEL_SANDBOX_ID]: removed.append(_id)  # type: ignore[method-assign]
    sandbox._require_client = lambda: SimpleNamespace(  # type: ignore[method-assign]
        containers=SimpleNamespace(
            list=lambda all=False, filters=None: containers  # noqa: A002
        )
    )
    return sandbox, removed


class SandboxContainerGcTest(unittest.TestCase):
    def test_reclaims_only_expired_unregistered_stopped_containers(self) -> None:
        containers = [
            _container("sbx_expired", cid="cid_expired"),
            _container("sbx_fresh", cid="cid_fresh", finished_days_ago=1),
            _container("sbx_running", cid="cid_running", status="running"),
            _container("sbx_live", cid="cid_live"),
        ]
        sandbox, removed = _fake_sandbox(containers)

        reclaimed = sandbox.reap_stopped_sandboxes()

        self.assertEqual(reclaimed, ["sbx_expired"])
        self.assertEqual(removed, ["sbx_expired"])

    def test_non_positive_ttl_disables_reclaim(self) -> None:
        sandbox, removed = _fake_sandbox([_container("sbx_expired", cid="cid_expired")])

        self.assertEqual(sandbox.reap_stopped_sandboxes(ttl_days=0), [])
        self.assertEqual(removed, [])

    def test_unknown_finish_time_is_not_reclaimed(self) -> None:
        container = _container("sbx_unknown", cid="cid_unknown")
        container.attrs = {
            "State": {"Status": "exited", "FinishedAt": ""},
            "Created": "",
        }
        sandbox, removed = _fake_sandbox([container])

        self.assertEqual(sandbox.reap_stopped_sandboxes(), [])
        self.assertEqual(removed, [])

    def test_collector_respects_switch(self) -> None:
        sandbox, _ = _fake_sandbox([])
        disabled = SimpleNamespace(
            sandbox_container_gc_enabled=False,
            sandbox_container_gc_interval_seconds=60,
        )
        self.assertIsNone(start_container_gc(sandbox, disabled))  # type: ignore[arg-type]

        enabled = SimpleNamespace(
            sandbox_container_gc_enabled=True,
            sandbox_container_gc_interval_seconds=60,
        )
        collector = start_container_gc(sandbox, enabled)  # type: ignore[arg-type]
        self.assertIsNotNone(collector)
        assert collector is not None
        collector.stop()


if __name__ == "__main__":
    unittest.main()
