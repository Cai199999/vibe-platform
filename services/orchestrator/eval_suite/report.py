"""评测报告输出：控制台、JSON、Markdown、JUnit XML。

报告是评测系统的产品界面。失败信息必须包含“哪条用例、哪个断言、
期望什么、实际什么”，而不是只给一个红色 FAIL。
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable

from eval_suite.models import CaseResult, CheckResult, SuiteResult

REPORT_FORMATS = ("console", "json", "markdown", "junit")


def render_console(result: SuiteResult, *, quiet: bool = False) -> str:
    """渲染终端友好的逐用例结果。"""
    summary = result.summary
    lines = [
        f"Eval Suite: {result.suite.name} v{result.suite.version}",
        f"Suite     : {result.suite.path}",
        f"Cases     : {summary.total}",
        "",
    ]
    for case in result.cases:
        status = "PASS" if case.passed else "FAIL"
        decision = _decision_text(case)
        lines.append(
            f"[{status}] {case.case_id}  {case.name}  "
            f"({case.duration_ms:.1f} ms)  {decision}"
        )
        if not case.passed and not quiet:
            for check in case.failures:
                lines.append(f"       - {_check_text(check)}")
            if case.error:
                lines.append(f"       - error: {case.error}")

    lines.extend(
        [
            "",
            f"Summary   : {summary.passed}/{summary.total} passed "
            f"({summary.pass_rate * 100:.1f}%) in {result.duration_ms:.1f} ms",
        ]
    )
    return "\n".join(lines)


def write_reports(
    result: SuiteResult,
    report_dir: str | Path,
    formats: Iterable[str],
) -> list[Path]:
    """按需写出报告文件；返回实际写出的路径。"""
    requested = set(formats)
    unknown = requested - set(REPORT_FORMATS)
    if unknown:
        raise ValueError(f"未知报告格式: {', '.join(sorted(unknown))}")

    output_dir = Path(report_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    if "json" in requested:
        path = output_dir / "eval-report.json"
        path.write_text(
            json.dumps(_result_to_dict(result), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written.append(path)

    if "markdown" in requested:
        path = output_dir / "eval-report.md"
        path.write_text(_render_markdown(result), encoding="utf-8")
        written.append(path)

    if "junit" in requested:
        path = output_dir / "eval-junit.xml"
        path.write_text(_render_junit(result), encoding="utf-8")
        written.append(path)

    return written


# ------------------------------------------------------------
# JSON
# ------------------------------------------------------------
def _result_to_dict(result: SuiteResult) -> dict[str, Any]:
    summary = result.summary
    return {
        "suite": {
            "name": result.suite.name,
            "version": result.suite.version,
            "path": str(result.suite.path),
            "description": result.suite.description,
        },
        "generatedAt": result.generated_at,
        "summary": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "passRate": round(summary.pass_rate, 4),
            "durationMs": result.duration_ms,
        },
        "cases": [_case_to_dict(case) for case in result.cases],
    }


def _case_to_dict(case: CaseResult) -> dict[str, Any]:
    return {
        "id": case.case_id,
        "name": case.name,
        "tags": list(case.tags),
        "passed": case.passed,
        "durationMs": case.duration_ms,
        "decision": {
            "handoff": case.handoff,
            "target": case.target,
            "taskType": case.task_type,
            "dispatchable": case.dispatchable,
            "missingInputs": case.missing_inputs,
            "handoffs": case.handoffs,
        },
        "checks": [_check_to_dict(check) for check in case.checks],
        "error": case.error,
    }


def _check_to_dict(check: CheckResult) -> dict[str, Any]:
    return {
        "name": check.name,
        "passed": check.passed,
        "expected": check.expected,
        "actual": check.actual,
        "message": check.message,
    }


# ------------------------------------------------------------
# Markdown
# ------------------------------------------------------------
def _render_markdown(result: SuiteResult) -> str:
    summary = result.summary
    lines = [
        f"# Eval Suite 报告 · {result.suite.name}",
        "",
        f"- 生成时间：`{result.generated_at}`",
        f"- 评测集：`{result.suite.path}`",
        f"- 结果：**{summary.passed}/{summary.total} 通过**"
        f"（{summary.pass_rate * 100:.1f}%），总耗时 {result.duration_ms:.1f} ms",
        "",
        "| 用例 | 名称 | 结果 | Handoff | Agent | 耗时 |",
        "|---|---|---|---|---|---|",
    ]
    for case in result.cases:
        status = "✅ PASS" if case.passed else "❌ FAIL"
        lines.append(
            f"| `{case.case_id}` | {_md_escape(case.name)} | {status} | "
            f"`{case.handoff or '-'}` | `{case.target or '-'}` | "
            f"{case.duration_ms:.1f} ms |"
        )

    failures = [case for case in result.cases if not case.passed]
    if failures:
        lines.extend(["", "## 失败详情", ""])
        for case in failures:
            lines.append(f"### `{case.case_id}` {case.name}")
            lines.append("")
            for check in case.failures:
                lines.append(f"- `{check.name}`：{_check_text(check)}")
            if case.error:
                lines.append(f"- `execution`：{case.error}")
            lines.append("")
    else:
        lines.extend(["", "全部断言通过。", ""])
    return "\n".join(lines)


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|")


# ------------------------------------------------------------
# JUnit XML
# ------------------------------------------------------------
def _render_junit(result: SuiteResult) -> str:
    summary = result.summary
    root = ET.Element(
        "testsuite",
        {
            "name": result.suite.name,
            "tests": str(summary.total),
            "failures": str(summary.failed),
            "errors": "0",
            "time": f"{result.duration_ms / 1000:.3f}",
            "timestamp": result.generated_at,
        },
    )
    for case in result.cases:
        testcase = ET.SubElement(
            root,
            "testcase",
            {
                "classname": "eval_suite",
                "name": f"{case.case_id} {case.name}",
                "time": f"{case.duration_ms / 1000:.3f}",
            },
        )
        if not case.passed:
            failure = ET.SubElement(
                testcase,
                "failure",
                {
                    "message": "断言失败",
                    "type": "EvalAssertionError",
                },
            )
            details = "\n".join(_check_text(check) for check in case.failures)
            if case.error:
                details = f"{details}\nerror: {case.error}".strip()
            failure.text = details

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}\n'


# ------------------------------------------------------------
# 文本工具
# ------------------------------------------------------------
def _decision_text(case: CaseResult) -> str:
    parts = [case.handoff or "-"]
    if case.target:
        parts.append(case.target)
    if case.dispatchable is not None:
        parts.append("dispatchable" if case.dispatchable else "blocked")
    return " · ".join(parts)


def _check_text(check: CheckResult) -> str:
    if check.passed:
        return f"{check.name}: ok"
    expected = _stringify(check.expected)
    actual = _stringify(check.actual)
    return f"{check.name}: expected {expected}, got {actual}"


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)
