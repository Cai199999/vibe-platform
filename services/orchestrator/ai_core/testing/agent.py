"""测试 Agent：计划 → 执行 → 结构化报告 → 回流 Architect。

边界：
  - 只读验收，不修改生成物；
  - 失败按确定性错误码进入 ``error_router``，单错误最多 3 次；
  - L1/L2 报告明确标注“仅供参考”，L3 未接入时如实 SKIPPED；
  - 测试报告同时写入 A2A 日志与 Architect 收件箱，供编排层决策。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ai_core.collaboration.error_router import GATE_ERRORS, GateError
from ai_core.testing.models import (
    CheckResult,
    TestPlan,
    TestReport,
    TestRequest,
)
from ai_core.testing.planner import build_plan
from ai_core.testing.runner import HttpTestRunner, TestRunner

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TestAgent:
    """符合平台小 Agent 规范的测试执行器。"""

    agent_id = "test_agent"

    def __init__(
        self,
        *,
        runner_factory: Callable[[], TestRunner] = HttpTestRunner,
        max_reports: int = 200,
        report_path: str | Path | None = None,
    ) -> None:
        self._runner_factory = runner_factory
        self._max_reports = max(1, max_reports)
        self._lock = threading.RLock()
        self._reports: dict[str, TestReport] = {}
        self._order: list[str] = []
        self._attempts: dict[str, int] = {}
        self._report_path = Path(
            report_path
            or os.environ.get("TEST_REPORT_PATH")
            or "data/test-reports.jsonl"
        )

    # ---- 计划 ---------------------------------------------------
    def plan(self, request: TestRequest) -> TestPlan:
        return build_plan(request)

    # ---- 执行 ---------------------------------------------------
    def execute(
        self,
        request: TestRequest,
        *,
        runner: TestRunner | None = None,
    ) -> TestReport:
        plan = self.plan(request)
        runner = runner or self._runner_factory()
        started = _now()
        results: list[CheckResult] = []
        for check in plan.checks:
            result = runner.run(check, request)
            results.append(result)
            logger.info(
                "test check run=%s tier=%s kind=%s status=%s ms=%s",
                request.source_run_id or "-",
                request.tier,
                check.kind,
                result.status,
                result.duration_ms,
            )

        summary = {
            "total": len(results),
            "passed": sum(1 for item in results if item.status == "PASSED"),
            "failed": sum(1 for item in results if item.status == "FAILED"),
            "error": sum(1 for item in results if item.status == "ERROR"),
            "skipped": sum(1 for item in results if item.status == "SKIPPED"),
        }
        if summary["error"]:
            status = "ERROR"
        elif summary["failed"]:
            status = "FAILED"
        elif summary["skipped"] and request.tier == "L3":
            # L3 的核心检查尚未接入：不能把“跳过了生产验收”报告成通过。
            status = "SKIPPED"
        else:
            status = "PASSED"

        warnings: list[str] = []
        if request.tier in {"L1", "L2"}:
            warnings.append("本报告为自动验收，仅供参考，不代表人工接受。")
        if summary["skipped"]:
            warnings.append(
                f"{summary['skipped']} 项检查因环境未接入而跳过，未计入通过。"
            )
        report = TestReport(
            report_id=f"trep_{uuid.uuid4().hex[:20]}",
            plan_id=plan.plan_id,
            source_run_id=request.source_run_id,
            project_id=request.project_id,
            contract_id=request.contract_id,
            tier=request.tier,
            status=status,  # type: ignore[arg-type]
            advisory=plan.advisory,
            checks=results,
            summary=summary,
            warnings=warnings,
            started_at=started,
            finished_at=_now(),
            evidence={
                "agentId": self.agent_id,
                "planId": plan.plan_id,
                "previewUrl": request.preview_url,
                "apiBaseUrl": request.api_base_url or request.preview_url,
            },
        )
        self._store(report)
        self._persist(report)
        try:
            from ai_core.testing.metrics import TEST_METRICS

            TEST_METRICS.record(report)
        except Exception:  # pragma: no cover - 指标失败不影响报告
            logger.exception("测试指标记录失败 report=%s", report.report_id)
        self._record_failures(report)
        self._notify_architect(report, request)
        return report

    # ---- 查询 ---------------------------------------------------
    def report_for(self, run_id: str) -> TestReport | None:
        with self._lock:
            report = self._reports.get(run_id)
        if report is not None:
            return report
        # 兼容按 reportId 查询
        with self._lock:
            for item in self._reports.values():
                if item.report_id == run_id:
                    return item
        stored = self._load_persisted(run_id)
        if stored is not None:
            self._store(stored)
        return stored

    def latest(self, limit: int = 20) -> list[TestReport]:
        with self._lock:
            keys = list(reversed(self._order))[: max(1, limit)]
            reports = [self._reports[key] for key in keys if key in self._reports]
        if reports:
            return reports
        # 进程重启后内存为空：从 JSONL 尾部恢复最近报告。
        try:
            if not self._report_path.exists():
                return []
            lines = self._report_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        restored: list[TestReport] = []
        seen: set[str] = set()
        for line in reversed(lines[-max(1, limit) * 4 :]):
            try:
                payload = json.loads(line)
                report = TestReport.model_validate(payload)
            except Exception:  # noqa: BLE001 - 单条坏数据跳过
                continue
            if report.report_id in seen:
                continue
            seen.add(report.report_id)
            restored.append(report)
            if len(restored) >= max(1, limit):
                break
        for report in reversed(restored):
            self._store(report)
        return restored

    # ---- 内部 ---------------------------------------------------
    def _store(self, report: TestReport) -> None:
        key = report.source_run_id or report.report_id
        with self._lock:
            self._reports[key] = report
            self._reports[report.report_id] = report
            self._order.append(key)
            while len(self._order) > self._max_reports:
                stale = self._order.pop(0)
                self._reports.pop(stale, None)

    def _persist(self, report: TestReport) -> None:
        """报告落 JSONL + RunStore，进程重启后仍可查询与回放。"""
        payload = report.model_dump(mode="json", by_alias=True)
        try:
            self._report_path.parent.mkdir(parents=True, exist_ok=True)
            with self._report_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("测试报告 JSONL 写入失败 %s: %s", self._report_path, exc)
        try:
            from ai_core.persistence.run_store import get_run_store

            get_run_store().save_snapshot(
                "test_report",
                report.source_run_id or report.report_id,
                payload,
            )
        except Exception:  # noqa: BLE001 - 持久化失败不影响本次报告
            logger.exception("测试报告 RunStore 写入失败 report=%s", report.report_id)

    def _load_persisted(self, run_id: str) -> TestReport | None:
        try:
            from ai_core.persistence.run_store import get_run_store

            stored = get_run_store().load_snapshot("test_report", run_id)
            if isinstance(stored, dict) and stored.get("reportId"):
                return TestReport.model_validate(stored)
        except Exception:  # noqa: BLE001 - 回源失败继续尝试 JSONL
            logger.exception("测试报告 RunStore 读取失败 run=%s", run_id)
        try:
            if not self._report_path.exists():
                return None
            lines = self._report_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for line in reversed(lines[-500:]):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            if run_id not in {
                str(payload.get("sourceRunId") or ""),
                str(payload.get("reportId") or ""),
            }:
                continue
            try:
                return TestReport.model_validate(payload)
            except Exception:  # noqa: BLE001 - 单条坏数据跳过
                continue
        return None

    def _record_failures(self, report: TestReport) -> None:
        for result in report.checks:
            code = result.error_code
            if not code or result.status not in {"FAILED", "ERROR"}:
                continue
            fingerprint = hashlib.sha1(
                f"{code}|{result.message}".encode("utf-8")
            ).hexdigest()[:16]
            with self._lock:
                self._attempts[fingerprint] = self._attempts.get(fingerprint, 0) + 1
                attempt = self._attempts[fingerprint]
            try:
                assignment = GATE_ERRORS.plan(
                    GateError(
                        code=code,
                        message=result.message,
                        run_id=report.source_run_id or report.report_id,
                        stage=f"testing.{report.tier}",
                        source="TestAgent",
                        attempt=attempt,
                        max_attempts=3,
                    )
                )
                GATE_ERRORS.ledger.record(
                    assignment, source="TestAgent", phase="testing"
                )
            except Exception:  # pragma: no cover - 记录失败不影响测试报告
                logger.exception(
                    "测试失败入账失败 code=%s run=%s",
                    code,
                    report.source_run_id,
                )

    def _notify_architect(self, report: TestReport, request: TestRequest) -> None:
        """把报告写回 A2A 日志与 Architect 收件箱。"""
        try:
            from ai_core.a2a import build_feedback
            from ai_core.collaboration.hub import ARCHITECT_HUB

            failures = [
                {
                    "checkId": item.check_id,
                    "kind": item.kind,
                    "status": item.status,
                    "errorCode": item.error_code,
                    "message": item.message,
                    "evidence": item.evidence,
                }
                for item in report.checks
                if item.status in {"FAILED", "ERROR"}
            ]
            feedback = build_feedback(
                run_id=request.source_run_id or report.report_id,
                from_agent="TestAgent",
                to_agent="Architect",
                kind="test_report",
                requested_action=(
                    "REVIEW_TEST_FAILURE" if failures else "ACK_TEST_REPORT"
                ),
                summary=(
                    f"{report.tier} 测试 {report.status}："
                    f"{report.summary.get('passed', 0)}/{report.summary.get('total', 0)} "
                    f"通过，{len(failures)} 条失败"
                ),
                evidence=[
                    f"{item.get('errorCode')}: {item.get('message')}"
                    for item in failures
                ],
                contract_id=request.contract_id,
                payload={
                    "reportId": report.report_id,
                    "tier": report.tier,
                    "advisory": report.advisory,
                    "summary": report.summary,
                    "failures": failures,
                },
            )
            ARCHITECT_HUB.ingest_feedback(
                feedback,
                task_id=request.contract_id or request.source_run_id,
            )
        except Exception:  # pragma: no cover - 回流失败不影响测试结论
            logger.exception("测试报告回传 Architect 失败 report=%s", report.report_id)


# 进程级单例：API / 未来 Fullstack 节点共用。
TEST_AGENT = TestAgent()


__all__ = ["TEST_AGENT", "TestAgent"]
