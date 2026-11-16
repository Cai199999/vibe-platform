"""全局错误信封与异常转译。

Phase 1 的唯一事实源：所有抛向前端的错误都必须经过这里，输出统一
``ErrorEnvelope``。技术细节（堆栈、绝对路径、异常类名）只进入服务端日志，
不进入用户可见字段。

设计边界：
  - 本模块只负责“分类 + 文案 + 选项占位 + traceId”，不负责重试与降级；
  - 业务图标（errorOptions）为空时也必须显式返回空数组；
  - SSE 已建立后不能再改 HTTP 状态，只能发送 terminal 事件，见
    ``stream_event_frames``。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ai_core.agent.events import TERMINAL_EVENT_TYPES, RunEvent, RunEventBuffer
from ai_core.ports.sandbox import (
    SandboxError,
    SandboxGitError,
    SandboxNotFoundError,
    SandboxPatchError,
    SandboxStartupError,
    SandboxValidationError,
)

logger = logging.getLogger(__name__)

TRACE_HEADER = "X-Trace-Id"


# ------------------------------------------------------------
# 数据模型
# ------------------------------------------------------------
@dataclass(frozen=True)
class ErrorOption:
    """一个可执行的业务选项；Phase 1 只定义协议，不负责恢复。"""

    option_id: str
    label: str
    decision_id: str = ""
    action: str = ""
    description: str = ""
    risk: str = "LOW"
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decisionId": self.decision_id,
            "optionId": self.option_id,
            "label": self.label,
            "action": self.action,
            "description": self.description,
            "risk": self.risk,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class ErrorEnvelope:
    """前端唯一可消费的错误结构。"""

    code: str
    user_message: str
    category: str = "INTERNAL"
    severity: str = "FATAL"
    stage: str = ""
    trace_id: str = ""
    run_id: str = ""
    retryable: bool = False
    degraded: bool = False
    error_options: tuple[ErrorOption, ...] = ()
    diagnostic: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        options = [item.to_dict() for item in self.error_options]
        envelope = {
            "code": self.code,
            # message 为旧前端兼容字段，新前端统一读 userMessage。
            "message": self.user_message,
            "userMessage": self.user_message,
            "category": self.category,
            "severity": self.severity,
            "stage": self.stage,
            "traceId": self.trace_id,
            "runId": self.run_id,
            "retryable": self.retryable,
            "degraded": self.degraded,
            "errorOptions": options,
            "detail": {"code": self.code, "message": self.user_message},
        }
        if self.diagnostic:
            envelope["diagnostic"] = dict(self.diagnostic)
        return envelope


# ------------------------------------------------------------
# 错误码注册表
# ------------------------------------------------------------
@dataclass(frozen=True)
class ErrorSpec:
    category: str
    severity: str
    user_message: str
    retryable: bool = False
    options_factory: (
        Callable[[dict[str, Any]], list[ErrorOption]] | None
    ) = None


_SPECS: dict[str, ErrorSpec] = {
    "REQUEST_VALIDATION_FAILED": ErrorSpec(
        "INPUT", "FATAL", "提交内容不完整或格式不正确，请检查后重试。"
    ),
    "REQUEST_BODY_INVALID": ErrorSpec(
        "INPUT", "FATAL", "请求内容无法解析，请刷新页面后重试。"
    ),
    "INTERNAL_ERROR": ErrorSpec(
        "INTERNAL", "FATAL", "系统遇到未预期问题，已记录诊断编号，请稍后重试。"
    ),
    "CONTRACT_VALIDATION_FAILED": ErrorSpec(
        "CONTRACT",
        "DECISION_REQUIRED",
        "生成方案中的数据库表与接口不一致，已停止以避免产生不可用代码。",
    ),
    "CONFIDENCE_EXHAUSTED": ErrorSpec(
        "REQUIREMENT",
        "DECISION_REQUIRED",
        "为避免猜错，还需要补充一些关键信息后才能继续。",
    ),
    "CONTRACT_CONFLICT_OPTION_INVALID": ErrorSpec(
        "CONTRACT", "FATAL", "所选方案已失效，请刷新后重新选择。"
    ),
    "CONTRACT_CONFLICT_RESOLUTION_FAILED": ErrorSpec(
        "CONTRACT",
        "DECISION_REQUIRED",
        "所选方案无法自动应用到当前契约，请重新选择处理方式。",
    ),
    "AST_EDIT_FAILED": ErrorSpec(
        "CODE_EDIT",
        "DECISION_REQUIRED",
        "代码结构较复杂，暂时无法安全写入；视觉草稿已保留。",
    ),
    "CODER_GENERATION_FAILED": ErrorSpec(
        "CODE_GEN",
        "DECISION_REQUIRED",
        "复杂代码生成未能完成，可以降级为可运行的模拟接口继续。",
    ),
    "CODER_INTERNAL_ERROR": ErrorSpec(
        "CODE_GEN", "RECOVERABLE", "代码生成遇到内部问题，正在尝试恢复。"
    ),
    "VERIFY_FAILED": ErrorSpec(
        "VERIFY", "RECOVERABLE", "生成结果未通过构建验证，可以自动修复或重试。"
    ),
    "SANDBOX_NOT_FOUND": ErrorSpec(
        "INFRA", "FATAL", "预览环境已失效，请重新创建项目沙盒。"
    ),
    "SANDBOX_INVALID_REQUEST": ErrorSpec(
        "INFRA", "FATAL", "沙盒请求不合法，请检查文件或路径后重试。"
    ),
    "SANDBOX_PATCH_FAILED": ErrorSpec(
        "INFRA", "RECOVERABLE", "代码补丁无法应用，可以自动修复或重试。"
    ),
    "SANDBOX_GIT_FAILED": ErrorSpec(
        "INFRA", "RECOVERABLE", "代码快照操作失败，可以重试或回滚。"
    ),
    "SANDBOX_UNAVAILABLE": ErrorSpec(
        "INFRA", "RECOVERABLE", "预览环境暂时不可用，代码结果仍可查看。"
    ),
    "SANDBOX_DOCKER_UNAVAILABLE": ErrorSpec(
        "INFRA", "RECOVERABLE", "沙盒服务暂时不可用，请稍后重试。"
    ),
    "SANDBOX_ERROR": ErrorSpec(
        "INFRA", "RECOVERABLE", "预览环境操作失败，可以重试。"
    ),
    "ARCHITECT_LLM_NOT_CONFIGURED": ErrorSpec(
        "CONFIG", "FATAL", "架构模型尚未配置，请联系管理员完成密钥设置。"
    ),
    "ARCHITECT_RAG_UNAVAILABLE": ErrorSpec(
        "INFRA", "RECOVERABLE", "知识库检索暂时不可用，可以继续生成或稍后重试。"
    ),
    "CONTRACT_REVISION_EXHAUSTED": ErrorSpec(
        "CONTRACT", "DECISION_REQUIRED", "契约修改次数已达上限，请重新发起架构澄清。"
    ),
    "CONTRACT_REVISION_REQUIRED": ErrorSpec(
        "CONTRACT",
        "DECISION_REQUIRED",
        "契约本身存在结构性矛盾，重试代码不会修复，请修改契约后重新生成。",
    ),
    "FULLSTACK_CONTRACT_ERROR": ErrorSpec(
        "CONTRACT",
        "DECISION_REQUIRED",
        "当前契约无法支撑代码生成，请调整契约后重试。",
    ),
    "FULLSTACK_SANDBOX_ERROR": ErrorSpec(
        "INFRA", "RECOVERABLE", "预览环境操作失败，可以重试或稍后继续。"
    ),
    "FULLSTACK_INTERNAL_ERROR": ErrorSpec(
        "INTERNAL", "FATAL", "全栈生成遇到内部问题，已记录诊断编号。"
    ),
    "ARCHITECT_INCOMPLETE_STATE": ErrorSpec(
        "ARCHITECT", "FATAL", "架构流程未到达可交付状态，请重新发起。"
    ),
    "ARCHITECT_RUN_NOT_FOUND": ErrorSpec(
        "ARCHITECT", "FATAL", "架构任务不存在或已过期，请重新发起。"
    ),
    "ARCHITECT_RUN_ID_CONFLICT": ErrorSpec(
        "ARCHITECT", "FATAL", "该架构任务编号已被占用，请刷新后重试。"
    ),
    "ARCHITECT_STATE_CONFLICT": ErrorSpec(
        "ARCHITECT", "FATAL", "架构流程状态已变化，请刷新后重新操作。"
    ),
    "ARCHITECT_CLARIFICATION_NOT_FOUND": ErrorSpec(
        "ARCHITECT", "FATAL", "待回答的问题已失效，请刷新后重新操作。"
    ),
    "ARCHITECT_CONTRACT_NOT_FOUND": ErrorSpec(
        "ARCHITECT", "FATAL", "待确认的契约已失效，请刷新后重新操作。"
    ),
    "ARCHITECT_ANSWERS_REQUIRED": ErrorSpec(
        "INPUT", "FATAL", "请至少回答一个问题或补充说明后继续。"
    ),
    "RUN_NOT_FOUND": ErrorSpec(
        "RUN", "FATAL", "执行任务不存在或已过期，请重新发起。"
    ),
    "RUN_BUSY": ErrorSpec(
        "RUN", "RECOVERABLE", "当前项目还有未完成的修改，请先处理后再试。"
    ),
    "RUN_ID_CONFLICT": ErrorSpec(
        "RUN", "FATAL", "该任务编号已被占用，请刷新后重试。"
    ),
    "CONTRACT_DRAFT_MISSING": ErrorSpec(
        "CONTRACT", "FATAL", "契约草稿丢失，请重新发起架构澄清。"
    ),
    "STREAM_UPSTREAM_ERROR": ErrorSpec(
        "TRANSPORT", "RECOVERABLE", "生成服务连接中断，请重试或稍后继续。"
    ),
    "BFF_INTERNAL_ERROR": ErrorSpec(
        "INTERNAL", "FATAL", "服务暂时不可用，已记录诊断编号，请稍后重试。"
    ),
}


def register_error(code: str, spec: ErrorSpec) -> None:
    """注册 / 覆盖错误码；Phase 2 的节点自愈可按需扩展。"""
    _SPECS[code] = spec


def _trace_id(value: str | None = None) -> str:
    raw = (value or "").strip()
    if raw:
        # 兼容 W3C traceparent：取 32 位 trace-id 部分。
        if len(raw.split("-")) >= 3 and len(raw.split("-")[1]) == 32:
            return f"trc_{raw.split('-')[1]}"
        return raw[:80]
    return f"trc_{uuid.uuid4().hex[:24]}"


def _code_from_exception(exc: BaseException) -> str:
    if isinstance(exc, StarletteHTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            code = str(detail.get("code") or "").strip()
            if code:
                return code
    code = str(getattr(exc, "code", "") or "").strip()
    if code:
        return code
    code = str(getattr(exc, "error_code", "") or "").strip()
    if code:
        return code
    if isinstance(exc, SandboxNotFoundError):
        return "SANDBOX_NOT_FOUND"
    if isinstance(exc, SandboxValidationError):
        return "SANDBOX_INVALID_REQUEST"
    if isinstance(exc, SandboxPatchError):
        return "SANDBOX_PATCH_FAILED"
    if isinstance(exc, SandboxGitError):
        return "SANDBOX_GIT_FAILED"
    if isinstance(exc, SandboxStartupError):
        return "SANDBOX_UNAVAILABLE"
    if isinstance(exc, SandboxError):
        return "SANDBOX_ERROR"
    name = type(exc).__name__
    if name == "ArchitectLLMConfigurationError":
        return "ARCHITECT_LLM_NOT_CONFIGURED"
    if name == "RAGProviderError":
        return "ARCHITECT_RAG_UNAVAILABLE"
    return "INTERNAL_ERROR"


def _technical_message(exc: BaseException) -> str:
    if isinstance(exc, StarletteHTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            return str(detail.get("message") or "")
        return str(detail or "")
    return str(exc or "")


def envelope_from_code(
    code: str,
    *,
    technical_message: str = "",
    trace_id: str | None = None,
    run_id: str = "",
    stage: str = "",
    error_options: list[ErrorOption] | None = None,
    diagnostic: dict[str, Any] | None = None,
) -> ErrorEnvelope:
    """按错误码构造信封；未知码统一降级为 INTERNAL_ERROR。"""
    resolved_code = code.strip() or "INTERNAL_ERROR"
    spec = _SPECS.get(resolved_code) or _SPECS["INTERNAL_ERROR"]
    options = tuple(error_options or [])
    if not options and spec.options_factory is not None:
        options = tuple(spec.options_factory({"message": technical_message}))
    return ErrorEnvelope(
        code=resolved_code,
        user_message=spec.user_message,
        category=spec.category,
        severity=spec.severity,
        stage=stage,
        trace_id=_trace_id(trace_id),
        run_id=run_id,
        retryable=spec.retryable,
        error_options=options,
        diagnostic=dict(diagnostic or {}),
    )


def translate_exception(
    exc: BaseException,
    *,
    trace_id: str | None = None,
    run_id: str = "",
    stage: str = "",
) -> ErrorEnvelope:
    """把任意异常转换为用户可见信封；技术消息只留在服务端日志。"""
    code = _code_from_exception(exc)
    technical = _technical_message(exc)
    if code == "INTERNAL_ERROR":
        logger.error(
            "unhandled exception trace_id=%s run_id=%s stage=%s type=%s",
            _trace_id(trace_id),
            run_id,
            stage,
            type(exc).__name__,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    return envelope_from_code(
        code,
        technical_message=technical,
        trace_id=trace_id,
        run_id=run_id,
        stage=stage,
    )


# ------------------------------------------------------------
# HTTP 边界
# ------------------------------------------------------------
def _request_trace_id(request: Request) -> str:
    existing = getattr(request.state, "trace_id", None)
    if existing:
        return str(existing)
    trace_id = _trace_id(
        request.headers.get(TRACE_HEADER) or request.headers.get("traceparent")
    )
    request.state.trace_id = trace_id
    return trace_id


def install_error_handlers(app: FastAPI) -> None:
    """安装 trace 中间件与全局异常处理器（JSON 接口）。"""

    @app.middleware("http")
    async def trace_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        trace_id = _request_trace_id(request)
        response = await call_next(request)
        response.headers[TRACE_HEADER] = trace_id
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields = []
        for item in exc.errors()[:20]:
            fields.append(
                {
                    "loc": ".".join(str(part) for part in item.get("loc") or ()),
                    "type": str(item.get("type") or ""),
                    "message": str(item.get("msg") or "")[:200],
                }
            )
        trace_id = _request_trace_id(request)
        envelope = envelope_from_code(
            "REQUEST_VALIDATION_FAILED",
            trace_id=trace_id,
            stage="http.validation",
            diagnostic={"fields": fields},
        )
        return JSONResponse(
            status_code=422,
            content=envelope.to_dict(),
            headers={TRACE_HEADER: trace_id},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        trace_id = _request_trace_id(request)
        envelope = translate_exception(
            exc,
            trace_id=trace_id,
            stage=f"http.{exc.status_code}",
        )
        headers = dict(exc.headers or {})
        headers[TRACE_HEADER] = trace_id
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope.to_dict(),
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        trace_id = _request_trace_id(request)
        envelope = translate_exception(
            exc,
            trace_id=trace_id,
            stage="http.unhandled",
        )
        return JSONResponse(
            status_code=500,
            content=envelope.to_dict(),
            headers={TRACE_HEADER: trace_id},
        )


# ------------------------------------------------------------
# SSE 终态
# ------------------------------------------------------------
def attach_error_envelope(
    payload: dict[str, Any],
    envelope: ErrorEnvelope,
) -> dict[str, Any]:
    """在既有事件 payload 上附加统一字段，保留旧字段兼容前端。"""
    merged = dict(payload)
    merged.setdefault("errorCode", envelope.code)
    merged.setdefault("message", envelope.user_message)
    merged["userMessage"] = envelope.user_message
    merged["errorOptions"] = [item.to_dict() for item in envelope.error_options]
    merged["traceId"] = envelope.trace_id
    merged.setdefault("retryable", envelope.retryable)
    merged["errorEnvelope"] = envelope.to_dict()
    return merged


def terminal_payload(
    terminal_event_type: str,
    envelope: ErrorEnvelope,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造各命名空间的 terminal 事件 payload。"""
    run_id = envelope.run_id
    if terminal_event_type == "fullstack.failed":
        base: dict[str, Any] = {
            "runId": run_id,
            "status": "FAILED",
            "error": envelope.user_message,
            "errorCode": envelope.code,
        }
    elif terminal_event_type == "architect.failed":
        base = {
            "runId": run_id,
            "error": envelope.user_message,
            "errorCode": envelope.code,
        }
    else:
        base = {
            "status": "FAILED",
            "code": envelope.code,
            "message": envelope.user_message,
        }
    if extra:
        base.update(extra)
    return attach_error_envelope(base, envelope)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


async def stream_event_frames(
    buffer: RunEventBuffer,
    after_seq: int,
    *,
    terminal_event_type: str,
    run_id: str | None = None,
) -> AsyncIterator[str]:
    """SSE 帧生成器：流建立后异常也必须收敛为 terminal 事件。

    普通 FastAPI 异常处理器在响应提交后无法再修改 HTTP 状态；如果这里
    直接抛出，浏览器只会看到连接断开并反复重连。因此必须：
      - 首次异常时记录日志；
      - 尝试把 terminal 事件写入缓冲区（可被 afterSeq 重放）；
      - 写入失败时直接合成一帧，保证前端一定收到终态。
    """
    resolved_run_id = run_id or getattr(buffer, "run_id", "")
    trace_id = _trace_id(
        getattr(buffer, "trace_id", None) or f"trc_{resolved_run_id or uuid.uuid4().hex}"
    )
    saw_terminal = False
    yield "retry: 3000\n\n"
    try:
        async for event in buffer.subscribe(after_seq):
            if event is None:
                yield ": ping\n\n"
                continue
            if event.type in TERMINAL_EVENT_TYPES:
                saw_terminal = True
            yield event.to_sse()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - SSE 边界必须兜底
        logger.exception(
            "sse stream failed run_id=%s trace_id=%s", resolved_run_id, trace_id
        )
        if saw_terminal:
            return
        envelope = translate_exception(
            exc,
            trace_id=trace_id,
            run_id=resolved_run_id,
            stage="sse",
        )
        payload = terminal_payload(terminal_event_type, envelope)
        event = None
        try:
            event = buffer.emit(
                terminal_event_type,
                payload,
                dedupe_key=f"{terminal_event_type}:sse:{resolved_run_id}",
            )
        except Exception:  # noqa: BLE001 - 缓冲区不可用时直接合成
            logger.exception(
                "failed to persist sse terminal event run_id=%s", resolved_run_id
            )
        if event is not None:
            yield event.to_sse()
            return
        synthetic = RunEvent(
            event_id=f"evt_{uuid.uuid4().hex}",
            run_id=resolved_run_id,
            # 不推进 seq：该事件不进入缓冲区，避免前端以不存在的 seq 重连。
            seq=max(0, getattr(buffer, "last_seq", 0)),
            type=terminal_event_type,
            version=1,
            ts=_utc_now(),
            payload=payload,
            trace_id=trace_id,
        )
        yield synthetic.to_sse()


__all__ = [
    "ErrorEnvelope",
    "ErrorOption",
    "ErrorSpec",
    "attach_error_envelope",
    "envelope_from_code",
    "install_error_handlers",
    "register_error",
    "stream_event_frames",
    "terminal_payload",
    "translate_exception",
]
