"""Architect 执行服务：后台 Graph、SSE 事件、澄清恢复与契约决策。

与 ``RunExecutionService`` 的结构保持相似，但状态、事件和 checkpoint
命名空间独立，避免全栈架构澄清污染局部 UI Run。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ai_core.adapters.checkpoint import get_checkpointer_provider
from ai_core.ports.checkpoint import CheckpointerProvider
from ai_core.agent.events import RunEventBuffer, get_event_buffer
from ai_core.architect.graph import (
    ArchitectGraphRunner,
    ArchitectRunCancelledError,
)
from ai_core.architect.llm import (
    ArchitectLLM,
    ArchitectLLMError,
    build_architect_llm,
)
from ai_core.domain.architect import (
    ArchitectAnswerResponse,
    ArchitectAnswersRequest,
    ArchitectCancelRequest,
    ArchitectCancelResponse,
    ArchitectClarification,
    ArchitectClarificationStatus,
    ArchitectContract,
    ArchitectContractStatus,
    ArchitectDecisionRequest,
    ArchitectDecisionResponse,
    ArchitectRunRequest,
    ArchitectRunStatus,
    ArchitectSnapshotResponse,
)
from ai_core.architect.source import collect_source
from ai_core.config import Settings, get_settings
from ai_core.domain.scope import scoped_checkpoint_key
from ai_core.ports.sandbox import SandboxProvider

logger = logging.getLogger(__name__)

TERMINAL_ARCHITECT_STATUSES = {"CONFIRMED", "CANCELLED", "FAILED"}


# ------------------------------------------------------------
# 领域异常
# ------------------------------------------------------------
class ArchitectServiceError(Exception):
    code = "ARCHITECT_ERROR"
    status_code = 409


class ArchitectRunNotFoundError(ArchitectServiceError):
    code = "ARCHITECT_RUN_NOT_FOUND"
    status_code = 404


class ArchitectRunIdConflictError(ArchitectServiceError):
    code = "ARCHITECT_RUN_ID_CONFLICT"
    status_code = 409


class ArchitectStateConflictError(ArchitectServiceError):
    code = "ARCHITECT_STATE_CONFLICT"
    status_code = 409


class ArchitectClarificationNotFoundError(ArchitectServiceError):
    code = "ARCHITECT_CLARIFICATION_NOT_FOUND"
    status_code = 404


class ArchitectAnswersRequiredError(ArchitectServiceError):
    code = "ARCHITECT_ANSWERS_REQUIRED"
    status_code = 422


class ArchitectContractNotFoundError(ArchitectServiceError):
    code = "ARCHITECT_CONTRACT_NOT_FOUND"
    status_code = 404


# ------------------------------------------------------------
# 运行期数据
# ------------------------------------------------------------
@dataclass
class ActiveArchitectRun:
    run_id: str
    fingerprint: str
    request: ArchitectRunRequest
    llm: ArchitectLLM
    buffer: RunEventBuffer
    status: str = "RUNNING"
    state: dict[str, Any] = field(default_factory=dict)
    source_hash: str = ""
    source_origin: str = "request"
    app_shape: str | None = None
    round: int = 0
    max_rounds: int = 5
    confidence_score: float = 0.0
    threshold: float = 0.85
    clarification: ArchitectClarification | None = None
    contract: ArchitectContract | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    error_code: str | None = None
    cancel_requested: bool = False
    cancel_reason: str | None = None
    # 暂停事件先于 graph.invoke 返回；用户可能立刻回答并启动下一段图。
    # 用 epoch 丢弃迟到的旧 outcome，避免旧澄清 / 契约覆盖新状态。
    execution_epoch: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_ARCHITECT_STATUSES

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)


_ACTIVE_RUNS: dict[str, ActiveArchitectRun] = {}
_ACTIVE_LOCK = threading.RLock()


def get_active_architect_run(run_id: str) -> ActiveArchitectRun | None:
    with _ACTIVE_LOCK:
        return _ACTIVE_RUNS.get(run_id)


def find_architect_contract(contract_id: str) -> ArchitectContract | None:
    """按 contractId 查找内存中的契约。

    Fullstack 请求允许只带 contractId；服务端从 Architect Run 注册表
    找回权威契约，避免前端回传过期快照。仅返回已确认契约，未确认的
    契约不能触发代码生成。
    """
    with _ACTIVE_LOCK:
        for active in _ACTIVE_RUNS.values():
            contract = active.contract
            if contract is None or contract.contract_id != contract_id:
                continue
            if contract.status != "CONFIRMED":
                return None
            return contract.model_copy(deep=True)
    return None


class ArchitectService:
    """Architect 执行入口。"""

    def __init__(
        self,
        sandbox: SandboxProvider | None = None,
        settings: Settings | None = None,
        checkpointer_provider: CheckpointerProvider | None = None,
        llm: ArchitectLLM | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._settings = settings or get_settings()
        self._checkpointer_provider = (
            checkpointer_provider or get_checkpointer_provider()
        )
        self._llm_override = llm

    # --------------------------------------------------------
    # 启动
    # --------------------------------------------------------
    def start_stream(self, request: ArchitectRunRequest) -> ActiveArchitectRun:
        run_id = request.run_id or f"arc_{uuid.uuid4().hex[:20]}"
        fingerprint = _request_fingerprint(request)
        llm = self._llm_override or build_architect_llm(
            request.options.llm_provider, self._settings
        )
        with _ACTIVE_LOCK:
            existing = _ACTIVE_RUNS.get(run_id)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise ArchitectRunIdConflictError(
                        f"Architect runId 已被其它请求占用: {run_id}"
                    )
                return existing

            active = ActiveArchitectRun(
                run_id=run_id,
                fingerprint=fingerprint,
                request=request,
                llm=llm,
                buffer=get_event_buffer(run_id),
                max_rounds=request.options.max_clarification_rounds,
                threshold=request.options.confidence_threshold,
            )
            _ACTIVE_RUNS[run_id] = active

        active.buffer.emit(
            "architect.started",
            {
                "runId": run_id,
                "projectId": request.project_id,
                "projectName": request.project_name,
                "threshold": active.threshold,
                "maxRounds": active.max_rounds,
                "llmProvider": request.options.llm_provider,
            },
            dedupe_key=f"architect.started:{run_id}",
        )
        threading.Thread(
            target=self._run_worker,
            args=(active,),
            name=f"architect-{run_id}",
            daemon=True,
        ).start()
        return active

    def _run_worker(self, active: ActiveArchitectRun) -> None:
        try:
            bundle = collect_source(
                active.request.source.sandbox_id,
                active.request.source.files,
                sandbox=self._sandbox,
                settings=self._settings,
            )
            active.source_hash = bundle.source_hash
            active.source_origin = bundle.origin
            initial_state = _initial_state(active, bundle)
            self._execute_graph(active, initial_state=initial_state)
        except ArchitectRunCancelledError:
            self._complete_cancelled(active, active.cancel_reason)
        except Exception as exc:  # noqa: BLE001 - 后台线程必须收敛
            logger.exception("architect run failed run_id=%s", active.run_id)
            self._complete_failure(active, exc)

    def _execute_graph(
        self,
        active: ActiveArchitectRun,
        *,
        initial_state: dict[str, Any] | None = None,
        resume: Any | None = None,
    ) -> None:
        runner = ArchitectGraphRunner(
            active.llm,
            event_sink=active.buffer,
            checkpointer_provider=self._checkpointer_provider,
            recursion_limit=self._settings.architect_graph_recursion_limit,
            cancel_check=lambda: active.cancel_requested,
            pause_hook=lambda event_type, payload: self._prepare_pause_state(
                active, event_type, payload
            ),
        )
        with _ACTIVE_LOCK:
            active.execution_epoch += 1
            epoch = active.execution_epoch
        outcome = runner.run(
            initial_state or {},
            run_id=active.run_id,
            # Architect 与 UI 使用不同 kind，且叠加 tenant/project 哈希；
            # 即使 runId 相同也不可能共享 checkpoint。
            thread_id=scoped_checkpoint_key(
                "architect_run",
                active.run_id,
                tenant_id=active.request.tenant_id,
                project_id=active.request.project_id,
            ),
            resume=resume,
        )
        self._apply_outcome(
            active,
            outcome.state,
            outcome.interrupted,
            outcome.interrupt,
            epoch=epoch,
        )

    def _prepare_pause_state(
        self,
        active: ActiveArchitectRun,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """在暂停事件对 SSE 客户端可见前，先把可操作状态写入内存。

        否则客户端收到 clarification.required / contract.ready 后立即提交，
        可能早于 ``graph.invoke`` 返回，命中 404/409 竞态。
        """
        with _ACTIVE_LOCK:
            if active.cancel_requested:
                return
            if event_type == "architect.clarification.required":
                clarification = ArchitectClarification.model_validate(payload)
                active.clarification = clarification
                active.status = "AWAITING_CLARIFICATION"
                active.round = clarification.round
                active.app_shape = clarification.app_shape or active.app_shape
                active.confidence_score = clarification.confidence_score
                active.threshold = clarification.threshold
            elif event_type == "architect.contract.ready":
                raw_contract = payload.get("contract") or {}
                contract = ArchitectContract.model_validate(raw_contract)
                contract.status = "AWAITING_CONFIRMATION"
                active.contract = contract
                active.clarification = None
                active.status = "AWAITING_CONTRACT_CONFIRMATION"
            active.touch()

    # --------------------------------------------------------
    # 澄清
    # --------------------------------------------------------
    def answer_clarification(
        self,
        run_id: str,
        clarification_id: str | None,
        request: ArchitectAnswersRequest,
    ) -> ArchitectAnswerResponse:
        active = _require_run(run_id)
        with _ACTIVE_LOCK:
            if active.status != "AWAITING_CLARIFICATION":
                raise ArchitectStateConflictError(
                    f"Architect 当前状态为 {active.status}，没有待回答的澄清"
                )
            clarification = active.clarification
            if clarification is None:
                raise ArchitectClarificationNotFoundError("当前没有待回答的澄清")
            if clarification_id and clarification.clarification_id != clarification_id:
                raise ArchitectClarificationNotFoundError(
                    f"澄清不存在: {clarification_id}"
                )

            question_ids = {item.question_id for item in clarification.questions}
            answers = [
                item.model_dump(by_alias=True)
                for item in request.answers
                if not item.question_id or item.question_id in question_ids
            ]
            if request.answers and not answers:
                raise ArchitectAnswersRequiredError("提交的 questionId 不属于当前澄清")
            if not answers and not (request.comment or "").strip():
                raise ArchitectAnswersRequiredError("至少回答一个问题或提供补充说明")

            answered_at = datetime.now(timezone.utc)
            clarification.status = "ANSWERED"
            clarification.answered_at = answered_at
            active.clarification = None
            active.status = "ANALYZING"
            active.touch()

        active.buffer.emit(
            "architect.clarification.answered",
            {
                "clarificationId": clarification.clarification_id,
                "round": clarification.round,
                "answers": answers,
                "comment": request.comment,
            },
            dedupe_key=(
                f"architect.clarification.answered:" f"{clarification.clarification_id}"
            ),
        )
        threading.Thread(
            target=self._resume_worker,
            args=(
                active,
                {
                    "answers": answers,
                    "comment": request.comment,
                },
            ),
            name=f"architect-resume-{run_id}",
            daemon=True,
        ).start()
        return ArchitectAnswerResponse(
            clarification=ArchitectClarificationStatus(
                clarification_id=clarification.clarification_id,
                status="ANSWERED",
                answered_at=answered_at,
            ),
            architect_run=ArchitectRunStatus(
                run_id=active.run_id,
                status="ANALYZING",
            ),
        )

    def _resume_worker(self, active: ActiveArchitectRun, resume: Any) -> None:
        try:
            self._execute_graph(active, resume=resume)
        except ArchitectRunCancelledError:
            self._complete_cancelled(active, active.cancel_reason)
        except Exception as exc:  # noqa: BLE001
            logger.exception("architect resume failed run_id=%s", active.run_id)
            self._complete_failure(active, exc)

    # --------------------------------------------------------
    # 契约决策
    # --------------------------------------------------------
    def decide_contract(
        self,
        run_id: str,
        contract_id: str | None,
        request: ArchitectDecisionRequest,
    ) -> ArchitectDecisionResponse:
        active = _require_run(run_id)
        expected_id = contract_id or request.contract_id
        decision = request.decision
        now = datetime.now(timezone.utc)

        with _ACTIVE_LOCK:
            contract = active.contract
            if contract is None:
                raise ArchitectContractNotFoundError("当前没有待确认的实施契约")
            if contract.contract_id != expected_id:
                raise ArchitectContractNotFoundError(f"实施契约不存在: {expected_id}")

            if active.is_terminal:
                idempotent = (
                    decision == "CONFIRM"
                    and active.status == "CONFIRMED"
                    and contract.status == "CONFIRMED"
                ) or (decision == "CANCEL" and active.status == "CANCELLED")
                if idempotent:
                    return _decision_response(active, contract, now)
                raise ArchitectStateConflictError(
                    f"Architect 已处于终态 {active.status}，不能再次决策"
                )

            if active.status != "AWAITING_CONTRACT_CONFIRMATION":
                raise ArchitectStateConflictError(
                    f"Architect 当前状态为 {active.status}，没有待确认契约"
                )
            if decision == "REVISE" and not (request.comment or "").strip():
                raise ArchitectStateConflictError("REVISE 必须提供 comment")

            active.status = {
                "CONFIRM": "CONFIRMING_CONTRACT",
                "REVISE": "REVISING_CONTRACT",
                "CANCEL": "CANCELLING",
            }[decision]
            active.touch()

        active.buffer.emit(
            "architect.contract.decision.received",
            {
                "contractId": contract.contract_id,
                "decision": decision,
                "comment": request.comment,
            },
            dedupe_key=(
                f"architect.contract.decision:"
                f"{contract.contract_id}:{decision}:"
                f"{int(active.state.get('contract_revision_count') or 0)}"
            ),
        )
        threading.Thread(
            target=self._resume_worker,
            args=(
                active,
                {
                    "decision": decision,
                    "comment": request.comment,
                },
            ),
            name=f"architect-decision-{run_id}",
            daemon=True,
        ).start()
        return _decision_response(active, contract, now)

    # --------------------------------------------------------
    # 取消 / 查询
    # --------------------------------------------------------
    def cancel(
        self, run_id: str, request: ArchitectCancelRequest
    ) -> ArchitectCancelResponse:
        active = _require_run(run_id)
        emit_cancel = False
        with _ACTIVE_LOCK:
            if active.is_terminal:
                return ArchitectCancelResponse(run_id=run_id, status=active.status)
            active.cancel_requested = True
            active.cancel_reason = request.reason
            if active.status in {
                "AWAITING_CLARIFICATION",
                "AWAITING_CONTRACT_CONFIRMATION",
            }:
                active.status = "CANCELLED"
                active.clarification = None
                if active.contract is not None:
                    active.contract.status = "CANCELLED"
                    active.contract.decided_at = datetime.now(timezone.utc)
                emit_cancel = True
            else:
                active.status = "CANCELLING"
            active.touch()

        if emit_cancel:
            self._emit_cancelled(active, request.reason)
            return ArchitectCancelResponse(run_id=run_id, status="CANCELLED")
        return ArchitectCancelResponse(run_id=run_id, status="CANCELLING")

    def snapshot(self, run_id: str) -> ArchitectSnapshotResponse:
        active = _require_run(run_id)
        with _ACTIVE_LOCK:
            return ArchitectSnapshotResponse(
                run_id=active.run_id,
                project_id=active.request.project_id,
                project_name=active.request.project_name,
                status=active.status,
                app_shape=active.app_shape,  # type: ignore[arg-type]
                round=active.round,
                max_rounds=active.max_rounds,
                confidence_score=active.confidence_score,
                threshold=active.threshold,
                source_hash=active.source_hash,
                last_seq=active.buffer.last_seq,
                clarification=(
                    active.clarification.model_copy(deep=True)
                    if active.clarification
                    else None
                ),
                contract=(
                    active.contract.model_copy(deep=True) if active.contract else None
                ),
                messages=[dict(item) for item in active.messages],
                error=active.error,
                error_code=active.error_code,
            )

    # --------------------------------------------------------
    # 状态推进
    # --------------------------------------------------------
    def _apply_outcome(
        self,
        active: ActiveArchitectRun,
        state: dict[str, Any],
        interrupted: bool,
        interrupt_payload: dict[str, Any] | None,
        *,
        epoch: int,
    ) -> None:
        with _ACTIVE_LOCK:
            if epoch != active.execution_epoch:
                # 旧图已过期：新一轮回答 / 决策已经启动，禁止回写旧状态。
                return
            if active.cancel_requested:
                # 取消请求与 graph 完成可能并发；取消优先，避免终态事件
                # 在 cancelled 之后又把状态改回可继续。
                active.state = state
                active.touch()
                should_cancel = True
            else:
                active.state = state
                active.round = int(state.get("round") or 0)
                active.app_shape = state.get("app_shape") or None
                active.confidence_score = float(state.get("confidence_score") or 0.0)
                active.threshold = float(state.get("threshold") or active.threshold)
                active.messages = list(state.get("messages") or [])
                active.error = state.get("error") or None
                active.error_code = state.get("error_code") or None
                active.contract = _contract_from_state(state) or active.contract
                should_cancel = False

                if interrupted:
                    payload = interrupt_payload or {}
                    if payload.get("kind") == "contract_confirmation":
                        active.status = "AWAITING_CONTRACT_CONFIRMATION"
                        active.clarification = None
                        if active.contract is not None:
                            active.contract.status = "AWAITING_CONFIRMATION"
                    else:
                        active.status = "AWAITING_CLARIFICATION"
                        active.clarification = _clarification_from_interrupt(
                            payload, state
                        )
                        active.round = active.clarification.round
                        active.app_shape = (
                            active.clarification.app_shape or active.app_shape
                        )
                        active.confidence_score = active.clarification.confidence_score
                        active.threshold = active.clarification.threshold
                else:
                    status = str(state.get("status") or "FAILED")
                    if status == "CONFIRMED":
                        active.status = "CONFIRMED"
                    elif status == "CANCELLED":
                        active.status = "CANCELLED"
                    elif status == "FAILED":
                        active.status = "FAILED"
                    elif status == "AWAITING_CONTRACT_CONFIRMATION":
                        # 理论上 contract_gate 一定会 interrupt；防御性收敛。
                        active.status = "AWAITING_CONTRACT_CONFIRMATION"
                    else:
                        active.status = "FAILED"
                        active.error = active.error or (
                            f"Architect 未到达终态: {status}"
                        )
                        active.error_code = active.error_code or (
                            "ARCHITECT_INCOMPLETE_STATE"
                        )
                active.touch()

        if should_cancel:
            self._complete_cancelled(active, active.cancel_reason)

    def _complete_cancelled(
        self,
        active: ActiveArchitectRun,
        reason: str | None,
    ) -> None:
        with _ACTIVE_LOCK:
            if active.status == "CANCELLED":
                return
            active.status = "CANCELLED"
            active.clarification = None
            if active.contract is not None:
                active.contract.status = "CANCELLED"
                active.contract.decided_at = datetime.now(timezone.utc)
            active.touch()
        self._emit_cancelled(active, reason)

    def _emit_cancelled(
        self,
        active: ActiveArchitectRun,
        reason: str | None,
    ) -> None:
        active.buffer.emit(
            "architect.cancelled",
            {
                "contractId": (
                    active.contract.contract_id if active.contract else None
                ),
                "reason": reason or "",
            },
            dedupe_key=f"architect.cancelled:{active.run_id}",
        )

    def _complete_failure(
        self,
        active: ActiveArchitectRun,
        exc: Exception,
    ) -> None:
        code = _error_code(exc)
        message = str(exc) or exc.__class__.__name__
        with _ACTIVE_LOCK:
            if active.status == "CANCELLED":
                return
            active.status = "FAILED"
            active.error = message
            active.error_code = code
            active.clarification = None
            active.touch()
        active.buffer.emit(
            "architect.failed",
            {
                "error": message,
                "errorCode": code,
            },
            dedupe_key=f"architect.failed:{active.run_id}:{code}",
        )


# ------------------------------------------------------------
# 工具
# ------------------------------------------------------------
def _require_run(run_id: str) -> ActiveArchitectRun:
    active = get_active_architect_run(run_id)
    if active is None:
        raise ArchitectRunNotFoundError(f"Architect Run 不存在或已过期: {run_id}")
    return active


def _initial_state(active: ActiveArchitectRun, bundle: Any) -> dict[str, Any]:
    request = active.request
    return {
        "run_id": active.run_id,
        "project_id": request.project_id,
        "project_name": request.project_name,
        "source_files": bundle.files,
        "source_hash": bundle.source_hash,
        "source_origin": bundle.origin,
        "source_warnings": bundle.warnings,
        "ui_history": list(request.ui_history),
        # 项目摘要 + 本次快照改动：Architect 分析 / 契约草稿必须复用其中
        # 已确认的技术选型，不再重复做选型问卷。
        "project_summary": request.project_summary,
        "latest_changes": request.latest_changes,
        # Token 优化：START 节点据此决定是否压缩历史
        "history_summary": "",
        "history_tokens_before": 0,
        "history_tokens_after": 0,
        "threshold": request.options.confidence_threshold,
        "max_rounds": request.options.max_clarification_rounds,
        "round": 0,
        "status": "INGESTING",
        "app_shape": "",
        "slots": {},
        "confidence_score": 0.0,
        "missing_slots": [],
        "questions": [],
        "analysis": {},
        "retrieved_chunks": [],
        "clarification": {},
        "contract": {},
        "contract_draft": {},
        "contract_issues": [],
        "contract_id": "",
        "contract_decision": "",
        "contract_feedback": "",
        "repair_count": 0,
        "repair_limit": request.options.contract_repair_limit,
        "contract_revision_count": 0,
        "asked_slots": [],
        "messages": [],
        "rag_provider": request.options.rag_provider,
        "rag_top_k": request.options.rag_top_k,
        "error": "",
        "error_code": "",
    }


def _clarification_from_interrupt(
    payload: dict[str, Any],
    state: dict[str, Any],
) -> ArchitectClarification:
    if payload.get("clarificationId"):
        try:
            return ArchitectClarification.model_validate(payload)
        except Exception:  # noqa: BLE001 - 回退到状态重建
            pass
    round_no = int(state.get("round") or 0) + 1
    return ArchitectClarification(
        clarification_id=f"clr_{state.get('run_id')}_{round_no}",
        run_id=str(state.get("run_id") or ""),
        round=round_no,
        max_rounds=int(state.get("max_rounds") or 5),
        status="PENDING",
        app_shape=state.get("app_shape") or None,  # type: ignore[arg-type]
        confidence_score=float(state.get("confidence_score") or 0.0),
        threshold=float(state.get("threshold") or 0.85),
        missing_slots=list(state.get("missing_slots") or []),
        questions=[],
    )


def _contract_from_state(state: dict[str, Any]) -> ArchitectContract | None:
    payload = state.get("contract") or {}
    if not payload:
        return None
    try:
        return ArchitectContract.model_validate(payload)
    except Exception:  # noqa: BLE001
        return None


def _decision_response(
    active: ActiveArchitectRun,
    contract: ArchitectContract,
    now: datetime,
) -> ArchitectDecisionResponse:
    status = {
        "CONFIRMING_CONTRACT": "CONFIRMING",
        "REVISING_CONTRACT": "REVISING",
        "CANCELLING": "CANCELLING",
        "CONFIRMED": "CONFIRMED",
        "CANCELLED": "CANCELLED",
    }.get(active.status, active.status)
    return ArchitectDecisionResponse(
        contract=ArchitectContractStatus(
            contract_id=contract.contract_id,
            status=status,
            decided_at=now if active.status in {"CONFIRMED", "CANCELLED"} else None,
        ),
        architect_run=ArchitectRunStatus(
            run_id=active.run_id,
            status=active.status,
        ),
    )


def _request_fingerprint(request: ArchitectRunRequest) -> str:
    raw = json.dumps(
        request.model_dump(by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ArchitectLLMError):
        return "ARCHITECT_LLM_ERROR"
    if isinstance(exc, ArchitectRunCancelledError):
        return "ARCHITECT_CANCELLED"
    return "ARCHITECT_INTERNAL_ERROR"
