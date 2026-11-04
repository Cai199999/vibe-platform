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

from ai_core.agent.checkpoint import (
    CheckpointerProvider,
    get_checkpointer_provider,
)
from ai_core.agent.context_doc import (
    VIBE_CONTEXT_FILENAME,
    render_vibe_context,
)
from ai_core.agent.events import RunEventBuffer, get_event_buffer
from ai_core.agent.graph import AgentGraphRunner, GraphRunOutcome
from ai_core.agent.llm import (
    AgentLLM,
    AgentLLMConfigurationError,
    AgentLLMError,
    build_agent_llm,
)
from ai_core.agent.schemas import (
    AnnotationEnvelope,
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
from ai_core.config import Settings, get_settings
from ai_core.sandbox import get_sandbox_provider
from ai_core.sandbox.base import SandboxError, SandboxHandle, SandboxProvider

logger = logging.getLogger(__name__)

DEFAULT_TARGET_FILE = "src/App.jsx"
TERMINAL_RUN_STATUSES = {
    "AWAITING_APPROVAL",
    "FAILED",
    "EXPIRED",
    "CANCELLED",
}
ACTIVE_RUN_STATUSES = {
    "RUNNING",
    "PLANNING",
    "CODING",
    "APPLYING",
    "VERIFYING",
    "REPAIRING",
    "AWAITING_CLARIFICATION",
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
    instructions: list[str]
    turn_counts: dict[str, int]
    run_turn_count: int
    handle: SandboxHandle
    reused_sandbox: bool
    current_files: dict[str, str]
    initial_state: dict[str, Any]
    event_buffer: RunEventBuffer


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
        try:
            # 先构造 LLM，避免密钥缺失时已经创建了沙盒。
            llm = build_agent_llm(request, self._settings)

            turn_counts = {
                thread_id: self._threads.next_turn(thread_id, annotation.chat_history)
                for annotation, thread_id in zip(annotations, thread_ids)
            }
            run_turn_count = max(turn_counts.values(), default=1)

            handle, reused_sandbox, current_files = self._open_sandbox(
                request, target_files, thread_ids
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

            thread_contexts = self._merge_thread_contexts(annotations, thread_ids)
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
                "plan": "",
                "generated_patch": "",
                "generated_files": {},
                "verify_result": {},
                "apply_failed": False,
                "retry_count": 0,
                "max_retries": min(request.options.max_retries, 3),
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
            instructions=instructions,
            turn_counts=turn_counts,
            run_turn_count=run_turn_count,
            handle=handle,
            reused_sandbox=reused_sandbox,
            current_files=current_files,
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
        )
        try:
            outcome = runner.run(
                prepared.initial_state,  # type: ignore[arg-type]
                thread_id=prepared.run_id,
                resume=resume,
            )
        except AgentLLMError as exc:
            logger.exception("agent llm failed run_id=%s", prepared.run_id)
            return self._complete_failure(
                active,
                prepared,
                error=f"模型执行失败: {exc}",
                code="LLM_EXECUTION_FAILED",
            )
        except SandboxError as exc:
            logger.exception("agent sandbox failed run_id=%s", prepared.run_id)
            return self._complete_failure(
                active,
                prepared,
                error=f"沙盒执行失败: {exc}",
                code="SANDBOX_ERROR",
            )
        except Exception as exc:  # noqa: BLE001 - 统一转为 run.failed
            logger.exception("agent unexpected failure run_id=%s", prepared.run_id)
            return self._complete_failure(
                active,
                prepared,
                error=f"执行失败: {exc}",
                code="INTERNAL_ERROR",
            )

        if outcome.interrupted:
            if outcome.interrupt:
                return self._complete_paused(active, prepared, outcome)
            return self._complete_failure(
                active,
                prepared,
                error="状态机已暂停但缺少澄清内容，checkpoint 可能已损坏",
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

        commit_hash: str | None = None
        baseline_commit: str | None = None
        commits: list[RunCommitRecord] = []
        if status == "AWAITING_APPROVAL":
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
            except SandboxError as exc:
                # 代码已经构建通过，但没有可回滚的 checkpoint 不能宣称成功。
                logger.exception("git checkpoint failed run_id=%s", prepared.run_id)
                status = "FAILED"
                error = f"Git checkpoint 失败: {exc}"
                error_code = "COMMIT_FAILED"
                commit_hash = None
                commits = []

        assistant_message = self._assistant_message(
            prepared.instructions,
            status=status,
            error=error,
            retry_count=retry_count,
        )
        final_files = dict(final_state.get("current_files") or prepared.current_files)
        generated_files = final_state.get("generated_files") or {}
        generated_patch = str(final_state.get("generated_patch") or "")
        changed_files = sorted(generated_files) or list(
            prepared.initial_state["target_files"]
        )
        response = self._to_response(
            prepared.run_id,
            prepared.handle.sandbox_id,
            prepared.handle.preview_url,
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
        )
        self._maybe_destroy(prepared.handle.sandbox_id, prepared.request)
        with _ACTIVE_LOCK:
            active.response = response
            active.status = status
            active.touch()

        if status == "AWAITING_APPROVAL":
            prepared.event_buffer.emit(
                "run.succeeded",
                {
                    "status": status,
                    "sandboxId": prepared.handle.sandbox_id,
                    "previewUrl": prepared.handle.preview_url,
                    "commitHash": commit_hash,
                    "baselineCommit": baseline_commit,
                    "retryCount": retry_count,
                    "assistantMessage": assistant_message,
                    # 前端据此渲染每条助手消息的“回滚到此步”。
                    "turnCount": prepared.run_turn_count,
                    "commits": [item.model_dump(by_alias=True) for item in commits],
                },
                dedupe_key=f"run.succeeded:{prepared.run_id}",
            )
        else:
            prepared.event_buffer.emit(
                "run.failed",
                {
                    "code": error_code or "VERIFY_FAILED",
                    "message": error or "构建验证未通过",
                    "retryable": False,
                    "retryCount": retry_count,
                    "sandboxId": prepared.handle.sandbox_id,
                    "previewUrl": prepared.handle.preview_url,
                },
                dedupe_key=f"run.failed:{prepared.run_id}",
            )
        return response

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

            requested_threads = set(thread_ids)
            for item in _ACTIVE_RUNS.values():
                if item.run_id == run_id or item.is_terminal:
                    continue
                if requested_threads.intersection(item.thread_ids):
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
    ) -> dict[str, dict[str, Any]]:
        """把前端历史与服务端记忆合并为模型可读的 threadContext。"""
        return {
            thread_id: self._threads.merge_context(thread_id, annotation.chat_history)
            for annotation, thread_id in zip(annotations, thread_ids)
        }

    def _open_sandbox(
        self,
        request: ExecuteRunRequest,
        target_files: list[str],
        thread_ids: list[str],
    ) -> tuple[SandboxHandle, bool, dict[str, str]]:
        """优先复用当前沙盒；失败时根据 thread 源码快照重建。"""
        candidates: list[str] = []
        if request.sandbox_id:
            candidates.append(request.sandbox_id)
        for thread_id in thread_ids:
            stored = self._threads.sandbox_id(thread_id)
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
        snapshot = self._threads.seed_files(thread_ids)
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
        if status == "AWAITING_APPROVAL":
            summary = "；".join(item for item in instructions if item)
            suffix = f"（自动修复 {retry_count} 次）" if retry_count else ""
            return f"已应用：{summary[:300]}{suffix}"
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
            success=status == "AWAITING_APPROVAL",
            sandbox_id=sandbox_id,
            files=files,
            patch=patch,
            changed_files=changed_files,
            retry_count=retry_count,
            turn_counts=turn_counts,
            commit_hash=commit_hash,
            baseline_commit=baseline_commit,
        )

    @staticmethod
    def _resolve_target_files(request: ExecuteRunRequest) -> list[str]:
        """按标注顺序去重目标文件；没有源码定位时回退到默认入口。"""
        files: list[str] = []
        for annotation in request.effective_annotations:
            file = None
            target = annotation.target
            if target and target.source and target.source.file:
                file = target.source.file
            file = file or DEFAULT_TARGET_FILE
            if file not in files:
                files.append(file)
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
            annotation_payload = annotation.model_dump(by_alias=True, exclude_none=True)
            # 前端 target.snippet 是圈选时的初始源码；多轮对话必须用沙盒当前
            # 源码覆盖，避免模型同时看到“旧片段 + 新文件”后回滚上一轮修改。
            target_file = source.file if source and source.file else fallback_file
            current_source = (current_files or {}).get(target_file)
            nested_target = annotation_payload.get("target")
            if current_source and isinstance(nested_target, dict):
                snippet = nested_target.get("snippet")
                if isinstance(snippet, dict):
                    snippet["content"] = current_source
                    snippet["startLine"] = 1
                    snippet["endLine"] = len(current_source.splitlines())
            annotation_payload["threadContext"] = (
                thread_contexts.get(thread_id)
                if thread_contexts
                else {
                    "threadId": thread_id,
                    "chatHistory": [],
                    "previousRuns": [],
                }
            )
            contexts.append(
                {
                    "index": index,
                    "annotation_id": annotation.annotation_id,
                    "thread_id": thread_id,
                    "action": annotation.action,
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
    ) -> ExecuteRunResponse:
        verify_state = state.get("verify_result") or {}
        verify_result = VerifyResultModel(**verify_state) if verify_state else None
        status = state.get("status")
        if status not in {
            "AWAITING_APPROVAL",
            "AWAITING_CLARIFICATION",
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
            error=state.get("error"),
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
