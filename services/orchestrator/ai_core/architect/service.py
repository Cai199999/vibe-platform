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
    CONTRACT_REVISION_SOFT_LIMIT,
    ArchitectGraphRunner,
    ArchitectRunCancelledError,
)
from ai_core.adapters.rag import get_rag_provider
from ai_core.architect.llm import (
    ArchitectContext,
    ArchitectLLM,
    ArchitectLLMError,
    build_architect_llm,
)
from ai_core.domain.architect import (
    ArchitectAnswerResponse,
    ArchitectAnswersRequest,
    ArchitectCancelRequest,
    ArchitectCancelResponse,
    ArchitectCitation,
    ArchitectClarification,
    ArchitectClarificationStatus,
    ArchitectConflictDecisionRequest,
    ArchitectConflictDecisionResponse,
    ArchitectConflictStatus,
    ArchitectContract,
    ArchitectContractConflict,
    ArchitectContractStatus,
    ArchitectDecisionRequest,
    ArchitectDecisionResponse,
    ArchitectHelperRequest,
    ArchitectHelperResponse,
    ArchitectRunRequest,
    ArchitectRunStatus,
    ArchitectSnapshotResponse,
)
from ai_core.architect.source import collect_source
from ai_core.config import Settings, get_settings
from ai_core.error_envelope import attach_error_envelope, envelope_from_code
from ai_core.domain.scope import scoped_checkpoint_key
from ai_core.observability.fullstack_metrics import FULLSTACK_METRICS
from ai_core.ports.rag import RAGProviderError
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
    max_rounds: int = 10
    confidence_score: float = 0.0
    threshold: float = 0.85
    clarification: ArchitectClarification | None = None
    # status=AWAITING_DECISION 时非空：契约与 DDL 命名矛盾等待用户三选一。
    conflict: ArchitectContractConflict | None = None
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


def find_confirmed_contract_for_project(project_id: str) -> ArchitectContract | None:
    """查找项目最近一次已确认的契约。

    Supervisor 派发 backend_coder / dba_agent 时，前端只传项目与消息，
    不重复回传契约；服务端按 ``updated_at`` 取最新快照，确保连续澄清后
    使用最后确认的权威版本。未确认契约绝不返回。
    """
    latest: tuple[datetime, ArchitectContract] | None = None
    with _ACTIVE_LOCK:
        for active in _ACTIVE_RUNS.values():
            if active.request.project_id != project_id:
                continue
            contract = active.contract
            if contract is None or contract.status != "CONFIRMED":
                continue
            if latest is None or active.updated_at > latest[0]:
                latest = (active.updated_at, contract.model_copy(deep=True))
    return latest[1] if latest else None


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
            elif event_type == "architect.contract.conflict":
                conflict = ArchitectContractConflict.model_validate(payload)
                active.conflict = conflict
                active.clarification = None
                active.contract = None
                active.status = "AWAITING_DECISION"
            elif event_type == "architect.contract.ready":
                raw_contract = payload.get("contract") or {}
                contract = ArchitectContract.model_validate(raw_contract)
                contract.status = "AWAITING_CONFIRMATION"
                active.contract = contract
                active.conflict = None
                active.clarification = None
                active.status = "AWAITING_CONTRACT_CONFIRMATION"
            elif event_type == "architect.contract.confirmed":
                # 让“已确认”在 SSE 事件可见前就生效。追加需求请求可能
                # 与图线程收尾并发到达，不能依赖 _apply_outcome 的时序。
                raw_contract = payload.get("contract") or {}
                if raw_contract:
                    contract = ArchitectContract.model_validate(raw_contract)
                    contract.status = "CONFIRMED"
                    active.contract = contract
                active.clarification = None
                active.conflict = None
                active.status = "CONFIRMED"
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
            if request.adopt_suggestions:
                # 用户显式授权：按默认值 / 推荐选项 / 类型默认值补齐全部问题。
                # 该路径只可能由 API 请求触发，模型输出不会携带该标记。
                answers = _merge_adopted_answers(clarification.questions, answers)
            if (
                not answers
                and not (request.comment or "").strip()
                and not request.adopt_suggestions
            ):
                raise ArchitectAnswersRequiredError("至少回答一个问题或提供补充说明")

            comment = (request.comment or "").strip()
            if request.adopt_suggestions and not comment:
                comment = "用户选择一键采用平台建议并跳过剩余提问"

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
                "comment": comment,
                "adoptSuggestions": request.adopt_suggestions,
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
                    "comment": comment,
                    "adopt_suggestions": request.adopt_suggestions,
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

    def _revision_worker(
        self,
        active: ActiveArchitectRun,
        initial_state: dict[str, Any],
    ) -> None:
        """已确认契约的追加修订线程。

        使用同一个 checkpoint thread 重新从 START 进入，但由
        ``_route_after_summary`` 直接跳到 revise_contract；这样既复用了
        原 Context，又不需要伪造一个新的 interrupt。
        """
        try:
            self._execute_graph(active, initial_state=initial_state)
        except ArchitectRunCancelledError:
            self._complete_cancelled(active, active.cancel_reason)
        except Exception as exc:  # noqa: BLE001
            logger.exception("architect revision failed run_id=%s", active.run_id)
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

            # 已确认契约仍保持“持续在线”：用户追加新需求时允许再次
            # REVISE。此时不是恢复旧 interrupt，而是从当前 checkpoint
            # 状态直接进入 revise_contract，避免重新澄清或脑补。
            confirming_revision = (
                active.is_terminal
                and active.status == "CONFIRMED"
                and decision == "REVISE"
            )
            if active.is_terminal and not confirming_revision:
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

            if (
                not confirming_revision
                and active.status != "AWAITING_CONTRACT_CONFIRMATION"
            ):
                raise ArchitectStateConflictError(
                    f"Architect 当前状态为 {active.status}，没有待确认契约"
                )
            if decision == "REVISE" and not (request.comment or "").strip():
                raise ArchitectStateConflictError("REVISE 必须提供 comment")
            # Contract_Revision_Rate = 契约回改次数 / 契约确认次数。
            # 幂等重复 CONFIRM 已在上面提前返回，不会重复计数。
            if decision == "CONFIRM":
                FULLSTACK_METRICS.record_contract_confirmation()
            elif decision == "REVISE":
                FULLSTACK_METRICS.record_contract_revision()
            if confirming_revision:
                revision_count = int(active.state.get("contract_revision_count") or 0)
                if revision_count >= CONTRACT_REVISION_SOFT_LIMIT:
                    raise ArchitectStateConflictError(
                        "契约修订次数超过上限，请重新发起架构澄清"
                    )
                active.status = "REVISING_CONTRACT"
                active.touch()
            else:
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
                "qualityTier": request.quality_tier,
            },
            dedupe_key=(
                f"architect.contract.decision:"
                f"{contract.contract_id}:{decision}:"
                f"{int(active.state.get('contract_revision_count') or 0)}"
            ),
        )
        if confirming_revision:
            # 追加需求进入当前 Context：把新消息写入 ui_history，并保留
            # 已确认契约的 draft / slots / 技术栈，只重写受影响部分。
            base_state = dict(active.state or {})
            history = list(base_state.get("ui_history") or [])
            history.append({"role": "user", "content": request.comment.strip()})
            revision_count = int(base_state.get("contract_revision_count") or 0)
            revision_state = {
                **base_state,
                "ui_history": history,
                "contract_decision": "REVISE",
                "contract_feedback": request.comment.strip(),
                "contract_revision_count": revision_count + 1,
                "contract_issues": [],
                "repair_count": 0,
                "status": "REVISING_CONTRACT",
            }
            # 追加需求时若重新选了质量分级，以最新选择为准；未传则沿用。
            if request.quality_tier:
                revision_state["quality_tier"] = request.quality_tier
            active.state = revision_state
            active.buffer.emit(
                "architect.contract.revision_requested",
                {
                    "contractId": contract.contract_id,
                    "comment": request.comment.strip(),
                    "revision": revision_count + 1,
                },
                dedupe_key=(
                    f"architect.contract.revision:"
                    f"{contract.contract_id}:{revision_count + 1}"
                ),
            )
            threading.Thread(
                target=self._revision_worker,
                args=(active, revision_state),
                name=f"architect-revision-{run_id}",
                daemon=True,
            ).start()
            return _decision_response(active, contract, now)

        threading.Thread(
            target=self._resume_worker,
            args=(
                active,
                {
                    "decision": decision,
                    "comment": request.comment,
                    "qualityTier": request.quality_tier,
                },
            ),
            name=f"architect-decision-{run_id}",
            daemon=True,
        ).start()
        return _decision_response(active, contract, now)

    # --------------------------------------------------------
    # 契约命名冲突决策
    # --------------------------------------------------------
    def decide_conflict(
        self,
        run_id: str,
        conflict_id: str | None,
        request: ArchitectConflictDecisionRequest,
    ) -> ArchitectConflictDecisionResponse:
        """用户在“映射已有表 / 新建表 / 取消”之间做出选择并恢复图。

        与契约 CONFIRM/REVISE 决策独立：此时契约尚未 ready，不能复用
        ``decide_contract``；Graph 在 ``conflict_gate`` 中等待这个回复。
        """
        active = _require_run(run_id)
        now = datetime.now(timezone.utc)
        with _ACTIVE_LOCK:
            if active.status != "AWAITING_DECISION":
                raise ArchitectStateConflictError(
                    f"Architect 当前状态为 {active.status}，没有待决策的契约冲突"
                )
            conflict = active.conflict
            if conflict is None:
                raise ArchitectContractNotFoundError("当前没有待决策的契约冲突")
            if conflict_id and conflict.conflict_id != conflict_id:
                raise ArchitectContractNotFoundError(f"契约冲突不存在: {conflict_id}")
            allowed = {item.option_id for item in conflict.options}
            if request.option_id not in allowed:
                raise ArchitectStateConflictError(
                    f"无效的冲突决策选项: {request.option_id}"
                )
            option = next(
                item for item in conflict.options if item.option_id == request.option_id
            )
            conflict.status = "ANSWERED"
            conflict.answered_at = now
            active.conflict = None
            # 保持 ANALYZING：Graph 恢复后会先应用决策再重新校验。
            active.status = "ANALYZING"
            active.touch()

        active.buffer.emit(
            "architect.contract.conflict.answered",
            {
                "conflictId": conflict.conflict_id,
                "optionId": option.option_id,
                "action": option.action,
                "targetTable": option.target_table,
                "comment": (request.comment or "").strip(),
            },
            # 与 Graph 恢复后发出的同名事件共用 dedupe key，避免重复入队。
            dedupe_key=f"architect.contract.conflict.answered:{conflict.conflict_id}",
        )
        threading.Thread(
            target=self._resume_worker,
            args=(
                active,
                {
                    "optionId": option.option_id,
                    "action": option.action,
                    "comment": (request.comment or "").strip(),
                },
            ),
            name=f"architect-conflict-{run_id}",
            daemon=True,
        ).start()
        return ArchitectConflictDecisionResponse(
            conflict=ArchitectConflictStatus(
                conflict_id=conflict.conflict_id,
                status="ANSWERED",
                answered_at=now,
            ),
            architect_run=ArchitectRunStatus(
                run_id=active.run_id,
                status="ANALYZING",
            ),
        )

    # --------------------------------------------------------
    # 影子助手（只读）
    # --------------------------------------------------------
    def helper(
        self,
        run_id: str,
        request: ArchitectHelperRequest,
    ) -> ArchitectHelperResponse:
        """回答用户的自由提问，绝不推进主架构状态机。

        与 ``answer_clarification`` 的关键区别：
          - 不修改 active 的任何字段（status / clarification / contract 等）；
          - 不启动 Graph、不写 checkpoint、不发 ``architect.*`` 事件；
          - 只读取当前快照与知识库，返回一段解释文本 + 引用列表。

        LLM 调用是慢操作，必须先复制快照、释放锁，再在锁外调用模型，
        避免 Helper 阻塞主流程的澄清 / 决策请求。
        """
        active = _require_run(run_id)
        with _ACTIVE_LOCK:
            state = dict(active.state or {})
            snapshot_text = _architecture_snapshot(active)
            llm = active.llm
            project_id = active.request.project_id
            project_name = active.request.project_name
            project_summary = active.request.project_summary
            latest_changes = active.request.latest_changes
            source_hash = active.source_hash
            app_shape = active.app_shape
            round_no = active.round
            threshold = active.threshold
            provider_name = active.request.options.llm_provider
            rag_provider_name = (
                state.get("rag_provider") or active.request.options.rag_provider
            )
            rag_top_k = max(1, int(state.get("rag_top_k") or 5))
            ui_history = list(
                state.get("ui_history") or active.request.ui_history or []
            )[-12:]

        # 知识库检索失败不影响问答：降级为无引用回答并记录日志。
        chunks: list[Any] = []
        if request.use_knowledge_base:
            try:
                provider = get_rag_provider(rag_provider_name or None)
                chunks = provider.retrieve(
                    request.question.strip(),
                    top_k=rag_top_k,
                )
            except RAGProviderError as exc:
                logger.warning("architect helper rag retrieve failed: %s", exc)

        context = ArchitectContext(
            project_id=project_id,
            project_name=project_name,
            # Helper 只看架构快照，不回灌整份源码，控制延迟与成本。
            source_files={},
            ui_history=ui_history,
            slots=dict(state.get("slots") or {}),
            app_shape=app_shape,
            round=round_no,
            threshold=threshold,
            retrieved_chunks=[
                {**chunk.to_citation(), "content": chunk.content} for chunk in chunks
            ],
            source_hash=source_hash,
            project_summary=project_summary,
            latest_changes=latest_changes,
            direct_changelog=list(state.get("direct_changelog") or []),
            architecture_snapshot=snapshot_text,
        )
        history = [item.model_dump(by_alias=True) for item in request.history]
        answer = llm.answer_helper(context, request.question, history)
        citations = [
            ArchitectCitation.model_validate(chunk.to_citation()) for chunk in chunks
        ]
        return ArchitectHelperResponse(
            run_id=run_id,
            answer=answer,
            citations=citations,
            read_only=True,
            model=provider_name,
        )

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
                "AWAITING_DECISION",
            }:
                active.status = "CANCELLED"
                active.clarification = None
                active.conflict = None
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
                conflict=(
                    active.conflict.model_copy(deep=True) if active.conflict else None
                ),
                contract=(
                    active.contract.model_copy(deep=True) if active.contract else None
                ),
                messages=[dict(item) for item in active.messages],
                direct_changelog=list(active.state.get("direct_changelog") or []),
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
                        active.conflict = None
                        if active.contract is not None:
                            active.contract.status = "AWAITING_CONFIRMATION"
                    elif payload.get("kind") == "contract_naming_conflict":
                        active.status = "AWAITING_DECISION"
                        active.clarification = None
                        active.contract = None
                        active.conflict = ArchitectContractConflict.model_validate(
                            payload
                        )
                    else:
                        active.status = "AWAITING_CLARIFICATION"
                        active.conflict = None
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
                    if status in {"CONFIRMED", "CANCELLED", "FAILED"}:
                        active.conflict = None
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
            active.conflict = None
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
            active.conflict = None
            active.touch()
        envelope = envelope_from_code(
            code,
            technical_message=message,
            trace_id=getattr(active.buffer, "trace_id", None),
            run_id=active.run_id,
            stage="architect.service",
        )
        active.buffer.emit(
            "architect.failed",
            attach_error_envelope(
                {
                    "error": envelope.user_message,
                    "errorCode": code,
                },
                envelope,
            ),
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


def _merge_adopted_answers(
    questions: list[Any],
    answers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """用户显式“采用建议”时为未答问题生成确定性答案。

    优先级：问题默认值 → 推荐选项 → 首个选项 → 按类型给出的安全默认值。
    每个答案标记 ``source=USER_ADOPTED``，便于审计和契约生成时写入假设，
    区别于用户逐题手填的答案。
    """
    merged = list(answers)
    answered_ids = {
        str(item.get("questionId") or item.get("question_id") or "") for item in answers
    }
    for question in questions:
        question_id = str(getattr(question, "question_id", "") or "")
        if not question_id or question_id in answered_ids:
            continue
        value = _adopted_value(question)
        answer: dict[str, Any] = {
            "questionId": question_id,
            "value": value,
            "source": "USER_ADOPTED",
        }
        if isinstance(value, str):
            answer["text"] = value
        merged.append(answer)
    return merged


def _adopted_value(question: Any) -> Any:
    default = getattr(question, "default_value", None)
    if default not in (None, "", [], {}):
        return default
    options = getattr(question, "options", None) or []
    for option in options:
        if isinstance(option, dict) and option.get("recommended"):
            if option.get("value") not in (None, ""):
                return option["value"]
    for option in options:
        if isinstance(option, dict) and option.get("value") not in (None, ""):
            return option["value"]
    question_type = str(getattr(question, "type", "") or "")
    if question_type == "multi_choice":
        return []
    if question_type in {"entity_fields", "schema_fields"}:
        return "由平台按业务目标设计实体、字段与关系"
    return "由平台按行业最佳实践决定"


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
        "direct_changelog": _recent_direct_changes(request.project_id),
        # Token 优化：START 节点据此决定是否压缩历史
        "history_summary": "",
        "history_tokens_before": 0,
        "history_tokens_after": 0,
        "threshold": request.options.confidence_threshold,
        "max_rounds": request.options.max_clarification_rounds,
        "round": 0,
        "status": "INGESTING",
        "app_shape": "",
        # 质量分级只在契约确认阶段由用户写入；初值留空，避免把默认值
        # 误当成“用户已选择”。
        "quality_tier": "",
        "skeleton_mode": False,
        "slots": {},
        "confidence_score": 0.0,
        "missing_slots": [],
        "adopt_suggestions": False,
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
        "contract_conflict": {},
        "conflict_resolution_applied": False,
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


def _recent_direct_changes(project_id: str) -> list[dict[str, Any]]:
    """读取最近 10 条 Direct Ops ChangeLog，供 Architect Prompt 使用。"""
    try:
        from ai_core.direct_ops.changelog import get_change_log_store

        entries = get_change_log_store().list_for_project(project_id, 10)
        return [entry.model_dump(mode="json", by_alias=True) for entry in entries]
    except Exception:  # noqa: BLE001 - ChangeLog 是增强上下文，不能阻断架构流程
        logger.warning(
            "failed to load direct changelog project=%s", project_id, exc_info=True
        )
        return []


def _architecture_snapshot(active: ActiveArchitectRun) -> str:
    """构造 Helper Agent 的只读架构快照。

    只包含已确认槽位、待答问题与当前契约，不包含源码全文；这样 Helper
    的回答始终与主状态机一致，又不会因为源码过大拖慢响应。
    """
    slots = active.state.get("slots") or {}
    lines = [
        f"- 项目：{active.request.project_name}（{active.request.project_id}）",
        f"- 当前状态：{active.status}",
        f"- 应用形态：{active.app_shape or '尚未确定'}",
        f"- 澄清轮次：{active.round}/{active.max_rounds}",
        (f"- 置信度：{active.confidence_score:.2f}" f"（阈值 {active.threshold:.2f}）"),
        f"- 已确认槽位：{json.dumps(slots, ensure_ascii=False, default=str)}",
    ]
    clarification = active.clarification
    if clarification is not None and clarification.questions:
        lines.append("- 当前待确认的关键问题：")
        for question in clarification.questions:
            lines.append(
                f"  · [{question.slot}] {question.prompt}"
                f"{'（必填）' if question.required else '（可选）'}"
            )
    contract = active.contract
    if contract is not None:
        lines.extend(
            [
                f"- 契约状态：{contract.status}",
                (
                    "- 技术栈："
                    f"{json.dumps(contract.tech_stack, ensure_ascii=False, default=str)}"
                ),
                ("- 数据库：" f"{contract.database_engine or '无 / 非全栈'}"),
                f"- 契约 Markdown（截断）：\n{contract.contract_markdown[:6000]}",
            ]
        )
    return "\n".join(lines)


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
        max_rounds=int(state.get("max_rounds") or 10),
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
