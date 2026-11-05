"""离线评测执行器。

默认引擎不启动 FastAPI、不创建 Docker 沙盒、不读取模型密钥：

  1. 用真实的 ``SupervisorService`` + ``AgentRegistry`` 做路由，断言
     handoff / target / dispatchable / missingInputs；
  2. 对有代码断言的 UI 用例，用 AI Core 的 ``MockAgentLLM`` 对 fixture
     生成确定性代码，再断言代码片段。

它不是端到端沙盒测试；它的价值是“每次提交都能在秒级发现路由与生成
契约的回归”。真实沙盒 / DeepSeek 的 E2E 由 ``scripts/*_e2e_test.py``
继续覆盖，二者互补。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from ai_core.agent.llm import MockAgentLLM
from ai_core.agent.schemas import RunOptions
from ai_core.supervisor.registry import AgentRegistry
from ai_core.supervisor.schemas import SupervisorOptions, SupervisorRequest
from ai_core.supervisor.service import SupervisorService

from eval_suite.loader import load_fixture_files
from eval_suite.models import (
    CaseResult,
    CheckResult,
    EvalCase,
    EvalSuite,
    SuiteResult,
)

ORCHESTRATOR_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CUSTOM_AGENTS_DIR = ORCHESTRATOR_ROOT / "config" / "custom_agents"


def build_registry(custom_agents_dir: str | Path | None = None) -> AgentRegistry:
    """构建与生产同源的注册中心，并显式扫描企业 CustomAgent。"""
    if custom_agents_dir is not None:
        directory = Path(custom_agents_dir)
    else:
        env_dir = os.environ.get("SUPERVISOR_CUSTOM_AGENTS_DIR")
        directory = Path(env_dir) if env_dir else DEFAULT_CUSTOM_AGENTS_DIR

    registry = AgentRegistry(custom_agents_dir=directory)
    registry.reload_custom_agents()
    return registry


class EvalEngine:
    """顺序执行评测用例；无状态，可安全复用于 CLI / CI。"""

    def __init__(
        self,
        registry: AgentRegistry | None = None,
        *,
        custom_agents_dir: str | Path | None = None,
    ) -> None:
        self.registry = registry or build_registry(custom_agents_dir)
        # route 只使用 Supervisor Graph；默认 dispatcher 不会启动任何子图。
        self.service = SupervisorService(self.registry)

    def run_suite(
        self,
        suite: EvalSuite,
        *,
        fail_fast: bool = False,
    ) -> SuiteResult:
        generated_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        started = time.perf_counter()
        results: list[CaseResult] = []
        for case in suite.cases:
            result = self.run_case(case, suite)
            results.append(result)
            if fail_fast and not result.passed:
                break
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        return SuiteResult(
            suite=suite,
            cases=tuple(results),
            duration_ms=duration_ms,
            generated_at=generated_at,
        )

    def run_case(self, case: EvalCase, suite: EvalSuite) -> CaseResult:
        started = time.perf_counter()
        checks: list[CheckResult] = []
        decision: Any = None
        route: Any = None
        error: str | None = None

        try:
            request = self._build_request(case, suite)
            route = self.service.route(request)
            decision = route.decision
            checks.extend(self._check_decision(case, decision))
            checks.extend(self._check_handoffs(case, route.handoffs))
            if case.needs_code_generation:
                checks.extend(self._check_generated_code(case, suite))
        except Exception as exc:  # noqa: BLE001 - 单条用例失败不拖垮整批
            error = f"{type(exc).__name__}: {exc}"
            checks.append(
                CheckResult(
                    name="execution",
                    passed=False,
                    expected="用例可执行",
                    actual=error,
                    message="执行用例时抛出异常",
                )
            )

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        if case.assertions.max_duration_ms is not None:
            checks.append(
                CheckResult(
                    name="latency_budget",
                    passed=duration_ms <= case.assertions.max_duration_ms,
                    expected=f"<= {case.assertions.max_duration_ms} ms",
                    actual=f"{duration_ms} ms",
                    message="用例耗时超出预算",
                )
            )

        return CaseResult(
            case_id=case.case_id,
            name=case.name,
            passed=error is None and all(check.passed for check in checks),
            duration_ms=duration_ms,
            checks=checks,
            handoff=getattr(decision, "handoff", None),
            target=getattr(decision, "target", None),
            task_type=getattr(decision, "task_type", None),
            dispatchable=getattr(decision, "dispatchable", None),
            missing_inputs=list(getattr(decision, "missing_inputs", []) or []),
            handoffs=(
                [item.agent_id for item in getattr(route, "handoffs", [])]
                if decision is not None
                else []
            ),
            error=error,
            tags=case.tags,
        )

    # --------------------------------------------------------
    # 请求构造
    # --------------------------------------------------------
    def _build_request(self, case: EvalCase, suite: EvalSuite) -> SupervisorRequest:
        files: dict[str, str] = {}
        if case.input.fixture:
            files = load_fixture_files(suite.fixtures_root, case.input.fixture)

        target_agent = case.input.target_agent.strip()
        options = SupervisorOptions(
            target_agent=None if target_agent == "auto" else target_agent,
            ui=RunOptions(
                llm_provider="mock",
                mock_scenario="none",
                clarification_mode="off",
            ),
        )
        return SupervisorRequest(
            project_id=case.input.project_id or f"eval_{case.case_id}",
            project_name=case.name,
            message=case.input.text,
            annotations=list(case.input.annotations),
            files=files,
            options=options,
        )

    # --------------------------------------------------------
    # 断言
    # --------------------------------------------------------
    @staticmethod
    def _check_decision(case: EvalCase, decision: Any) -> list[CheckResult]:
        expected = case.assertions
        checks = [
            CheckResult(
                name="handoff",
                passed=decision.handoff == expected.expected_handoff,
                expected=expected.expected_handoff,
                actual=decision.handoff,
                message="Supervisor Handoff 与预期不一致",
            )
        ]
        if expected.expected_agent is not None:
            checks.append(
                CheckResult(
                    name="agent",
                    passed=decision.target == expected.expected_agent,
                    expected=expected.expected_agent,
                    actual=decision.target,
                    message="目标 Agent 与预期不一致",
                )
            )
        if expected.expected_task_type is not None:
            checks.append(
                CheckResult(
                    name="task_type",
                    passed=decision.task_type == expected.expected_task_type,
                    expected=expected.expected_task_type,
                    actual=decision.task_type,
                    message="taskType 与预期不一致",
                )
            )
        if expected.expected_dispatchable is not None:
            checks.append(
                CheckResult(
                    name="dispatchable",
                    passed=decision.dispatchable is expected.expected_dispatchable,
                    expected=expected.expected_dispatchable,
                    actual=decision.dispatchable,
                    message="可派发性与预期不一致",
                )
            )
        if expected.expected_missing_inputs is not None:
            actual_missing = list(decision.missing_inputs)
            missing_ok = all(
                item in actual_missing for item in expected.expected_missing_inputs
            )
            if not expected.expected_missing_inputs:
                missing_ok = not actual_missing
            checks.append(
                CheckResult(
                    name="missing_inputs",
                    passed=missing_ok,
                    expected=list(expected.expected_missing_inputs),
                    actual=actual_missing,
                    message="missingInputs 未覆盖预期",
                )
            )
        return checks

    @staticmethod
    def _check_handoffs(case: EvalCase, handoffs: list[Any]) -> list[CheckResult]:
        expected = case.assertions.expected_handoffs
        if expected is None:
            return []
        actual = [item.agent_id for item in handoffs]
        return [
            CheckResult(
                name="handoffs",
                passed=actual == list(expected),
                expected=list(expected),
                actual=actual,
                message="Handoff 顺序或数量与预期不一致",
            )
        ]

    @staticmethod
    def _check_generated_code(
        case: EvalCase,
        suite: EvalSuite,
    ) -> list[CheckResult]:
        if not case.input.fixture:
            return [
                CheckResult(
                    name="code_fixture",
                    passed=False,
                    expected="fixture",
                    actual=None,
                    message="代码断言缺少 fixture",
                )
            ]
        if not case.input.annotations:
            return [
                CheckResult(
                    name="code_annotations",
                    passed=False,
                    expected="至少 1 个标注",
                    actual=0,
                    message="代码生成需要画布标注",
                )
            ]

        files = load_fixture_files(suite.fixtures_root, case.input.fixture)
        annotations = [dict(item) for item in case.input.annotations]
        llm = MockAgentLLM(scenario="none")
        plan = llm.plan(annotations, files)
        if plan.needs_clarification:
            return [
                CheckResult(
                    name="mock_plan",
                    passed=False,
                    expected="无需澄清",
                    actual=plan.clarification,
                    message="Mock Planner 判定指令信息不足，代码未生成",
                )
            ]

        target_files = _target_files(annotations, files)
        result = llm.generate(
            annotations,
            plan.plan,
            files,
            target_files,
            {"success": True},
            0,
        )
        code = "\n".join(result.files[path] for path in sorted(result.files))

        checks: list[CheckResult] = []
        for snippet in case.assertions.expected_code_contains:
            checks.append(
                CheckResult(
                    name=f"code_contains:{snippet}",
                    passed=snippet in code,
                    expected=snippet,
                    actual="present" if snippet in code else "missing",
                    message="生成代码缺少预期片段",
                )
            )
        for snippet in case.assertions.expected_code_not_contains:
            checks.append(
                CheckResult(
                    name=f"code_not_contains:{snippet}",
                    passed=snippet not in code,
                    expected=f"not {snippet}",
                    actual="present" if snippet in code else "absent",
                    message="生成代码包含禁止片段",
                )
            )
        return checks


def _target_files(
    annotations: list[dict[str, Any]],
    files: dict[str, str],
) -> list[str]:
    """按标注来源文件解析目标；没有来源时回退到 App.jsx。"""
    targets: set[str] = set()
    for annotation in annotations:
        target = annotation.get("target") or {}
        source = target.get("source") or {}
        path = str(source.get("file") or "").strip()
        if path and path in files:
            targets.add(path)
    if not targets:
        default = "src/App.jsx"
        targets.add(default if default in files else sorted(files)[0])
    return sorted(targets)
