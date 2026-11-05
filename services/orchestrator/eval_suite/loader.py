"""YAML 评测集加载与校验。

加载阶段就把结构错误、路径穿越、重复 ID、未知字段全部拦下，避免跑批
到一半才发现配置问题。这里的校验是“评测集契约”的第一道防线。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from ai_core.agent.schemas import AnnotationEnvelope

from eval_suite.models import (
    ALLOWED_HANDOFFS,
    EvalAssertions,
    EvalCase,
    EvalInput,
    EvalSuite,
)


class EvalSuiteError(ValueError):
    """评测集格式或语义错误。"""


_TOP_LEVEL_KEYS = {"name", "version", "description", "cases"}
_CASE_KEYS = {"id", "name", "tags", "input", "assertions"}
_INPUT_KEYS = {
    "target_agent",
    "text",
    "annotation",
    "annotations",
    "fixture",
    "project_id",
}
_ASSERTION_KEYS = {
    "expected_handoff",
    "expected_agent",
    "expected_task_type",
    "expected_dispatchable",
    "expected_handoffs",
    "expected_missing_inputs",
    "expected_code_contains",
    "expected_code_not_contains",
    "max_duration_ms",
}


def load_suite(
    path: str | Path,
    fixtures_root: str | Path | None = None,
) -> EvalSuite:
    """加载 YAML 评测集；失败时抛出带上下文的 ``EvalSuiteError``。"""
    suite_path = Path(path).expanduser().resolve()
    if not suite_path.is_file():
        raise EvalSuiteError(f"评测集文件不存在: {suite_path}")

    try:
        raw = yaml.safe_load(suite_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise EvalSuiteError(f"YAML 解析失败: {suite_path}: {exc}") from exc

    suite_name = suite_path.stem
    version = "1"
    description = ""
    if isinstance(raw, list):
        raw_cases = raw
    elif isinstance(raw, Mapping):
        _reject_unknown_keys(raw, _TOP_LEVEL_KEYS, context="评测集根节点")
        raw_cases = raw.get("cases")
        suite_name = _optional_str(raw.get("name"), "评测集 name") or suite_name
        version = _optional_str(raw.get("version"), "评测集 version") or "1"
        description = _optional_str(raw.get("description"), "评测集 description") or ""
    else:
        raise EvalSuiteError("评测集根节点必须是用例列表或包含 cases 的映射")

    if not isinstance(raw_cases, list) or not raw_cases:
        raise EvalSuiteError("评测集必须包含至少 1 条用例")

    resolved_fixtures = (
        Path(fixtures_root).expanduser().resolve()
        if fixtures_root is not None
        else suite_path.parent / "fixtures"
    )

    cases: list[EvalCase] = []
    seen_ids: set[str] = set()
    for index, raw_case in enumerate(raw_cases, start=1):
        case = _parse_case(
            raw_case,
            index=index,
            fixtures_root=resolved_fixtures,
            source_path=suite_path,
        )
        if case.case_id in seen_ids:
            raise EvalSuiteError(f"用例 ID 重复: {case.case_id}")
        seen_ids.add(case.case_id)
        cases.append(case)

    return EvalSuite(
        name=suite_name,
        version=version,
        description=description,
        cases=tuple(cases),
        path=suite_path,
        fixtures_root=resolved_fixtures,
    )


def _parse_case(
    raw: Any,
    *,
    index: int,
    fixtures_root: Path,
    source_path: Path,
) -> EvalCase:
    context = f"第 {index} 条用例"
    if not isinstance(raw, Mapping):
        raise EvalSuiteError(f"{context} 必须是映射")

    _reject_unknown_keys(raw, _CASE_KEYS, context=context)

    case_id = _required_str(raw.get("id"), f"{context} id")
    if not all(ch.isalnum() or ch in {"_", "-"} for ch in case_id):
        raise EvalSuiteError(
            f"{context} id 只允许字母、数字、下划线与连字符: {case_id}"
        )
    name = _required_str(raw.get("name"), f"{context} name")
    tags = _string_list(raw.get("tags", []), f"{context} tags")

    raw_input = raw.get("input")
    if not isinstance(raw_input, Mapping):
        raise EvalSuiteError(f"{context} input 必须是映射")
    _reject_unknown_keys(raw_input, _INPUT_KEYS, context=f"{context} input")

    raw_assertions = raw.get("assertions")
    if not isinstance(raw_assertions, Mapping):
        raise EvalSuiteError(f"{context} assertions 必须是映射")
    _reject_unknown_keys(
        raw_assertions,
        _ASSERTION_KEYS,
        context=f"{context} assertions",
    )

    eval_input = _parse_input(raw_input, context=f"{context} input")
    assertions = _parse_assertions(raw_assertions, context=f"{context} assertions")

    if eval_input.fixture:
        _resolve_fixture_dir(fixtures_root, eval_input.fixture)
    if (
        assertions.expected_code_contains or assertions.expected_code_not_contains
    ) and (not eval_input.fixture):
        raise EvalSuiteError(
            f"{context} 声明了代码断言，但 input.fixture 为空；"
            "请提供 fixture 目录，避免对空代码做无意义断言"
        )

    return EvalCase(
        case_id=case_id,
        name=name,
        input=eval_input,
        assertions=assertions,
        tags=tags,
        source_path=source_path,
    )


def _parse_input(raw: Mapping[str, Any], *, context: str) -> EvalInput:
    target_agent = _optional_str(raw.get("target_agent"), f"{context} target_agent")
    text = _optional_str(raw.get("text"), f"{context} text") or ""
    fixture = _optional_str(raw.get("fixture"), f"{context} fixture")
    project_id = _optional_str(raw.get("project_id"), f"{context} project_id")

    has_single = "annotation" in raw
    has_many = "annotations" in raw
    if has_single and has_many:
        raise EvalSuiteError(f"{context} 不能同时声明 annotation 与 annotations")

    raw_annotations: list[Any]
    if has_single:
        raw_annotations = [raw["annotation"]]
    elif has_many:
        value = raw["annotations"]
        if not isinstance(value, list):
            raise EvalSuiteError(f"{context} annotations 必须是列表")
        raw_annotations = value
    else:
        raw_annotations = []

    if len(raw_annotations) > 3:
        raise EvalSuiteError(f"{context} 一次最多 3 个标注")

    annotations: list[dict[str, Any]] = []
    for annotation_index, raw_annotation in enumerate(raw_annotations, start=1):
        if not isinstance(raw_annotation, Mapping):
            raise EvalSuiteError(f"{context} 第 {annotation_index} 个标注必须是映射")
        try:
            envelope = AnnotationEnvelope.model_validate(dict(raw_annotation))
        except Exception as exc:  # noqa: BLE001 - 统一转为配置错误
            raise EvalSuiteError(
                f"{context} 第 {annotation_index} 个标注不符合 AnnotationEnvelope: {exc}"
            ) from exc
        dumped = envelope.model_dump(by_alias=True, exclude_none=True)
        if len(raw_annotations) == 1 and text:
            payload = dumped.setdefault("payload", {})
            if not payload.get("text"):
                payload["text"] = text
        annotations.append(dumped)

    return EvalInput(
        target_agent=target_agent or "auto",
        text=text,
        annotations=tuple(annotations),
        fixture=fixture,
        project_id=project_id,
    )


def _parse_assertions(raw: Mapping[str, Any], *, context: str) -> EvalAssertions:
    handoff = _required_str(
        raw.get("expected_handoff"),
        f"{context} expected_handoff",
    )
    if handoff not in ALLOWED_HANDOFFS:
        allowed = ", ".join(sorted(ALLOWED_HANDOFFS))
        raise EvalSuiteError(
            f"{context} expected_handoff 非法: {handoff}；允许值: {allowed}"
        )

    dispatchable = raw.get("expected_dispatchable")
    if dispatchable is not None and not isinstance(dispatchable, bool):
        raise EvalSuiteError(f"{context} expected_dispatchable 必须是布尔值")

    max_duration = raw.get("max_duration_ms")
    if max_duration is not None:
        if isinstance(max_duration, bool) or not isinstance(max_duration, int):
            raise EvalSuiteError(f"{context} max_duration_ms 必须是整数")
        if max_duration <= 0:
            raise EvalSuiteError(f"{context} max_duration_ms 必须大于 0")

    expected_handoffs: tuple[str, ...] | None = None
    if "expected_handoffs" in raw:
        expected_handoffs = _string_list(
            raw["expected_handoffs"],
            f"{context} expected_handoffs",
        )

    expected_missing: tuple[str, ...] | None = None
    if "expected_missing_inputs" in raw:
        expected_missing = _string_list(
            raw["expected_missing_inputs"],
            f"{context} expected_missing_inputs",
        )

    return EvalAssertions(
        expected_handoff=handoff,
        expected_agent=_optional_str(
            raw.get("expected_agent"),
            f"{context} expected_agent",
        ),
        expected_task_type=_optional_str(
            raw.get("expected_task_type"),
            f"{context} expected_task_type",
        ),
        expected_dispatchable=dispatchable,
        expected_handoffs=expected_handoffs,
        expected_missing_inputs=expected_missing,
        expected_code_contains=_string_list(
            raw.get("expected_code_contains", []),
            f"{context} expected_code_contains",
        ),
        expected_code_not_contains=_string_list(
            raw.get("expected_code_not_contains", []),
            f"{context} expected_code_not_contains",
        ),
        max_duration_ms=max_duration,
    )


def _resolve_fixture_dir(fixtures_root: Path, fixture: str) -> Path:
    pure = PurePosixPath(fixture)
    if pure.is_absolute() or ".." in pure.parts:
        raise EvalSuiteError(f"fixture 路径不允许绝对路径或目录穿越: {fixture}")
    candidate = (fixtures_root / fixture).resolve()
    if not candidate.is_dir():
        raise EvalSuiteError(f"fixture 目录不存在: {candidate}")
    if not candidate.is_relative_to(fixtures_root.resolve()):
        raise EvalSuiteError(f"fixture 路径越界: {fixture}")
    return candidate


def load_fixture_files(fixtures_root: Path, fixture: str) -> dict[str, str]:
    """读取 fixture 目录下的所有文本文件，路径相对 fixture 根目录。"""
    fixture_dir = _resolve_fixture_dir(fixtures_root, fixture)
    files: dict[str, str] = {}
    for path in sorted(fixture_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(fixture_dir).as_posix()
        try:
            files[relative] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise EvalSuiteError(f"fixture 文件必须是 UTF-8 文本: {path}") from exc
    if not files:
        raise EvalSuiteError(f"fixture 目录为空: {fixture_dir}")
    return files


def _reject_unknown_keys(
    raw: Mapping[str, Any],
    allowed: set[str],
    *,
    context: str,
) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise EvalSuiteError(f"{context} 存在未知字段: {', '.join(unknown)}")


def _required_str(value: Any, context: str) -> str:
    text = _optional_str(value, context)
    if not text:
        raise EvalSuiteError(f"{context} 不能为空")
    return text


def _optional_str(value: Any, context: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise EvalSuiteError(f"{context} 必须是字符串")
    return value.strip() or None


def _string_list(value: Any, context: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise EvalSuiteError(f"{context} 必须是列表")
    items: list[str] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, str) or not item.strip():
            raise EvalSuiteError(f"{context} 第 {index} 项必须是非空字符串")
        items.append(item.strip())
    return tuple(items)
