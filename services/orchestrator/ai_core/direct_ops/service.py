"""Direct Ops 服务：确定性目标解析 + 原子写入 + Git checkpoint + ChangeLog。

Phase 1 只实现 UPDATE_TEXT：
  - 服务端重新解析 target，不信任前端 selector；
  - 仅允许唯一 JSXText 子节点的叶子元素；
  - 主工作树写入受项目写锁保护，并用 baseRevision 做乐观并发校验；
  - 成功后写 Git commit 与 ChangeLog。
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from ai_core.adapters.sandbox import get_sandbox_provider
from ai_core.agent.events import RunEventBuffer
from ai_core.config import Settings, get_settings
from ai_core.direct_ops.changelog import get_change_log_store
from ai_core.domain.direct_ops import (
    ChangeLogEntry,
    DirectActionPayload,
    DirectActionResponse,
    DirectActionRisk,
    DirectErrorCode,
)
from ai_core.error_envelope import attach_error_envelope, envelope_from_code
from ai_core.ports.sandbox import SandboxError, SandboxProvider
from ai_core.project_lock import ProjectBusyError, project_write_lock

logger = logging.getLogger(__name__)

DIRECT_EVENT_STARTED = "direct.started"
DIRECT_EVENT_SUCCEEDED = "direct.succeeded"
DIRECT_EVENT_FAILED = "direct.failed"
DIRECT_EVENT_DECISION_REQUIRED = "direct.decision.required"

_EVENT_BUFFERS: dict[str, RunEventBuffer] = {}
_EVENT_LOCK = threading.RLock()
_COMPLETED: dict[str, DirectActionResponse] = {}
_COMPLETED_LOCK = threading.RLock()


class DirectActionError(Exception):
    """可直接转译为 ErrorEnvelope 的 Direct Ops 业务异常。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 500,
        diagnostic: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.diagnostic = dict(diagnostic or {})


def get_direct_event_buffer(direct_action_id: str) -> RunEventBuffer | None:
    with _EVENT_LOCK:
        return _EVENT_BUFFERS.get(direct_action_id)


def _event_buffer(direct_action_id: str) -> RunEventBuffer:
    with _EVENT_LOCK:
        buffer = _EVENT_BUFFERS.get(direct_action_id)
        if buffer is None:
            buffer = RunEventBuffer(
                direct_action_id,
                trace_id=f"trc_{direct_action_id}",
                max_events=200,
            )
            _EVENT_BUFFERS[direct_action_id] = buffer
        return buffer


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _diff_summary(before: str, after: str, limit: int = 2000) -> str:
    diff = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile="before",
            tofile="after",
            n=1,
            lineterm="",
        )
    )
    text = "\n".join(diff)
    return text[:limit]


class DirectOperationService:
    def __init__(
        self,
        sandbox: SandboxProvider | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._sandbox = sandbox or get_sandbox_provider()
        self._settings = settings or get_settings()
        self._changelog = get_change_log_store()

    # --------------------------------------------------------
    # 查询
    # --------------------------------------------------------
    def revision(self, project_id: str, sandbox_id: str) -> dict[str, str]:
        handle = self._sandbox.get(sandbox_id)
        if handle.project_id != project_id:
            raise DirectActionError(
                DirectErrorCode.TARGET_NOT_FOUND,
                "沙盒不属于当前项目",
                status_code=404,
            )
        return {
            "projectId": project_id,
            "sandboxId": sandbox_id,
            "revision": self._sandbox.git_head(sandbox_id),
        }

    # --------------------------------------------------------
    # 执行
    # --------------------------------------------------------
    def execute(self, payload: DirectActionPayload) -> DirectActionResponse:
        idem = payload.idempotency_key or payload.direct_action_id
        with _COMPLETED_LOCK:
            cached = _COMPLETED.get(idem)
        if cached is not None:
            return cached.model_copy(deep=True)

        buffer = _event_buffer(payload.direct_action_id)
        started = time.perf_counter()
        timings: dict[str, float] = {}
        buffer.emit(
            DIRECT_EVENT_STARTED,
            {
                "directActionId": payload.direct_action_id,
                "runId": payload.run_id,
                "projectId": payload.project_id,
                "sandboxId": payload.sandbox_id,
                "action": payload.action,
                "dryRun": payload.options.dry_run,
            },
            dedupe_key=f"direct.started:{payload.direct_action_id}",
        )

        try:
            response = self._execute_locked(payload, timings, started)
        except DirectActionError as exc:
            envelope = envelope_from_code(
                exc.code,
                technical_message=str(exc),
                trace_id=buffer.trace_id,
                run_id=payload.run_id or "",
                stage="direct_ops",
                diagnostic=exc.diagnostic,
            )
            buffer.emit(
                DIRECT_EVENT_FAILED,
                attach_error_envelope(
                    {
                        "directActionId": payload.direct_action_id,
                        "runId": payload.run_id,
                        "action": payload.action,
                        "status": "FAILED",
                        "errorCode": exc.code,
                        "error": envelope.user_message,
                    },
                    envelope,
                ),
                dedupe_key=f"direct.failed:{payload.direct_action_id}:{exc.code}",
            )
            raise
        except Exception as exc:  # noqa: BLE001 - 统一收口为业务错误
            logger.exception("direct action failed id=%s", payload.direct_action_id)
            error = DirectActionError(
                DirectErrorCode.TOOL_FAILED,
                "直接操作执行失败，已自动回滚。",
                status_code=500,
            )
            envelope = envelope_from_code(
                error.code,
                technical_message=str(exc),
                trace_id=buffer.trace_id,
                run_id=payload.run_id or "",
                stage="direct_ops",
            )
            buffer.emit(
                DIRECT_EVENT_FAILED,
                attach_error_envelope(
                    {
                        "directActionId": payload.direct_action_id,
                        "runId": payload.run_id,
                        "action": payload.action,
                        "status": "FAILED",
                        "errorCode": error.code,
                        "error": envelope.user_message,
                    },
                    envelope,
                ),
                dedupe_key=(f"direct.failed:{payload.direct_action_id}:{error.code}"),
            )
            raise error from exc

        # 只缓存真正写入成功的动作；dryRun 不缓存，避免同一
        # idempotencyKey 先从 dryRun 变成真实写入时命中旧结果。
        if response.status == "SUCCEEDED":
            with _COMPLETED_LOCK:
                _COMPLETED[idem] = response.model_copy(deep=True)
        buffer.emit(
            DIRECT_EVENT_SUCCEEDED,
            {
                "directActionId": payload.direct_action_id,
                "runId": payload.run_id,
                "action": payload.action,
                "status": response.status,
                "dryRun": response.dry_run,
                "risk": response.risk,
                "commitHash": response.commit_hash,
                "changeId": response.change_id,
                "latencyMs": response.latency_ms,
            },
            dedupe_key=f"direct.succeeded:{payload.direct_action_id}",
        )
        return response

    def _execute_locked(
        self,
        payload: DirectActionPayload,
        timings: dict[str, float],
        started: float,
    ) -> DirectActionResponse:
        handle = self._sandbox.get(payload.sandbox_id)
        if handle.project_id != payload.project_id:
            raise DirectActionError(
                DirectErrorCode.TARGET_NOT_FOUND,
                "沙盒不属于当前项目",
                status_code=404,
            )

        lease = None
        try:
            if not payload.options.dry_run:
                # 只有真正写代码才拿锁；dryRun 只读，不阻塞 AI Run。
                lease = project_write_lock.acquire(
                    payload.project_id,
                    owner=f"direct:{payload.direct_action_id}",
                    timeout=self._settings.direct_ops_lock_timeout_seconds,
                )
            mark = time.perf_counter()
            current_revision = self._sandbox.git_head(payload.sandbox_id)
            timings["revisionMs"] = _elapsed_ms(mark)
            if (
                not payload.options.dry_run
                and payload.base_revision
                and payload.base_revision != current_revision
            ):
                raise DirectActionError(
                    DirectErrorCode.REVISION_MISMATCH,
                    "代码已被其它操作更新，请刷新后重试。",
                    status_code=409,
                    diagnostic={
                        "expectedRevision": payload.base_revision,
                        "actualRevision": current_revision,
                    },
                )

            mark = time.perf_counter()
            anchor = self._resolve(payload)
            timings["resolveMs"] = _elapsed_ms(mark)
            risk = self._risk(payload, anchor)
            resolved_target = {
                "file": anchor.get("file"),
                "component": anchor.get("component"),
                "range": anchor.get("range"),
                "nodeType": anchor.get("nodeType"),
                "nodeSource": str(anchor.get("nodeSource") or "")[:500],
                "isShared": bool(anchor.get("isShared")),
                "protected": bool(anchor.get("protected")),
                "sharedReason": anchor.get("sharedReason") or "",
            }

            if risk in {"L2_HITL", "L3_AI_REQUIRED"}:
                if payload.options.dry_run:
                    return self._response(
                        payload,
                        status="DRY_RUN",
                        risk=risk,
                        resolved=resolved_target,
                        revision=current_revision,
                        timings=timings,
                        started=started,
                    )
                raise DirectActionError(
                    DirectErrorCode.UNSAFE_OPERATION,
                    "该组件被多处使用，直接修改可能影响其它页面，请改用 AI 修改。",
                    status_code=409,
                    diagnostic={"sharedReason": anchor.get("sharedReason") or ""},
                )

            file = str(anchor.get("file") or "")
            mark = time.perf_counter()
            old_content = self._sandbox.read_file(payload.sandbox_id, file)
            new_content = self._build_new_content(payload, anchor)
            timings["astEditMs"] = _elapsed_ms(mark)

            if payload.options.dry_run:
                timings["totalMs"] = _elapsed_ms(started)
                return self._response(
                    payload,
                    status="DRY_RUN",
                    risk=risk,
                    resolved=resolved_target,
                    revision=current_revision,
                    timings=timings,
                    started=started,
                    wouldChange=old_content != new_content,
                )

            mark = time.perf_counter()
            self._sandbox.apply_patch(payload.sandbox_id, files={file: new_content})
            timings["writeMs"] = _elapsed_ms(mark)

            if payload.options.verify_build:
                mark = time.perf_counter()
                verify = self._sandbox.verify(payload.sandbox_id, check="build")
                timings["verifyMs"] = _elapsed_ms(mark)
                if not verify.success:
                    self._rollback(payload, file, old_content)
                    raise DirectActionError(
                        DirectErrorCode.TOOL_FAILED,
                        "修改后构建未通过，已自动回滚。",
                        status_code=500,
                    )

            mark = time.perf_counter()
            try:
                commit_hash = self._sandbox.git_commit(
                    payload.sandbox_id,
                    f"direct: {payload.direct_action_id} - {payload.action}",
                )
            except SandboxError as exc:
                self._rollback(payload, file, old_content)
                raise DirectActionError(
                    DirectErrorCode.TOOL_FAILED,
                    "代码已回滚：无法创建 Git 快照。",
                    status_code=500,
                ) from exc
            timings["commitMs"] = _elapsed_ms(mark)

            mark = time.perf_counter()
            change = self._write_changelog(
                payload,
                anchor,
                old_content,
                new_content,
                commit_hash,
                risk,
            )
            timings["changelogMs"] = _elapsed_ms(mark)
            timings["totalMs"] = _elapsed_ms(started)
            return self._response(
                payload,
                status="SUCCEEDED",
                risk=risk,
                resolved=resolved_target,
                revision=current_revision,
                revision_after=commit_hash,
                commit_hash=commit_hash,
                change_id=change.change_id,
                timings=timings,
                started=started,
            )
        except ProjectBusyError as exc:
            raise DirectActionError(
                DirectErrorCode.PROJECT_BUSY,
                "项目正在生成代码，请稍后再试。",
                status_code=409,
            ) from exc
        finally:
            if lease is not None:
                project_write_lock.release(lease)

    # --------------------------------------------------------
    # 内部
    # --------------------------------------------------------
    def _resolve(self, payload: DirectActionPayload) -> dict[str, Any]:
        target = payload.target
        anchor_id = target.anchor_id or f"anc_{payload.direct_action_id}"
        request = {
            "anchorId": anchor_id,
            "annotationId": target.annotation_id or payload.direct_action_id,
            "file": target.file or "",
            "selector": target.selector or "",
            "line": target.line,
            "column": target.column,
            "component": target.component,
            "instance": target.instance,
            "instanceCount": target.instance_count,
            "callSiteFile": target.call_site_file,
            "placement": target.placement,
        }
        try:
            results = self._sandbox.resolve_targets(payload.sandbox_id, [request])
        except SandboxError as exc:
            raise DirectActionError(
                DirectErrorCode.RESOLVER_UNAVAILABLE,
                "目标解析服务暂时不可用，请稍后重试。",
                status_code=503,
            ) from exc
        if results is None:
            raise DirectActionError(
                DirectErrorCode.RESOLVER_UNAVAILABLE,
                "当前沙盒不支持确定性目标解析。",
                status_code=503,
            )
        anchor = next(
            (
                item
                for item in results
                if isinstance(item, dict)
                and str(item.get("anchorId") or "") == anchor_id
            ),
            None,
        )
        if anchor is None:
            raise DirectActionError(
                DirectErrorCode.TARGET_NOT_FOUND,
                "没有找到要修改的组件，请重新选择。",
                status_code=404,
            )
        status = str(anchor.get("status") or "")
        if status == "TARGET_AMBIGUOUS":
            raise DirectActionError(
                DirectErrorCode.TARGET_AMBIGUOUS,
                "该位置命中多个组件，请更精确地选择后再试。",
                status_code=409,
                diagnostic={
                    "candidates": anchor.get("candidates") or [],
                },
            )
        if status != "RESOLVED":
            raise DirectActionError(
                DirectErrorCode.TARGET_NOT_FOUND,
                "目标组件已失效，请重新选择。",
                status_code=404,
                diagnostic={"reason": anchor.get("reason") or ""},
            )
        return anchor

    @staticmethod
    def _risk(
        payload: DirectActionPayload,
        anchor: dict[str, Any],
    ) -> DirectActionRisk:
        if payload.action == "UPDATE_TEXT":
            if anchor.get("isShared") or anchor.get("protected"):
                return "L2_HITL"
            return "L0_DIRECT"
        # Phase 1 仅 UPDATE_TEXT 可执行；其它动作在路线图后续阶段实现。
        return "L3_AI_REQUIRED"

    def _build_new_content(
        self,
        payload: DirectActionPayload,
        anchor: dict[str, Any],
    ) -> str:
        if payload.action != "UPDATE_TEXT":
            raise DirectActionError(
                DirectErrorCode.UNSAFE_OPERATION,
                "当前版本只支持直接修改文本。",
                status_code=409,
            )
        file = str(anchor.get("file") or "")
        result = self._sandbox.apply_ast_edit(
            payload.sandbox_id,
            {
                "operation": "update_text",
                "file": file,
                "anchorRange": anchor.get("range") or {},
                "targetRange": anchor.get("range") or {},
                "text": str(payload.payload.get("text") or ""),
            },
        )
        if not result or result.get("status") != "OK":
            code = str((result or {}).get("code") or DirectErrorCode.TOOL_FAILED)
            message = str((result or {}).get("message") or "文本修改失败")
            raise DirectActionError(
                DirectErrorCode.TOOL_FAILED,
                "该组件无法安全直接修改文本，请改用 AI 修改。",
                status_code=409,
                diagnostic={"toolCode": code, "toolMessage": message[:500]},
            )
        return str(result.get("content") or "")

    def _rollback(self, payload: DirectActionPayload, file: str, old: str) -> None:
        try:
            self._sandbox.apply_patch(payload.sandbox_id, files={file: old})
        except SandboxError:
            logger.exception(
                "direct rollback failed id=%s file=%s",
                payload.direct_action_id,
                file,
            )

    def _write_changelog(
        self,
        payload: DirectActionPayload,
        anchor: dict[str, Any],
        before: str,
        after: str,
        commit_hash: str,
        risk: DirectActionRisk,
    ) -> ChangeLogEntry:
        file = str(anchor.get("file") or "")
        text = str(payload.payload.get("text") or "")
        entry = ChangeLogEntry(
            change_id=f"chg_{uuid.uuid4().hex[:20]}",
            project_id=payload.project_id,
            sandbox_id=payload.sandbox_id,
            run_id=payload.run_id,
            action=payload.action,
            target={
                "file": file,
                "component": anchor.get("component") or "",
                "anchorId": anchor.get("anchorId") or "",
                "range": anchor.get("range") or {},
            },
            summary=f"将 {anchor.get('component') or file} 的文案改为“{text}”",
            diff_summary=_diff_summary(before, after),
            before_hash=_sha256(before),
            after_hash=_sha256(after),
            commit_hash=commit_hash,
            risk=risk,
            created_at=datetime.now(timezone.utc),
        )
        self._changelog.append(entry)
        return entry

    @staticmethod
    def _response(
        payload: DirectActionPayload,
        *,
        status: str,
        risk: DirectActionRisk,
        resolved: dict[str, Any],
        revision: str,
        timings: dict[str, float],
        started: float,
        revision_after: str = "",
        commit_hash: str = "",
        change_id: str = "",
        wouldChange: bool = True,
    ) -> DirectActionResponse:
        resolved = dict(resolved)
        resolved["wouldChange"] = wouldChange
        return DirectActionResponse(
            direct_action_id=payload.direct_action_id,
            run_id=payload.run_id,
            status=status,  # type: ignore[arg-type]
            action=payload.action,
            dry_run=payload.options.dry_run,
            risk=risk,
            resolved_target=resolved,
            revision_before=revision,
            revision_after=revision_after or revision,
            commit_hash=commit_hash,
            change_id=change_id,
            latency_ms=int(_elapsed_ms(started)),
            timings={key: round(value, 2) for key, value in timings.items()},
        )


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


__all__ = [
    "DIRECT_EVENT_DECISION_REQUIRED",
    "DIRECT_EVENT_FAILED",
    "DIRECT_EVENT_STARTED",
    "DIRECT_EVENT_SUCCEEDED",
    "DirectActionError",
    "DirectOperationService",
    "get_direct_event_buffer",
]
