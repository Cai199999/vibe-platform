"""Eval Suite 的领域模型。

这里只放“评测集应该长什么样”与“一次评测结果是什么”，不包含任何
Supervisor / LLM 细节，便于 CLI、报告与未来 HTTP 引擎复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ALLOWED_HANDOFFS: frozenset[str] = frozenset(
    {
        "handoff_ui",
        "handoff_architect",
        "handoff_both",
        "handoff_custom",
        "rejected",
    }
)


@dataclass(frozen=True)
class EvalInput:
    """一条用例的输入。

    ``target_agent`` 为 ``auto`` 时交给 Supervisor 规则路由；否则把它写入
    ``options.targetAgent``，用于显式指定专家 Agent。
    """

    target_agent: str = "auto"
    text: str = ""
    annotations: tuple[dict[str, Any], ...] = ()
    fixture: str | None = None
    project_id: str | None = None


@dataclass(frozen=True)
class EvalAssertions:
    """一条用例的断言集合。

    所有字段都只做“包含 / 等于”这种稳定比较，不做模糊匹配；失败时报告
    必须能直接定位到期望值与实际值。
    """

    expected_handoff: str
    expected_agent: str | None = None
    expected_task_type: str | None = None
    expected_dispatchable: bool | None = None
    # None 表示“不断言”；空元组表示“必须为空”，两者语义不同。
    expected_handoffs: tuple[str, ...] | None = None
    expected_missing_inputs: tuple[str, ...] | None = None
    expected_code_contains: tuple[str, ...] = ()
    expected_code_not_contains: tuple[str, ...] = ()
    # 安全策略 / 路由原因断言。
    expected_reason_contains: tuple[str, ...] = ()
    # Architect HITL：真实执行子图并断言暂停事件与首个问题槽位。
    expected_hitl: str | None = None
    expected_hitl_slot: str | None = None
    # both 门控：dispatch 后断言门控状态。
    expected_gate_status: str | None = None
    max_duration_ms: int | None = None


@dataclass(frozen=True)
class EvalCase:
    """一条可执行的评测用例。"""

    case_id: str
    name: str
    input: EvalInput
    assertions: EvalAssertions
    tags: tuple[str, ...] = ()
    source_line: int | None = None
    source_path: Path | None = None

    @property
    def needs_code_generation(self) -> bool:
        """只有显式声明代码断言的用例才走 Mock 代码生成。"""
        return bool(
            self.assertions.expected_code_contains
            or self.assertions.expected_code_not_contains
        )

    @property
    def needs_hitl(self) -> bool:
        """是否真实执行 Architect 子图并验证 HITL 暂停。"""
        return bool(self.assertions.expected_hitl)

    @property
    def needs_gate(self) -> bool:
        """是否真实 dispatch both 并验证门控。"""
        return bool(self.assertions.expected_gate_status)


@dataclass(frozen=True)
class EvalSuite:
    """一份完整的评测集。"""

    name: str
    cases: tuple[EvalCase, ...]
    path: Path
    fixtures_root: Path
    version: str = "1"
    description: str = ""


@dataclass(frozen=True)
class CheckResult:
    """单个断言的执行结果。"""

    name: str
    passed: bool
    expected: Any = None
    actual: Any = None
    message: str = ""


@dataclass
class CaseResult:
    """单条用例的执行结果。"""

    case_id: str
    name: str
    passed: bool
    duration_ms: float
    checks: list[CheckResult] = field(default_factory=list)
    handoff: str | None = None
    target: str | None = None
    task_type: str | None = None
    dispatchable: bool | None = None
    missing_inputs: list[str] = field(default_factory=list)
    handoffs: list[str] = field(default_factory=list)
    hitl_event: str | None = None
    hitl_slot: str | None = None
    gate_status: str | None = None
    error: str | None = None
    tags: tuple[str, ...] = ()

    @property
    def failures(self) -> list[CheckResult]:
        return [check for check in self.checks if not check.passed]


@dataclass(frozen=True)
class SuiteSummary:
    total: int
    passed: int
    failed: int
    duration_ms: float
    pass_rate: float


@dataclass(frozen=True)
class SuiteResult:
    suite: EvalSuite
    cases: tuple[CaseResult, ...]
    duration_ms: float
    generated_at: str

    @property
    def summary(self) -> SuiteSummary:
        total = len(self.cases)
        passed = sum(1 for case in self.cases if case.passed)
        failed = total - passed
        return SuiteSummary(
            total=total,
            passed=passed,
            failed=failed,
            duration_ms=self.duration_ms,
            pass_rate=(passed / total) if total else 0.0,
        )
