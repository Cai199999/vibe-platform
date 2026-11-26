"""测试层指标聚合：可观测、可评测、可迭代的最小闭环。

指标只保存聚合值，不保存报告正文；报告由 TestReport + JSONL/RunStore
持久化，二者通过 reportId / runId 关联。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TestMetrics:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs = 0
        self._by_tier: dict[str, dict[str, int]] = {}
        self._checks: dict[str, int] = {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "error": 0,
            "skipped": 0,
        }
        self._by_kind: dict[str, dict[str, int]] = {}
        self._duration_sum_ms = 0
        self._duration_count = 0
        self._last_run_at = ""

    def record(self, report: Any) -> None:
        with self._lock:
            self._runs += 1
            tier = str(getattr(report, "tier", "") or "UNKNOWN")
            bucket = self._by_tier.setdefault(
                tier, {"runs": 0, "passed": 0, "failed": 0, "error": 0, "skipped": 0}
            )
            bucket["runs"] += 1
            status = str(getattr(report, "status", "") or "").lower()
            if status in bucket:
                bucket[status] += 1
            for check in getattr(report, "checks", []) or []:
                check_status = str(getattr(check, "status", "") or "").lower()
                if check_status not in {"passed", "failed", "error", "skipped"}:
                    continue
                self._checks["total"] += 1
                self._checks[check_status] += 1
                kind = str(getattr(check, "kind", "") or "unknown")
                kind_bucket = self._by_kind.setdefault(
                    kind,
                    {"total": 0, "passed": 0, "failed": 0, "error": 0, "skipped": 0},
                )
                kind_bucket["total"] += 1
                kind_bucket[check_status] += 1
                self._duration_sum_ms += int(getattr(check, "duration_ms", 0) or 0)
                self._duration_count += 1
            self._last_run_at = str(getattr(report, "finished_at", "") or _now())

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total = self._checks["total"]
            pass_rate = self._checks["passed"] / total if total else 0.0
            return {
                "totalRuns": self._runs,
                "byTier": {
                    tier: dict(values) for tier, values in self._by_tier.items()
                },
                "checks": dict(self._checks),
                "checkPassRate": round(pass_rate, 4),
                "byKind": {
                    kind: dict(values) for kind, values in self._by_kind.items()
                },
                "averageCheckDurationMs": (
                    int(self._duration_sum_ms / self._duration_count)
                    if self._duration_count
                    else 0
                ),
                "lastRunAt": self._last_run_at,
            }

    def reset(self) -> None:
        with self._lock:
            self._runs = 0
            self._by_tier.clear()
            self._checks = {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "error": 0,
                "skipped": 0,
            }
            self._by_kind.clear()
            self._duration_sum_ms = 0
            self._duration_count = 0
            self._last_run_at = ""


TEST_METRICS = TestMetrics()


__all__ = ["TEST_METRICS", "TestMetrics"]
