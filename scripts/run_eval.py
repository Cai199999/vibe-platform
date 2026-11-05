#!/usr/bin/env python3
"""基于真实沙盒的 Eval Suite 评测引擎。

与 ``services/orchestrator/eval_suite`` 的离线引擎不同，本脚本不导入
AI Core 代码，而是通过 HTTP 调用正在运行的服务：

1. 读取 ``config/eval_suite/core_cases.yaml``；
2. 每条用例先调用 Supervisor ``/route``，断言 ``decision.handoff``；
3. 对声明了 ``expected_code_contains`` 的用例，调用 UI 子图真实执行，
   由 Docker 沙盒完成代码写入、构建与预览；
4. 通过沙盒导出接口读取真实项目源码 ``src/App.jsx``（失败时回退到接口
   返回的 ``generatedFiles`` / ``generatedPatch``），用正则做代码片段断言；
5. 输出终端报告并写入 ``docs/eval_report.md``，最后清理沙盒。

该脚本只依赖 Python 标准库 + PyYAML，可直接在宿主机运行::

    python scripts/run_eval.py
    python scripts/run_eval.py --filter case_001 --verbose
    EVAL_BASE_URL=http://127.0.0.1:8100 python scripts/run_eval.py
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - 环境缺失时给出可执行提示
    print(
        "缺少依赖 PyYAML。请执行: python3 -m pip install pyyaml",
        file=sys.stderr,
    )
    raise SystemExit(2)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUITE = REPO_ROOT / "config" / "eval_suite" / "core_cases.yaml"
DEFAULT_REPORT = REPO_ROOT / "docs" / "eval_report.md"
DEFAULT_BASE_URL = os.environ.get("EVAL_BASE_URL", "http://127.0.0.1:8080")
TENANT_ID = "tenant_eval"

ALLOWED_HANDOFFS = {
    "handoff_ui",
    "handoff_architect",
    "handoff_both",
    "handoff_custom",
    "rejected",
}


class EvalError(RuntimeError):
    """评测配置或执行过程中的可报告错误。"""


# ============================================================
# 评测集模型与加载
# ============================================================
@dataclass(frozen=True)
class Assertions:
    expected_handoff: str
    expected_agent: str | None = None
    expected_task_type: str | None = None
    expected_dispatchable: bool | None = None
    expected_handoffs: tuple[str, ...] | None = None
    expected_missing_inputs: tuple[str, ...] | None = None
    expected_code_contains: tuple[str, ...] = ()
    expected_code_not_contains: tuple[str, ...] = ()
    # 安全策略 / 路由原因断言：避免“未知意图 rejected”冒充“策略拦截”。
    expected_reason_contains: tuple[str, ...] = ()
    # Architect HITL：真实执行子图并等待暂停事件。
    expected_hitl: str | None = None
    expected_hitl_slot: str | None = None
    # both 门控：dispatch 后断言门控状态。
    expected_gate_status: str | None = None


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    name: str
    tags: tuple[str, ...]
    text: str
    annotations: tuple[dict[str, Any], ...]
    fixture_files: dict[str, str]
    fixture_name: str | None
    assertions: Assertions

    @property
    def needs_code_generation(self) -> bool:
        return bool(
            self.assertions.expected_code_contains
            or self.assertions.expected_code_not_contains
        )

    @property
    def needs_hitl(self) -> bool:
        """是否真实启动 Architect 并等待澄清暂停事件。"""
        return bool(self.assertions.expected_hitl)

    @property
    def needs_gate(self) -> bool:
        """是否真实 dispatch both 并读取门控状态。"""
        return bool(self.assertions.expected_gate_status)


@dataclass
class Check:
    name: str
    passed: bool
    expected: Any = None
    actual: Any = None
    message: str = ""


@dataclass
class CaseResult:
    case_id: str
    name: str
    tags: tuple[str, ...]
    duration_ms: float = 0.0
    checks: list[Check] = field(default_factory=list)
    handoff: str | None = None
    target: str | None = None
    task_type: str | None = None
    dispatchable: bool | None = None
    missing_inputs: list[str] = field(default_factory=list)
    handoffs: list[str] = field(default_factory=list)
    run_id: str | None = None
    sandbox_id: str | None = None
    run_status: str | None = None
    code_source: str | None = None
    hitl_event: str | None = None
    hitl_slot: str | None = None
    gate_status: str | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(check.passed for check in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.passed]


# ============================================================
# HTTP 客户端
# ============================================================
class ApiClient:
    """极简 JSON HTTP 客户端，避免给宿主机引入额外依赖。"""

    def __init__(self, base_url: str, *, api_mode: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_mode = api_mode
        self.timeout = timeout

    @property
    def is_bff(self) -> bool:
        return self.api_mode == "bff"

    @property
    def route_path(self) -> str:
        return (
            "/api/v1/supervisor/route"
            if self.is_bff
            else "/api/v1/internal/supervisor/route"
        )

    @property
    def run_path(self) -> str:
        return "/api/v1/runs" if self.is_bff else "/api/v1/internal/runs/execute"

    @property
    def supervisor_dispatch_path(self) -> str:
        return (
            "/api/v1/supervisor/dispatch"
            if self.is_bff
            else "/api/v1/internal/supervisor/dispatch"
        )

    @property
    def architect_stream_path(self) -> str:
        return (
            "/api/v1/architect/runs/stream"
            if self.is_bff
            else "/api/v1/internal/architect/runs/stream"
        )

    def architect_cancel_path(self, run_id: str) -> str:
        if self.is_bff:
            return f"/api/v1/architect/runs/{run_id}/cancel"
        return f"/api/v1/internal/architect/runs/{run_id}/cancel"

    def sandbox_path(self, sandbox_id: str) -> str:
        if self.is_bff:
            return f"/api/v1/sandboxes/{sandbox_id}"
        return f"/api/v1/internal/sandbox/{sandbox_id}"

    def sandbox_export_path(self, sandbox_id: str) -> str:
        if self.is_bff:
            return f"/api/v1/sandboxes/{sandbox_id}/export"
        return f"/api/v1/internal/sandbox/{sandbox_id}/export"

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> tuple[int, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout or self.timeout,
            ) as response:
                body = response.read().decode("utf-8", errors="replace")
                return response.status, _json_or_text(body)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return exc.code, _json_or_text(body)
        except urllib.error.URLError as exc:
            raise EvalError(f"无法连接 {self.base_url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise EvalError(f"请求超时: {method} {path}") from exc

    def request_bytes(
        self,
        method: str,
        path: str,
        *,
        timeout: float = 60,
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            method=method,
            headers={"Accept": "application/gzip, application/octet-stream"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except urllib.error.URLError as exc:
            raise EvalError(f"无法连接 {self.base_url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise EvalError(f"请求超时: {method} {path}") from exc

    def post_sse_until(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        stop_types: set[str],
        timeout: float = 120,
    ) -> list[dict[str, Any]]:
        """POST 并读取 SSE，直到命中终态事件或连接关闭。

        只解析 ``data:`` 帧，忽略 ``retry:`` / 心跳注释。返回读到的事件
        列表（含终止事件），由调用方判断实际类型，避免把“流正常结束”
        误判成“事件命中”。
        """
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )
        events: list[dict[str, Any]] = []
        data_lines: list[str] = []
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        if data_lines:
                            event = _decode_sse_data(data_lines)
                            data_lines = []
                            if event is not None:
                                events.append(event)
                                if event.get("type") in stop_types:
                                    break
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                if data_lines:
                    event = _decode_sse_data(data_lines)
                    if event is not None:
                        events.append(event)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise EvalError(f"SSE HTTP {exc.code}: {body[:500]}") from exc
        except urllib.error.URLError as exc:
            raise EvalError(f"无法连接 {self.base_url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise EvalError(f"SSE 超时: {path}") from exc
        return events

    def cancel_architect_run(self, run_id: str, reason: str = "eval cleanup") -> None:
        status, body = self.request_json(
            "POST",
            self.architect_cancel_path(run_id),
            {"reason": reason},
            timeout=30,
        )
        if status not in (200, 202, 204, 404):
            raise EvalError(f"取消 Architect Run 失败 HTTP {status}: {body}")

    def read_sandbox_source(self, sandbox_id: str, path: str) -> str:
        """从沙盒导出包中读取真实源码，而不是 Vite 转换后的模块。"""
        status, data = self.request_bytes(
            "GET",
            self.sandbox_export_path(sandbox_id),
            timeout=60,
        )
        if status != 200:
            detail = data[:500].decode("utf-8", errors="replace")
            raise EvalError(f"沙盒导出 HTTP {status}: {detail}")
        if len(data) > 50 * 1024 * 1024:
            raise EvalError("沙盒导出包超过 50MB，拒绝解析")

        target = path.lstrip("./")
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                for member in archive.getmembers():
                    if not member.isfile():
                        continue
                    normalized = PurePosixPath(member.name)
                    if ".." in normalized.parts:
                        continue
                    relative = "/".join(
                        part for part in normalized.parts if part not in {"", "."}
                    )
                    if relative != target:
                        continue
                    if member.size > 5 * 1024 * 1024:
                        raise EvalError(f"沙盒源码文件过大: {path}")
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        continue
                    return extracted.read().decode("utf-8")
        except tarfile.TarError as exc:
            raise EvalError(f"沙盒导出包解析失败: {exc}") from exc
        raise EvalError(f"沙盒导出包中不存在 {path}")

    def delete_sandbox(self, sandbox_id: str) -> None:
        status, body = self.request_json(
            "DELETE",
            self.sandbox_path(sandbox_id),
            timeout=60,
        )
        if status not in (200, 202, 204, 404):
            raise EvalError(f"清理沙盒失败 HTTP {status}: {body}")


def _decode_sse_data(lines: list[str]) -> dict[str, Any] | None:
    """把 SSE data 行拼成事件 dict；非 JSON 帧直接忽略。"""
    raw = "\n".join(lines).strip()
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _json_or_text(body: str) -> Any:
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def _resolve_api_mode(base_url: str) -> str:
    explicit = os.environ.get("EVAL_API_MODE", "").strip().lower()
    if explicit in {"bff", "core"}:
        return explicit
    port = urllib.parse.urlparse(base_url).port
    if port == 8100:
        return "core"
    if port in {8080, 18080}:
        return "bff"
    # 默认按公开 BFF 契约处理；直连 AI Core 时显式设置 EVAL_API_MODE=core。
    return "bff"


# ============================================================
# 评测集加载
# ============================================================
def load_suite(path: Path) -> tuple[str, list[CaseSpec]]:
    if not path.is_file():
        raise EvalError(f"评测集不存在: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise EvalError(f"YAML 解析失败: {path}: {exc}") from exc

    if not isinstance(raw, list) or not raw:
        raise EvalError("评测集根节点必须是非空用例列表")

    fixtures_root = path.parent / "fixtures"
    cases: list[CaseSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        case = _parse_case(
            item,
            index=index,
            fixtures_root=fixtures_root,
        )
        if case.case_id in seen:
            raise EvalError(f"用例 ID 重复: {case.case_id}")
        seen.add(case.case_id)
        cases.append(case)
    return path.stem, cases


def _parse_case(
    raw: Any,
    *,
    index: int,
    fixtures_root: Path,
) -> CaseSpec:
    context = f"第 {index} 条用例"
    if not isinstance(raw, dict):
        raise EvalError(f"{context} 必须是映射")

    case_id = _required_str(raw.get("id"), f"{context} id")
    if not all(ch.isalnum() or ch in {"_", "-"} for ch in case_id):
        raise EvalError(f"{context} id 只允许字母、数字、下划线、连字符: {case_id}")
    name = _required_str(raw.get("name"), f"{context} name")
    tags = tuple(_str_list(raw.get("tags", []), f"{context} tags"))

    raw_input = raw.get("input")
    if not isinstance(raw_input, dict):
        raise EvalError(f"{context} input 必须是映射")
    text = _optional_str(raw_input.get("text")) or ""
    fixture_name = _optional_str(raw_input.get("fixture"))

    raw_annotations: list[Any] = []
    if "annotation" in raw_input and "annotations" in raw_input:
        raise EvalError(f"{context} 不能同时声明 annotation 与 annotations")
    if "annotation" in raw_input:
        raw_annotations = [raw_input["annotation"]]
    elif "annotations" in raw_input:
        value = raw_input["annotations"]
        if not isinstance(value, list):
            raise EvalError(f"{context} annotations 必须是列表")
        raw_annotations = value
    if len(raw_annotations) > 3:
        raise EvalError(f"{context} 一次最多 3 个标注")

    annotations: list[dict[str, Any]] = []
    for annotation_index, annotation in enumerate(raw_annotations, start=1):
        if not isinstance(annotation, dict):
            raise EvalError(f"{context} 第 {annotation_index} 个标注必须是映射")
        dumped = dict(annotation)
        # 与离线引擎保持一致：单标注自动继承 input.text。
        if len(raw_annotations) == 1 and text:
            payload = dumped.setdefault("payload", {})
            if isinstance(payload, dict) and not payload.get("text"):
                payload["text"] = text
        annotations.append(dumped)

    fixture_files: dict[str, str] = {}
    if fixture_name:
        fixture_files = _load_fixture_files(fixtures_root, fixture_name)

    raw_assertions = raw.get("assertions")
    if not isinstance(raw_assertions, dict):
        raise EvalError(f"{context} assertions 必须是映射")
    assertions = _parse_assertions(raw_assertions, context=f"{context} assertions")

    if (
        assertions.expected_code_contains or assertions.expected_code_not_contains
    ) and not fixture_name:
        raise EvalError(
            f"{context} 声明了代码断言但缺少 input.fixture；"
            "真实沙盒需要一个可复现的初始项目"
        )
    if assertions.expected_code_contains or assertions.expected_code_not_contains:
        if not annotations:
            raise EvalError(f"{context} 代码断言需要至少 1 个画布标注")

    return CaseSpec(
        case_id=case_id,
        name=name,
        tags=tags,
        text=text,
        annotations=tuple(annotations),
        fixture_files=fixture_files,
        fixture_name=fixture_name,
        assertions=assertions,
    )


def _parse_assertions(raw: dict[str, Any], *, context: str) -> Assertions:
    handoff = _required_str(raw.get("expected_handoff"), f"{context} expected_handoff")
    if handoff not in ALLOWED_HANDOFFS:
        raise EvalError(f"{context} expected_handoff 非法: {handoff}")

    dispatchable = raw.get("expected_dispatchable")
    if dispatchable is not None and not isinstance(dispatchable, bool):
        raise EvalError(f"{context} expected_dispatchable 必须是布尔值")

    expected_handoffs: tuple[str, ...] | None = None
    if "expected_handoffs" in raw:
        expected_handoffs = tuple(
            _str_list(raw["expected_handoffs"], f"{context} expected_handoffs")
        )

    expected_missing: tuple[str, ...] | None = None
    if "expected_missing_inputs" in raw:
        expected_missing = tuple(
            _str_list(
                raw["expected_missing_inputs"],
                f"{context} expected_missing_inputs",
            )
        )

    expected_hitl = _optional_str(raw.get("expected_hitl"))
    if expected_hitl is not None and expected_hitl not in {
        "architect.clarification.required",
        "clarification.required",
    }:
        raise EvalError(f"{context} expected_hitl 非法: {expected_hitl}")

    expected_hitl_slot = _optional_str(raw.get("expected_hitl_slot"))
    if expected_hitl_slot and not expected_hitl:
        raise EvalError(f"{context} expected_hitl_slot 必须与 expected_hitl 同时声明")

    expected_gate_status = _optional_str(raw.get("expected_gate_status"))
    if expected_gate_status is not None and expected_gate_status not in {
        "AWAITING_CONTRACT",
        "RELEASING",
        "RELEASED",
        "FAILED",
    }:
        raise EvalError(f"{context} expected_gate_status 非法: {expected_gate_status}")

    return Assertions(
        expected_handoff=handoff,
        expected_agent=_optional_str(raw.get("expected_agent")),
        expected_task_type=_optional_str(raw.get("expected_task_type")),
        expected_dispatchable=dispatchable,
        expected_handoffs=expected_handoffs,
        expected_missing_inputs=expected_missing,
        expected_code_contains=tuple(
            _str_list(
                raw.get("expected_code_contains", []),
                f"{context} expected_code_contains",
            )
        ),
        expected_code_not_contains=tuple(
            _str_list(
                raw.get("expected_code_not_contains", []),
                f"{context} expected_code_not_contains",
            )
        ),
        expected_reason_contains=tuple(
            _str_list(
                raw.get("expected_reason_contains", []),
                f"{context} expected_reason_contains",
            )
        ),
        expected_hitl=expected_hitl,
        expected_hitl_slot=expected_hitl_slot,
        expected_gate_status=expected_gate_status,
    )


def _load_fixture_files(fixtures_root: Path, fixture: str) -> dict[str, str]:
    pure = Path(fixture)
    if pure.is_absolute() or ".." in pure.parts:
        raise EvalError(f"fixture 路径不允许绝对路径或目录穿越: {fixture}")
    fixture_dir = (fixtures_root / fixture).resolve()
    root = fixtures_root.resolve()
    if not fixture_dir.is_dir():
        raise EvalError(f"fixture 目录不存在: {fixture_dir}")
    if not fixture_dir.is_relative_to(root):
        raise EvalError(f"fixture 路径越界: {fixture}")

    files: dict[str, str] = {}
    for path in sorted(fixture_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(fixture_dir).as_posix()
        try:
            files[relative] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise EvalError(f"fixture 只允许 UTF-8 文本: {path}") from exc
    if not files:
        raise EvalError(f"fixture 目录为空: {fixture_dir}")
    return files


def _required_str(value: Any, context: str) -> str:
    result = _optional_str(value)
    if not result:
        raise EvalError(f"{context} 不能为空")
    return result


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise EvalError(f"期望字符串，实际 {type(value).__name__}")
    return value.strip() or None


def _str_list(value: Any, context: str) -> list[str]:
    if not isinstance(value, list):
        raise EvalError(f"{context} 必须是列表")
    result: list[str] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, str) or not item.strip():
            raise EvalError(f"{context} 第 {index} 项必须是非空字符串")
        result.append(item.strip())
    return result


# ============================================================
# 执行器
# ============================================================
class RealSandboxEvalRunner:
    """逐条执行评测用例，任何单条失败都不阻断后续用例。"""

    def __init__(
        self,
        client: ApiClient,
        *,
        run_tag: str,
        keep_sandboxes: bool = False,
    ) -> None:
        self.client = client
        self.run_tag = run_tag
        self.keep_sandboxes = keep_sandboxes
        self.sandboxes_created = 0
        self.sandboxes_cleaned = 0
        self.cleanup_warnings: list[str] = []

    def run(self, case: CaseSpec) -> CaseResult:
        result = CaseResult(case_id=case.case_id, name=case.name, tags=case.tags)
        started = time.monotonic()
        sandbox_id: str | None = None
        try:
            project_id = f"eval_{self.run_tag}_{case.case_id}"[:64]
            # 每条用例使用全新的 threadId，隔绝对话级短期记忆。
            # 否则固定 annotationId 会命中上一次评测留下的源码快照，导致
            # 同一修改被重复叠加（例如新增按钮越跑越多）。
            annotations = _isolate_annotations(
                case.annotations,
                run_tag=self.run_tag,
                case_id=case.case_id,
            )
            route_payload = self._route_payload(case, project_id, annotations)
            status, body = self.client.request_json(
                "POST",
                self.client.route_path,
                route_payload,
                timeout=min(self.client.timeout, 120),
            )

            if status != 200:
                raise EvalError(f"Supervisor route HTTP {status}: {body}")
            if not isinstance(body, dict):
                raise EvalError(f"Supervisor route 返回非 JSON: {body}")

            decision = body.get("decision") or {}
            handoffs = body.get("handoffs") or []
            result.handoff = decision.get("handoff")
            result.target = decision.get("target")
            result.task_type = decision.get("taskType")
            result.dispatchable = decision.get("dispatchable")
            result.missing_inputs = list(decision.get("missingInputs") or [])
            result.handoffs = [
                item.get("agentId")
                for item in handoffs
                if isinstance(item, dict) and item.get("agentId")
            ]
            result.checks.extend(_check_decision(decision, case.assertions, handoffs))
            result.checks.extend(_check_reason(decision, case.assertions))

            if case.needs_hitl:
                (
                    hitl_checks,
                    hitl_event,
                    hitl_slot,
                    hitl_run_id,
                ) = self._execute_architect_hitl(case, project_id)
                result.checks.extend(hitl_checks)
                result.hitl_event = hitl_event
                result.hitl_slot = hitl_slot
                result.run_id = hitl_run_id

            if case.needs_gate:
                gate_checks, gate_status, gate_run_id = self._execute_both_gate(
                    route_payload,
                    str(case.assertions.expected_gate_status),
                )
                result.checks.extend(gate_checks)
                result.gate_status = gate_status
                result.run_id = result.run_id or gate_run_id

            if case.needs_code_generation:
                (
                    code,
                    code_source,
                    run_id,
                    sandbox_id,
                    run_status,
                    run_checks,
                ) = self._execute_ui_case(case, project_id, annotations)
                result.run_id = run_id
                result.sandbox_id = sandbox_id
                result.run_status = run_status
                result.code_source = code_source
                result.checks.extend(run_checks)
                result.checks.extend(
                    _check_code(
                        code,
                        case.assertions,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - 单条失败必须被完整记录
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            if sandbox_id and not self.keep_sandboxes:
                try:
                    self.client.delete_sandbox(sandbox_id)
                    self.sandboxes_cleaned += 1
                except Exception as exc:  # noqa: BLE001 - 清理失败只告警
                    self.cleanup_warnings.append(f"{case.case_id}: {sandbox_id}: {exc}")
            result.duration_ms = (time.monotonic() - started) * 1000
        return result

    def _route_payload(
        self,
        case: CaseSpec,
        project_id: str,
        annotations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "tenantId": TENANT_ID,
            "projectId": project_id,
            "projectName": f"Eval {case.case_id} - {case.name}"[:128],
            "message": case.text,
            "annotations": annotations,
            "files": dict(case.fixture_files),
            "options": {
                "scopeHint": "auto",
                "ui": {
                    "llmProvider": "mock",
                    "mockScenario": "none",
                    "clarificationMode": "off",
                },
                "architect": {
                    "llmProvider": "mock",
                    "ragProvider": "markdown",
                    "ragTopK": 3,
                },
            },
        }

    def _execute_architect_hitl(
        self,
        case: CaseSpec,
        project_id: str,
    ) -> tuple[list[Check], str | None, str | None, str | None]:
        """真实启动 Architect SSE，验证其在生成契约前暂停澄清。

        断言“事件命中 + 首个问题槽位”，而不是只看 HTTP 200：Architect
        也可能直接跑完并返回契约，那说明 HITL 中断失效。
        """
        expected_event = str(case.assertions.expected_hitl)
        expected_slot = case.assertions.expected_hitl_slot
        run_id = f"eval_hitl_{self.run_tag}_{case.case_id}"[:64]
        payload = {
            "runId": run_id,
            "tenantId": TENANT_ID,
            "projectId": project_id,
            "projectName": f"Eval HITL {case.case_id}"[:128],
            "source": {},
            "options": {
                "llmProvider": "mock",
                "ragProvider": "markdown",
                "ragTopK": 3,
            },
        }

        events: list[dict[str, Any]] = []
        checks: list[Check] = []
        try:
            events = self.client.post_sse_until(
                self.client.architect_stream_path,
                payload,
                stop_types={
                    expected_event,
                    "architect.contract.ready",
                    "architect.contract.confirmed",
                    "architect.cancelled",
                    "architect.failed",
                },
                timeout=min(self.client.timeout, 120),
            )
        finally:
            try:
                self.client.cancel_architect_run(run_id)
            except Exception as exc:  # noqa: BLE001 - 清理失败只告警
                self.cleanup_warnings.append(
                    f"{case.case_id}: architect {run_id}: {exc}"
                )

        event = next(
            (item for item in events if item.get("type") == expected_event),
            None,
        )
        actual_type = events[-1].get("type") if events else None
        checks.append(
            Check(
                name="hitl_event",
                passed=event is not None,
                expected=expected_event,
                actual=actual_type,
                message="Architect 未在契约生成前暂停澄清",
            )
        )

        first_slot = None
        if event is not None:
            event_payload = event.get("payload") or {}
            questions = event_payload.get("questions") or []
            if questions and isinstance(questions[0], dict):
                first_slot = questions[0].get("slot")
        if expected_slot is not None:
            checks.append(
                Check(
                    name="hitl_first_slot",
                    passed=first_slot == expected_slot,
                    expected=expected_slot,
                    actual=first_slot,
                    message="首个澄清问题的槽位与预期不一致",
                )
            )
        return checks, event.get("type") if event else None, first_slot, run_id

    def _execute_both_gate(
        self,
        route_payload: dict[str, Any],
        expected_status: str,
    ) -> tuple[list[Check], str | None, str | None]:
        """真实 dispatch both，断言只启动 Architect 且门控等待契约。

        dispatch 只负责启动 Architect；UI 必须等到契约 CONFIRMED 后由
        ``/gates/{id}/release`` 放行，因此这里绝不能出现 ui_graph。
        """
        status, body = self.client.request_json(
            "POST",
            self.client.supervisor_dispatch_path,
            route_payload,
            timeout=min(self.client.timeout, 120),
        )
        # 派发是异步启动：BFF 返回 202 Accepted 与 AI Core 一致。
        if status not in (200, 202):
            raise EvalError(f"Supervisor dispatch HTTP {status}: {body}")
        if not isinstance(body, dict):
            raise EvalError(f"Supervisor dispatch 返回非 JSON: {body}")

        gate = body.get("gate") if isinstance(body.get("gate"), dict) else None
        actual_status = gate.get("status") if gate else None
        architect_run_id = gate.get("architectRunId") if gate else None
        dispatched = [
            item.get("agentId")
            for item in (body.get("dispatches") or [])
            if isinstance(item, dict)
        ]

        checks = [
            Check(
                name="gate_status",
                passed=actual_status == expected_status,
                expected=expected_status,
                actual=actual_status,
                message="both 门控状态与预期不一致",
            ),
            Check(
                name="gate_only_architect",
                passed=dispatched == ["architect_graph"],
                expected=["architect_graph"],
                actual=dispatched,
                message="契约确认前必须只启动 Architect",
            ),
            Check(
                name="gate_ui_blocked",
                passed="ui_graph" not in dispatched,
                expected="不包含 ui_graph",
                actual=dispatched,
                message="UI 子图在门控释放前被启动",
            ),
        ]

        if architect_run_id:
            try:
                self.client.cancel_architect_run(architect_run_id)
            except Exception as exc:  # noqa: BLE001 - 清理失败只告警
                self.cleanup_warnings.append(
                    f"gate architect {architect_run_id}: {exc}"
                )
        return checks, actual_status, architect_run_id

    def _execute_ui_case(
        self,
        case: CaseSpec,
        project_id: str,
        annotations: list[dict[str, Any]],
    ) -> tuple[
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
        list[Check],
    ]:
        """通过 UI 子图执行真实沙盒 Run，并读取修改后的源码。"""
        payload = {
            "tenantId": TENANT_ID,
            "projectId": project_id,
            "annotations": annotations,
            "files": dict(case.fixture_files),
            "options": {
                "llmProvider": "mock",
                "mockScenario": "none",
                "maxRetries": 3,
                "keepSandbox": True,
                "clarificationMode": "off",
                "requireManualApproval": False,
            },
        }
        status, body = self.client.request_json(
            "POST",
            self.client.run_path,
            payload,
            timeout=self.client.timeout,
        )
        if status != 200:
            raise EvalError(f"UI Run HTTP {status}: {body}")
        if not isinstance(body, dict):
            raise EvalError(f"UI Run 返回非 JSON: {body}")

        run_id = body.get("runId")
        sandbox_id = body.get("sandboxId")
        run_status = body.get("status")
        if sandbox_id:
            self.sandboxes_created += 1

        checks = [
            Check(
                name="run_status",
                passed=run_status == "SUCCEEDED",
                expected="SUCCEEDED",
                actual=run_status,
                message=body.get("error") or "真实沙盒 Run 未成功",
            )
        ]
        verify = body.get("verifyResult") or {}
        if isinstance(verify, dict) and verify:
            checks.append(
                Check(
                    name="verify",
                    passed=verify.get("success") is True,
                    expected=True,
                    actual=verify.get("success"),
                    message=verify.get("errorLogs") or "构建验证未通过",
                )
            )

        files = body.get("generatedFiles") or {}
        generated = files.get("src/App.jsx") if isinstance(files, dict) else None

        code: str | None = None
        code_source: str | None = None
        if sandbox_id:
            try:
                code = self.client.read_sandbox_source(sandbox_id, "src/App.jsx")
                code_source = "sandbox-export:/src/App.jsx"
            except EvalError as exc:
                checks.append(
                    Check(
                        name="sandbox_export",
                        passed=False,
                        expected="从沙盒导出包读取 src/App.jsx",
                        actual=str(exc),
                        message="沙盒源码读取失败，回退到接口返回代码",
                    )
                )
        else:
            checks.append(
                Check(
                    name="sandbox_export",
                    passed=False,
                    expected="sandboxId",
                    actual=None,
                    message="Run 未返回沙盒，无法验证真实执行结果",
                )
            )

        if code is None and generated:
            code = generated
            code_source = "response:generatedFiles"
        if code is None and body.get("generatedPatch"):
            code = body["generatedPatch"]
            code_source = "response:generatedPatch"

        if code is None:
            checks.append(
                Check(
                    name="sandbox_code",
                    passed=False,
                    expected="从沙盒读取 src/App.jsx",
                    actual=None,
                    message="未获取到生成代码，无法执行代码断言",
                )
            )
        return code, code_source, run_id, sandbox_id, run_status, checks


def _isolate_annotations(
    annotations: tuple[dict[str, Any], ...],
    *,
    run_tag: str,
    case_id: str,
) -> list[dict[str, Any]]:
    """为每条用例生成全新的 threadId，隔绝对话级短期记忆。

    评测必须可重复：如果复用 YAML 里的固定 annotationId，服务端会命中
    历史 thread 的源码快照，导致同一修改在上一轮结果上继续叠加。
    """
    isolated: list[dict[str, Any]] = []
    for index, annotation in enumerate(annotations, start=1):
        item = dict(annotation)
        suffix = f"_{index}"
        base = f"eval_{run_tag}_{case_id}"
        item["threadId"] = base[: 64 - len(suffix)] + suffix
        isolated.append(item)
    return isolated


def _check_decision(
    decision: dict[str, Any],
    expected: Assertions,
    handoffs: list[Any],
) -> list[Check]:
    checks = [
        Check(
            name="handoff",
            passed=decision.get("handoff") == expected.expected_handoff,
            expected=expected.expected_handoff,
            actual=decision.get("handoff"),
            message="Supervisor Handoff 与预期不一致",
        )
    ]

    if expected.expected_agent is not None:
        checks.append(
            Check(
                name="agent",
                passed=decision.get("target") == expected.expected_agent,
                expected=expected.expected_agent,
                actual=decision.get("target"),
                message="目标 Agent 与预期不一致",
            )
        )
    if expected.expected_task_type is not None:
        checks.append(
            Check(
                name="task_type",
                passed=decision.get("taskType") == expected.expected_task_type,
                expected=expected.expected_task_type,
                actual=decision.get("taskType"),
                message="taskType 与预期不一致",
            )
        )
    if expected.expected_dispatchable is not None:
        checks.append(
            Check(
                name="dispatchable",
                passed=decision.get("dispatchable") is expected.expected_dispatchable,
                expected=expected.expected_dispatchable,
                actual=decision.get("dispatchable"),
                message="可派发性与预期不一致",
            )
        )
    if expected.expected_missing_inputs is not None:
        actual_missing = list(decision.get("missingInputs") or [])
        missing_ok = all(
            item in actual_missing for item in expected.expected_missing_inputs
        )
        if not expected.expected_missing_inputs:
            missing_ok = not actual_missing
        checks.append(
            Check(
                name="missing_inputs",
                passed=missing_ok,
                expected=list(expected.expected_missing_inputs),
                actual=actual_missing,
                message="missingInputs 未覆盖预期",
            )
        )
    if expected.expected_handoffs is not None:
        actual_handoffs = [
            item.get("agentId")
            for item in handoffs
            if isinstance(item, dict) and item.get("agentId")
        ]
        checks.append(
            Check(
                name="handoffs",
                passed=actual_handoffs == list(expected.expected_handoffs),
                expected=list(expected.expected_handoffs),
                actual=actual_handoffs,
                message="Handoff 数量或顺序与预期不一致",
            )
        )
    return checks


def _check_reason(decision: dict[str, Any], expected: Assertions) -> list[Check]:
    """安全拦截必须验证原因关键词，防止未知意图冒充策略拒绝。"""
    if not expected.expected_reason_contains:
        return []
    reason = str(decision.get("reason") or "")
    return [
        Check(
            name="reason_contains",
            passed=all(item in reason for item in expected.expected_reason_contains),
            expected=list(expected.expected_reason_contains),
            actual=reason,
            message="路由原因未覆盖预期关键词",
        )
    ]


def _check_code(code: str | None, expected: Assertions) -> list[Check]:
    if not expected.expected_code_contains and not expected.expected_code_not_contains:
        return []
    if code is None:
        return [
            Check(
                name="code_available",
                passed=False,
                expected="生成代码",
                actual=None,
                message="没有代码可断言",
            )
        ]

    checks: list[Check] = []
    for index, snippet in enumerate(expected.expected_code_contains):
        pattern = _compile_snippet(snippet)
        match = pattern.search(code)
        checks.append(
            Check(
                name=f"code_contains[{index}]",
                passed=match is not None,
                expected=snippet,
                actual="matched" if match else "not found",
                message="生成代码缺少必需片段",
            )
        )
    for index, snippet in enumerate(expected.expected_code_not_contains):
        pattern = _compile_snippet(snippet)
        match = pattern.search(code)
        checks.append(
            Check(
                name=f"code_not_contains[{index}]",
                passed=match is None,
                expected=f"不包含: {snippet}",
                actual="found" if match else "not found",
                message="生成代码出现禁止片段",
            )
        )
    return checks


def _compile_snippet(snippet: str) -> re.Pattern[str]:
    """把代码片段编译为“空白不敏感”的正则。

    ``background-color:red`` 与 ``background-color: red`` 视为同一语义，
    但除空白外的所有字符都按字面量匹配，避免把期望片段误当成通配表达式。
    """
    parts = re.split(r"\s+", snippet.strip())
    return re.compile(r"\s*".join(re.escape(part) for part in parts))


# ============================================================
# 报告
# ============================================================
@dataclass(frozen=True)
class RunContext:
    suite_name: str
    suite_path: Path
    base_url: str
    api_mode: str
    started_at: datetime
    run_tag: str
    keep_sandboxes: bool


def _render_console(
    results: list[CaseResult],
    context: RunContext,
    runner: RealSandboxEvalRunner,
    *,
    verbose: bool,
) -> str:
    total = len(results)
    passed = sum(1 for result in results if result.passed)
    failed = total - passed
    total_ms = sum(result.duration_ms for result in results)
    lines = [
        "=" * 78,
        "  vibe-platform Eval Suite · 真实沙盒评测",
        "=" * 78,
        f"  API      : {context.base_url} ({context.api_mode.upper()})",
        f"  Suite    : {context.suite_name} · {context.suite_path}",
        f"  Started  : {context.started_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}",
        f"  Run tag  : {context.run_tag}",
        "-" * 78,
    ]

    for result in results:
        status = "PASS" if result.passed else "FAIL"
        handoff = result.handoff or "-"
        lines.append(
            f"  {status:<4} {result.case_id:<10} {result.duration_ms / 1000:6.1f}s "
            f"handoff={handoff:<17} {result.name}"
        )
        if result.run_status:
            lines.append(
                f"       run={result.run_id or '-'} status={result.run_status} "
                f"code={result.code_source or '-'}"
            )
        if result.hitl_event or result.gate_status:
            lines.append(f"       evidence={_evidence(result)}")
        if result.error:
            lines.append(f"       ERROR: {result.error}")
        for check in result.failures:
            lines.append(
                f"       x {check.name}: expected={_short(check.expected)} "
                f"actual={_short(check.actual)}"
            )
            if check.message:
                lines.append(f"         {check.message}")
        if verbose and result.passed:
            lines.append(f"       checks={len(result.checks)} all passed")

    pass_rate = (passed / total * 100) if total else 0.0
    lines.extend(
        [
            "-" * 78,
            f"  Total    : {total}",
            f"  Passed   : {passed}",
            f"  Failed   : {failed}",
            f"  Pass rate: {pass_rate:.1f}%",
            f"  Duration : {total_ms / 1000:.2f}s",
            f"  Sandboxes: created={runner.sandboxes_created} "
            f"cleaned={runner.sandboxes_cleaned}",
        ]
    )
    if context.keep_sandboxes:
        lines.append("  NOTE     : --keep-sandboxes 已开启，沙盒未清理")
    for warning in runner.cleanup_warnings:
        lines.append(f"  WARN     : 清理失败 {warning}")
    lines.append("=" * 78)
    return "\n".join(lines)


def _render_markdown(
    results: list[CaseResult],
    context: RunContext,
    runner: RealSandboxEvalRunner,
) -> str:
    total = len(results)
    passed = sum(1 for result in results if result.passed)
    failed = total - passed
    total_ms = sum(result.duration_ms for result in results)
    pass_rate = (passed / total * 100) if total else 0.0
    generated_at = datetime.now(timezone.utc).astimezone()

    lines = [
        "# Eval Report · 真实沙盒评测",
        "",
        f"> 生成时间：{generated_at.strftime('%Y-%m-%d %H:%M:%S %Z')}  ",
        f"> 评测集：`{context.suite_path}`  ",
        f"> 服务地址：`{context.base_url}`（{context.api_mode.upper()}）  ",
        f"> 运行标识：`{context.run_tag}`",
        "",
        "## 1. 总览",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
        f"| 用例总数 | {total} |",
        f"| 通过 | {passed} |",
        f"| 失败 | {failed} |",
        f"| 通过率 | {pass_rate:.1f}% |",
        f"| 总耗时 | {total_ms / 1000:.2f}s |",
        f"| 创建沙盒 | {runner.sandboxes_created} |",
        f"| 清理沙盒 | {runner.sandboxes_cleaned} |",
        "",
        "## 2. 用例结果",
        "",
        "| 用例 | 名称 | Handoff | 期望 | 执行证据 | 耗时 | 结果 |",
        "|---|---|---|---|---|---:|---|",
    ]

    for result in results:
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{result.case_id}`",
                    _escape_cell(result.name),
                    f"`{result.handoff or '-'}`",
                    f"`{_expected_handoff(result)}`",
                    _escape_cell(_evidence(result)),
                    f"{result.duration_ms / 1000:.2f}s",
                    "PASS" if result.passed else "FAIL",
                ]
            )
            + " |"
        )

    failed_results = [result for result in results if not result.passed]
    lines.extend(["", "## 3. 失败明细", ""])
    if not failed_results:
        lines.extend(["全部用例通过。", ""])
    else:
        for result in failed_results:
            lines.extend(
                [
                    f"### {result.case_id} · {result.name}",
                    "",
                ]
            )
            if result.error:
                lines.extend([f"- **执行异常**：`{result.error}`", ""])
            if result.run_id:
                lines.append(f"- Run：`{result.run_id}`")
            if result.sandbox_id:
                lines.append(f"- Sandbox：`{result.sandbox_id}`")
            if result.code_source:
                lines.append(f"- 代码来源：`{result.code_source}`")
            if result.run_id or result.sandbox_id or result.code_source:
                lines.append("")
            lines.extend(
                [
                    "| 断言 | 期望 | 实际 | 说明 |",
                    "|---|---|---|---|",
                ]
            )
            for check in result.failures:
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            f"`{check.name}`",
                            _escape_cell(_short(check.expected, 160)),
                            _escape_cell(_short(check.actual, 160)),
                            _escape_cell(check.message or "-"),
                        ]
                    )
                    + " |"
                )
            lines.append("")

    lines.extend(
        [
            "## 4. 执行方式",
            "",
            "1. 读取 `config/eval_suite/core_cases.yaml`，逐条调用 Supervisor 路由接口；",
            "2. 对声明了代码断言的用例，调用 UI 子图 `/runs` 创建**真实 Docker 沙盒**，",
            "   由 Mock LLM 生成代码、安装依赖、构建并启动预览；",
            "3. 对 HITL 用例，调用 Architect SSE 并等待 `architect.clarification.required`，",
            "   断言首个澄清问题槽位，随后取消 Run；",
            "4. 对 both 用例，调用 Supervisor `/dispatch`，断言只启动 Architect 且门控为",
            "   `AWAITING_CONTRACT`，随后取消 Architect Run；",
            "5. 通过沙盒导出接口读取真实源码 `src/App.jsx`，用空白不敏感正则断言代码片段；",
            "6. 每条用例结束后删除沙盒 / 取消后台 Run，避免资源泄漏。",
            "",
            "> 说明：路由断言走真实 Supervisor 规则；代码生成使用 `llmProvider=mock`，",
            "> 因而本报告验证的是“路由契约 + 沙盒执行链路”，不评价真实模型质量。",
            "",
        ]
    )
    warnings: list[str] = [f"- `{warning}`" for warning in runner.cleanup_warnings]
    if context.keep_sandboxes:
        warnings.append(
            "- 本次以 `--keep-sandboxes` 运行，沙盒保留用于排查，请手动清理。"
        )
    if warnings:
        lines.extend(["## 5. 清理告警", "", *warnings, ""])
    return "\n".join(lines)


def _evidence(result: CaseResult) -> str:
    """用一行展示 HITL / 门控 / 沙盒三类运行证据。"""
    parts: list[str] = []
    if result.hitl_event:
        slot = f":{result.hitl_slot}" if result.hitl_slot else ""
        parts.append(f"{result.hitl_event}{slot}")
    if result.gate_status:
        parts.append(f"gate={result.gate_status}")
    if result.run_status:
        parts.append(f"run={result.run_status}")
    if result.code_source:
        parts.append(result.code_source)
    return " · ".join(parts) if parts else "-"


def _expected_handoff(result: CaseResult) -> str:
    # 报告层只展示 Handoff；完整断言在失败明细中展开。
    for check in result.checks:
        if check.name == "handoff":
            return str(check.expected)
    return "-"


def _escape_cell(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _short(value: Any, limit: int = 120) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    elif value is None:
        text = "None"
    else:
        text = str(value)
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ============================================================
# CLI
# ============================================================
def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        suite_name, cases = load_suite(Path(args.suite).expanduser().resolve())
    except EvalError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2

    selected = _select_cases(cases, args.filter)
    if not selected:
        print("CONFIG ERROR: 过滤后没有可执行用例", file=sys.stderr)
        return 2

    base_url = args.base_url.rstrip("/")
    context = RunContext(
        suite_name=suite_name,
        suite_path=Path(args.suite).expanduser().resolve(),
        base_url=base_url,
        api_mode=_resolve_api_mode(base_url),
        started_at=datetime.now(timezone.utc),
        run_tag=datetime.now().strftime("%Y%m%d_%H%M%S"),
        keep_sandboxes=args.keep_sandboxes,
    )
    client = ApiClient(
        base_url,
        api_mode=context.api_mode,
        timeout=args.timeout,
    )
    runner = RealSandboxEvalRunner(
        client,
        run_tag=context.run_tag,
        keep_sandboxes=args.keep_sandboxes,
    )

    print(f"Running {len(selected)} case(s) against {base_url} ...", flush=True)
    results: list[CaseResult] = []
    for case in selected:
        result = runner.run(case)
        results.append(result)
        print(
            f"  [{'PASS' if result.passed else 'FAIL'}] "
            f"{case.case_id} ({result.duration_ms / 1000:.1f}s)",
            flush=True,
        )
        if args.fail_fast and not result.passed:
            print("  --fail-fast: 停止后续用例", flush=True)
            break

    console = _render_console(results, context, runner, verbose=args.verbose)
    print("", flush=True)
    print(console, flush=True)

    report_path = Path(args.report).expanduser().resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        _render_markdown(results, context, runner),
        encoding="utf-8",
    )
    print(f"\nReport written: {report_path}", flush=True)

    return 0 if all(result.passed for result in results) else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/run_eval.py",
        description="基于真实 Docker 沙盒的 Eval Suite 评测引擎",
    )
    parser.add_argument(
        "--suite",
        default=str(DEFAULT_SUITE),
        help=f"评测集 YAML；默认 {DEFAULT_SUITE}",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"BFF 或 AI Core 地址；默认 {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--report",
        default=str(DEFAULT_REPORT),
        help=f"Markdown 报告输出路径；默认 {DEFAULT_REPORT}",
    )
    parser.add_argument(
        "--filter",
        action="append",
        default=[],
        help="按 id / 名称 / tag 子串过滤，可重复",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="单次真实 Run 超时秒数；默认 300",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="首条失败即停止",
    )
    parser.add_argument(
        "--keep-sandboxes",
        action="store_true",
        help="保留沙盒用于排查（默认每条用例后删除）",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="在终端展开每条用例的断言数量",
    )
    return parser


def _select_cases(cases: list[CaseSpec], filters: list[str]) -> list[CaseSpec]:
    needles = [item.casefold() for item in filters if item.strip()]
    if not needles:
        return cases
    selected: list[CaseSpec] = []
    for case in cases:
        haystack = " ".join([case.case_id, case.name, *case.tags]).casefold()
        if any(needle in haystack for needle in needles):
            selected.append(case)
    return selected


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
