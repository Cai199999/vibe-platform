"""测试层 API：计划、执行与报告查询（内部/管理员可见）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ai_core.testing.agent import TEST_AGENT
from ai_core.testing.metrics import TEST_METRICS
from ai_core.testing.models import TestReport, TestRequest
from ai_core.testing.planner import build_plan

router = APIRouter(prefix="/api/v1/internal/testing", tags=["testing"])


@router.post("/runs", response_model=TestReport)
def run_tests(request: TestRequest) -> TestReport:
    """同步执行一次测试并返回结构化报告。

    当前是 HTTP 级验收；L3 未接入时检查项会如实 SKIPPED，不伪造通过。
    """
    return TEST_AGENT.execute(request)


@router.post("/plans")
def plan_tests(request: TestRequest) -> dict[str, Any]:
    """只生成计划，不执行；供前端预览或编排层确认。"""
    plan = build_plan(request)
    return plan.model_dump(mode="json", by_alias=True)


@router.get("/runs/{run_id}", response_model=TestReport)
def get_report(run_id: str) -> TestReport:
    report = TEST_AGENT.report_for(run_id)
    if report is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "TEST_REPORT_NOT_FOUND",
                "message": f"测试报告不存在: {run_id}",
            },
        )
    return report


@router.get("/metrics")
def get_metrics() -> dict[str, Any]:
    """测试层聚合指标：Run 数、分级通过率、检查耗时、最近运行时间。"""
    snapshot = TEST_METRICS.snapshot()
    if snapshot["totalRuns"] == 0:
        # 进程重启后从持久化报告恢复指标，避免看板归零。
        for report in TEST_AGENT.latest(200):
            TEST_METRICS.record(report)
        snapshot = TEST_METRICS.snapshot()
    return snapshot


@router.get("/reports")
def list_reports(
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    items = TEST_AGENT.latest(limit)
    return {
        "count": len(items),
        "reports": [item.model_dump(mode="json", by_alias=True) for item in items],
    }


__all__ = ["router"]
