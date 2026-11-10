#!/usr/bin/env python3
"""业务 Eval：真实模型契约理解 + 全栈产物语义验收。

与 ``scripts/run_eval.py``（Supervisor 路由 / UI 局部改码）互补，本引擎只
回答一个问题：**AI 是否真的理解了用户描述的业务？**

每条用例执行：

1. 真实 DeepSeek 启动 Architect，用户 Prompt 直接作为输入；
2. 自动回答澄清问题（YAML 的 ``clarifications`` 优先，其余按槽位兜底）；
3. 拿到《全栈实施契约》，断言应用形态、API 路由、数据库表与字段；
4. 需要全栈的用例继续生成代码、导出 tar.gz 并检查：
   - 契约路由是否真的出现在后端；
   - 上传业务是否引入 multer 类中间件与文件字段；
   - 存在 API 交互时前端是否有成功态提示；
5. 输出“业务通过率 / 契约理解准确率 / 产物语义通过率”，并写 Markdown。

运行::

    python3 scripts/business_eval.py
    python3 scripts/business_eval.py --filter biz_003 --verbose
    python3 scripts/business_eval.py --no-fullstack
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import acceptance_test as acceptance  # noqa: E402
import fullstack_generation_e2e_test as generation  # noqa: E402

DEFAULT_SUITE = REPO_ROOT / "config" / "eval_suite" / "business_cases.yaml"
DEFAULT_REPORT = REPO_ROOT / "docs" / "business_eval_report.md"
DEFAULT_PROVIDER = os.environ.get("BUSINESS_EVAL_LLM_PROVIDER", "deepseek")
STARTER_APP = "export default function App() { return null }\n"
SSE_TIMEOUT = float(os.environ.get("BUSINESS_EVAL_SSE_TIMEOUT", "300"))
MAX_ROUNDS = 8

ALLOWED_CASE_KEYS = {
    "id",
    "name",
    "tags",
    "prompt",
    "expected_app_shape",
    "expected_api_routes",
    "expected_tables",
    "expected_columns",
    "clarifications",
    "run_fullstack",
    "semantic",
}


class BusinessEvalError(RuntimeError):
    """用例执行或配置错误。"""


@dataclass(frozen=True)
class RouteExpectation:
    method: str
    path: str

    @property
    def normalized(self) -> str:
        return acceptance.normalize_route(self.path)


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    name: str
    tags: tuple[str, ...]
    prompt: str
    expected_app_shape: str
    expected_api_routes: tuple[RouteExpectation, ...]
    expected_tables: tuple[str, ...]
    expected_columns: dict[str, tuple[str, ...]]
    clarifications: dict[str, str]
    run_fullstack: bool
    requires_upload: bool
    requires_success_state: bool


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""
    category: str = "contract"


@dataclass
class CaseResult:
    case_id: str
    name: str
    checks: list[Check] = field(default_factory=list)
    tokens: int = 0
    cost: float = 0.0
    duration_s: float = 0.0
    error: str | None = None
    app_shape: str = ""
    routes: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.error is None and all(check.passed for check in self.checks)

    @property
    def contract_passed(self) -> bool:
        checks = [check for check in self.checks if check.category == "contract"]
        return bool(checks) and all(check.passed for check in checks)

    @property
    def artifact_checks(self) -> list[Check]:
        return [check for check in self.checks if check.category == "artifact"]

    @property
    def artifact_passed(self) -> bool:
        checks = self.artifact_checks
        return bool(checks) and all(check.passed for check in checks)


# ------------------------------------------------------------
# 加载与校验
# ------------------------------------------------------------
def load_cases(path: Path) -> list[CaseSpec]:
    if not path.is_file():
        raise BusinessEvalError(f"评测集不存在: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise BusinessEvalError("评测集必须是非空 YAML 数组")

    cases: list[CaseSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise BusinessEvalError(f"第 {index} 条用例不是对象")
        unknown = set(item) - ALLOWED_CASE_KEYS
        if unknown:
            raise BusinessEvalError(
                f"第 {index} 条用例包含未知字段: {', '.join(sorted(unknown))}"
            )
        case_id = str(item.get("id") or "").strip()
        if not case_id or case_id in seen:
            raise BusinessEvalError(f"第 {index} 条用例 id 缺失或重复: {case_id}")
        seen.add(case_id)

        routes = tuple(
            RouteExpectation(
                method=str(route.get("method") or "").upper(),
                path=str(route.get("path") or ""),
            )
            for route in (item.get("expected_api_routes") or [])
            if isinstance(route, dict) and route.get("method") and route.get("path")
        )
        columns = {
            str(table): tuple(str(column) for column in specs)
            for table, specs in (item.get("expected_columns") or {}).items()
        }
        semantic = item.get("semantic") or {}
        cases.append(
            CaseSpec(
                case_id=case_id,
                name=str(item.get("name") or case_id),
                tags=tuple(str(tag) for tag in (item.get("tags") or [])),
                prompt=str(item.get("prompt") or "").strip(),
                expected_app_shape=str(
                    item.get("expected_app_shape") or "full_stack_system"
                ),
                expected_api_routes=routes,
                expected_tables=tuple(
                    str(table) for table in (item.get("expected_tables") or [])
                ),
                expected_columns=columns,
                clarifications={
                    str(slot): str(value)
                    for slot, value in (item.get("clarifications") or {}).items()
                },
                run_fullstack=bool(item.get("run_fullstack", False)),
                requires_upload=bool(semantic.get("requires_upload", False)),
                requires_success_state=bool(
                    semantic.get("requires_success_state", False)
                ),
            )
        )
        if not cases[-1].prompt:
            raise BusinessEvalError(f"{case_id} 缺少 prompt")
    return cases


def filter_cases(cases: list[CaseSpec], keyword: str | None) -> list[CaseSpec]:
    if not keyword:
        return cases
    needle = keyword.casefold()
    return [
        case
        for case in cases
        if needle in case.case_id.casefold()
        or needle in case.name.casefold()
        or any(needle in tag.casefold() for tag in case.tags)
    ]


# ------------------------------------------------------------
# Architect 驱动
# ------------------------------------------------------------
def choose_answer(case: CaseSpec, question: dict[str, Any]) -> str:
    slot = str(question.get("slot") or "")
    if slot in case.clarifications:
        return case.clarifications[slot]
    if slot == "app.shape":
        return case.expected_app_shape

    options = [
        option
        for option in (question.get("options") or [])
        if isinstance(option, dict) and option.get("value")
    ]

    def option_containing(*keywords: str) -> str | None:
        for option in options:
            haystack = f"{option.get('value')} {option.get('label')}".casefold()
            if any(keyword in haystack for keyword in keywords):
                return str(option["value"])
        return None

    defaults = {
        "backend.framework": option_containing("express", "node") or "node_express",
        "database.engine": option_containing("mysql") or "mysql",
        "ui.interactions": option_containing("save_and_refresh", "刷新", "成功")
        or (str(options[0]["value"]) if options else "save_and_refresh"),
        "auth.required": option_containing("no", "不需要") or "no",
    }
    if slot in defaults:
        return defaults[slot]

    if options:
        return str(options[0]["value"])
    return "按上述需求的默认最佳实践实现，保持前后端与数据库一致。"


def sum_usage(events: list[dict[str, Any]]) -> tuple[int, float]:
    total_tokens = 0
    total_cost = 0.0
    for event in events:
        if event.get("type") != "architect.node.finished":
            continue
        usage = (event.get("payload") or {}).get("usage") or {}
        total_tokens += int(usage.get("totalTokens") or 0)
        total_cost += float(usage.get("cost") or 0)
    return total_tokens, total_cost


def drive_to_contract(
    case: CaseSpec,
    provider: str,
) -> tuple[str, dict[str, Any], int, int, float]:
    """返回 runId、contract、最后 seq、tokens、cost。"""
    run_id = f"biz_{case.case_id}_{uuid.uuid4().hex[:8]}"
    payload = {
        "runId": run_id,
        "tenantId": "tenant_business_eval",
        "projectId": f"project_{run_id}",
        "projectName": case.name,
        "source": {"files": {"src/App.jsx": STARTER_APP}},
        "uiHistory": [{"role": "user", "content": case.prompt}],
        "options": {
            "llmProvider": provider,
            "confidenceThreshold": 0.8,
            "maxClarificationRounds": 6,
            "ragProvider": "markdown",
            "ragTopK": 3,
        },
    }
    events, terminal = generation.read_sse(
        f"{generation.ARCHITECT_ROOT}/runs/stream",
        payload,
        timeout=SSE_TIMEOUT,
    )
    all_events = list(events)
    last_seq = int(terminal.get("seq") or 0)

    for round_no in range(1, MAX_ROUNDS + 1):
        terminal_type = terminal.get("type")
        if terminal_type == "architect.contract.ready":
            tokens, cost = sum_usage(all_events)
            return (
                run_id,
                terminal["payload"]["contract"],
                last_seq,
                tokens,
                cost,
            )
        if terminal_type != "architect.clarification.required":
            raise BusinessEvalError(
                f"Architect 未生成契约: {terminal_type} -> "
                f"{json.dumps(terminal.get('payload'), ensure_ascii=False)[:800]}"
            )

        clarification = terminal.get("payload") or {}
        answers = [
            {
                "questionId": question["questionId"],
                "value": choose_answer(case, question),
            }
            for question in clarification.get("questions") or []
            if question.get("questionId")
        ]
        if not answers:
            raise BusinessEvalError(f"第 {round_no} 轮澄清没有可回答的问题")
        status, body = generation.request_json(
            "POST",
            f"{generation.ARCHITECT_ROOT}/runs/{run_id}/clarifications/"
            f"{clarification['clarificationId']}/answers",
            {"answers": answers},
        )
        if status not in (200, 202):
            raise BusinessEvalError(f"提交澄清失败: HTTP {status} {body}")
        replay, terminal = generation.read_sse(
            f"{generation.ARCHITECT_ROOT}/runs/{run_id}/events/stream",
            after_seq=last_seq,
            timeout=SSE_TIMEOUT,
        )
        all_events.extend(replay)
        last_seq = max(
            [last_seq] + [int(event.get("seq") or 0) for event in replay + [terminal]]
        )
    raise BusinessEvalError(f"超过 {MAX_ROUNDS} 轮仍未生成契约")


# ------------------------------------------------------------
# 断言
# ------------------------------------------------------------
def singular(name: str) -> str:
    value = name.casefold()
    if value.endswith("ies"):
        return value[:-3] + "y"
    if value.endswith("ses"):
        return value[:-2]
    if value.endswith("s"):
        return value[:-1]
    return value


def table_matches(actual: str, expected: str) -> bool:
    return actual.casefold() == expected.casefold() or singular(actual) == singular(
        expected
    )


def actual_routes(contract: dict[str, Any]) -> dict[str, set[str]]:
    routes: dict[str, set[str]] = {}
    for route in contract.get("apiRoutes") or contract.get("api_routes") or []:
        if not isinstance(route, dict):
            continue
        method = str(route.get("method") or "").upper()
        path = str(route.get("path") or "")
        if method and path:
            routes.setdefault(acceptance.normalize_route(path), set()).add(method)
    return routes


def route_present(expectation: RouteExpectation, routes: dict[str, set[str]]) -> bool:
    methods = {expectation.method}
    # REST 更新语义允许 PUT / PATCH 二选一，不把框架习惯当成业务错误。
    if expectation.method in {"PUT", "PATCH"}:
        methods = {"PUT", "PATCH"}
    return bool(methods & routes.get(expectation.normalized, set()))


def contract_checks(case: CaseSpec, contract: dict[str, Any]) -> list[Check]:
    checks: list[Check] = []
    shape = str(contract.get("appShape") or contract.get("app_shape") or "")
    checks.append(
        Check(
            "应用形态",
            shape == case.expected_app_shape,
            f"期望 {case.expected_app_shape}，实际 {shape or '(空)'}",
        )
    )

    routes = actual_routes(contract)
    if case.expected_api_routes:
        for expectation in case.expected_api_routes:
            checks.append(
                Check(
                    f"路由 {expectation.method} {expectation.path}",
                    route_present(expectation, routes),
                    "契约 apiRoutes 中未找到",
                )
            )
    else:
        checks.append(
            Check(
                "无后端路由",
                not routes,
                f"轻量应用不应生成 API，实际 {sorted(routes)}",
            )
        )

    ddl = str(contract.get("databaseDdl") or contract.get("database_ddl") or "")
    schema = acceptance.parse_ddl_schema(ddl)
    for expected_table in case.expected_tables:
        matched = next(
            (table for table in schema if table_matches(table, expected_table)),
            None,
        )
        checks.append(
            Check(
                f"数据表 {expected_table}",
                matched is not None,
                f"DDL 中只有 {sorted(schema)}",
            )
        )
    if not case.expected_tables:
        checks.append(
            Check(
                "无数据库表",
                not schema,
                f"轻量应用不应生成 DDL，实际 {sorted(schema)}",
            )
        )

    for expected_table, expected_columns in case.expected_columns.items():
        matched = next(
            (table for table in schema if table_matches(table, expected_table)),
            None,
        )
        actual_columns = [column.casefold() for column in schema.get(matched or "", [])]
        for spec in expected_columns:
            alternatives = [item.casefold() for item in spec.split("|") if item]
            present = any(
                alternative == column or alternative in column or column in alternative
                for alternative in alternatives
                for column in actual_columns
            )
            checks.append(
                Check(
                    f"字段 {expected_table}.{spec}",
                    present,
                    f"实际字段 {schema.get(matched or '', [])}",
                )
            )
    return checks


def artifact_checks(
    case: CaseSpec,
    contract: dict[str, Any],
    run_id: str,
    sandbox_id: str,
    work_dir: Path,
) -> list[Check]:
    checks: list[Check] = []
    archive_path = work_dir / "project-export.tar.gz"
    package_dir = work_dir / "package"
    acceptance.download_export(run_id, sandbox_id, archive_path)
    acceptance.extract_archive(archive_path, package_dir)
    package_root = acceptance.resolve_package_root(package_dir)

    missing = acceptance.check_required_files(package_root)
    checks.append(
        Check(
            "部署文件",
            not missing,
            f"缺少 {missing}",
            category="artifact",
        )
    )
    backend_count, route_issues = acceptance.check_route_consistency(package_root)
    checks.append(
        Check(
            "前后端路由一致",
            not route_issues,
            "; ".join(route_issues) or f"后端 {backend_count} 条路由全部命中",
            category="artifact",
        )
    )
    semantic_issues = acceptance.check_business_semantics(
        package_root,
        contract,
        require_upload=case.requires_upload or None,
        require_success_state=case.requires_success_state or None,
    )
    checks.append(
        Check(
            "业务语义落码",
            not semantic_issues,
            "; ".join(semantic_issues) or "契约能力均已在生成代码中体现",
            category="artifact",
        )
    )
    return checks


# ------------------------------------------------------------
# 用例执行
# ------------------------------------------------------------
def run_case(case: CaseSpec, provider: str, fullstack_enabled: bool) -> CaseResult:
    started = time.perf_counter()
    result = CaseResult(case_id=case.case_id, name=case.name)
    sandbox_id = ""
    work_dir = Path(tempfile.mkdtemp(prefix=f"business-eval-{case.case_id}-"))
    try:
        run_id, contract, contract_seq, tokens, cost = drive_to_contract(case, provider)
        result.tokens = tokens
        result.cost = cost
        result.app_shape = str(
            contract.get("appShape") or contract.get("app_shape") or ""
        )
        result.routes = sorted(
            f"{method} {path}"
            for path, methods in actual_routes(contract).items()
            for method in sorted(methods)
        )
        result.tables = sorted(
            acceptance.parse_ddl_schema(
                str(contract.get("databaseDdl") or contract.get("database_ddl") or "")
            )
        )
        result.checks.extend(contract_checks(case, contract))

        if case.run_fullstack and fullstack_enabled:
            if result.app_shape != "full_stack_system":
                result.checks.append(
                    Check(
                        "全栈产物",
                        False,
                        "契约未判定为 full_stack_system，跳过生成",
                        category="artifact",
                    )
                )
            else:
                confirmed = generation.confirm_contract(run_id, contract, contract_seq)
                _, terminal = generation.start_fullstack(f"fsk_{run_id}", confirmed)
                if terminal.get("type") != "fullstack.completed":
                    raise BusinessEvalError(
                        f"全栈生成失败: {terminal.get('type')} -> "
                        f"{json.dumps(terminal.get('payload'), ensure_ascii=False)[:800]}"
                    )
                payload = terminal.get("payload") or {}
                if payload.get("status") != "SUCCEEDED":
                    raise BusinessEvalError(f"全栈终态不是 SUCCEEDED: {payload}")
                sandbox_id = str(payload.get("sandboxId") or "")
                if not sandbox_id:
                    raise BusinessEvalError(f"全栈终态缺少 sandboxId: {payload}")
                result.checks.extend(
                    artifact_checks(case, contract, run_id, sandbox_id, work_dir)
                )
    except Exception as exc:  # noqa: BLE001 - 单用例失败不能中断整套评测
        result.error = str(exc)
        result.checks.append(Check("执行", False, str(exc), category="contract"))
    finally:
        acceptance.cleanup_sandbox(sandbox_id)
        shutil.rmtree(work_dir, ignore_errors=True)
        result.duration_s = time.perf_counter() - started
    return result


# ------------------------------------------------------------
# 报告
# ------------------------------------------------------------
def print_case(result: CaseResult, verbose: bool) -> None:
    mark = "PASS" if result.passed else "FAIL"
    print(f"\n[{result.case_id}] {result.name} · {mark}")
    print(
        f"  形态={result.app_shape or '-'} 路由={len(result.routes)} "
        f"表={','.join(result.tables) or '-'} tokens={result.tokens} "
        f"cost=¥{result.cost:.6f} {result.duration_s:.1f}s"
    )
    for check in result.checks:
        if check.passed and not verbose:
            continue
        icon = "✓" if check.passed else "✗"
        suffix = f" — {check.detail}" if check.detail and not check.passed else ""
        print(f"  {icon} [{check.category}] {check.name}{suffix}")


def write_report(
    *,
    path: Path,
    suite: Path,
    provider: str,
    results: list[CaseResult],
    total_tokens: int,
    total_cost: float,
    elapsed: float,
) -> None:
    passed = sum(1 for result in results if result.passed)
    contract_passed = sum(1 for result in results if result.contract_passed)
    artifact_cases = [result for result in results if result.artifact_checks]
    artifact_passed = sum(1 for result in artifact_cases if result.artifact_passed)

    lines = [
        "# 业务 Eval 报告",
        "",
        f"- 时间：{datetime.now(timezone.utc).isoformat()}",
        f"- 评测集：`{suite}`",
        f"- 模型：`{provider}`",
        f"- 服务：`{generation.BASE_URL}`",
        f"- 耗时：{elapsed:.1f}s",
        f"- Tokens：{total_tokens}（¥{total_cost:.6f}）",
        "",
        "## 汇总",
        "",
        "| 指标 | 结果 |",
        "|---|---|",
        f"| 业务通过率 | {passed}/{len(results)} = {passed / len(results):.1%} |",
        f"| 契约理解准确率 | {contract_passed}/{len(results)} = "
        f"{contract_passed / len(results):.1%} |",
    ]
    if artifact_cases:
        lines.append(
            f"| 产物语义通过率 | {artifact_passed}/{len(artifact_cases)} = "
            f"{artifact_passed / len(artifact_cases):.1%} |"
        )
    lines.extend(
        [
            "",
            "## 用例明细",
            "",
            "| 用例 | 形态 | 契约 | 产物 | 总耗时 | Tokens |",
            "|---|---|---|---|---|---|",
        ]
    )
    for result in results:
        artifact = (
            "通过"
            if result.artifact_passed
            else ("失败" if result.artifact_checks else "不适用")
        )
        lines.append(
            f"| {result.case_id} {result.name} | {result.app_shape or '-'} | "
            f"{'通过' if result.contract_passed else '失败'} | {artifact} | "
            f"{result.duration_s:.1f}s | {result.tokens} |"
        )

    lines.extend(["", "## 失败明细", ""])
    failures = 0
    for result in results:
        failing = [check for check in result.checks if not check.passed]
        if not failing:
            continue
        failures += 1
        lines.append(f"### {result.case_id} {result.name}")
        for check in failing:
            lines.append(f"- **[{check.category}] {check.name}**：{check.detail}")
        lines.append("")
    if failures == 0:
        lines.append("本套用例全部通过。")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="业务语义 Eval Suite")
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--filter", default=None, help="按 id / 名称 / tag 过滤")
    parser.add_argument("--llm-provider", default=DEFAULT_PROVIDER)
    parser.add_argument(
        "--no-fullstack",
        action="store_true",
        help="只评测契约，不生成导出包（更快、更省 Token）",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    cases = filter_cases(load_cases(args.suite), args.filter)
    if not cases:
        print("没有匹配的用例", file=sys.stderr)
        return 2

    print(
        f"🔬 业务 Eval · provider={args.llm_provider} · "
        f"{len(cases)} cases · fullstack={not args.no_fullstack}"
    )
    started = time.perf_counter()
    results: list[CaseResult] = []
    for case in cases:
        result = run_case(case, args.llm_provider, not args.no_fullstack)
        results.append(result)
        print_case(result, args.verbose)

    elapsed = time.perf_counter() - started
    total_tokens = sum(result.tokens for result in results)
    total_cost = sum(result.cost for result in results)
    passed = sum(1 for result in results if result.passed)
    contract_passed = sum(1 for result in results if result.contract_passed)
    artifact_cases = [result for result in results if result.artifact_checks]
    artifact_passed = sum(1 for result in artifact_cases if result.artifact_passed)

    print("\n" + "=" * 60)
    print(f"业务通过率: {passed}/{len(results)} = {passed / len(results):.1%}")
    print(
        f"AI 契约理解准确率: {contract_passed}/{len(results)} = "
        f"{contract_passed / len(results):.1%}"
    )
    if artifact_cases:
        print(
            f"产物语义通过率: {artifact_passed}/{len(artifact_cases)} = "
            f"{artifact_passed / len(artifact_cases):.1%}"
        )
    print(f"Tokens: {total_tokens} | Cost: ¥{total_cost:.6f} | 耗时: {elapsed:.1f}s")

    write_report(
        path=args.report,
        suite=args.suite,
        provider=args.llm_provider,
        results=results,
        total_tokens=total_tokens,
        total_cost=total_cost,
        elapsed=elapsed,
    )
    print(f"报告: {args.report}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BusinessEvalError as exc:
        print(f"配置或执行错误: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        raise SystemExit(130)
