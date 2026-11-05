"""Eval Suite CLI。

命令：

    python -m eval_suite run        # 执行并生成报告
    python -m eval_suite list       # 查看用例清单
    python -m eval_suite validate   # 只校验 YAML 契约

``validate`` / ``list`` 只依赖 PyYAML 与 pydantic，不需要 LangGraph；
``run`` 才加载 AI Core 的 Supervisor 与 MockAgentLLM。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from eval_suite.loader import EvalSuiteError, load_suite
from eval_suite.models import EvalSuite

ORCHESTRATOR_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = (
    ORCHESTRATOR_ROOT.parents[1]
    if len(ORCHESTRATOR_ROOT.parents) > 1
    else ORCHESTRATOR_ROOT
)
_DEFAULT_REPORT_DIR = _REPO_ROOT / "reports" / "eval"


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list:
        args_list = ["run"]

    parser = _build_parser()
    args = parser.parse_args(args_list)

    suite_path, fixtures_root = _resolve_suite_paths(args)
    try:
        suite = load_suite(suite_path, fixtures_root=fixtures_root)
    except EvalSuiteError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2

    if args.command == "validate":
        return _validate(suite)
    if args.command == "list":
        return _list(suite, args)

    selected = _select_cases(suite, args)
    if not selected:
        print("CONFIG ERROR: 过滤后没有可执行用例", file=sys.stderr)
        return 2

    return _run(selected, args)


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--suite",
        help="评测集 YAML 路径；默认读取 EVAL_SUITE_PATH 或 "
        "config/eval_suite/core_cases.yaml",
    )
    common.add_argument(
        "--fixtures",
        help="fixture 根目录；默认为评测集同级的 fixtures/",
    )

    parser = argparse.ArgumentParser(
        prog="python -m eval_suite",
        description="vibe-platform 离线自动化评测套件",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run",
        parents=[common],
        help="执行评测并生成报告",
    )
    run_parser.add_argument(
        "--report-dir",
        help="报告输出目录；默认 EVAL_REPORT_DIR 或 reports/eval",
    )
    run_parser.add_argument(
        "--format",
        default="json,markdown,junit",
        help="报告文件格式，逗号分隔：json,markdown,junit,console；默认三类文件",
    )
    run_parser.add_argument(
        "--custom-agents-dir",
        help="CustomAgent Manifest 目录；默认 SUPERVISOR_CUSTOM_AGENTS_DIR "
        "或 services/orchestrator/config/custom_agents",
    )
    run_parser.add_argument(
        "--filter",
        action="append",
        default=[],
        help="按 id / 名称 / tag 的子串过滤，可重复",
    )
    run_parser.add_argument(
        "--tag",
        action="append",
        default=[],
        help="只执行同时包含这些 tag 的用例，可重复",
    )
    run_parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="首条失败即停止",
    )
    run_parser.add_argument(
        "--min-pass-rate",
        type=float,
        default=1.0,
        help="最低通过率阈值（0~1），低于阈值返回非 0；默认 1.0",
    )
    run_parser.add_argument(
        "--quiet",
        action="store_true",
        help="控制台不展开失败断言明细",
    )

    list_parser = subparsers.add_parser(
        "list",
        parents=[common],
        help="列出评测集用例",
    )
    list_parser.add_argument(
        "--filter",
        action="append",
        default=[],
        help="按 id / 名称 / tag 的子串过滤，可重复",
    )
    list_parser.add_argument(
        "--tag",
        action="append",
        default=[],
        help="只显示同时包含这些 tag 的用例，可重复",
    )

    subparsers.add_parser(
        "validate",
        parents=[common],
        help="只校验 YAML 契约，不执行",
    )
    return parser


def _resolve_suite_paths(args: argparse.Namespace) -> tuple[Path, Path | None]:
    suite = _resolve_suite_path(getattr(args, "suite", None))
    fixtures = None
    if getattr(args, "fixtures", None):
        fixtures = Path(args.fixtures).expanduser().resolve()
    return suite, fixtures


def _resolve_suite_path(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    env_path = os.environ.get("EVAL_SUITE_PATH")
    if env_path:
        return Path(env_path).expanduser().resolve()
    candidates = [
        Path.cwd() / "config" / "eval_suite" / "core_cases.yaml",
        _REPO_ROOT / "config" / "eval_suite" / "core_cases.yaml",
        Path("/config/eval_suite/core_cases.yaml"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _select_cases(suite: EvalSuite, args: argparse.Namespace) -> EvalSuite:
    filters = [item.casefold() for item in getattr(args, "filter", [])]
    tags = {item.casefold() for item in getattr(args, "tag", [])}

    cases = []
    for case in suite.cases:
        haystack = " ".join([case.case_id, case.name, *case.tags]).casefold()
        if filters and not any(item in haystack for item in filters):
            continue
        if tags and not tags.issubset({tag.casefold() for tag in case.tags}):
            continue
        cases.append(case)
    return EvalSuite(
        name=suite.name,
        version=suite.version,
        description=suite.description,
        cases=tuple(cases),
        path=suite.path,
        fixtures_root=suite.fixtures_root,
    )


def _validate(suite: EvalSuite) -> int:
    code_cases = sum(1 for case in suite.cases if case.needs_code_generation)
    print(f"OK: {suite.path}")
    print(f"  用例数        : {len(suite.cases)}")
    print(f"  代码生成用例  : {code_cases}")
    print(f"  fixture 根目录: {suite.fixtures_root}")
    return 0


def _list(suite: EvalSuite, args: argparse.Namespace) -> int:
    selected = _select_cases(suite, args)
    print(f"{'ID':<12} {'HANDOFF':<18} {'AGENT':<22} NAME")
    for case in selected.cases:
        print(
            f"{case.case_id:<12} "
            f"{case.assertions.expected_handoff:<18} "
            f"{(case.assertions.expected_agent or '-'):<22} "
            f"{case.name}"
        )
    print(f"\n共 {len(selected.cases)} 条用例")
    return 0


def _run(suite: EvalSuite, args: argparse.Namespace) -> int:
    if not 0.0 <= args.min_pass_rate <= 1.0:
        print("CONFIG ERROR: --min-pass-rate 必须在 0~1 之间", file=sys.stderr)
        return 2

    try:
        formats = _parse_formats(args.format)
    except EvalSuiteError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    report_dir = (
        Path(args.report_dir).expanduser().resolve()
        if args.report_dir
        else Path(os.environ.get("EVAL_REPORT_DIR", _DEFAULT_REPORT_DIR))
    )

    # 延迟导入：list / validate 不需要 LangGraph 与模型依赖。
    from eval_suite.engine import EvalEngine
    from eval_suite.report import render_console, write_reports

    engine = EvalEngine(custom_agents_dir=args.custom_agents_dir)
    result = engine.run_suite(suite, fail_fast=args.fail_fast)

    print(render_console(result, quiet=args.quiet), flush=True)
    written = write_reports(result, report_dir, formats)
    if written:
        print("\nReports:", flush=True)
        for path in written:
            print(f"  - {path}", flush=True)

    summary = result.summary
    if summary.pass_rate < args.min_pass_rate:
        print(
            f"\nFAIL: pass rate {summary.pass_rate:.1%} "
            f"< threshold {args.min_pass_rate:.1%}",
            file=sys.stderr,
        )
        return 1
    return 0 if summary.failed == 0 else 1


def _parse_formats(value: str) -> list[str]:
    allowed = {"json", "markdown", "junit"}
    formats: list[str] = []
    for item in value.split(","):
        item = item.strip().lower()
        if not item or item == "console":
            continue
        if item not in allowed:
            raise EvalSuiteError(
                f"未知报告格式: {item}；允许: {', '.join(sorted(allowed))}"
            )
        if item not in formats:
            formats.append(item)
    return formats


if __name__ == "__main__":
    raise SystemExit(main())
