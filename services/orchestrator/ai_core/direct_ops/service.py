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
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from ai_core.adapters.sandbox import get_sandbox_provider
from ai_core.agent.events import RunEventBuffer
from ai_core.config import Settings, get_settings
from ai_core.direct_ops.changelog import get_change_log_store
from ai_core.direct_ops.failure_router import (
    ROUTE_LLM_CODER,
    route_add_component,
)
from ai_core.direct_ops.scope_policy import ScopeDecision, decide_scope
from ai_core.direct_ops.source_sync import SourceSnapshotSync
from ai_core.direct_ops.source_sync_queue import (
    ensure_source_sync_retry_worker,
    get_source_sync_queue,
)
from ai_core.direct_ops.static_analysis import analyze_delete_anchor
from ai_core.direct_ops.templates import (
    COMPONENT_TEMPLATES,
    apply_style_context,
    ensure_imports,
)
from ai_core.domain.node_identity import identity_from_anchor
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

# Direct Ops HITL 决策暂存：analyze-delete 生成，前端选择后消费。
# 只保存脱敏后的定位摘要，不保存完整源码；10 分钟未选择即失效。
_DECISIONS: dict[str, dict[str, Any]] = {}
_DECISIONS_LOCK = threading.RLock()
_DECISION_TTL_SECONDS = 600
_DECISION_LIMIT = 200


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


def _oid_from_selector(selector: str) -> str:
    """从 ``[data-oid="..."]`` 反推 oid；其它选择器返回空串。

    SDK 的 source.selector 对 OID 节点就是 data-oid 选择器，但 data-oid
    只存在于构建期 Source Map，并不写进源码。父容器解析必须拿到 oid
    才能走地图定位，否则命中不到任何 JSX 节点。
    """
    match = re.search(r'\[data-oid\s*=\s*"([^"]+)"\]', str(selector or ""))
    return match.group(1) if match else ""


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


def _remember_decision(decision_id: str, value: dict[str, Any]) -> None:
    """登记待决策项；顺带清理过期项，防止无界增长。"""
    now = time.time()
    with _DECISIONS_LOCK:
        expired = [
            key
            for key, item in _DECISIONS.items()
            if now - float(item.get("created_at") or 0) > _DECISION_TTL_SECONDS
        ]
        for key in expired:
            _DECISIONS.pop(key, None)
        if len(_DECISIONS) >= _DECISION_LIMIT:
            oldest = min(
                _DECISIONS,
                key=lambda key: float(_DECISIONS[key].get("created_at") or 0),
            )
            _DECISIONS.pop(oldest, None)
        _DECISIONS[decision_id] = value


def _take_decision(decision_id: str) -> dict[str, Any] | None:
    """取出并消费决策；决策一次性有效，避免重复执行。"""
    now = time.time()
    with _DECISIONS_LOCK:
        item = _DECISIONS.pop(decision_id, None)
    if item is None:
        return None
    if now - float(item.get("created_at") or 0) > _DECISION_TTL_SECONDS:
        return None
    return item


class DirectOperationService:
    def __init__(
        self,
        sandbox: SandboxProvider | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._sandbox = sandbox or get_sandbox_provider()
        self._settings = settings or get_settings()
        self._changelog = get_change_log_store()
        sync_enabled = bool(
            getattr(self._settings, "source_snapshot_sync_enabled", True)
        )
        sync_base_url = getattr(self._settings, "bff_base_url", "")
        sync_timeout = float(
            getattr(self._settings, "source_snapshot_sync_timeout_seconds", 3.0)
        )
        queue_enabled = sync_enabled and bool(
            getattr(self._settings, "source_snapshot_queue_enabled", True)
        )
        self._source_queue = (
            get_source_sync_queue(
                getattr(
                    self._settings,
                    "source_snapshot_queue_path",
                    "data/source-sync-queue.json",
                )
            )
            if queue_enabled
            else None
        )
        self._source_sync = SourceSnapshotSync(
            base_url=sync_base_url,
            timeout_seconds=sync_timeout,
            enabled=sync_enabled,
            queue=self._source_queue,
        )
        self._source_sync_base_url = sync_base_url
        self._source_sync_timeout = sync_timeout
        self._source_sync_async = (
            bool(getattr(self._settings, "source_snapshot_async", True))
            and self._source_queue is not None
        )
        if queue_enabled:
            ensure_source_sync_retry_worker(
                self._source_queue,
                sync_factory=lambda: SourceSnapshotSync(
                    base_url=sync_base_url,
                    timeout_seconds=sync_timeout,
                    enabled=True,
                    queue=None,
                ),
                sandbox_provider=self._sandbox,
                interval_seconds=float(
                    getattr(
                        self._settings,
                        "source_snapshot_retry_interval_seconds",
                        15.0,
                    )
                ),
            )

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
    # 静态分析（不写代码）
    # --------------------------------------------------------
    def analyze_delete(self, payload: DirectActionPayload) -> dict[str, Any]:
        """只分析删除安全性，不执行删除。"""
        anchor = self._resolve(payload)
        identity = identity_from_anchor(anchor)
        scope = decide_scope("DELETE_NODE", identity)
        analysis = analyze_delete_anchor(dict(anchor))
        result = analysis.to_dict()
        result["safe"] = scope.allowed
        result["reasons"] = list(scope.reasons)
        result["scope"] = scope.scope
        result["strategy"] = scope.strategy
        result["options"] = list(scope.options)
        result["identity"] = identity.to_dict()
        # 静态数组的循环项：直接按“删除当前数据项”处理，不进入 HITL
        # 安全路径面板。execute 会把 DELETE_NODE 自动转成 DELETE_DATA_ITEM。
        map_source = anchor.get("mapSource") or {}
        if anchor.get("inMap") and str(map_source.get("kind") or "") in {
            "array_literal",
            "variable_array",
        }:
            result["safe"] = True
            result["scope"] = "data_source"
            result["strategy"] = "direct"
            result["reasons"] = ["将只删除当前数据项"]
            result["options"] = []
            result["autoAction"] = "DELETE_DATA_ITEM"
        call_site = anchor.get("callSiteAnchor")
        anchor_summary = {
            "file": anchor.get("file"),
            "component": anchor.get("component"),
            "range": anchor.get("range"),
            "nodeSource": str(anchor.get("nodeSource") or "")[:500],
            "isShared": bool(anchor.get("isShared")),
            "protected": bool(anchor.get("protected")),
            "inMap": bool(anchor.get("inMap")),
            "isRouteRoot": bool(anchor.get("isRouteRoot")),
            "deleteSafe": bool(anchor.get("deleteSafe")),
            "mapSource": anchor.get("mapSource") or None,
            "callSiteFile": anchor.get("callSiteFile") or "",
            "hasCallSite": isinstance(call_site, dict) and bool(call_site.get("file")),
            "instanceCount": int(anchor.get("instanceCount") or 1),
            "importerCount": int(anchor.get("importerCount") or 0),
            "sharedByInstance": bool(anchor.get("sharedByInstance")),
            "sharedByImport": bool(anchor.get("sharedByImport")),
            "sharedReason": anchor.get("sharedReason") or "",
        }
        result["anchor"] = anchor_summary
        if scope.strategy == "hitl" and scope.options and not result.get("safe"):
            decision_id = f"ddec_{uuid.uuid4().hex[:20]}"
            _remember_decision(
                decision_id,
                {
                    "payload": payload.model_dump(by_alias=True),
                    "anchor": anchor_summary,
                    "identity": identity.to_dict(),
                    "scope": scope.scope,
                    "options": list(scope.options),
                    "created_at": time.time(),
                },
            )
            result["decisionId"] = decision_id
        return result

    def decide(
        self,
        decision_id: str,
        option_id: str,
        comment: str = "",
    ) -> dict[str, Any]:
        """消费一次 Direct Ops HITL 决策。

        目前只执行无副作用的 VISUAL_ONLY/CANCEL；需要改数据源或组件定义的
        选项统一返回 HANDOFF_TO_AI，由前端显式转交架构师，绝不静默失败。
        """
        pending = _take_decision(decision_id)
        if pending is None:
            raise DirectActionError(
                DirectErrorCode.ACTION_INVALID,
                "该决策已过期或不存在，请重新操作",
                status_code=409,
            )
        option = next(
            (
                item
                for item in pending.get("options") or []
                if str(item.get("optionId")) == option_id
            ),
            None,
        )
        if option is None:
            raise DirectActionError(
                DirectErrorCode.ACTION_INVALID,
                f"未知的决策选项: {option_id}",
                status_code=400,
            )

        action = str(option.get("action") or "").upper()
        base = {
            "decisionId": decision_id,
            "optionId": option_id,
            "scope": pending.get("scope") or "",
        }
        if action == "CANCEL":
            return {
                **base,
                "status": "CANCELLED",
                "applied": False,
                "committed": False,
                "message": "已取消",
            }
        if action == "VISUAL_ONLY":
            change_id = self._record_visual_only(pending, option, comment)
            return {
                **base,
                "status": "VISUAL_ONLY",
                "scope": "visual_only",
                "applied": False,
                "committed": False,
                "changeId": change_id,
                "message": "已隐藏当前项（仅视觉，未修改代码）",
            }
        if (
            action == "AI_DATA_EDIT"
            and str(option.get("optionId")) == "remove_data_item"
        ):
            try:
                response = self._delete_data_item(pending)
            except DirectActionError as exc:
                if exc.code in {
                    DirectErrorCode.ROUTE_TO_LLM,
                    DirectErrorCode.TARGET_NOT_FOUND,
                }:
                    return {
                        **base,
                        "status": "HANDOFF_TO_AI",
                        "applied": False,
                        "committed": False,
                        "message": f"{exc.message}（已保留目标信息）",
                    }
                raise
            if response.status == "HITL_REQUIRED":
                return {
                    **base,
                    "status": "HANDOFF_TO_AI",
                    "applied": False,
                    "committed": False,
                    "message": "数据源不是静态数组，需要 AI 定位并修改",
                }
            return {
                **base,
                "status": response.status,
                "applied": response.status == "SUCCEEDED",
                "committed": bool(response.commit_hash),
                "changeId": response.change_id,
                "commitHash": response.commit_hash,
                "message": (
                    "已删除数据源中的对应项"
                    if response.status == "SUCCEEDED"
                    else "删除数据项未完成"
                ),
            }
        return {
            **base,
            "status": "HANDOFF_TO_AI",
            "applied": False,
            "committed": False,
            "message": "该操作需要 AI 修改数据源或组件定义，已保留目标信息",
        }

    def _delete_data_item(
        self,
        pending: dict[str, Any],
    ) -> DirectActionResponse:
        """把 map 删除决策转成一次 DELETE_DATA_ITEM Direct Ops。"""
        original = dict(pending.get("payload") or {})
        sandbox_id = str(original.get("sandboxId") or "")
        if not sandbox_id:
            raise DirectActionError(
                DirectErrorCode.TARGET_NOT_FOUND,
                "缺少沙盒，无法删除数据项",
                status_code=404,
            )
        revision = self._sandbox.git_head(sandbox_id)
        action_id = f"dact_data_{uuid.uuid4().hex[:16]}"
        original["directActionId"] = action_id
        original["idempotencyKey"] = f"{original.get('projectId')}:{action_id}"
        original["action"] = "DELETE_DATA_ITEM"
        original["baseRevision"] = revision
        original["payload"] = {}
        original["options"] = {
            "source": "HITL_DECISION",
            "dryRun": False,
        }
        return self.execute(DirectActionPayload.model_validate(original))

    def _record_visual_only(
        self,
        pending: dict[str, Any],
        option: dict[str, Any],
        comment: str,
    ) -> str:
        """记录一条 applied=false 的视觉变更，便于 Architect 回顾。"""
        payload = pending.get("payload") or {}
        anchor = pending.get("anchor") or {}
        component = anchor.get("component") or anchor.get("file") or "目标节点"
        change_id = f"chg_{uuid.uuid4().hex[:20]}"
        entry = ChangeLogEntry(
            change_id=change_id,
            project_id=str(payload.get("projectId") or ""),
            sandbox_id=str(payload.get("sandboxId") or ""),
            run_id=payload.get("runId"),
            action="DELETE_NODE",
            target={
                "file": anchor.get("file") or "",
                "component": anchor.get("component") or "",
                "range": anchor.get("range") or {},
                "nodeSource": anchor.get("nodeSource") or "",
            },
            summary=f"仅视觉隐藏 {component}（未修改代码）",
            diff_summary=(f"用户选择：{option.get('label') or ''}\n{comment}".strip()),
            risk="L2_HITL",
            applied=False,
            visual_only=True,
            created_at=datetime.now(timezone.utc),
        )
        self._changelog.append(entry)
        return change_id

    def undo_last(self, project_id: str, sandbox_id: str) -> dict[str, Any]:
        """撤回最近一次已落盘的 Direct Op。

        只允许 HEAD 仍等于该动作的 commit 时执行，避免把用户后续的
        AI 提交一起 reset 掉。撤回本身也写一条 ChangeLog。
        """
        entries = [
            item
            for item in self._changelog.list_for_project(project_id, 50)
            if item.applied
            and not item.visual_only
            and item.commit_hash
            and item.before_commit
        ]
        if not entries:
            raise DirectActionError(
                DirectErrorCode.TOOL_FAILED,
                "没有可撤回的直接操作。",
                status_code=409,
            )
        entry = entries[-1]
        handle = self._sandbox.get(sandbox_id)
        if handle.project_id != project_id:
            raise DirectActionError(
                DirectErrorCode.TARGET_NOT_FOUND,
                "沙盒不属于当前项目",
                status_code=404,
            )
        try:
            lease = project_write_lock.acquire(
                project_id,
                owner=f"direct-undo:{project_id}",
                timeout=self._settings.direct_ops_lock_timeout_seconds,
            )
        except ProjectBusyError as exc:
            raise DirectActionError(
                DirectErrorCode.PROJECT_BUSY,
                "项目正在写入，请稍后再试。",
                status_code=409,
            ) from exc
        try:
            head = self._sandbox.git_head(sandbox_id)
            if head != entry.commit_hash:
                raise DirectActionError(
                    DirectErrorCode.REVISION_MISMATCH,
                    "最近一次修改之后又有新的代码提交，无法安全撤回。",
                    status_code=409,
                    diagnostic={
                        "expectedRevision": entry.commit_hash,
                        "actualRevision": head,
                    },
                )
            self._sandbox.git_reset_hard(sandbox_id, entry.before_commit)
            undo_entry = ChangeLogEntry(
                change_id=f"chg_{uuid.uuid4().hex[:20]}",
                project_id=project_id,
                sandbox_id=sandbox_id,
                run_id=entry.run_id,
                action=entry.action,
                target=dict(entry.target),
                summary=f"撤回：{entry.summary}",
                diff_summary="",
                before_hash=entry.after_hash,
                after_hash=entry.before_hash,
                commit_hash=entry.before_commit,
                before_commit=entry.commit_hash,
                risk="L1_VERIFY_DIRECT",
                applied=False,
                visual_only=False,
                created_at=datetime.now(timezone.utc),
            )
            self._changelog.append(undo_entry)
            return {
                "undoneChangeId": entry.change_id,
                "undoChangeId": undo_entry.change_id,
                "restoredCommit": entry.before_commit,
                "summary": entry.summary,
            }
        finally:
            project_write_lock.release(lease)

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
                f"直接操作执行失败（{type(exc).__name__}）：{exc}",
                status_code=500,
                diagnostic={
                    "exceptionType": type(exc).__name__,
                    "error": str(exc)[:500],
                    "action": payload.action,
                },
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
        # 入口分流必须先于沙盒 / 锚点访问：目录外组件直接转交 AI Coder，
        # 不能因为沙盒暂时不可用而被误报为 Direct 工具失败。
        if payload.action == "ADD_COMPONENT":
            route = route_add_component(str(payload.payload.get("componentType") or ""))
            if route.route == ROUTE_LLM_CODER:
                raise DirectActionError(
                    DirectErrorCode.ROUTE_TO_LLM,
                    "该组件不在直接新增白名单内，需要交给 AI Coder 生成。",
                    status_code=422,
                    diagnostic={
                        "route": route.route,
                        "componentType": route.component_type,
                        "reason": route.reason,
                    },
                )

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
            # 循环模板的静态数组：删除单个渲染项等于删除一个数据元素。
            # 这是可确定的低级操作，直接执行，不弹“安全路径”决策面板。
            if payload.action == "DELETE_NODE" and anchor.get("inMap"):
                map_source = anchor.get("mapSource") or {}
                if str(map_source.get("kind") or "") in {
                    "array_literal",
                    "variable_array",
                }:
                    payload = payload.model_copy(
                        update={"action": "DELETE_DATA_ITEM", "payload": {}}
                    )
            parent = (
                self._resolve_parent(payload, anchor)
                if payload.action == "MOVE_NODE"
                else None
            )
            timings["resolveMs"] = _elapsed_ms(mark)
            move_anchor = (
                self._move_source_anchor(payload, anchor)
                if payload.action == "MOVE_NODE"
                else anchor
            )
            if payload.action == "DELETE_NODE":
                move_anchor = self._delete_source_anchor(anchor)
            identity = identity_from_anchor(anchor)
            scope = decide_scope(payload.action, identity)
            risk = "L0_DIRECT" if scope.allowed else "L2_HITL"
            if scope.strategy == "blocked":
                risk = "L3_AI_REQUIRED"
            if payload.action == "MOVE_NODE" and parent is not None:
                if parent.get("file") != move_anchor.get("file"):
                    scope = ScopeDecision(
                        allowed=False,
                        scope="blocked",
                        strategy="blocked",
                        reasons=["跨文件移动需要 AI 处理"],
                    )
                    risk = "L3_AI_REQUIRED"
                parent_scope = str(parent.get("containerScope") or "local")
                if parent_scope in {
                    "component_definition",
                    "repeated_template",
                }:
                    scope = ScopeDecision(
                        allowed=False,
                        scope=(
                            "all_instances"
                            if parent_scope == "repeated_template"
                            else "definition"
                        ),
                        strategy="hitl",
                        reasons=[
                            (
                                "落点是组件定义内部，移动会影响所有实例"
                                if parent_scope == "component_definition"
                                else "落点是循环模板，移动会影响所有实例"
                            ),
                        ],
                        options=[
                            {
                                "optionId": "edit_template",
                                "label": "改模板（影响全部实例）",
                                "action": "DEFINITION",
                                "description": "需要 AI 修改组件定义或循环模板",
                            },
                            {
                                "optionId": "cancel",
                                "label": "取消",
                                "action": "CANCEL",
                                "description": "",
                            },
                        ],
                    )
                    risk = "L2_HITL"
            resolved_target = {
                "identity": identity.to_dict(),
                "scope": scope.scope,
                "strategy": scope.strategy,
                "file": move_anchor.get("file"),
                "component": move_anchor.get("component") or anchor.get("component"),
                "range": anchor.get("range"),
                "nodeType": anchor.get("nodeType"),
                "nodeSource": str(anchor.get("nodeSource") or "")[:500],
                "isShared": bool(anchor.get("isShared")),
                "protected": bool(anchor.get("protected")),
                "sharedReason": anchor.get("sharedReason") or "",
            }
            if parent is not None:
                resolved_target["parent"] = {
                    "file": parent.get("file"),
                    "component": parent.get("component"),
                    "range": parent.get("range"),
                    "isShared": bool(parent.get("isShared")),
                    "protected": bool(parent.get("protected")),
                    "containerScope": parent.get("containerScope") or "local",
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
                # 模板 / 多实例修改：返回可选范围，由前端弹出 HITL 面板，
                # 不再直接把“会影响全部实例”简化成一条错误 toast。
                if scope.strategy == "hitl" and scope.options:
                    decision_id = f"ddec_{uuid.uuid4().hex[:20]}"
                    _remember_decision(
                        decision_id,
                        {
                            "payload": payload.model_dump(by_alias=True),
                            "anchor": {
                                "file": anchor.get("file"),
                                "component": anchor.get("component"),
                                "range": anchor.get("range"),
                                "nodeSource": str(anchor.get("nodeSource") or "")[:500],
                            },
                            "identity": identity.to_dict(),
                            "scope": scope.scope,
                            "options": list(scope.options),
                            "created_at": time.time(),
                        },
                    )
                    timings["totalMs"] = _elapsed_ms(started)
                    return self._response(
                        payload,
                        status="HITL_REQUIRED",
                        risk=risk,
                        resolved=resolved_target,
                        revision=current_revision,
                        timings=timings,
                        started=started,
                        decision_id=decision_id,
                        options=list(scope.options),
                        reasons=list(scope.reasons),
                    )
                shared_reason = str(anchor.get("sharedReason") or "")
                policy_reason = "；".join(scope.reasons)
                if policy_reason:
                    shared_reason = f"{policy_reason}；{shared_reason}".strip("；")
                call_site_file = str(anchor.get("callSiteFile") or "")
                parent_file = str((parent or {}).get("file") or "")
                parent_shared = bool(
                    (parent or {}).get("isShared") or (parent or {}).get("protected")
                )
                detail = (
                    f"risk={risk} source={anchor.get('file') or ''} "
                    f"parent={parent_file} callSite={call_site_file or '(none)'} "
                    f"parentShared={parent_shared} {shared_reason}"
                )
                action_label = "移动" if payload.action == "MOVE_NODE" else "修改"
                raise DirectActionError(
                    DirectErrorCode.UNSAFE_OPERATION,
                    f"该组件无法直接{action_label}：" + detail.strip(),
                    status_code=409,
                    diagnostic={
                        "risk": risk,
                        "sourceFile": anchor.get("file") or "",
                        "parentFile": parent_file,
                        "callSiteFile": call_site_file,
                        "parentShared": parent_shared,
                        "sharedReason": shared_reason,
                    },
                )

            file = str(move_anchor.get("file") or "")
            mark = time.perf_counter()
            old_content = self._sandbox.read_file(payload.sandbox_id, file)
            new_content = self._build_new_content(payload, move_anchor, parent, scope)
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
                move_anchor,
                old_content,
                new_content,
                commit_hash,
                risk,
                before_commit=current_revision,
            )
            timings["changelogMs"] = _elapsed_ms(mark)

            # Git commit 已成功：立即把最新源码同步到 BFF 快照。
            # 同步失败不回滚用户修改，但在响应中给出可观测状态。
            mark = time.perf_counter()
            source_sync = self._sync_source_snapshot(
                payload=payload,
                touched_file=file,
                commit_hash=commit_hash,
            )
            timings["sourceSyncMs"] = _elapsed_ms(mark)
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
                source_sync=source_sync,
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
            "oid": target.oid or "",
            "sourceRange": dict(target.source_range or {}) or None,
            "callSiteOid": target.call_site_oid or "",
            "instancePath": list(target.instance_path or []),
            "instanceKey": target.instance_key or "",
            "file": target.file or "",
            "selector": target.selector or "",
            "line": target.line,
            "column": target.column,
            "component": target.component,
            "instance": target.instance,
            "instanceCount": target.instance_count,
            "callSiteFile": target.call_site_file,
            "identity": dict(target.identity or {}),
            "placement": target.placement,
        }
        results = None
        last_error: SandboxError | None = None
        # HMR / 其它直接操作可能在短暂窗口内重建 Vite 模块；解析器偶发
        # 失败时重试一次，仍失败才对外报 RESOLVER_UNAVAILABLE。
        for attempt in range(2):
            try:
                results = self._sandbox.resolve_targets(payload.sandbox_id, [request])
                break
            except SandboxError as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.08)
        if results is None and last_error is not None:
            logger.warning(
                "direct resolver unavailable sandbox=%s error=%s",
                payload.sandbox_id,
                last_error,
                exc_info=True,
            )
            raise DirectActionError(
                DirectErrorCode.RESOLVER_UNAVAILABLE,
                f"目标解析服务暂时不可用：{last_error}",
                status_code=503,
                diagnostic={
                    "resolverError": str(last_error)[:500],
                    "file": target.file or "",
                    "line": target.line,
                    "column": target.column,
                },
            ) from last_error
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

    def _resolve_parent(
        self,
        payload: DirectActionPayload,
        source: dict[str, Any],
    ) -> dict[str, Any]:
        """按 placement 定位移动目标容器；不再让模型猜测父级。"""
        placement = payload.target.placement or {}
        parent_selector = str(placement.get("parentSelector") or "")
        parent_anchor_id = f"anc_{payload.direct_action_id}_parent"
        request = {
            "anchorId": parent_anchor_id,
            "annotationId": f"{payload.direct_action_id}_parent",
            # Source map 节点在源码里没有 data-oid 属性，只有构建期地图。
            # 因此 [data-oid="..."] 选择器必须同时带 oid，否则父容器永远
            # 解析不到，表现为“移动成功但组件没动 / 找不到落点”。
            "oid": placement.get("parentOid") or _oid_from_selector(parent_selector),
            "file": placement.get("parentFile")
            or payload.target.file
            or source.get("file")
            or "",
            "selector": parent_selector,
            "callSiteOid": placement.get("parentCallSiteOid") or "",
            "line": placement.get("parentLine"),
            "column": placement.get("parentColumn"),
            "instance": placement.get("parentInstance"),
            "instanceCount": placement.get("parentInstanceCount"),
        }
        try:
            results = self._sandbox.resolve_targets(payload.sandbox_id, [request])
        except SandboxError as exc:
            raise DirectActionError(
                DirectErrorCode.RESOLVER_UNAVAILABLE,
                "目标容器解析服务暂时不可用，请稍后重试。",
                status_code=503,
            ) from exc
        parent = next(
            (
                item
                for item in results or []
                if isinstance(item, dict)
                and str(item.get("anchorId") or "") == parent_anchor_id
            ),
            None,
        )
        if parent is None or str(parent.get("status") or "") != "RESOLVED":
            raise DirectActionError(
                DirectErrorCode.TARGET_NOT_FOUND,
                "没有找到要移动到的容器，请重新拖拽落位。",
                status_code=404,
            )
        return parent

    @staticmethod
    def _move_source_anchor(
        payload: DirectActionPayload,
        anchor: dict[str, Any],
    ) -> dict[str, Any]:
        """共享组件的移动必须落在 callSite，不修改公共组件定义本身。"""
        if payload.action != "MOVE_NODE":
            return anchor
        call_site = anchor.get("callSiteAnchor")
        if not isinstance(call_site, dict):
            return anchor
        if not call_site.get("file") or not call_site.get("range"):
            return anchor
        merged = dict(call_site)
        merged.setdefault("component", anchor.get("component") or "")
        return merged

    @staticmethod
    def _delete_source_anchor(anchor: dict[str, Any]) -> dict[str, Any]:
        """共享组件删除当前 callSite 实例，不删除公共组件定义。"""
        call_site = anchor.get("callSiteAnchor")
        if not isinstance(call_site, dict):
            return anchor
        if not call_site.get("file") or not call_site.get("range"):
            return anchor
        merged = dict(call_site)
        merged.setdefault("component", anchor.get("component") or "")
        return merged

    def _sync_source_snapshot(
        self,
        *,
        payload: DirectActionPayload,
        touched_file: str,
        commit_hash: str,
    ) -> dict[str, Any]:
        """Git commit 已完成；快照同步可异步执行。

        同步任务先落盘到补偿队列，再由后台线程立即尝试。即使进程在此
        期间退出，队列仍会在下次启动时重试，不会丢用户代码。
        """
        task = {
            "projectId": payload.project_id,
            "sandboxId": payload.sandbox_id,
            "tenantId": payload.tenant_id,
            "touchedFile": touched_file,
            "commitHash": commit_hash,
        }
        if not self._source_sync_async:
            return self._source_sync.sync(
                project_id=payload.project_id,
                sandbox_id=payload.sandbox_id,
                tenant_id=payload.tenant_id,
                touched_file=touched_file,
                sandbox=self._sandbox,
                commit_hash=commit_hash,
            )

        queue = self._source_queue
        try:
            key = queue.add(task)
        except Exception as exc:  # pragma: no cover - 队列落盘失败降级同步
            logger.warning("source sync enqueue failed: %s", exc)
            return self._source_sync.sync(
                project_id=payload.project_id,
                sandbox_id=payload.sandbox_id,
                tenant_id=payload.tenant_id,
                touched_file=touched_file,
                sandbox=self._sandbox,
                commit_hash=commit_hash,
            )

        def worker() -> None:
            sync = SourceSnapshotSync(
                base_url=self._source_sync_base_url,
                timeout_seconds=self._source_sync_timeout,
                enabled=True,
                queue=None,
            )
            try:
                result = sync.sync(
                    project_id=payload.project_id,
                    sandbox_id=payload.sandbox_id,
                    tenant_id=payload.tenant_id,
                    touched_file=touched_file,
                    sandbox=self._sandbox,
                    commit_hash=commit_hash,
                )
            except Exception as exc:  # noqa: BLE001 - 后台线程必须收敛
                result = {"status": "FAILED", "message": str(exc)}
            status = str(result.get("status") or "")
            if status in {"SYNCED", "SKIPPED", "DISABLED"}:
                queue.remove(key)
                logger.info(
                    "source sync async succeeded project=%s key=%s",
                    payload.project_id,
                    key,
                )
            else:
                queue.reschedule(key, str(result.get("message") or result))
                logger.warning(
                    "source sync async failed project=%s key=%s error=%s",
                    payload.project_id,
                    key,
                    result.get("message"),
                )

        threading.Thread(
            target=worker,
            name=f"source-sync-{payload.direct_action_id}",
            daemon=True,
        ).start()
        return {
            "status": "PENDING",
            "async": True,
            "queued": True,
            "queueKey": key,
        }

    @staticmethod
    def _risk(
        payload: DirectActionPayload,
        anchor: dict[str, Any],
        parent: dict[str, Any] | None = None,
    ) -> DirectActionRisk:
        if payload.action == "UPDATE_TEXT":
            if anchor.get("isShared") or anchor.get("protected"):
                return "L2_HITL"
            return "L0_DIRECT"
        if payload.action == "MOVE_NODE":
            if parent is None:
                return "L3_AI_REQUIRED"
            call_site = anchor.get("callSiteAnchor")
            if isinstance(call_site, dict) and call_site.get("file"):
                # 共享组件只在当前调用点做实例级移动，绝不改定义文件。
                if parent.get("file") != call_site.get("file"):
                    return "L3_AI_REQUIRED"
                if parent.get("isShared") or parent.get("protected"):
                    return "L2_HITL"
                return "L0_DIRECT"
            if parent.get("file") != anchor.get("file"):
                return "L3_AI_REQUIRED"
            # 同一源码渲染多次：移动源节点会确定性移动全部实例；
            # 只要不是 import 定义、不在 map、不是页面根，就允许执行。
            if (
                int(anchor.get("instanceCount") or 1) > 1
                and int(anchor.get("importerCount") or 0) == 0
                and not anchor.get("inMap")
                and not anchor.get("isRouteRoot")
            ):
                return "L0_DIRECT"
            if any(
                item.get("isShared") or item.get("protected")
                for item in (anchor, parent)
            ):
                return "L2_HITL"
            return "L0_DIRECT"
        if payload.action == "ADD_COMPONENT":
            if anchor.get("isShared") or anchor.get("protected"):
                return "L2_HITL"
            return "L0_DIRECT"
        if payload.action == "DELETE_NODE":
            safe = anchor.get("deleteSafe")
            if safe is True:
                return "L0_DIRECT"
            call_site = anchor.get("callSiteAnchor")
            if isinstance(call_site, dict) and call_site.get("file"):
                # 删除的是当前调用点的实例 JSX，不触碰公共组件定义。
                return "L0_DIRECT"
            # 仅因同一源码渲染多次：删除源节点会确定性移除全部实例，
            # 允许用户在确认后执行；import 出来的组件定义仍按 callSite /
            # HITL 处理。
            if (
                int(anchor.get("instanceCount") or 1) > 1
                and int(anchor.get("importerCount") or 0) == 0
                and not anchor.get("inMap")
                and not anchor.get("isRouteRoot")
            ):
                return "L0_DIRECT"
            if safe is False:
                return "L2_HITL"
            # 旧 Resolver / 测试替身没有 deleteSafe 字段时按同一规则兜底。
            if (
                anchor.get("isShared")
                or anchor.get("protected")
                or anchor.get("inMap")
                or anchor.get("isRouteRoot")
            ):
                return "L2_HITL"
            return "L0_DIRECT"
        return "L3_AI_REQUIRED"

    def _build_new_content(
        self,
        payload: DirectActionPayload,
        anchor: dict[str, Any],
        parent: dict[str, Any] | None = None,
        scope: ScopeDecision | None = None,
    ) -> str:
        file = str(anchor.get("file") or "")
        if payload.action == "UPDATE_TEXT":
            edit = {
                "operation": "update_text",
                "file": file,
                "anchorRange": anchor.get("range") or {},
                "targetRange": anchor.get("range") or {},
                "text": str(payload.payload.get("text") or ""),
            }
        elif payload.action == "UPDATE_PROPS":
            edit = {
                "operation": "update_props",
                "file": file,
                "anchorRange": anchor.get("range") or {},
                "props": dict(payload.payload.get("props") or {}),
            }
        elif payload.action == "UPDATE_CLASS":
            edit = {
                "operation": "update_props",
                "file": file,
                "anchorRange": anchor.get("range") or {},
                "props": {"className": str(payload.payload.get("className") or "")},
            }
        elif payload.action == "UPDATE_STYLE":
            edit = {
                "operation": "update_style",
                "file": file,
                "anchorRange": anchor.get("range") or {},
                "style": dict(payload.payload.get("style") or {}),
            }
        elif payload.action == "DELETE_DATA_ITEM":
            map_source = anchor.get("mapSource") or {}
            if map_source.get("kind") not in {
                "array_literal",
                "variable_array",
            }:
                raise DirectActionError(
                    DirectErrorCode.ROUTE_TO_LLM,
                    "数据源不是静态数组，需要交给 AI 修改。",
                    status_code=422,
                )
            instance = int(anchor.get("instance") or 1)
            item_index = instance - 1
            count = int(map_source.get("count") or 0)
            if item_index < 0 or (count and item_index >= count):
                raise DirectActionError(
                    DirectErrorCode.TARGET_NOT_FOUND,
                    "无法定位要删除的数据项，请刷新后重试。",
                    status_code=404,
                )
            edit = {
                "operation": "delete_array_item",
                "file": file,
                "anchorRange": anchor.get("range") or {},
                "arrayRange": {
                    "startOffset": int(map_source.get("start") or 0),
                    "endOffset": int(map_source.get("end") or 0),
                },
                "itemIndex": item_index,
            }
        elif payload.action == "MOVE_NODE":
            if parent is None:
                raise DirectActionError(
                    DirectErrorCode.UNSAFE_OPERATION,
                    "没有找到移动目标容器，请重新拖拽落位。",
                    status_code=409,
                )
            if parent.get("file") != file:
                raise DirectActionError(
                    DirectErrorCode.UNSAFE_OPERATION,
                    "暂不支持跨文件移动组件，请改用 AI 修改。",
                    status_code=409,
                )
            placement = payload.target.placement or {}
            position = str(placement.get("insertPosition") or "inside_index")
            if position not in {
                "inside_start",
                "inside_end",
                "inside_index",
            }:
                position = "inside_end"
            raw_index = placement.get("siblingIndex")
            index = (
                int(raw_index)
                if isinstance(raw_index, (int, float)) and int(raw_index) >= 0
                else None
            )
            if position == "inside_index" and index is None:
                index = 0
            edit = {
                "operation": "move_node",
                "file": file,
                "anchorRange": anchor.get("range") or {},
                "targetRange": parent.get("range") or {},
                "insertPosition": position,
                "insertIndex": index,
            }
        elif payload.action == "ADD_COMPONENT":
            component_type = str(payload.payload.get("componentType") or "")
            template = COMPONENT_TEMPLATES.get(component_type)
            if template is None:
                raise DirectActionError(
                    DirectErrorCode.UNSAFE_OPERATION,
                    "该组件不在直接新增白名单内。",
                    status_code=409,
                )
            placement = payload.target.placement or {}
            position = str(placement.get("insertPosition") or "inside_end")
            if position not in {
                "inside_start",
                "inside_end",
                "inside_index",
            }:
                position = "inside_end"
            raw_index = placement.get("siblingIndex")
            index = (
                int(raw_index)
                if isinstance(raw_index, (int, float)) and int(raw_index) >= 0
                else None
            )
            if position == "inside_index" and index is None:
                index = 0
            jsx_code = apply_style_context(
                template.jsx,
                payload.payload.get("styleContext"),
            )
            # 新节点必须立刻拥有可回解的身份：oid 给 SDK / source map，
            # node-id 给生成工程 resolver；两者都由本次动作唯一派生。
            identity = hashlib.sha256(
                f"{payload.project_id}:{payload.direct_action_id}".encode()
            ).hexdigest()[:16]
            jsx_code = (
                jsx_code.replace("__VIBE_OID__", f"oid_direct_{identity}")
                .replace("__VIBE_NODE_ID__", f"nd_direct_{identity}")
                .replace("__VIBE_SOURCE_FILE__", file)
            )
            edit = {
                "operation": "insert",
                "file": file,
                "anchorRange": anchor.get("range") or {},
                "insertPosition": position,
                "insertIndex": index,
                "jsxCode": jsx_code,
            }
        elif payload.action == "DELETE_NODE":
            if scope is not None and scope.strategy == "replace_placeholder":
                edit = {
                    "operation": "modify",
                    "file": file,
                    "anchorRange": anchor.get("range") or {},
                    "targetRange": anchor.get("range") or {},
                    "jsxCode": ('<div className="empty-placeholder">空节点</div>'),
                }
            else:
                edit = {
                    "operation": "delete",
                    "file": file,
                    "anchorRange": anchor.get("range") or {},
                    "targetRange": anchor.get("range") or {},
                }
        else:
            raise DirectActionError(
                DirectErrorCode.UNSAFE_OPERATION,
                "当前版本只支持直接修改文本、移动与新增基础组件。",
                status_code=409,
            )
        result = self._sandbox.apply_ast_edit(payload.sandbox_id, edit)
        if not result or result.get("status") != "OK":
            code = str((result or {}).get("code") or DirectErrorCode.TOOL_FAILED)
            message = str((result or {}).get("message") or "文本修改失败")
            raise DirectActionError(
                DirectErrorCode.TOOL_FAILED,
                "该组件无法安全直接修改，请改用 AI 修改。",
                status_code=409,
                diagnostic={"toolCode": code, "toolMessage": message[:500]},
            )
        content = str(result.get("content") or "")
        if payload.action == "ADD_COMPONENT":
            component_type = str(payload.payload.get("componentType") or "")
            template = COMPONENT_TEMPLATES[component_type]
            before = result.get("nodeCountBefore")
            after = result.get("nodeCountAfter")
            if isinstance(before, int) and isinstance(after, int):
                if after - before != template.node_count:
                    raise DirectActionError(
                        DirectErrorCode.TOOL_FAILED,
                        "新增组件的结构校验未通过，代码已保持原样。",
                        status_code=409,
                        diagnostic={
                            "nodeCountBefore": before,
                            "nodeCountAfter": after,
                            "expectedDelta": template.node_count,
                        },
                    )
            else:
                raise DirectActionError(
                    DirectErrorCode.TOOL_FAILED,
                    "新增组件缺少节点数量校验结果，已阻断。",
                    status_code=409,
                )
            content = ensure_imports(content, template.imports)
        if payload.action == "DELETE_NODE" and not (
            scope is not None and scope.strategy == "replace_placeholder"
        ):
            before = result.get("nodeCountBefore")
            after = result.get("nodeCountAfter")
            target_nodes = result.get("targetNodeCount")
            if not (
                isinstance(before, int)
                and isinstance(after, int)
                and isinstance(target_nodes, int)
            ):
                raise DirectActionError(
                    DirectErrorCode.TOOL_FAILED,
                    "删除组件缺少节点数量校验结果，已阻断。",
                    status_code=409,
                )
            removed = before - after
            if target_nodes < 1 or removed != target_nodes:
                raise DirectActionError(
                    DirectErrorCode.TOOL_FAILED,
                    "删除的节点范围与目标不一致，代码已保持原样。",
                    status_code=409,
                    diagnostic={
                        "nodeCountBefore": before,
                        "nodeCountAfter": after,
                        "removed": removed,
                        "targetNodeCount": target_nodes,
                    },
                )
        return content

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
        *,
        before_commit: str = "",
    ) -> ChangeLogEntry:
        file = str(anchor.get("file") or "")
        text = str(payload.payload.get("text") or "")
        component = anchor.get("component") or file
        placement = payload.target.placement or {}
        if payload.action == "UPDATE_TEXT":
            summary = f"将 {component} 的文案改为“{text}”"
        elif payload.action == "MOVE_NODE":
            summary = (
                f"将 {component} 移动到 "
                f"{placement.get('parentSelector') or placement.get('parentFile') or '新容器'}"
            )
        elif payload.action == "ADD_COMPONENT":
            summary = (
                f"在 {component} 中新增 "
                f"{payload.payload.get('componentType') or '基础组件'}"
            )
        elif payload.action == "DELETE_NODE":
            node_preview = " ".join(str(anchor.get("nodeSource") or "").split())[:80]
            summary = (
                f"删除 {component} 内的节点 {node_preview}"
                if node_preview
                else f"删除 {component} 内的节点"
            )
        elif payload.action == "UPDATE_CLASS":
            summary = f"更新 {component} 的样式类"
        elif payload.action == "UPDATE_STYLE":
            summary = f"更新 {component} 的 inline style"
        elif payload.action == "DELETE_DATA_ITEM":
            summary = (
                f"删除 {component} 对应数据源的第 "
                f"{int(anchor.get('instance') or 1)} 项"
            )
        elif payload.action == "UPDATE_PROPS":
            prop_names = ", ".join(sorted(dict(payload.payload.get("props") or {})))
            summary = f"更新 {anchor.get('component') or component} 调用点 Props: {prop_names}"
        else:
            summary = f"对 {component} 执行 {payload.action}"
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
                "callSiteOid": anchor.get("callSiteOid") or "",
                "range": anchor.get("range") or {},
                "placement": placement,
                "nodeSource": " ".join(str(anchor.get("nodeSource") or "").split())[
                    :200
                ],
            },
            summary=summary,
            diff_summary=_diff_summary(before, after),
            before_hash=_sha256(before),
            after_hash=_sha256(after),
            commit_hash=commit_hash,
            before_commit=before_commit,
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
        source_sync: dict[str, Any] | None = None,
        decision_id: str | None = None,
        options: list[dict[str, Any]] | None = None,
        reasons: list[str] | None = None,
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
            source_sync=source_sync,
            decision_id=decision_id,
            options=list(options or []),
            reasons=list(reasons or []),
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
