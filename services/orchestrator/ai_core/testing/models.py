"""测试层的领域模型。

设计目标：
  - 测试计划与报告可序列化、可入库、可回传 Architect；
  - 分级（L1/L2/L3）显式声明，L1/L2 报告必须带“仅供参考”提示；
  - 失败结果带确定性错误码，交给 ``error_router`` 分派，而不是自由重试。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import Field

from ai_core.domain.common import CamelModel

TestTier = Literal["L1", "L2", "L3"]
TestStatus = Literal["PASSED", "FAILED", "SKIPPED", "ERROR"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TestRequest(CamelModel):
    """一次测试请求；由 Architect / Fullstack / 前端触发。"""

    source_run_id: str = Field(default="", max_length=128)
    project_id: str = Field(default="", max_length=128)
    contract_id: str = Field(default="", max_length=128)
    tier: TestTier = "L1"
    # 预览地址与 API 基地址；缺省时 API 与预览同源。
    preview_url: str = ""
    api_base_url: str = ""
    # 契约快照（ArchitectContract.model_dump / 前端 JSON）；用于生成测试计划。
    contract: dict[str, Any] = Field(default_factory=dict)
    # 可选：VisualDocument，用于后续 UI 断言扩展。
    visual_document: dict[str, Any] = Field(default_factory=dict)
    auth: dict[str, str] = Field(default_factory=dict)
    sample_payload: dict[str, Any] = Field(default_factory=dict)
    # Playwright UI runner 地址；为空时回退环境变量 TEST_UI_RUNNER_URL。
    ui_runner_url: str = ""
    timeout_seconds: int = Field(default=20, ge=1, le=120)


class TestCheck(CamelModel):
    """计划中的一条检查项。"""

    check_id: str
    tier: TestTier
    kind: str
    description: str = ""
    target: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class TestPlan(CamelModel):
    plan_id: str
    source_run_id: str = ""
    project_id: str = ""
    contract_id: str = ""
    tier: TestTier
    checks: list[TestCheck] = Field(default_factory=list)
    advisory: str = ""
    created_at: str = Field(default_factory=_now)


class CheckResult(CamelModel):
    check_id: str
    kind: str
    status: TestStatus
    message: str = ""
    duration_ms: int = 0
    error_code: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class TestReport(CamelModel):
    report_id: str
    plan_id: str
    source_run_id: str = ""
    project_id: str = ""
    contract_id: str = ""
    tier: TestTier
    status: TestStatus
    advisory: str = ""
    checks: list[CheckResult] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    started_at: str = Field(default_factory=_now)
    finished_at: str = Field(default_factory=_now)
    evidence: dict[str, Any] = Field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status in {"FAILED", "ERROR"}


__all__ = [
    "CheckResult",
    "TestCheck",
    "TestPlan",
    "TestReport",
    "TestRequest",
    "TestStatus",
    "TestTier",
]
