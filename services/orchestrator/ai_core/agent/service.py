"""Run 执行服务：沙盒生命周期、Graph 执行、SSE 事件与 HITL 暂停/恢复。

M2 新增两条执行路径，共享同一套 prepare / run / finalize 逻辑：

  - ``execute()``       同步执行，保留给旧客户端与回归测试；
  - ``start_stream()``  立即返回 runId + 事件缓冲，后台线程执行 Run。

HITL 暂停后，Java BFF 提交答案调用 ``answer_clarification()``。服务从
``ActiveRun`` 中恢复同一个 PreparedRun，并通过 LangGraph checkpoint 以
``Command(resume=answers)`` 继续状态机。
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from docker.errors import DockerException

from ai_core.adapters.checkpoint import get_checkpointer_provider
from ai_core.ports.checkpoint import CheckpointerProvider
from ai_core.agent.context_doc import (
    VIBE_CONTEXT_FILENAME,
    render_vibe_context,
)
from ai_core.agent.events import RunEventBuffer, get_event_buffer
from ai_core.agent.graph import (
    AgentGraphRunner,
    GraphRunOutcome,
    RunCancelledError,
)
from ai_core.agent.llm import (
    AgentLLM,
    AgentLLMConfigurationError,
    AgentLLMError,
    build_agent_llm,
)
from ai_core.agent.ui_context import extract_jsx_window
from ai_core.domain.agent import (
    AnnotationEnvelope,
    Approval,
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ApprovalListResponse,
    ApprovalStatus,
    CancelRunRequest,
    CancelRunResponse,
    Clarification,
    ClarificationAnswerResponse,
    ClarificationAnswersRequest,
    ClarificationListResponse,
    ClarificationRunStatus,
    ClarificationStatus,
    ExecuteRunRequest,
    ExecuteRunResponse,
    RollbackRunRequest,
    RollbackRunResponse,
    RunCommitRecord,
    RunSnapshotResponse,
    VerifyResultModel,
)
from ai_core.agent.thread_memory import ThreadMemoryStore, get_thread_store
from ai_core.context_memory import ContextSummarizer, get_project_memory
from ai_core.domain.scope import memory_scope, scoped_checkpoint_key
from ai_core.config import Settings, get_settings
from ai_core.adapters.sandbox import get_sandbox_provider
from ai_core.ports.sandbox import SandboxError, SandboxHandle, SandboxProvider

logger = logging.getLogger(__name__)

DEFAULT_TARGET_FILE = "src/App.jsx"
TERMINAL_RUN_STATUSES = {
    "SUCCEEDED",
    "FAILED",
    "EXPIRED",
    "CANCELLED",
    "REJECTED",
}
# AWAITING_APPROVAL 是暂停态而不是终态：审批通过前代码尚未形成 Git
# checkpoint，同一 thread 不能被新的 Run 复用。
ACTIVE_RUN_STATUSES = {
    "RUNNING",
    "CANCELLING",
    "PLANNING",
    "CODING",
    "APPLYING",
    "VERIFYING",
    "REPAIRING",
    "AWAITING_CLARIFICATION",
    "AWAITING_APPROVAL",
}


# ------------------------------------------------------------
# 领域异常（API 层映射为 4xx / 5xx）
# ------------------------------------------------------------
class RunServiceError(Exception):
    code = "RUN_ERROR"
    status_code = 409


class FullStackNotSupportedError(RunServiceError):
    """M2 尚未实现全栈生成。"""

    code = "FULLSTACK_NOT_SUPPORTED"
    status_code = 422


class TargetFileNotFoundError(RunServiceError):
    """标注指向的文件不存在。"""

    code = "TARGET_FILE_NOT_FOUND"
    status_code = 422


class RollbackTargetMismatchError(RunServiceError):
    """前端提交的 commit 与服务端 thread checkpoint 不一致。"""

    code = "ROLLBACK_TARGET_MISMATCH"
    status_code = 409


class RunNotFoundError(RunServiceError):
    code = "RUN_NOT_FOUND"
    status_code = 404


class RunIdConflictError(RunServiceError):
    code = "RUN_ID_CONFLICT"
    status_code = 409


class RunBusyError(RunServiceError):
    code = "RUN_BUSY"
    status_code = 409


class ClarificationNotFoundError(RunServiceError):
    code = "CLARIFICATION_NOT_FOUND"
    status_code = 404


class ClarificationStateConflictError(RunServiceError):
    code = "CLARIFICATION_STATE_CONFLICT"
    status_code = 409


class ClarificationExpiredError(RunServiceError):
    code = "CLARIFICATION_EXPIRED"
    status_code = 410


class ClarificationAnswersRequiredError(RunServiceError):
    code = "CLARIFICATION_ANSWERS_REQUIRED"
    status_code = 422


# ------------------------------------------------------------
# 审批 / 取消异常
# ------------------------------------------------------------
class ApprovalNotFoundError(RunServiceError):
    code = "APPROVAL_NOT_FOUND"
    status_code = 404


class ApprovalStateConflictError(RunServiceError):
    code = "APPROVAL_STATE_CONFLICT"
    status_code = 409


class ApprovalExpiredError(RunServiceError):
    code = "APPROVAL_EXPIRED"
    status_code = 410


class ApprovalDecisionConflictError(RunServiceError):
    """同一审批重复提交了不同 decision。"""

    code = "APPROVAL_DECISION_CONFLICT"
    status_code = 409


class RunCancelNotAllowedError(RunServiceError):
    code = "RUN_CANCEL_NOT_ALLOWED"
    status_code = 409


# ------------------------------------------------------------
# 运行期数据结构
# ------------------------------------------------------------
@dataclass
class PreparedRun:
    """沙盒已就绪、Graph 尚未执行的一次 Run。"""

    run_id: str
    request: ExecuteRunRequest
    llm: AgentLLM
    annotations: list[AnnotationEnvelope]
    thread_ids: list[str]
    # LangGraph Checkpointer 物理键：tenant + project + ui_run + runId。
    # 与业务 threadId 解耦，避免不同项目 / Agent 的 checkpoint 碰撞。
    checkpoint_thread_id: str
    # ThreadMemoryStore 的租户 + 项目作用域哈希。
    memory_scope: str
    instructions: list[str]
    turn_counts: dict[str, int]
    run_turn_count: int
    handle: SandboxHandle
    reused_sandbox: bool
    current_files: dict[str, str]
    # Run 开始前的 HEAD；审批拒绝 / 取消时回滚到该提交。
    base_commit: str | None
    initial_state: dict[str, Any]
    event_buffer: RunEventBuffer
    # 本次 Run 的 Token 用量 / 费用；Runner 结束后回填，供终态事件与审计使用。
    token_usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActiveRun:
    """进程内 Run 快照：事件重放、澄清恢复与状态查询共用。"""

    run_id: str
    fingerprint: str
    request: ExecuteRunRequest
    buffer: RunEventBuffer
    thread_ids: list[str]
    status: str = "RUNNING"
    prepared: PreparedRun | None = None
    response: ExecuteRunResponse | None = None
    clarification: Clarification | None = None
    clarifications: list[Clarification] = field(default_factory=list)
    approval: Approval | None = None
    approvals: list[Approval] = field(default_factory=list)
    # 协作式取消：API 只置位，后台线程在节点安全点响应。
    cancel_requested: bool = False
    cancel_requested_at: datetime | None = None
    cancel_reason: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)


_ACTIVE_RUNS: dict[str, ActiveRun] = {}
_ACTIVE_LOCK = threading.RLock()


def get_active_run(run_id: str) -> ActiveRun | None:
    with _ACTIVE_LOCK:
        return _ACTIVE_RUNS.get(run_id)


class RunExecutionService:
    """Run 执行入口。

    API 每次请求都会新建一个 Service；所有跨请求状态都放在模块级
    ``_ACTIVE_RUNS`` 与 LangGraph Checkpointer 中。
    """

    def __init__(
        self,
        sandbox: SandboxProvider | None = None,
        settings: Settings | None = None,
        thread_store: ThreadMemoryStore | None = None,
        checkpointer_provider: CheckpointerProvider | None = None,
    ) -> None:
        self._sandbox = sandbox or get_sandbox_provider()
        self._settings = settings or get_settings()
        self._threads = thread_store or get_thread_store()
        self._checkpointer_provider = (
            checkpointer_provider or get_checkpointer_provider()
        )

    # --------------------------------------------------------
    # 同步执行（旧客户端 / 回归）
    # --------------------------------------------------------
    def execute(self, request: ExecuteRunRequest) -> ExecuteRunResponse:
        run_id = request.run_id or _new_run_id()
        fingerprint = _request_fingerprint(request)
        thread_ids = self._thread_ids(request)
        active, created = self._claim_run(
            request, run_id=run_id, fingerprint=fingerprint, thread_ids=thread_ids
        )
        if not created:
            if active.response is not None:
                return active.response
            raise RunBusyError(f"运行 {run_id} 正在执行，请通过事件流等待结果")

        self._emit_run_started(active)
        try:
            prepared = self._prepare(request, run_id, active.buffer)
            active.prepared = prepared
            return self._execute_prepared(active, prepared)
        except Exception as exc:
            self._mark_failed_before_prepared(active, exc)
            raise

    # --------------------------------------------------------
    # 流式执行（SSE）
    # --------------------------------------------------------
    def start_stream(self, request: ExecuteRunRequest) -> ActiveRun:
        """启动后台 Run 并立即返回事件缓冲。

        同 runId + 相同请求指纹是幂等的：不重复执行，直接回放并继续订阅。
        """
        run_id = request.run_id or _new_run_id()
        fingerprint = _request_fingerprint(request)
        thread_ids = self._thread_ids(request)
        active, created = self._claim_run(
            request, run_id=run_id, fingerprint=fingerprint, thread_ids=thread_ids
        )
        if not created:
            return active

        self._emit_run_started(active)
        thread = threading.Thread(
            target=self._stream_worker,
            args=(active,),
            name=f"run-{run_id}",
            daemon=True,
        )
        thread.start()
        return active

    def _stream_worker(self, active: ActiveRun) -> None:
        try:
            prepared = self._prepare(active.request, active.run_id, active.buffer)
            active.prepared = prepared
            # 准备沙盒期间用户可能已经取消：还没有执行任何节点，直接收敛。
            if active.cancel_requested:
                self._complete_cancelled(
                    active,
                    prepared,
                    reason=active.cancel_reason,
                )
                return
            self._execute_prepared(active, prepared)
        except Exception as exc:  # noqa: BLE001 - 后台线程必须把错误转成事件
            logger.exception("stream run failed run_id=%s", active.run_id)
            self._emit_prepare_failure(active, exc)

    # --------------------------------------------------------
    # HITL：回答澄清
    # --------------------------------------------------------
    def answer_clarification(
        self,
        run_id: str,
        clarification_id: str | None,
        request: ClarificationAnswersRequest,
    ) -> ClarificationAnswerResponse:
        active = get_active_run(run_id)
        if active is None:
            raise RunNotFoundError(f"Run 不存在或已过期: {run_id}")

        with _ACTIVE_LOCK:
            self._expire_if_needed(active)
            if active.status == "EXPIRED":
                raise ClarificationExpiredError("澄清已超过有效期，请重新发起修改")
            if active.status != "AWAITING_CLARIFICATION":
                raise ClarificationStateConflictError(
                    f"Run 当前状态为 {active.status}，没有待回答的澄清"
                )
            clarification = active.clarification
            if clarification is None:
                raise ClarificationNotFoundError("当前 Run 没有待回答的澄清")
            if clarification_id and clarification.clarification_id != clarification_id:
                raise ClarificationNotFoundError(f"澄清不存在: {clarification_id}")
            if (
                request.expected_version is not None
                and request.expected_version != clarification.round
            ):
                raise ClarificationStateConflictError(
                    "expectedVersion 与当前澄清轮次不一致"
                )

            answers = self._normalize_answers(clarification, request)
            if not answers:
                raise ClarificationAnswersRequiredError("至少回答一个澄清问题")

            answered_at = datetime.now(timezone.utc)
            clarification.status = "ANSWERED"
            clarification.answered_at = answered_at
            active.clarification = None
            active.status = "PLANNING"
            active.touch()

        payload = {
            "clarificationId": clarification.clarification_id,
            "round": clarification.round,
            "answers": answers,
        }
        active.buffer.emit(
            "clarification.answered",
            payload,
            dedupe_key=f"clarification.answered:{clarification.clarification_id}",
        )
        # 先把连接可订阅所需的非终态事件写入缓冲，再后台恢复；前端收到 202
        # 后用 afterSeq 订阅即可继续时间轴。
        thread = threading.Thread(
            target=self._resume_worker,
            args=(active, answers),
            name=f"resume-{run_id}",
            daemon=True,
        )
        thread.start()
        return ClarificationAnswerResponse(
            clarification=ClarificationStatus(
                clarification_id=clarification.clarification_id,
                status="ANSWERED",
                answered_at=answered_at,
            ),
            run=ClarificationRunStatus(run_id=run_id, status="PLANNING"),
        )

    def _resume_worker(self, active: ActiveRun, answers: list[dict[str, Any]]) -> None:
        prepared = active.prepared
        if prepared is None:
            self._emit_prepare_failure(
                active, RunCheckpointLostError("澄清 checkpoint 已丢失")
            )
            return
        try:
            self._execute_prepared(active, prepared, resume=answers)
        except Exception as exc:  # noqa: BLE001
            logger.exception("resume run failed run_id=%s", active.run_id)
            self._complete_failure(
                active,
                prepared,
                error=f"恢复执行失败: {exc}",
                code="RESUME_FAILED",
            )

    # --------------------------------------------------------
    # HITL：Diff 审批
    # --------------------------------------------------------
    def decide_approval(
        self,
        run_id: str,
        approval_id: str | None,
        request: ApprovalDecisionRequest,
    ) -> ApprovalDecisionResponse:
        """批准 / 拒绝待审批的 Run，并通过 checkpoint 恢复状态机。

        - 批准：恢复 ``approval_gate`` -> ``awaiting_approval``，Service 写
          Git checkpoint 后发送 ``run.succeeded``；
        - 拒绝：恢复后进入 ``rejected`` 分支，沙盒 ``git reset`` 回基线，
          发送 ``run.rejected``；
        - 同一 decision 重复提交幂等；不同 decision 返回 409。
        """
        active = get_active_run(run_id)
        if active is None:
            raise RunNotFoundError(f"Run 不存在或已过期: {run_id}")

        decision = _normalize_decision(request.decision)
        expected_id = approval_id or request.approval_id
        now = datetime.now(timezone.utc)
        expired = False
        paused_approval: Approval | None = None
        prepared: PreparedRun | None = None

        with _ACTIVE_LOCK:
            # 幂等：先按 approvalId 查找历史决策。
            recorded = _find_approval(active, expected_id)
            if recorded is not None and recorded.status in {
                "APPROVED",
                "REJECTED",
                "CANCELLED",
                "EXPIRED",
            }:
                recorded_decision = (
                    "approve" if recorded.status == "APPROVED" else "reject"
                )
                if recorded.status == "CANCELLED":
                    raise ApprovalStateConflictError("审批已随 Run 取消，不能再次决策")
                if recorded.status == "EXPIRED":
                    raise ApprovalExpiredError("审批已过期，请重新发起修改")
                if recorded_decision != decision:
                    raise ApprovalDecisionConflictError(
                        "该审批已提交其它决策，不能修改"
                    )
                return ApprovalDecisionResponse(
                    approval=ApprovalStatus(
                        approval_id=recorded.approval_id,
                        status=recorded.status,
                        decided_at=recorded.decided_at or now,
                    ),
                    run=ClarificationRunStatus(run_id=run_id, status=active.status),
                )

            approval = active.approval
            if active.status != "AWAITING_APPROVAL" or approval is None:
                raise ApprovalStateConflictError(
                    f"Run 当前状态为 {active.status}，没有待审批的修改"
                )
            if expected_id and approval.approval_id != expected_id:
                raise ApprovalNotFoundError(f"审批不存在: {expected_id}")
            if request.expected_version is not None and request.expected_version != 1:
                raise ApprovalStateConflictError("expectedVersion 与当前审批不一致")
            if approval.expires_at is not None:
                expires_at = approval.expires_at
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if now > expires_at:
                    approval.status = "EXPIRED"
                    approval.decided_at = now
                    active.approval = None
                    active.status = "EXPIRED"
                    active.touch()
                    expired = True
                else:
                    paused_approval = approval
            if not expired:
                paused_approval = approval
                approval.status = "APPROVED" if decision == "approve" else "REJECTED"
                approval.decided_at = now
                approval.comment = request.comment
                active.approval = None
                # 恢复执行期间保持非终态，事件流可继续订阅。
                active.status = "RUNNING"
                active.touch()
                prepared = active.prepared

        if expired:
            # 过期后同样要丢弃未批准的代码，避免污染下一轮。
            expired_prepared = active.prepared
            if expired_prepared is not None:
                self._revert_sandbox(expired_prepared)
            raise ApprovalExpiredError("审批已过期，请重新发起修改")

        if paused_approval is None:
            raise ApprovalStateConflictError("审批状态异常")

        active.buffer.emit(
            "approval.answered",
            {
                "approvalId": paused_approval.approval_id,
                "decision": decision,
                "comment": request.comment,
                "status": paused_approval.status,
            },
            dedupe_key=f"approval.answered:{paused_approval.approval_id}",
        )

        if prepared is None:
            self._emit_prepare_failure(
                active, RunCheckpointLostError("审批 checkpoint 已丢失")
            )
            raise RunCheckpointLostError("审批 checkpoint 已丢失")

        # 后台恢复，API 立即返回 202；前端用 lastSeq 重放事件流。
        thread = threading.Thread(
            target=self._approval_resume_worker,
            args=(
                active,
                prepared,
                decision,
                paused_approval.approval_id,
                request.comment,
            ),
            name=f"approval-{run_id}",
            daemon=True,
        )
        thread.start()
        return ApprovalDecisionResponse(
            approval=ApprovalStatus(
                approval_id=paused_approval.approval_id,
                status=paused_approval.status,
                decided_at=now,
            ),
            run=ClarificationRunStatus(run_id=run_id, status="RUNNING"),
        )

    def _approval_resume_worker(
        self,
        active: ActiveRun,
        prepared: PreparedRun,
        decision: str,
        approval_id: str,
        comment: str | None,
    ) -> None:
        try:
            self._execute_prepared(
                active,
                prepared,
                resume={
                    "kind": "approval",
                    "approvalId": approval_id,
                    "decision": decision,
                    "comment": comment,
                },
            )
        except Exception as exc:  # noqa: BLE001 - 后台线程必须收敛为事件
            logger.exception("approval resume failed run_id=%s", prepared.run_id)
            self._complete_failure(
                active,
                prepared,
                error=f"审批恢复失败: {exc}",
                code="APPROVAL_RESUME_FAILED",
            )

    def list_approvals(self, run_id: str) -> ApprovalListResponse:
        active = get_active_run(run_id)
        if active is None:
            raise RunNotFoundError(f"Run 不存在或已过期: {run_id}")
        with _ACTIVE_LOCK:
            items = [item.model_copy(deep=True) for item in active.approvals]
        return ApprovalListResponse(run_id=run_id, approvals=items)

    # --------------------------------------------------------
    # 取消 Run
    # --------------------------------------------------------
    def cancel_run(
        self,
        run_id: str,
        request: CancelRunRequest,
    ) -> CancelRunResponse:
        """协作式取消。

        - 暂停在澄清 / 审批：可立即回滚并发送 ``run.cancelled``；
        - 正在执行：置位取消标志，节点安全点抛 ``RunCancelledError``，
          由后台线程发送终态；
        - 已终态：幂等返回当前状态，不重复发事件。
        """
        active = get_active_run(run_id)
        if active is None:
            raise RunNotFoundError(f"Run 不存在或已过期: {run_id}")

        now = datetime.now(timezone.utc)
        immediate_prepared: PreparedRun | None = None
        emit_requested = False
        with _ACTIVE_LOCK:
            self._expire_if_needed(active)
            if active.status in TERMINAL_RUN_STATUSES:
                return CancelRunResponse(
                    run_id=run_id,
                    status=active.status,
                    cancel_requested_at=active.cancel_requested_at or now,
                )

            if active.cancel_requested:
                # 重复取消：保持幂等，不再发事件。
                return CancelRunResponse(
                    run_id=run_id,
                    status=active.status,
                    cancel_requested_at=active.cancel_requested_at or now,
                )

            active.cancel_requested = True
            active.cancel_requested_at = now
            active.cancel_reason = request.reason or "用户取消执行"

            if active.status in {"AWAITING_APPROVAL", "AWAITING_CLARIFICATION"}:
                if active.approval is not None:
                    active.approval.status = "CANCELLED"
                    active.approval.decided_at = now
                    active.approval = None
                active.clarification = None
                active.status = "CANCELLED"
                active.touch()
                immediate_prepared = active.prepared
            else:
                # 执行中：进入 CANCELLING，等待后台线程在安全点收敛。
                active.status = "CANCELLING"
                active.touch()
                emit_requested = True

        if immediate_prepared is not None:
            self._complete_cancelled(
                active,
                immediate_prepared,
                reason=request.reason,
            )
            return CancelRunResponse(
                run_id=run_id,
                status="CANCELLED",
                cancel_requested_at=now,
            )

        if emit_requested:
            active.buffer.emit(
                "run.cancel_requested",
                {
                    "status": "CANCELLING",
                    "reason": request.reason or "用户取消执行",
                    "requestedAt": now.isoformat(timespec="seconds").replace(
                        "+00:00", "Z"
                    ),
                },
                dedupe_key=f"run.cancel_requested:{run_id}",
            )
        return CancelRunResponse(
            run_id=run_id,
            status=active.status,
            cancel_requested_at=now,
        )

    # --------------------------------------------------------
    # 查询
    # --------------------------------------------------------
    def snapshot(self, run_id: str) -> RunSnapshotResponse:
        active = get_active_run(run_id)
        if active is None:
            raise RunNotFoundError(f"Run 不存在或已过期: {run_id}")
        with _ACTIVE_LOCK:
            self._expire_if_needed(active)
        response = active.response
        return RunSnapshotResponse(
            run_id=run_id,
            status=active.status,
            sandbox_id=response.sandbox_id if response else "",
            preview_url=response.preview_url if response else "",
            clarification=active.clarification,
            approval=active.approval,
            cancel_requested=active.cancel_requested,
            assistant_message=response.assistant_message if response else "",
            error=response.error if response else None,
            last_seq=active.buffer.last_seq,
        )

    def list_clarifications(self, run_id: str) -> ClarificationListResponse:
        active = get_active_run(run_id)
        if active is None:
            raise RunNotFoundError(f"Run 不存在或已过期: {run_id}")
        with _ACTIVE_LOCK:
            self._expire_if_needed(active)
            items = [item.model_copy(deep=True) for item in active.clarifications]
        return ClarificationListResponse(run_id=run_id, clarifications=items)

    # --------------------------------------------------------
    # Prepare / Execute / Finalize
    # --------------------------------------------------------
    def _prepare(
        self,
        request: ExecuteRunRequest,
        run_id: str,
        buffer: RunEventBuffer,
    ) -> PreparedRun:
        annotations = request.effective_annotations
        intent_kinds = {
            annotation.intent.kind if annotation.intent else "UI_ONLY"
            for annotation in annotations
        }
        if "FULLSTACK" in intent_kinds:
            raise FullStackNotSupportedError(
                "M2 只支持 UI_ONLY / UI_AND_BUSINESS，FULLSTACK 需等待后续里程碑"
            )

        target_files = self._resolve_target_files(request)
        # 业务 threadId 只用于前端展示与同圈多轮定位；所有持久化键都必须
        # 再叠加 tenant/project 作用域，禁止跨项目读取同名会话。
        memory_scope_key = memory_scope(request.tenant_id, request.project_id)
        checkpoint_thread_id = scoped_checkpoint_key(
            "ui_run",
            run_id,
            tenant_id=request.tenant_id,
            project_id=request.project_id,
        )
        thread_ids = [
            self._thread_id(annotation, index)
            for index, annotation in enumerate(annotations, start=1)
        ]
        thread_ids = list(dict.fromkeys(thread_ids))
        instructions = [
            self._annotation_instruction(annotation) for annotation in annotations
        ]

        buffer.emit(
            "node.started",
            {"node": "provision", "label": "准备沙盒", "attempt": 0},
            dedupe_key=f"node.started:provision:{run_id}",
        )
        base_commit: str | None = None
        try:
            # 先构造 LLM，避免密钥缺失时已经创建了沙盒。
            llm = build_agent_llm(request, self._settings)
            # 注入项目长期记忆（global_summary + user_profile）。记忆是可选
            # 增强能力，读取失败只记录日志，绝不影响 Run。
            try:
                memory = get_project_memory(
                    self._settings,
                    request.tenant_id,
                    request.project_id,
                )
                llm.set_memory_context(memory.prompt_context())
                # Reflection：把多次确认过的实现偏好作为默认值注入，
                # 未达到证据阈值（默认 2 次）的偏好不会影响生成。
                llm.set_preference_context(
                    memory.preference_context(
                        min_evidence=self._settings.agent_preference_min_evidence
                    )
                )
            except Exception:  # noqa: BLE001 - 记忆不可用不能阻断 Run
                logger.warning("load project memory failed", exc_info=True)

            turn_counts = {
                thread_id: self._threads.next_turn(
                    thread_id,
                    annotation.chat_history,
                    scope=memory_scope_key,
                )
                for annotation, thread_id in zip(annotations, thread_ids)
            }
            run_turn_count = max(turn_counts.values(), default=1)

            handle, reused_sandbox, current_files = self._open_sandbox(
                request,
                target_files,
                thread_ids,
                scope=memory_scope_key,
            )
            # 记录写代码前的 HEAD：审批拒绝 / 取消时 git reset 回这里，
            # 未批准的修改不会污染同一 thread 的下一轮。
            try:
                base_commit = self._sandbox.git_head(handle.sandbox_id)
            except SandboxError:
                logger.warning(
                    "failed to read git HEAD run_id=%s sandbox_id=%s",
                    run_id,
                    handle.sandbox_id,
                    exc_info=True,
                )
            try:
                # 把全部画布标注渲染成 VIBE_CONTEXT.md 写进沙盒，导出源码包时
                # 自动带上；写入失败只记录日志，不阻塞 Run。
                self._sandbox.apply_patch(
                    handle.sandbox_id,
                    files={
                        VIBE_CONTEXT_FILENAME: render_vibe_context(
                            request,
                            run_id,
                            handle.sandbox_id,
                            target_files,
                        )
                    },
                )
            except SandboxError:
                logger.warning(
                    "failed to write %s run_id=%s",
                    VIBE_CONTEXT_FILENAME,
                    run_id,
                    exc_info=True,
                )

            thread_contexts = self._merge_thread_contexts(
                annotations,
                thread_ids,
                scope=memory_scope_key,
            )
            annotation_context = self._build_annotation_context(
                request, target_files, thread_contexts, current_files
            )
            primary_file = target_files[0]
            initial_state: dict[str, Any] = {
                "run_id": run_id,
                "tenant_id": request.tenant_id or "",
                "project_id": request.project_id,
                "sandbox_id": handle.sandbox_id,
                "preview_url": handle.preview_url,
                "target_file": primary_file,
                "target_files": target_files,
                "annotation_context": annotation_context,
                "messages": [],
                "current_code": current_files[primary_file],
                "current_files": current_files,
                # 写沙盒前的快照：审批 Diff 与拒绝回滚的共同基线。
                "initial_files": dict(current_files),
                "plan": "",
                "generated_patch": "",
                "generated_files": {},
                "verify_result": {},
                "apply_failed": False,
                "retry_count": 0,
                "max_retries": min(request.options.max_retries, 3),
                # 大型重构可通过前端“转后台运行”获得更长超时；普通请求
                # 仍保持严格熔断，避免一个卡死请求无限占用线程。
                "background_run": bool(request.options.background),
                "coder_timeout_seconds": (
                    self._settings.agent_coder_background_timeout_seconds
                    if request.options.background
                    else self._settings.agent_coder_timeout_seconds
                ),
                "coder_fallback_used": False,
                "background_available": False,
                # Token 优化：历史压缩与同错去重
                "history_summary": "",
                "history_tokens_before": 0,
                "history_tokens_after": 0,
                "last_repair_error_fingerprint": "",
                # 企业知识检索：与 Architect 共用 Hybrid（BM25 + Chroma + RRF）。
                "rag_provider": self._settings.architect_rag_provider,
                "rag_top_k": self._settings.agent_rag_top_k,
                "rag_query": "",
                "rag_context": "",
                "rag_citations": [],
                # HITL
                "clarification_mode": request.options.clarification_mode,
                "max_clarification_rounds": request.options.max_clarification_rounds,
                "confidence_threshold": (
                    request.options.confidence_threshold
                    if request.options.confidence_threshold is not None
                    else self._settings.agent_clarification_threshold
                ),
                "clarification_ttl_hours": self._settings.agent_clarification_ttl_hours,
                "clarification_round": 0,
                "clarification_answers": [],
                "clarification": None,
                # HITL Diff 审批：新字段 requireManualApproval 优先，
                # 旧 approvalMode 仅作为兼容回退。
                "approval_required": request.options.manual_approval_required,
                "approval": None,
                "approval_id": None,
                "approval_decision": None,
                "approval_comment": None,
                "approval_ttl_hours": self._settings.agent_approval_ttl_hours,
                "status": "RUNNING",
                "error": None,
                "error_code": None,
            }
        except Exception as exc:  # noqa: BLE001 - 事件后继续抛出给 API / worker
            buffer.emit(
                "node.finished",
                {
                    "node": "provision",
                    "status": "error",
                    "durationMs": 0,
                    "summary": str(exc)[:300],
                },
                dedupe_key=f"node.finished:provision:{run_id}:error",
            )
            raise

        buffer.emit(
            "node.finished",
            {
                "node": "provision",
                "status": "ok",
                "durationMs": 0,
                "summary": "沙盒已就绪",
            },
            dedupe_key=f"node.finished:provision:{run_id}:ok",
        )
        return PreparedRun(
            run_id=run_id,
            request=request,
            llm=llm,
            annotations=annotations,
            thread_ids=thread_ids,
            checkpoint_thread_id=checkpoint_thread_id,
            memory_scope=memory_scope_key,
            instructions=instructions,
            turn_counts=turn_counts,
            run_turn_count=run_turn_count,
            handle=handle,
            reused_sandbox=reused_sandbox,
            current_files=current_files,
            base_commit=base_commit,
            initial_state=initial_state,
            event_buffer=buffer,
        )

    def _execute_prepared(
        self,
        active: ActiveRun,
        prepared: PreparedRun,
        *,
        resume: list[dict[str, Any]] | None = None,
    ) -> ExecuteRunResponse:
        runner = AgentGraphRunner(
            sandbox=self._sandbox,
            llm=prepared.llm,
            max_retries=int(prepared.initial_state.get("max_retries", 3)),
            recursion_limit=self._settings.agent_graph_recursion_limit,
            event_sink=prepared.event_buffer,
            checkpointer_provider=self._checkpointer_provider,
            # 取消由 API 置位，Graph 在节点安全点检查；不打断进行中的
            # 模型调用，但保证写沙盒 / 构建后不再继续推进。
            cancel_check=lambda: active.cancel_requested,
        )
        try:
            outcome = runner.run(
                prepared.initial_state,  # type: ignore[arg-type]
                thread_id=prepared.checkpoint_thread_id,
                resume=resume,
            )
        except RunCancelledError:
            logger.info("agent run cancelled run_id=%s", prepared.run_id)
            prepared.token_usage = runner.usage_total()
            return self._complete_cancelled(active, prepared)
        except AgentLLMError as exc:
            logger.exception("agent llm failed run_id=%s", prepared.run_id)
            prepared.token_usage = runner.usage_total()
            return self._complete_failure(
                active,
                prepared,
                error=f"模型执行失败: {exc}",
                code="LLM_EXECUTION_FAILED",
            )
        except SandboxError as exc:
            logger.exception("agent sandbox failed run_id=%s", prepared.run_id)
            prepared.token_usage = runner.usage_total()
            return self._complete_failure(
                active,
                prepared,
                error=f"沙盒执行失败: {exc}",
                code="SANDBOX_ERROR",
            )
        except Exception as exc:  # noqa: BLE001 - 统一转为 run.failed
            logger.exception("agent unexpected failure run_id=%s", prepared.run_id)
            prepared.token_usage = runner.usage_total()
            return self._complete_failure(
                active,
                prepared,
                error=f"执行失败: {exc}",
                code="INTERNAL_ERROR",
            )

        prepared.token_usage = outcome.usage
        # 用户在最后一个节点执行期间点了取消：取消优先于成功落地。
        if active.cancel_requested:
            return self._complete_cancelled(active, prepared)

        if outcome.interrupted:
            payload = outcome.interrupt
            if payload and payload.get("kind") == "approval":
                return self._complete_approval_pause(active, prepared, outcome)
            if payload:
                return self._complete_paused(active, prepared, outcome)
            return self._complete_failure(
                active,
                prepared,
                error="状态机已暂停但缺少澄清/审批内容，checkpoint 可能已损坏",
                code="RUN_CHECKPOINT_LOST",
            )
        return self._complete_finished(active, prepared, outcome)

    def _complete_paused(
        self,
        active: ActiveRun,
        prepared: PreparedRun,
        outcome: GraphRunOutcome,
    ) -> ExecuteRunResponse:
        payload = dict(outcome.interrupt or {})
        clarification = Clarification.model_validate(payload)
        with _ACTIVE_LOCK:
            active.clarification = clarification
            active.clarifications.append(clarification)
            active.status = "AWAITING_CLARIFICATION"
            active.touch()

        questions = clarification.questions
        prompt = questions[0].prompt if questions else "请补充更具体的修改要求"
        assistant_message = f"需要确认：{prompt}"
        response = self._to_response(
            prepared.run_id,
            prepared.handle.sandbox_id,
            prepared.handle.preview_url,
            {
                **outcome.state,
                "status": "AWAITING_CLARIFICATION",
                "error": None,
            },
            reused_sandbox=prepared.reused_sandbox,
            assistant_message=assistant_message,
            clarification=clarification,
        )
        with _ACTIVE_LOCK:
            active.response = response
            active.touch()
        # clarification.required 已由 Graph 在 interrupt 前写入事件缓冲；
        # 这里不再追加终态事件，SSE 订阅者会在该事件后自动关闭。
        return response

    def _persist_preferences(
        self,
        prepared: PreparedRun,
        final_state: dict[str, Any],
    ) -> None:
        """把本轮成功提取的偏好证据合并进项目偏好账本（best effort）。"""
        observations = final_state.get("reflection_preferences") or []
        if not observations:
            return
        try:
            memory = get_project_memory(
                self._settings,
                prepared.request.tenant_id,
                prepared.request.project_id,
            )
            memory.record_preferences(
                observations,
                min_evidence=self._settings.agent_preference_min_evidence,
            )
        except Exception:  # noqa: BLE001 - 偏好落盘失败不能影响 Run
            logger.warning(
                "persist reflection preferences failed run_id=%s",
                prepared.run_id,
                exc_info=True,
            )

    def _complete_finished(
        self,
        active: ActiveRun,
        prepared: PreparedRun,
        outcome: GraphRunOutcome,
    ) -> ExecuteRunResponse:
        final_state = outcome.state
        status = str(final_state.get("status") or "FAILED")
        error = final_state.get("error")
        error_code = final_state.get("error_code")
        retry_count = int(final_state.get("retry_count", 0))
        preview_url = prepared.handle.preview_url
        # 整次 Run 的 token / 费用（优先取 Checkpoint 中的累计值）。
        usage = dict(final_state.get("token_usage") or outcome.usage or {})

        commit_hash: str | None = None
        baseline_commit: str | None = None
        commits: list[RunCommitRecord] = []

        if status in {"AWAITING_APPROVAL", "SUCCEEDED"}:
            # 审批通过（AWAITING_APPROVAL）或免审批直接成功（SUCCEEDED）：
            # 此时才允许形成可回滚 checkpoint，并统一收敛为 SUCCEEDED。
            try:
                commit_hash = self._sandbox.git_commit(
                    prepared.handle.sandbox_id,
                    f"run: {prepared.run_id} - turn: {prepared.run_turn_count}",
                )
                commits = [
                    RunCommitRecord(
                        thread_id=thread_id,
                        turn_count=prepared.turn_counts[thread_id],
                        commit_hash=commit_hash,
                        run_id=prepared.run_id,
                    )
                    for thread_id in prepared.thread_ids
                ]
                try:
                    baseline_commit = self._sandbox.git_baseline_commit(
                        prepared.handle.sandbox_id
                    )
                except SandboxError:
                    logger.warning(
                        "failed to read git baseline run_id=%s",
                        prepared.run_id,
                        exc_info=True,
                    )
                status = "SUCCEEDED"
            except SandboxError as exc:
                # 代码已经构建通过，但没有可回滚的 checkpoint 不能宣称成功。
                logger.exception("git checkpoint failed run_id=%s", prepared.run_id)
                status = "FAILED"
                error = f"Git checkpoint 失败: {exc}"
                error_code = "COMMIT_FAILED"
                commit_hash = None
                commits = []
        elif status == "REJECTED":
            # 用户拒绝：丢弃工作区改动，不生成 commit，不写 thread 成功记忆。
            preview_url = self._revert_sandbox(prepared)

        # Reflection 证据只在真正成功（含审批通过）后落盘；失败、拒绝、
        # 取消的运行不会影响长期偏好。
        if status == "SUCCEEDED":
            self._persist_preferences(prepared, final_state)

        assistant_message = self._assistant_message(
            prepared.instructions,
            status=status,
            error=error,
            retry_count=retry_count,
        )
        if status == "REJECTED":
            final_files = dict(
                final_state.get("initial_files") or prepared.current_files
            )
            generated_files: dict[str, Any] = {}
            generated_patch = ""
            changed_files: list[str] = []
        else:
            final_files = dict(
                final_state.get("current_files") or prepared.current_files
            )
            generated_files = final_state.get("generated_files") or {}
            generated_patch = str(final_state.get("generated_patch") or "")
            changed_files = sorted(generated_files) or list(
                prepared.initial_state["target_files"]
            )

        response = self._to_response(
            prepared.run_id,
            prepared.handle.sandbox_id,
            preview_url,
            {**final_state, "status": status, "error": error},
            reused_sandbox=prepared.reused_sandbox,
            assistant_message=assistant_message,
            commit_hash=commit_hash,
            turn_count=prepared.run_turn_count if commit_hash else 0,
            baseline_commit=baseline_commit,
            commits=commits,
        )
        self._record_threads(
            prepared.annotations,
            prepared.thread_ids,
            run_id=prepared.run_id,
            assistant_message=assistant_message,
            status=status,
            sandbox_id=prepared.handle.sandbox_id,
            files=final_files,
            patch=generated_patch,
            changed_files=changed_files,
            retry_count=retry_count,
            turn_counts=prepared.turn_counts if commit_hash else None,
            commit_hash=commit_hash,
            baseline_commit=baseline_commit,
            scope=prepared.memory_scope,
        )
        # 每 N 轮写一次局部摘要；快照保存时再汇总为 global_summary.md。
        self._maybe_write_local_summary(
            prepared,
            status=status,
            changed_files=changed_files,
            assistant_message=assistant_message,
        )
        self._maybe_destroy(prepared.handle.sandbox_id, prepared.request)
        with _ACTIVE_LOCK:
            active.response = response
            active.status = status
            active.approval = None
            active.touch()

        if status == "SUCCEEDED":
            prepared.event_buffer.emit(
                "run.succeeded",
                {
                    "status": status,
                    "approvalId": final_state.get("approval_id"),
                    "sandboxId": prepared.handle.sandbox_id,
                    "previewUrl": preview_url,
                    "commitHash": commit_hash,
                    "baselineCommit": baseline_commit,
                    "retryCount": retry_count,
                    "assistantMessage": assistant_message,
                    # 前端据此渲染每条助手消息的“回滚到此步”。
                    "turnCount": prepared.run_turn_count,
                    "commits": [item.model_dump(by_alias=True) for item in commits],
                    "usage": usage,
                },
                dedupe_key=f"run.succeeded:{prepared.run_id}",
            )
        elif status == "REJECTED":
            prepared.event_buffer.emit(
                "run.rejected",
                {
                    "status": status,
                    "approvalId": final_state.get("approval_id"),
                    "sandboxId": prepared.handle.sandbox_id,
                    "previewUrl": preview_url,
                    "message": assistant_message,
                    "assistantMessage": assistant_message,
                    "usage": usage,
                },
                dedupe_key=f"run.rejected:{prepared.run_id}",
            )
        else:
            prepared.event_buffer.emit(
                "run.failed",
                {
                    "code": error_code or "VERIFY_FAILED",
                    "message": error or "构建验证未通过",
                    # 可恢复超时不应被前端当成“只能重来”：允许一键转后台。
                    "retryable": bool(final_state.get("background_available")),
                    "backgroundAvailable": bool(
                        final_state.get("background_available")
                    ),
                    "retryCount": retry_count,
                    "sandboxId": prepared.handle.sandbox_id,
                    "previewUrl": preview_url,
                    "usage": usage,
                },
                dedupe_key=f"run.failed:{prepared.run_id}",
            )
        return response

    def _complete_approval_pause(
        self,
        active: ActiveRun,
        prepared: PreparedRun,
        outcome: GraphRunOutcome,
    ) -> ExecuteRunResponse:
        """构建通过但等待审批：不提交 Git，不销毁沙盒，不写 thread 记忆。"""
        payload = dict(outcome.interrupt or {})
        approval = Approval.model_validate(payload)
        with _ACTIVE_LOCK:
            active.approval = approval
            active.approvals.append(approval)
            active.status = "AWAITING_APPROVAL"
            active.touch()

        evidence = approval.evidence
        files_changed = evidence.files_changed if evidence else 0
        assistant_message = (
            f"构建已通过，{files_changed} 个文件待审批。" "请查看 Diff 后批准或拒绝。"
        )
        response = self._to_response(
            prepared.run_id,
            prepared.handle.sandbox_id,
            prepared.handle.preview_url,
            {
                **outcome.state,
                "status": "AWAITING_APPROVAL",
                "error": None,
            },
            reused_sandbox=prepared.reused_sandbox,
            assistant_message=assistant_message,
            approval=approval,
        )
        with _ACTIVE_LOCK:
            active.response = response
            active.touch()
        # approval.required 已由 Graph 在 interrupt 前写入事件缓冲；
        # 这里不追加终态事件，订阅者会在该事件后自动关闭。
        return response

    def _complete_cancelled(
        self,
        active: ActiveRun,
        prepared: PreparedRun,
        *,
        reason: str | None = None,
    ) -> ExecuteRunResponse:
        """用户取消：回滚到 Run 开始前的 HEAD，不写 thread 成功记忆。"""
        preview_url = self._revert_sandbox(prepared)
        reason = reason or active.cancel_reason or "用户取消执行"
        assistant_message = "已取消本次修改，沙盒代码已回滚到执行前状态。"
        response = self._to_response(
            prepared.run_id,
            prepared.handle.sandbox_id,
            preview_url,
            {
                **prepared.initial_state,
                "status": "CANCELLED",
                "error": None,
                "error_code": "RUN_CANCELLED",
            },
            reused_sandbox=prepared.reused_sandbox,
            assistant_message=assistant_message,
        )
        response.token_usage = dict(prepared.token_usage)
        with _ACTIVE_LOCK:
            active.response = response
            active.status = "CANCELLED"
            active.approval = None
            active.clarification = None
            active.touch()
        prepared.event_buffer.emit(
            "run.cancelled",
            {
                "status": "CANCELLED",
                "code": "RUN_CANCELLED",
                "message": assistant_message,
                "reason": reason,
                "sandboxId": prepared.handle.sandbox_id,
                "previewUrl": preview_url,
                "assistantMessage": assistant_message,
                "usage": prepared.token_usage,
            },
            dedupe_key=f"run.cancelled:{prepared.run_id}",
        )
        return response

    def _revert_sandbox(self, prepared: PreparedRun) -> str:
        """把沙盒恢复到本轮 Run 开始前；返回带 cache-busting 的预览地址。"""
        preview_url = prepared.handle.preview_url
        if not prepared.base_commit:
            logger.warning(
                "no base commit to revert run_id=%s sandbox_id=%s",
                prepared.run_id,
                prepared.handle.sandbox_id,
            )
            return preview_url
        try:
            self._sandbox.git_reset_hard(
                prepared.handle.sandbox_id,
                prepared.base_commit,
            )
        except SandboxError:
            logger.warning(
                "failed to revert sandbox run_id=%s commit=%s",
                prepared.run_id,
                prepared.base_commit,
                exc_info=True,
            )
            return preview_url
        revision = str(int(time.time() * 1000))
        separator = "&" if "?" in preview_url else "?"
        return f"{preview_url}{separator}v={revision}"

    def _complete_failure(
        self,
        active: ActiveRun,
        prepared: PreparedRun,
        *,
        error: str,
        code: str,
    ) -> ExecuteRunResponse:
        assistant_message = self._assistant_message(
            prepared.instructions, status="FAILED", error=error, retry_count=0
        )
        self._record_threads(
            prepared.annotations,
            prepared.thread_ids,
            run_id=prepared.run_id,
            assistant_message=assistant_message,
            status="FAILED",
            sandbox_id=prepared.handle.sandbox_id,
            files=prepared.current_files,
            patch="",
            changed_files=[],
            retry_count=0,
            scope=prepared.memory_scope,
        )
        self._maybe_destroy(prepared.handle.sandbox_id, prepared.request)
        response = self._to_response(
            prepared.run_id,
            prepared.handle.sandbox_id,
            prepared.handle.preview_url,
            {
                **prepared.initial_state,
                "status": "FAILED",
                "error": error,
                "error_code": code,
                "messages": [
                    {
                        "role": "assistant",
                        "node": "service",
                        "content": error,
                        "level": "error",
                    }
                ],
            },
            reused_sandbox=prepared.reused_sandbox,
            assistant_message=assistant_message,
        )
        response.token_usage = dict(prepared.token_usage)
        with _ACTIVE_LOCK:
            active.response = response
            active.status = "FAILED"
            active.touch()
        prepared.event_buffer.emit(
            "run.failed",
            {
                "code": code,
                "message": error,
                "retryable": False,
                "retryCount": 0,
                "sandboxId": prepared.handle.sandbox_id,
                "previewUrl": prepared.handle.preview_url,
                "usage": prepared.token_usage,
            },
            dedupe_key=f"run.failed:{prepared.run_id}",
        )
        return response

    # --------------------------------------------------------
    # 运行注册与事件启动
    # --------------------------------------------------------
    def _claim_run(
        self,
        request: ExecuteRunRequest,
        *,
        run_id: str,
        fingerprint: str,
        thread_ids: list[str],
    ) -> tuple[ActiveRun, bool]:
        with _ACTIVE_LOCK:
            existing = _ACTIVE_RUNS.get(run_id)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise RunIdConflictError(f"runId={run_id} 已存在且请求内容不同")
                return existing, False

            # “同一 thread 正在执行”必须同时比较项目：不同项目的同名
            # threadId 是两条完全独立的会话，不能互相阻塞或共享状态。
            requested = {(request.project_id, thread_id) for thread_id in thread_ids}
            for item in _ACTIVE_RUNS.values():
                if item.run_id == run_id or item.is_terminal:
                    continue
                active_threads = {
                    (item.request.project_id, thread_id)
                    for thread_id in item.thread_ids
                }
                if requested.intersection(active_threads):
                    raise RunBusyError(
                        "当前标注仍有未完成的修改，请先完成澄清或等待执行结束"
                    )

            active = ActiveRun(
                run_id=run_id,
                fingerprint=fingerprint,
                request=request,
                buffer=get_event_buffer(
                    run_id,
                    trace_id=_trace_id(request),
                    max_events=self._settings.agent_event_buffer_size,
                ),
                thread_ids=thread_ids,
            )
            _ACTIVE_RUNS[run_id] = active
            return active, True

    def _emit_run_started(self, active: ActiveRun) -> None:
        active.buffer.emit(
            "run.started",
            {
                "status": "RUNNING",
                # 沙盒在 provision 阶段创建，此时尚未知；provision 完成后
                # 前端可通过 node.finished / 终态事件拿到 sandboxId。
                "sandboxId": None,
                "reusedSandbox": False,
            },
            dedupe_key=f"run.started:{active.run_id}",
        )

    def _emit_prepare_failure(self, active: ActiveRun, exc: Exception) -> None:
        code = _error_code(exc)
        with _ACTIVE_LOCK:
            active.status = "FAILED"
            active.touch()
        active.buffer.emit(
            "run.failed",
            {
                "code": code,
                "message": str(exc),
                "retryable": code in {"SANDBOX_DOCKER_UNAVAILABLE", "INTERNAL_ERROR"},
                "retryCount": 0,
                "sandboxId": None,
            },
            dedupe_key=f"run.failed:{active.run_id}",
        )

    def _mark_failed_before_prepared(self, active: ActiveRun, exc: Exception) -> None:
        self._emit_prepare_failure(active, exc)

    # --------------------------------------------------------
    # 回滚（单标注 / 删除标注）
    # --------------------------------------------------------
    def rollback(self, request: RollbackRunRequest) -> RollbackRunResponse:
        """把沙盒代码与单个 thread 的上下文回滚到指定成功轮次。

        顺序很重要：
          1. 先校验 thread checkpoint 与前端 commit 是否指向同一轮；
          2. 再执行 ``git reset --hard``，失败时绝不修改 thread 记忆；
          3. 最后恢复 ThreadMemoryStore 的 chatHistory / runs / files 快照，
             丢弃该轮之后的所有上下文。

        当前 Git 历史是全局线性的：回滚线程 B 也会丢弃 B 之后的 A/C 提交。
        M1.5 接受该限制；M2 用事件溯源重放未撤销标注的意图。
        """
        handle = self._sandbox.get(request.sandbox_id)
        # 回滚也必须带租户 / 项目作用域；没有 projectId 时 scope 为空，
        # 只能执行 Git 回滚，绝不能误读其它项目的同名 thread 记忆。
        rollback_scope = memory_scope(request.tenant_id, request.project_id)
        checkpoint = self._threads.checkpoint(
            request.thread_id,
            run_id=request.run_id,
            turn_count=request.turn_count,
            commit_hash=request.commit_hash,
            scope=rollback_scope,
        )
        if checkpoint and checkpoint.get("commitHash") != request.commit_hash:
            raise RollbackTargetMismatchError(
                "回滚目标与 thread checkpoint 不一致："
                f"request={request.commit_hash}, "
                f"checkpoint={checkpoint.get('commitHash')}"
            )

        self._sandbox.git_reset_hard(request.sandbox_id, request.commit_hash)
        restored = self._threads.rollback_to_checkpoint(
            request.thread_id,
            run_id=request.run_id,
            turn_count=request.turn_count,
            commit_hash=request.commit_hash,
            scope=rollback_scope,
        )
        if restored is None:
            # python-ai 重启后内存 checkpoint 可能不存在；Git 回滚仍然有效，
            # 前端会按消息坐标自行截断本地历史。
            logger.warning(
                "thread checkpoint missing thread_id=%s run_id=%s commit=%s",
                request.thread_id,
                request.run_id,
                request.commit_hash,
            )

        revision = str(int(time.time() * 1000))
        separator = "&" if "?" in handle.preview_url else "?"
        preview_url = f"{handle.preview_url}{separator}v={revision}"
        return RollbackRunResponse(
            sandbox_id=request.sandbox_id,
            thread_id=request.thread_id,
            commit_hash=request.commit_hash,
            turn_count=(restored or checkpoint or {}).get("turnCount")
            or request.turn_count,
            run_id=(restored or checkpoint or {}).get("runId") or request.run_id,
            checkpoint_restored=restored is not None,
            preview_url=preview_url,
            preview_revision=revision,
        )

    # --------------------------------------------------------
    # 澄清状态
    # --------------------------------------------------------
    def _expire_if_needed(self, active: ActiveRun) -> None:
        clarification = active.clarification
        if clarification is None or active.status != "AWAITING_CLARIFICATION":
            return
        if clarification.expires_at is None:
            return
        expires_at = clarification.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) <= expires_at:
            return
        clarification.status = "EXPIRED"
        active.clarification = None
        active.status = "EXPIRED"
        active.touch()
        active.buffer.emit(
            "clarification.expired",
            {
                "clarificationId": clarification.clarification_id,
                "expiredAt": datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            },
            dedupe_key=f"clarification.expired:{clarification.clarification_id}",
        )

    @staticmethod
    def _normalize_answers(
        clarification: Clarification,
        request: ClarificationAnswersRequest,
    ) -> list[dict[str, Any]]:
        answers: list[dict[str, Any]] = []
        answered_ids: set[str] = set()
        for item in request.answers:
            value = item.value if item.value is not None else item.text
            if value is None or value == "":
                continue
            answers.append(
                {
                    "questionId": item.question_id,
                    "value": value,
                    "text": item.text,
                }
            )
            answered_ids.add(item.question_id)

        if request.use_defaults:
            for question in clarification.questions:
                if question.question_id in answered_ids:
                    continue
                if question.default_value is None:
                    continue
                answers.append(
                    {
                        "questionId": question.question_id,
                        "value": question.default_value,
                        "text": None,
                    }
                )

        required = {
            question.question_id
            for question in clarification.questions
            if question.required
        }
        answered = {item["questionId"] for item in answers}
        missing = required - answered
        if missing:
            raise ClarificationAnswersRequiredError(
                f"缺少必填澄清回答: {sorted(missing)}"
            )

        # 开发者可在主输入栏 / 自定义折叠栏补充大段说明。服务端把 comment
        # 作为一条伪回答并入上下文：它不参与必填校验，但会随 answers 进入
        # Planner Prompt，并被 _augment_context_with_answers 写回标注指令，
        # 确保 Coder 也能看到。
        comment = (request.comment or "").strip()
        if comment:
            answers.append(
                {
                    "questionId": "__developer_comment__",
                    "value": comment,
                    "text": None,
                }
            )
        return answers

    # --------------------------------------------------------
    # 内部工具
    # --------------------------------------------------------
    @staticmethod
    def _thread_ids(request: ExecuteRunRequest) -> list[str]:
        return list(
            dict.fromkeys(
                RunExecutionService._thread_id(annotation, index)
                for index, annotation in enumerate(
                    request.effective_annotations, start=1
                )
            )
        )

    @staticmethod
    def _thread_id(annotation: AnnotationEnvelope, index: int) -> str:
        """每个标注拥有独立 threadId；旧客户端回退到 annotationId。"""
        return (
            annotation.thread_id
            or annotation.annotation_id
            or f"thread_annotation_{index}"
        )

    @staticmethod
    def _annotation_instruction(annotation: AnnotationEnvelope) -> str:
        text = str(annotation.payload.get("text") or "").strip()
        if text:
            return text
        intent = annotation.intent
        if intent and intent.ui and intent.ui.summary:
            return str(intent.ui.summary).strip()
        return "继续按当前标注修改 UI"

    def _merge_thread_contexts(
        self,
        annotations: list[AnnotationEnvelope],
        thread_ids: list[str],
        *,
        scope: str,
    ) -> dict[str, dict[str, Any]]:
        """把前端历史与服务端记忆合并为模型可读的 threadContext。"""
        return {
            thread_id: self._threads.merge_context(
                thread_id,
                annotation.chat_history,
                scope=scope,
            )
            for annotation, thread_id in zip(annotations, thread_ids)
        }

    def _open_sandbox(
        self,
        request: ExecuteRunRequest,
        target_files: list[str],
        thread_ids: list[str],
        *,
        scope: str,
    ) -> tuple[SandboxHandle, bool, dict[str, str]]:
        """优先复用当前沙盒；失败时根据 thread 源码快照重建。"""
        candidates: list[str] = []
        if request.sandbox_id:
            candidates.append(request.sandbox_id)
        for thread_id in thread_ids:
            stored = self._threads.sandbox_id(thread_id, scope=scope)
            if stored and stored not in candidates:
                candidates.append(stored)

        for sandbox_id in candidates:
            try:
                handle = self._sandbox.get(sandbox_id)
                if handle.project_id and handle.project_id != request.project_id:
                    logger.warning(
                        "skip sandbox %s from another project %s",
                        sandbox_id,
                        handle.project_id,
                    )
                    continue
                current_files = {
                    path: self._sandbox.read_file(sandbox_id, path)
                    for path in target_files
                }
                logger.info(
                    "reuse sandbox project_id=%s sandbox_id=%s",
                    request.project_id,
                    sandbox_id,
                )
                return handle, True, current_files
            except SandboxError as exc:
                logger.warning(
                    "failed to reuse sandbox %s, fallback to new: %s",
                    sandbox_id,
                    exc,
                )

        # 沙盒已销毁时，用服务端保存的最近源码快照重建，避免多轮修改丢失。
        seed_files = dict(request.files)
        snapshot = self._threads.seed_files(thread_ids, scope=scope)
        if snapshot:
            seed_files.update(snapshot)

        handle = self._sandbox.create(
            project_id=request.project_id,
            files=seed_files,
            template="react-vite",
        )
        try:
            current_files = {
                path: self._sandbox.read_file(handle.sandbox_id, path)
                for path in target_files
            }
        except SandboxError as exc:
            self._sandbox.destroy(handle.sandbox_id)
            raise TargetFileNotFoundError(
                f"读取目标文件失败 {list(target_files)}: {exc}"
            ) from exc
        return handle, False, current_files

    @staticmethod
    def _assistant_message(
        instructions: list[str],
        *,
        status: str,
        error: str | None,
        retry_count: int,
    ) -> str:
        """生成与前端聊天记录、服务端 thread 记忆一致的一条助手回复。"""
        if status in {"SUCCEEDED", "AWAITING_APPROVAL"}:
            summary = "；".join(item for item in instructions if item)
            suffix = f"（自动修复 {retry_count} 次）" if retry_count else ""
            return f"已应用：{summary[:300]}{suffix}"
        if status == "REJECTED":
            return "已拒绝本次修改，沙盒代码已回滚到执行前状态。"
        if status == "CANCELLED":
            return "已取消本次修改。"
        reason = str(error or "构建验证未通过").strip()
        return f"执行失败：{reason[:300]}"

    def _record_threads(
        self,
        annotations: list[AnnotationEnvelope],
        thread_ids: list[str],
        *,
        run_id: str,
        assistant_message: str,
        status: str,
        sandbox_id: str,
        files: dict[str, str],
        patch: str,
        changed_files: list[str],
        retry_count: int,
        turn_counts: dict[str, int] | None = None,
        commit_hash: str | None = None,
        baseline_commit: str | None = None,
        scope: str | None = None,
    ) -> None:
        """把本轮对话与修改记录写入每个标注自己的 thread。"""
        user_messages: dict[str, Any] = {}
        for annotation, thread_id in zip(annotations, thread_ids):
            last = annotation.chat_history[-1] if annotation.chat_history else None
            if last is not None and last.role == "user":
                user_messages[thread_id] = last
                continue
            content = self._annotation_instruction(annotation)
            user_messages[thread_id] = {
                "role": "user",
                "content": content,
                "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        self._threads.record_run(
            thread_ids,
            run_id=run_id,
            user_messages=user_messages,
            assistant_message=assistant_message,
            success=status == "SUCCEEDED",
            sandbox_id=sandbox_id,
            files=files,
            patch=patch,
            changed_files=changed_files,
            retry_count=retry_count,
            turn_counts=turn_counts,
            commit_hash=commit_hash,
            baseline_commit=baseline_commit,
            scope=scope,
        )

    def _maybe_write_local_summary(
        self,
        prepared: PreparedRun,
        *,
        status: str,
        changed_files: list[str],
        assistant_message: str,
    ) -> None:
        """每 N 个成功轮次生成一条 local_summary.md。

        - 只统计成功轮次（失败 / 拒绝不污染项目记忆）；
        - 以 project 为作用域，多个标注 thread 共用一份项目摘要；
        - 同一 turn 幂等：多进程 / 重试不会重复追加。
        """
        if status != "SUCCEEDED":
            return
        interval = int(self._settings.history_local_summary_interval)
        turn = int(prepared.run_turn_count or 0)
        if interval <= 0 or turn <= 0 or turn % interval != 0:
            return
        try:
            memory = get_project_memory(
                self._settings,
                prepared.request.tenant_id,
                prepared.request.project_id,
            )
            if memory.has_local_summary(turn):
                return
            transcript = self._threads.transcript(
                prepared.thread_ids,
                scope=prepared.memory_scope,
                limit=60,
            )
            extras: list[str] = []
            if changed_files:
                extras.append("本轮修改文件：" + "、".join(changed_files))
            if assistant_message:
                extras.append("本轮结果：" + assistant_message[:500])
            if extras:
                transcript = f"{transcript}\n" + "\n".join(extras)
            summary = ContextSummarizer(self._settings).local_summary(
                transcript,
                int(self._settings.history_local_summary_max_chars),
            )
            if summary:
                memory.append_local_summary(
                    summary,
                    turn=turn,
                    run_id=prepared.run_id,
                )
        except Exception:  # noqa: BLE001 - 记忆写入失败不能影响 Run 终态
            logger.warning(
                "write local summary failed run_id=%s",
                prepared.run_id,
                exc_info=True,
            )

    @staticmethod
    def _resolve_target_files(request: ExecuteRunRequest) -> list[str]:
        """按标注顺序去重目标文件；没有源码定位时回退到默认入口。

        BATCH_LAYOUT 把多个新增组件 / 位移藏在 payload 里，顶层 target
        只代表第一个落点；必须把每个嵌套目标文件都加载进来，否则 Coder
        在跨文件批次里读不到真实源码。
        """
        files: list[str] = []

        def add(raw_file: Any) -> None:
            file = str(raw_file or "").strip()
            if file and file not in files:
                files.append(file)

        for annotation in request.effective_annotations:
            target = annotation.target
            add(
                target.source.file
                if target and target.source and target.source.file
                else DEFAULT_TARGET_FILE
            )
            # 可复用组件的 DOM 节点落在组件文件里，但局部修改必须在
            # callSite 所在的父级组件完成；两个文件都要加载，Coder 才能
            # 改调用点而不去污染基础组件。
            if target and target.source and target.source.call_site:
                add(target.source.call_site.file)
            if str(annotation.action).upper() != "BATCH_LAYOUT":
                continue
            payload = annotation.payload or {}
            for addition in payload.get("additions") or []:
                if not isinstance(addition, dict):
                    continue
                nested = addition.get("annotation")
                nested = nested if isinstance(nested, dict) else {}
                nested_target = nested.get("target")
                nested_target = nested_target if isinstance(nested_target, dict) else {}
                nested_source = nested_target.get("source")
                nested_source = nested_source if isinstance(nested_source, dict) else {}
                add(nested_source.get("file"))
                call_site = nested_source.get("callSite")
                if isinstance(call_site, dict):
                    add(call_site.get("file"))
            for move in payload.get("moves") or []:
                if isinstance(move, dict):
                    add(move.get("file"))
        return files or [DEFAULT_TARGET_FILE]

    @staticmethod
    def _build_annotation_context(
        request: ExecuteRunRequest,
        target_files: list[str],
        thread_contexts: dict[str, dict[str, Any]] | None = None,
        current_files: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """构造 Planner / Coder 需要的逐标注上下文。"""
        contexts: list[dict[str, Any]] = []
        fallback_file = target_files[0] if target_files else DEFAULT_TARGET_FILE
        for index, annotation in enumerate(request.effective_annotations, start=1):
            intent = annotation.intent
            ui = intent.ui if intent else None
            target = annotation.target
            source = target.source if target else None
            thread_id = (
                annotation.thread_id
                or annotation.annotation_id
                or f"thread_annotation_{index}"
            )
            # mode="json" 必须显式指定：默认 python mode 会保留 datetime
            # 对象，后续进入 Prompt 的 json.dumps / SSE 时会直接抛
            # “Object of type datetime is not JSON serializable”。
            annotation_payload = annotation.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            )
            # 前端 target.snippet 是圈选时的初始源码；多轮对话必须用沙盒当前
            # 源码覆盖，避免模型同时看到“旧片段 + 新文件”后回滚上一轮修改。
            # 但绝不能把整份文件塞回 snippet：UI Coder 只应看到目标节点附近
            # 的 JSX 窗口（父级 3 层 + 兄弟节点），否则大项目会直接超时。
            target_file = source.file if source and source.file else fallback_file
            current_source = (current_files or {}).get(target_file)
            nested_target = annotation_payload.get("target")
            if current_source and isinstance(nested_target, dict):
                snippet = nested_target.get("snippet")
                if isinstance(snippet, dict):
                    window = extract_jsx_window(
                        current_source,
                        selector=target.selector if target else None,
                        component=source.component if source else None,
                        line=source.line if source else None,
                    )
                    snippet["content"] = window.text
                    snippet["startLine"] = window.start_line
                    snippet["endLine"] = window.end_line
                    snippet["totalLines"] = window.total_lines
                    snippet["truncated"] = window.truncated
            annotation_payload["threadContext"] = (
                thread_contexts.get(thread_id)
                if thread_contexts
                else {
                    "threadId": thread_id,
                    "chatHistory": [],
                    "previousRuns": [],
                }
            )
            snippet_meta = (
                nested_target.get("snippet")
                if isinstance(nested_target, dict)
                else None
            )
            window_meta = (
                {
                    key: snippet_meta.get(key)
                    for key in ("startLine", "endLine", "totalLines", "truncated")
                }
                if isinstance(snippet_meta, dict)
                else {}
            )
            contexts.append(
                {
                    "index": index,
                    "annotation_id": annotation.annotation_id,
                    "thread_id": thread_id,
                    # 新协议语义：CREATE_NEW / MODIFY_EXISTING。
                    # annotation.action 可能是旧别名 ADD / MODIFY，Planner
                    # 与 Coder 统一读取 intent_action，避免两条分支漂移。
                    "action": annotation.intent_action,
                    "intent_action": annotation.intent_action,
                    "legacy_action": annotation.action,
                    "annotation": annotation_payload,
                    "intent_kind": intent.kind if intent else "UI_ONLY",
                    "instruction": str(annotation.payload.get("text") or "").strip(),
                    "ui_summary": (ui.summary if ui else None)
                    or annotation.payload.get("text", ""),
                    "acceptance": ui.acceptance if ui else [],
                    "business": (intent.business if intent else None),
                    "target_file": (
                        source.file if source and source.file else fallback_file
                    ),
                    "selector": target.selector if target else None,
                    "component": source.component if source else None,
                    "target_snippet": (
                        target.snippet.model_dump(by_alias=True, exclude_none=True)
                        if target and target.snippet
                        else None
                    ),
                    # 源码窗口元数据：只做审计 / 调试，不直接进入 UI Prompt。
                    # 完整源码仍保留在 state.current_files，供 Applier / Verifier
                    # 写入与构建使用。
                    "source_window": window_meta,
                }
            )
        return {
            "count": len(contexts),
            "annotations": contexts,
            "target_files": target_files,
        }

    @staticmethod
    def _to_response(
        run_id: str,
        sandbox_id: str,
        preview_url: str,
        state: dict[str, Any],
        *,
        reused_sandbox: bool = False,
        assistant_message: str = "",
        commit_hash: str | None = None,
        turn_count: int = 0,
        baseline_commit: str | None = None,
        commits: list[RunCommitRecord] | None = None,
        clarification: Clarification | None = None,
        approval: Approval | None = None,
    ) -> ExecuteRunResponse:
        verify_state = state.get("verify_result") or {}
        verify_result = VerifyResultModel(**verify_state) if verify_state else None
        status = state.get("status")
        if status not in {
            "AWAITING_APPROVAL",
            "AWAITING_CLARIFICATION",
            "SUCCEEDED",
            "REJECTED",
            "CANCELLED",
            "EXPIRED",
            "FAILED",
        }:
            status = "FAILED"

        return ExecuteRunResponse(
            run_id=run_id,
            status=status,
            sandbox_id=sandbox_id,
            preview_url=preview_url,
            reused_sandbox=reused_sandbox,
            plan=state.get("plan", ""),
            generated_patch=state.get("generated_patch", ""),
            generated_files=state.get("generated_files", {}),
            verify_result=verify_result,
            retry_count=int(state.get("retry_count", 0)),
            messages=state.get("messages", []),
            assistant_message=assistant_message,
            commit_hash=commit_hash,
            turn_count=turn_count,
            baseline_commit=baseline_commit,
            commits=commits or [],
            clarification=clarification,
            approval=approval,
            token_usage=dict(state.get("token_usage") or {}),
            error=state.get("error"),
            error_code=state.get("error_code"),
            background_available=bool(state.get("background_available")),
        )

    def _maybe_destroy(self, sandbox_id: str, request: ExecuteRunRequest) -> None:
        if request.options.keep_sandbox:
            return
        try:
            self._sandbox.destroy(sandbox_id)
        except SandboxError:
            logger.warning("failed to destroy sandbox %s", sandbox_id, exc_info=True)


class RunCheckpointLostError(RunServiceError):
    code = "RUN_CHECKPOINT_LOST"
    status_code = 409


# ------------------------------------------------------------
# 模块级工具函数
# ------------------------------------------------------------
def _new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:20]}"


def _trace_id(request: ExecuteRunRequest) -> str | None:
    # 03 的 traceId 由 BFF 的 W3C traceparent 注入；M2 先用 runId 派生一个
    # 可关联的短 ID，避免事件里出现空字段。
    return None


def _request_fingerprint(request: ExecuteRunRequest) -> str:
    payload = request.model_dump_json(by_alias=True, exclude_none=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_decision(raw: str) -> str:
    """把前端 / BFF 的决策写法归一为 approve / reject。"""
    normalized = (raw or "").strip().lower()
    if normalized in {"approve", "approved", "accept", "accepted", "ok"}:
        return "approve"
    if normalized in {"reject", "rejected", "deny", "denied", "takeover"}:
        return "reject"
    raise ApprovalStateConflictError(f"不支持的审批决策: {raw}")


def _find_approval(active: ActiveRun, approval_id: str | None) -> Approval | None:
    if not approval_id:
        return None
    for item in active.approvals:
        if item.approval_id == approval_id:
            return item
    return None


def _error_code(exc: Exception) -> str:
    if isinstance(exc, RunServiceError):
        return exc.code
    if isinstance(exc, AgentLLMConfigurationError):
        return "LLM_NOT_CONFIGURED"
    if isinstance(exc, FullStackNotSupportedError):
        return "FULLSTACK_NOT_SUPPORTED"
    if isinstance(exc, TargetFileNotFoundError):
        return "TARGET_FILE_NOT_FOUND"
    if isinstance(exc, SandboxError):
        return "SANDBOX_ERROR"
    if isinstance(exc, DockerException):
        return "SANDBOX_DOCKER_UNAVAILABLE"
    return "INTERNAL_ERROR"
