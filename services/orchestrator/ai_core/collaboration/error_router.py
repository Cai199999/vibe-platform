"""确定性门禁错误的统一目录、Architect 路由、尝试账本与高频统计。

平台的门禁分散在契约校验、Applier AST、ConsistencyVerifier、BuildVerifier、
Sandbox 等位置。这里提供一个统一入口：

- ``catalog``：全部已知错误码 → 负责 Agent（Architect / DBACoder /
  BackendCoder / FrontendCoder / InfraCoder ...）；未知错误默认交 Architect。
- ``plan``：把一条错误拆成「谁先修、谁兜底、第几次 / 最多几次」。
- ``ingest_feedback``：把现有 A2A 反馈转成门禁错误记录。
- ``ledger``：每条错误写结构化日志 + JSONL，并按错误码 / 指纹统计高频项。

约束：单个错误最多修复 ``MAX_FIX_ATTEMPTS`` 次；耗尽后必须回到 Architect，
由架构师复核契约或转人工，禁止未知 Agent 无限空转。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

MAX_FIX_ATTEMPTS = 3
DEFAULT_LEDGER_PATH = "data/gate-errors.jsonl"
_MAX_LEDGER_RECORDS = 5000
_MAX_RUN_RECORDS = 200

_WHITESPACE_RE = re.compile(r"\s+")
_VOLATILE_RE = re.compile(r"(?<![A-Za-z])(?:0x)?[0-9a-f]{4,}|\d+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_code(code: Any) -> str:
    """统一错误码大小写与分隔符；未知值归为 ``UNKNOWN_GATE_ERROR``。"""
    value = re.sub(r"[^A-Za-z0-9_]+", "_", str(code or "").strip()).strip("_")
    return value.upper() or "UNKNOWN_GATE_ERROR"


def error_fingerprint(code: str, message: str) -> str:
    """同一错误的不同实例（数字 / 文件行号不同）归并为同一指纹。"""
    normalized = _WHITESPACE_RE.sub(" ", _VOLATILE_RE.sub("N", message.lower())).strip()
    digest = hashlib.sha1(
        f"{canonical_code(code)}|{normalized[:240]}".encode("utf-8")
    ).hexdigest()
    return digest[:16]


@dataclass(frozen=True)
class GateErrorSpec:
    code: str
    owners: tuple[str, ...]
    stage: str
    summary: str = ""
    max_attempts: int = MAX_FIX_ATTEMPTS

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "owners": list(self.owners),
            "stage": self.stage,
            "summary": self.summary,
            "maxAttempts": self.max_attempts,
        }


_CATALOG: dict[str, GateErrorSpec] = {}


def _register(
    code: str,
    owners: Iterable[str],
    stage: str,
    summary: str = "",
) -> None:
    _CATALOG[canonical_code(code)] = GateErrorSpec(
        code=canonical_code(code),
        owners=tuple(dict.fromkeys(str(owner) for owner in owners)),
        stage=stage,
        summary=summary,
    )


# 契约结构 / 关系缺陷：先由 Architect 复核契约，必要时 DBACoder 重建。
for _code in (
    "MISSING_RELATION",
    "NESTED_TABLE_MISSING",
    "MISSING_DDL",
    "MISSING_CORE_TABLE",
    "COLLECTION_TABLE_MISSING",
    "INVALID_RELATION",
    "MISSING_API_ROUTES",
    "INVALID_API_PATH",
    "INVALID_INTERACTION_ROUTE",
    "UNDECLARED_INTERACTION_ROUTE",
):
    _register(_code, ("Architect", "DBACoder"), "contract", "契约结构或关系缺陷")

_register("CONTRACT_DEFECT", ("Architect", "DBACoder"), "contract", "契约自相矛盾")
_register("CONTRACT_REVIEW", ("Architect",), "contract", "契约需要复核")
_register("CONTRACT_REVISION", ("Architect",), "contract", "契约需要修订")
_register("CONTRACT_REVISION_REQUIRED", ("Architect",), "contract", "必须修订契约")
_register("CONTRACT_REVISION_EXHAUSTED", ("Architect",), "contract", "契约修订次数耗尽")
_register(
    "CONTRACT_VALIDATION_FAILED", ("Architect",), "contract", "契约未通过确定性校验"
)
_register("VISUAL_V0_INVALID", ("Architect",), "contract", "V0 虚拟树结构非法")
_register(
    "ARCHITECT_VISUAL_REQUIRED", ("Architect",), "contract", "后端绑定缺少冻结虚拟树"
)

# 持久化 / 代码 / 一致性门禁。
_register("ORM_SCHEMA_MISMATCH", ("DBACoder",), "codegen", "ORM 与 DDL 字段错位")
_register(
    "JOIN_ALIAS_MISMATCH",
    ("DBACoder", "BackendCoder"),
    "codegen",
    "JOIN 别名与契约不一致",
)
_register(
    "AST_SYNTAX_ERROR",
    ("BackendCoder", "FrontendCoder"),
    "codegen",
    "产物 AST 语法错误",
)
_register("AST_UNAVAILABLE", ("InfraCoder",), "environment", "AST 校验器不可用")
_register(
    "CODE_DEFECT",
    ("BackendCoder", "FrontendCoder"),
    "codegen",
    "代码实现缺陷",
)
_register(
    "CODER_GENERATION_FAILED",
    ("BackendCoder", "FrontendCoder"),
    "codegen",
    "Coder 生成失败",
)
_register(
    "CODER_INTERNAL_ERROR",
    ("BackendCoder", "FrontendCoder"),
    "codegen",
    "Coder 内部错误",
)
_register("CONSISTENCY_MISSING_ROUTE", ("BackendCoder",), "codegen", "前后端路由缺失")
_register(
    "VERIFY_FAILED",
    ("ConsistencyVerifier", "BackendCoder"),
    "verify",
    "一致性校验失败",
)
_register("FULLSTACK_CONSISTENCY_FAILED", ("BackendCoder",), "verify", "全栈一致性失败")
_register(
    "FULLSTACK_GENERATION_FAILED",
    ("BackendCoder", "DBACoder", "FrontendCoder"),
    "codegen",
    "全栈代码生成失败",
)
_register(
    "FULLSTACK_BUILD_FAILED",
    ("FrontendCoder", "InfraCoder"),
    "build",
    "前端构建失败",
)
_register(
    "BUILD_FAILED",
    ("FrontendCoder", "InfraCoder"),
    "build",
    "构建失败",
)
_register(
    "VITE_BUILD_FAILED",
    ("FrontendCoder", "InfraCoder"),
    "build",
    "Vite 构建失败",
)
_register(
    "BUILD_VERIFY_FAILED",
    ("FrontendCoder", "InfraCoder"),
    "build",
    "构建校验失败",
)

# 虚拟文档 → JSX 编译门禁。
_register(
    "VISUAL_CODEGEN_FAILED",
    ("Architect", "FrontendCoder"),
    "codegen",
    "虚拟树编译失败",
)
_register(
    "VISUAL_CODEGEN_INVALID_DOCUMENT",
    ("Architect", "FrontendCoder"),
    "codegen",
    "虚拟树不合法",
)
_register(
    "VISUAL_CODEGEN_REQUIRES_FROZEN",
    ("Architect", "FrontendCoder"),
    "codegen",
    "需要冻结虚拟树",
)
_register(
    "VISUAL_CODEGEN_COMPILER_ERROR",
    ("FrontendCoder", "InfraCoder"),
    "codegen",
    "JSX 编译器错误",
)
_register(
    "VISUAL_CODEGEN_FORBIDDEN_STYLE",
    ("FrontendCoder",),
    "codegen",
    "虚拟树包含禁止样式",
)
_register(
    "VISUAL_CODEGEN_UNSAFE_IMAGE",
    ("FrontendCoder",),
    "codegen",
    "虚拟树包含不安全图片",
)

# 沙盒 / 基础设施。
for _code in (
    "SANDBOX_ERROR",
    "SANDBOX_UNAVAILABLE",
    "SANDBOX_NOT_FOUND",
    "SANDBOX_DOCKER_UNAVAILABLE",
    "SANDBOX_PATCH_FAILED",
    "SANDBOX_GIT_FAILED",
    "SANDBOX_INVALID_REQUEST",
    "FULLSTACK_SANDBOX_ERROR",
    "FULLSTACK_L3_MYSQL_UNAVAILABLE",
):
    _register(_code, ("InfraCoder",), "environment", "沙盒或环境操作失败")

# 测试层：预览/登录/业务闭环失败都有明确负责 Agent。
_register(
    "TEST_PREVIEW_NOT_READY",
    ("InfraCoder", "FrontendCoder"),
    "testing",
    "预览未在超时前就绪",
)
_register(
    "TEST_PREVIEW_MISSING",
    ("InfraCoder",),
    "testing",
    "测试请求缺少预览地址",
)
_register(
    "TEST_UI_SMOKE_FAILED",
    ("FrontendCoder",),
    "testing",
    "页面返回 200 但 React 根节点未挂载",
)
_register(
    "TEST_AUTH_FAILED",
    ("BackendCoder", "FrontendCoder"),
    "testing",
    "测试账号登录失败",
)
_register(
    "TEST_API_FLOW_FAILED",
    ("BackendCoder",),
    "testing",
    "集合查询或创建接口失败",
)
_register(
    "TEST_CRUD_VERIFY_FAILED",
    ("BackendCoder", "DBACoder"),
    "testing",
    "CRUD 写入后查不到或更新/删除失败",
)
_register(
    "TEST_CONTRACT_INCOMPLETE",
    ("Architect", "DBACoder"),
    "testing",
    "契约缺少集合根或可推导的请求体",
)
_register(
    "UI_RUNNER_UNAVAILABLE", ("InfraCoder",), "testing", "Playwright UI runner 不可用"
)
_register(
    "UI_ELEMENT_NOT_FOUND",
    ("FrontendCoder",),
    "testing",
    "生成页面缺少表单/按钮/输入框",
)
_register(
    "UI_CLICK_NO_EFFECT", ("FrontendCoder",), "testing", "点击提交后页面无反馈"
)
_register(
    "UI_ASSERTION_FAILED",
    ("FrontendCoder",),
    "testing",
    "UI 提交成功文案断言失败",
)
_register(
    "UI_DATA_NOT_PERSISTED",
    ("BackendCoder", "DBACoder", "FrontendCoder"),
    "testing",
    "刷新后 UI 看不到刚提交的数据",
)
_register("TEST_CHECK_UNSUPPORTED", ("Architect",), "testing", "测试检查类型未实现")
_register("TEST_CHECK_EXCEPTION", ("Architect",), "testing", "测试执行器异常")
_register("TEST_REPORT_NOT_FOUND", ("Architect",), "testing", "测试报告不存在")

_register("FULLSTACK_CONTRACT_ERROR", ("Architect",), "contract", "全栈契约错误")
_register("FULLSTACK_INTERNAL_ERROR", ("Architect",), "internal", "全栈内部错误")
_register("INTERNAL_ERROR", ("Architect",), "internal", "平台内部错误")

# 错误信封里存在、但没有专门门禁归属的错误码：按 category 给默认 owner。
_CATEGORY_OWNERS: dict[str, tuple[str, ...]] = {
    "INPUT": ("Architect",),
    "CONTRACT": ("Architect",),
    "ARCHITECT": ("Architect",),
    "CODER": ("BackendCoder", "FrontendCoder"),
    "RUN": ("Architect",),
    "SANDBOX": ("InfraCoder",),
    "INFRA": ("InfraCoder",),
    "MODEL": ("Architect",),
    "TRANSPORT": ("InfraCoder",),
    "DIRECT_OP": ("FrontendCoder",),
    "VERIFY": ("ConsistencyVerifier", "BackendCoder"),
    "FULLSTACK": ("Architect", "BackendCoder"),
    "BFF": ("Architect",),
    "INTERNAL": ("Architect",),
}

_UNKNOWN_SPEC = GateErrorSpec(
    code="UNKNOWN_GATE_ERROR",
    owners=("Architect",),
    stage="unknown",
    summary="未登记的门禁错误，默认交架构师判断",
)


def _envelope_specs() -> Mapping[str, Any]:
    try:
        from ai_core.error_envelope import _SPECS  # noqa: PLC0415 - 避免循环依赖

        return _SPECS
    except Exception:  # pragma: no cover - 防御性兜底
        return {}


def spec_for(code: str) -> GateErrorSpec:
    key = canonical_code(code)
    spec = _CATALOG.get(key)
    if spec is not None:
        return spec
    envelope = _envelope_specs().get(key)
    if envelope is not None:
        owners = _CATEGORY_OWNERS.get(canonical_code(envelope.category), ("Architect",))
        return GateErrorSpec(
            code=key,
            owners=owners,
            stage=str(envelope.category or "unknown").lower(),
            summary=str(envelope.user_message or ""),
        )
    if key == "UNKNOWN_GATE_ERROR":
        return _UNKNOWN_SPEC
    return GateErrorSpec(
        code=key,
        owners=_UNKNOWN_SPEC.owners,
        stage="unknown",
        summary=_UNKNOWN_SPEC.summary,
    )


def all_specs() -> list[GateErrorSpec]:
    """显式目录 + 错误信封注册表合并后的完整错误清单。"""
    merged: dict[str, GateErrorSpec] = dict(_CATALOG)
    for code in _envelope_specs():
        key = canonical_code(code)
        if key not in merged:
            merged[key] = spec_for(key)
    return [merged[key] for key in sorted(merged)]


@dataclass(frozen=True)
class GateError:
    code: str
    message: str
    run_id: str = ""
    stage: str = ""
    source: str = ""
    attempt: int = 1
    max_attempts: int = MAX_FIX_ATTEMPTS

    @property
    def fingerprint(self) -> str:
        return error_fingerprint(self.code, self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "runId": self.run_id,
            "stage": self.stage,
            "source": self.source,
            "attempt": self.attempt,
            "maxAttempts": self.max_attempts,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class GateAssignment:
    error: GateError
    owners: tuple[str, ...]
    next_agents: tuple[str, ...]
    assigned_to: str
    exhausted: bool

    @property
    def fingerprint(self) -> str:
        return self.error.fingerprint

    def to_dict(self) -> dict[str, Any]:
        payload = self.error.to_dict()
        payload.update(
            {
                "owners": list(self.owners),
                "nextAgents": list(self.next_agents),
                "assignedTo": self.assigned_to,
                "exhausted": self.exhausted,
            }
        )
        return payload


class GateErrorLedger:
    """门禁错误的结构化日志 + JSONL + 高频统计。"""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        max_records: int = _MAX_LEDGER_RECORDS,
    ) -> None:
        self.path = Path(
            path or os.environ.get("GATE_ERROR_LEDGER_PATH") or DEFAULT_LEDGER_PATH
        )
        self._lock = threading.RLock()
        self._file_lock = threading.Lock()
        self._max_records = max(1, max_records)
        self._records: deque[dict[str, Any]] = deque(maxlen=self._max_records)
        self._by_run: dict[str, list[dict[str, Any]]] = {}
        self._by_code: Counter[str] = Counter()
        self._by_agent: Counter[str] = Counter()
        self._by_fingerprint: Counter[str] = Counter()
        self._latest: dict[str, dict[str, Any]] = {}
        self._exhausted = 0
        self._final_failures = 0
        # 进程重启后统计不能归零：从 JSONL 恢复最近记录。
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                lines = deque(handle, maxlen=self._max_records)
        except OSError:
            return
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            self._records.append(item)
            code = str(item.get("code") or "")
            fingerprint = str(item.get("fingerprint") or "")
            agent = str(item.get("assignedTo") or "")
            if code:
                self._by_code[code] += 1
            if agent:
                self._by_agent[agent] += 1
            if fingerprint:
                self._by_fingerprint[fingerprint] += 1
                self._latest[fingerprint] = {
                    "fingerprint": fingerprint,
                    "code": code,
                    "message": str(item.get("message") or "")[:600],
                    "count": self._by_fingerprint[fingerprint],
                    "lastRunId": str(item.get("runId") or ""),
                    "lastStage": str(item.get("stage") or ""),
                    "lastAt": str(item.get("createdAt") or ""),
                }
            run_id = str(item.get("runId") or "")
            if run_id:
                bucket = self._by_run.setdefault(run_id, [])
                bucket.append(item)
                if len(bucket) > _MAX_RUN_RECORDS:
                    del bucket[: len(bucket) - _MAX_RUN_RECORDS]
            if item.get("exhausted"):
                self._exhausted += 1
            if item.get("phase") == "final":
                self._final_failures += 1

    def record(
        self,
        assignment: GateAssignment,
        *,
        source: str = "",
        phase: str = "feedback",
    ) -> dict[str, Any]:
        error = assignment.error
        item = {
            **assignment.to_dict(),
            "eventId": hashlib.sha1(
                f"{error.run_id}|{error.fingerprint}|{_now()}".encode("utf-8")
            ).hexdigest()[:20],
            "phase": phase,
            "fromAgent": source,
            "createdAt": _now(),
        }
        run_id = str(error.run_id or "")
        fingerprint = error.fingerprint
        with self._lock:
            self._records.append(item)
            self._by_code[error.code] += 1
            self._by_agent[assignment.assigned_to] += 1
            self._by_fingerprint[fingerprint] += 1
            self._latest[fingerprint] = {
                "fingerprint": fingerprint,
                "code": error.code,
                "message": error.message[:600],
                "count": self._by_fingerprint[fingerprint],
                "lastRunId": run_id,
                "lastStage": error.stage,
                "lastAt": item["createdAt"],
            }
            if run_id:
                bucket = self._by_run.setdefault(run_id, [])
                bucket.append(item)
                if len(bucket) > _MAX_RUN_RECORDS:
                    del bucket[: len(bucket) - _MAX_RUN_RECORDS]
            if assignment.exhausted:
                self._exhausted += 1
            if phase == "final":
                self._final_failures += 1

        logger.warning(
            "gate error code=%s stage=%s run=%s attempt=%d/%d agent=%s "
            "exhausted=%s fingerprint=%s message=%s",
            error.code,
            error.stage or error.source,
            run_id or "-",
            error.attempt,
            error.max_attempts,
            assignment.assigned_to,
            assignment.exhausted,
            fingerprint,
            error.message[:600],
        )
        self._append_jsonl(item)
        return item

    def _append_jsonl(self, item: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(item, ensure_ascii=False)
            with self._file_lock:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except Exception as exc:  # noqa: BLE001 - 日志失败不能阻断生成
            logger.warning("gate error ledger 写入失败 path=%s: %s", self.path, exc)

    def for_run(
        self, run_id: str, limit: int = _MAX_RUN_RECORDS
    ) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._by_run.get(str(run_id), [])][-limit:]

    def stats(self, top: int = 10) -> dict[str, Any]:
        capped = max(1, min(int(top or 10), 50))
        with self._lock:
            by_code = [
                {"code": code, "count": count}
                for code, count in self._by_code.most_common(capped)
            ]
            top_errors = sorted(
                self._latest.values(),
                key=lambda item: int(item.get("count") or 0),
                reverse=True,
            )[:capped]
            by_agent = [
                {"agent": agent, "count": count}
                for agent, count in self._by_agent.most_common(capped)
            ]
            return {
                "total": sum(self._by_code.values()),
                "distinctErrors": len(self._latest),
                "byCode": by_code,
                "byAgent": by_agent,
                "topErrors": top_errors,
                "exhausted": self._exhausted,
                "finalFailures": self._final_failures,
                "maxAttempts": MAX_FIX_ATTEMPTS,
            }

    def reset(self) -> None:
        with self._lock:
            self._records.clear()
            self._by_run.clear()
            self._by_code.clear()
            self._by_agent.clear()
            self._by_fingerprint.clear()
            self._latest.clear()
            self._exhausted = 0
            self._final_failures = 0


class GateErrorRouter:
    """按目录把门禁错误分派给具体 Agent，并记录尝试次数。"""

    _REPAIR_KINDS = {
        "schema_repair",
        "code_repair",
        "contract_revision",
        "contract_review",
        "repair_request",
    }
    _REPAIR_ACTIONS = {
        "REPAIR_CODE",
        "REGENERATE_DB",
        "REVISE_CONTRACT",
        "REVIEW_CONTRACT",
        "REPAIR",
    }
    _KIND_CODES = {
        "schema_repair": "ORM_SCHEMA_MISMATCH",
        "code_repair": "CODE_DEFECT",
        "contract_revision": "CONTRACT_REVISION",
        "contract_review": "CONTRACT_REVIEW",
        "repair_request": "CODE_DEFECT",
    }

    def __init__(self, ledger: GateErrorLedger | None = None) -> None:
        self.ledger = ledger or GateErrorLedger()

    # ---- 目录 -------------------------------------------------
    def catalog(self) -> dict[str, Any]:
        specs = all_specs()
        return {
            "maxAttempts": MAX_FIX_ATTEMPTS,
            "count": len(specs),
            "codes": [spec.to_dict() for spec in specs],
        }

    def catalog_summary(self, limit: int = 18) -> str:
        # 优先展示最常触发、最能指导分派的错误，而不是按注册顺序截断。
        priority = (
            "CONTRACT_DEFECT",
            "CONTRACT_VALIDATION_FAILED",
            "VISUAL_V0_INVALID",
            "ARCHITECT_VISUAL_REQUIRED",
            "ORM_SCHEMA_MISMATCH",
            "JOIN_ALIAS_MISMATCH",
            "AST_SYNTAX_ERROR",
            "AST_UNAVAILABLE",
            "CODE_DEFECT",
            "CODER_GENERATION_FAILED",
            "CONSISTENCY_MISSING_ROUTE",
            "VERIFY_FAILED",
            "BUILD_FAILED",
            "VITE_BUILD_FAILED",
            "SANDBOX_ERROR",
            "FULLSTACK_GENERATION_FAILED",
            "FULLSTACK_BUILD_FAILED",
            "MISSING_RELATION",
        )
        merged = {spec.code: spec for spec in all_specs()}
        ordered = [merged[code] for code in priority if code in merged]
        seen = {spec.code for spec in ordered}
        ordered.extend(spec for spec in merged.values() if spec.code not in seen)
        lines = [
            f"- {spec.code} → {' / '.join(spec.owners)}"
            for spec in ordered[: max(1, limit)]
        ]
        return "\n".join(lines)

    # ---- 路由 -------------------------------------------------
    def plan(self, error: GateError) -> GateAssignment:
        spec = spec_for(error.code)
        max_attempts = min(
            MAX_FIX_ATTEMPTS,
            max(1, int(error.max_attempts or spec.max_attempts)),
        )
        attempt = max(1, int(error.attempt or 1))
        # 第 max 次仍然允许修复；只有超过预算才回到 Architect。
        exhausted = attempt > max_attempts
        owners = spec.owners or ("Architect",)
        if exhausted:
            next_agents: tuple[str, ...] = ("Architect",)
        else:
            next_agents = owners
        source = str(error.source or "")
        assigned_to = (
            "Architect" if exhausted else source if source in owners else next_agents[0]
        )
        return GateAssignment(
            error=GateError(
                code=canonical_code(error.code),
                message=error.message,
                run_id=error.run_id,
                stage=error.stage or spec.stage,
                source=source,
                attempt=attempt,
                max_attempts=max_attempts,
            ),
            owners=owners,
            next_agents=next_agents,
            assigned_to=assigned_to,
            exhausted=exhausted,
        )

    def _feedback_codes(
        self, feedback: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        for issue in payload.get("issues") or []:
            if isinstance(issue, Mapping) and issue.get("code"):
                entries.append(
                    (
                        str(issue["code"]),
                        str(issue.get("message") or feedback.get("summary") or ""),
                    )
                )
        audit = payload.get("audit")
        if isinstance(audit, Mapping):
            for problem in audit.get("problems") or []:
                if isinstance(problem, Mapping) and problem.get("code"):
                    entries.append(
                        (
                            str(problem["code"]),
                            str(
                                problem.get("message") or feedback.get("summary") or ""
                            ),
                        )
                    )
        if not entries and payload.get("defectKind"):
            entries.append(
                (
                    str(payload["defectKind"]),
                    str(feedback.get("summary") or ""),
                )
            )
        return entries

    def ingest_feedback(self, feedback: Mapping[str, Any]) -> list[GateAssignment]:
        """把一条 A2A 反馈转成门禁错误并分派；非修复类反馈直接忽略。"""
        if not isinstance(feedback, Mapping):
            return []
        kind = str(feedback.get("kind") or "")
        action = str(feedback.get("requestedAction") or "")
        payload = feedback.get("payload")
        if not isinstance(payload, Mapping):
            payload = {}
        entries = self._feedback_codes(feedback, payload)
        if not entries:
            if kind not in self._REPAIR_KINDS and action not in self._REPAIR_ACTIONS:
                return []
            code = self._KIND_CODES.get(kind, "UNKNOWN_GATE_ERROR")
            entries = [(code, str(feedback.get("summary") or ""))]
        if not entries:
            return []
        # 同一反馈里重复出现的同一错误只记一次。
        deduped: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for code, message in entries:
            key = (canonical_code(code), message)
            if key in seen:
                continue
            seen.add(key)
            deduped.append((code, message))
        entries = deduped

        attempt = max(1, int(payload.get("attempt") or 0) + 1)
        max_attempts = int(payload.get("maxAttempts") or MAX_FIX_ATTEMPTS)
        source = str(feedback.get("toAgent") or feedback.get("fromAgent") or "")
        run_id = str(feedback.get("runId") or "")
        assignments: list[GateAssignment] = []
        for code, message in entries:
            assignment = self.plan(
                GateError(
                    code=code,
                    message=message,
                    run_id=run_id,
                    stage=kind or "a2a.feedback",
                    source=source,
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
            )
            self.ledger.record(
                assignment,
                source=str(feedback.get("fromAgent") or ""),
                phase="feedback",
            )
            assignments.append(assignment)
        return assignments

    def record_final_failure(
        self,
        *,
        run_id: str,
        code: str,
        message: str,
        stage: str = "",
        source: str = "",
    ) -> GateAssignment:
        """运行最终失败也要入账，保证日志里能看到每条报错。"""
        spec = spec_for(code)
        assignment = self.plan(
            GateError(
                code=code,
                message=message,
                run_id=run_id,
                stage=stage or spec.stage,
                source=source,
                attempt=MAX_FIX_ATTEMPTS + 1,
                max_attempts=MAX_FIX_ATTEMPTS,
            )
        )
        self.ledger.record(assignment, source=source, phase="final")
        return assignment

    def record_validation_issues(
        self,
        *,
        run_id: str,
        issues: Iterable[str],
        code: str = "CONTRACT_VALIDATION_FAILED",
        stage: str = "architect.contract.validate",
        attempt: int = 1,
        max_attempts: int = MAX_FIX_ATTEMPTS,
    ) -> list[GateAssignment]:
        assignments: list[GateAssignment] = []
        for message in issues:
            text = str(message or "").strip()
            if not text:
                continue
            assignment = self.plan(
                GateError(
                    code=code,
                    message=text,
                    run_id=run_id,
                    stage=stage,
                    source="Architect",
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
            )
            self.ledger.record(assignment, source="Architect", phase="feedback")
            assignments.append(assignment)
        return assignments

    def stats(self, top: int = 10) -> dict[str, Any]:
        return self.ledger.stats(top)


# 单进程单例：Graph / Service / API 共用。
GATE_ERRORS = GateErrorRouter()


__all__ = [
    "GATE_ERRORS",
    "MAX_FIX_ATTEMPTS",
    "GateAssignment",
    "GateError",
    "GateErrorLedger",
    "GateErrorRouter",
    "GateErrorSpec",
    "all_specs",
    "canonical_code",
    "error_fingerprint",
    "spec_for",
]
