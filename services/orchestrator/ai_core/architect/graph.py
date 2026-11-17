"""Architect Graph：应用形态判别 -> 动态澄清 -> 分级契约 -> 最终确认。

拓扑：

    START -> summarize_history -> ingest_context -> retrieve_knowledge
                               -> architect_analyze
                                                             │
                                                             ▼
                                                     confidence_gate
                                          │(不足)                    │(足够)
                                          ▼                          ▼
                                    clarify_gate              draft_contract
                                          │ interrupt()              │
                                          │                          ▼
                                          └──────► architect_analyze validate_contract
                                                                     │      │(修复)
                                                                     │      ▼
                                                                     │ revise_contract
                                                                     │      │
                                                                     ▼      ▼
                                                              contract_ready ◄┘
                                                                     │
                                                                     ▼
                                                               contract_gate
                                                          interrupt(CONFIRM/REVISE/CANCEL)
                                                           │        │          │
                                                           ▼        ▼          ▼
                                                       confirmed  revise   cancelled

关键约束：
  - 应用形态未知时第一轮必须询问形态，不允许直接套用全栈模板；
  - 动态槽位校验与置信度封顶由确定性代码完成，不信任 LLM 自报分数；
  - 契约分级校验失败时最多修复一次，仍失败则明确终止；
  - Architect 使用独立 thread 前缀，与局部 UI Run 的 checkpoint 完全隔离。
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ai_core.adapters.checkpoint import get_checkpointer_provider
from ai_core.ports.checkpoint import CheckpointerProvider
from ai_core.agent.events import EventSink, NullEventSink
from ai_core.agent.memory import IntentClassifier, compress_architect_history
from ai_core.architect.confidence import apply_confidence_guard
from ai_core.architect.contract import (
    assemble_contract,
    draft_from_dict,
    draft_to_dict,
    validate_draft,
)
from ai_core.architect.llm import (
    ArchitectContext,
    ArchitectLLM,
    ContractDraft,
)
from ai_core.architect.skeleton import (
    build_skeleton_draft,
    is_abstract_brief,
    latest_user_text,
    should_use_skeleton,
)
from ai_core.architect.slots import merge_inferred_slots
from ai_core.domain.architect import (
    APP_SHAPE_LABELS,
    QUALITY_TIER_OPTIONS,
    QUALITY_TIER_PROMPT,
    ArchitectCitation,
    ArchitectClarification,
    ArchitectContract,
    ArchitectQuestion,
    normalize_quality_tier,
)
from ai_core.domain.state import ArchitectState
from ai_core.error_envelope import attach_error_envelope, envelope_from_code
from ai_core.config import get_settings
from ai_core.observability.streaming import (
    TokenStreamEmitter,
    collect_tokens,
)
from ai_core.observability.token_usage import TokenUsage, UsageCollector, collect_usage
from ai_core.adapters.rag import get_rag_provider
from ai_core.ports.rag import RAGProvider, RAGProviderError

logger = logging.getLogger(__name__)

ARCHITECT_THREAD_PREFIX = "architect:"
CONTRACT_REVISION_SOFT_LIMIT = 8

NODE_LABELS = {
    "summarize_history": "压缩对话记忆",
    "ingest_context": "读取当前源码",
    "retrieve_knowledge": "检索架构规范",
    "skeleton_gate": "检测抽象需求",
    "architect_analyze": "判别应用形态",
    "confidence_gate": "校验置信度",
    "clarify_gate": "等待用户澄清",
    "draft_contract": "起草实施契约",
    "validate_contract": "校验契约分级",
    "revise_contract": "修订实施契约",
    "conflict_gate": "等待契约冲突决策",
    "contract_ready": "生成实施契约",
    "contract_gate": "等待契约决策",
    "confirmed": "契约已确认",
    "cancelled": "已取消",
    "confidence_exhausted": "澄清轮次耗尽",
    "failed": "架构澄清失败",
}

# 真正调用模型且需要打字机效果的 Architect 节点。
ARCHITECT_STREAMING_NODES = {
    "architect_analyze",
    "draft_contract",
    "revise_contract",
}


class ArchitectRunCancelledError(Exception):
    """用户取消 Architect Run。"""


@dataclass
class ArchitectGraphOutcome:
    state: ArchitectState
    interrupted: bool
    interrupt: dict[str, Any] | None
    thread_id: str
    # 整次 Architect Run 的 Token 用量 / 费用。
    usage: dict[str, Any] = field(default_factory=dict)


class ArchitectGraphRunner:
    """构建并执行 Architect 状态机。"""

    def __init__(
        self,
        llm: ArchitectLLM,
        *,
        event_sink: EventSink | None = None,
        checkpointer_provider: CheckpointerProvider | None = None,
        rag_provider_factory: Callable[[str | None], RAGProvider] | None = None,
        recursion_limit: int = 80,
        cancel_check: Callable[[], bool] | None = None,
        pause_hook: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._llm = llm
        self._event_sink = event_sink or NullEventSink()
        # Token 采集：每个节点独立聚合，最终以 Checkpoint 累计值为准。
        self._usage_total = TokenUsage()
        self._checkpointer_provider = (
            checkpointer_provider or get_checkpointer_provider()
        )
        # Graph 只依赖 RAGProvider 端口；默认工厂是组合根，测试可注入假实现。
        self._rag_provider_factory = rag_provider_factory or get_rag_provider
        self._recursion_limit = max(20, recursion_limit)
        self._cancel_check = cancel_check
        # 暂停事件先于 graph.invoke 返回，服务层必须在该事件可见前拿到
        # 待回答的澄清 / 待确认的契约，否则快速点击会命中“尚不存在”的竞态。
        self._pause_hook = pause_hook
        self._settings = get_settings()
        self._graph = self._build()

    def run(
        self,
        initial_state: ArchitectState,
        *,
        run_id: str | None = None,
        thread_id: str | None = None,
        resume: Any | None = None,
    ) -> ArchitectGraphOutcome:
        """执行 / 恢复 Architect 状态机。

        ``thread_id`` 由应用层传入项目作用域后的物理 Checkpoint 键；未传时
        保持旧行为 ``architect:<runId>``，仅用于单机测试。
        """
        resolved_run_id = run_id or str(initial_state.get("run_id") or "")
        resolved_thread_id = thread_id or (
            f"{ARCHITECT_THREAD_PREFIX}{resolved_run_id}"
        )
        config = {
            "recursion_limit": self._recursion_limit,
            "configurable": {"thread_id": resolved_thread_id},
        }
        if resume is None:
            self._graph.invoke(dict(initial_state), config=config)
        else:
            self._graph.invoke(Command(resume=resume), config=config)

        snapshot = self._graph.get_state(config)
        state = dict(snapshot.values or {})
        usage = state.get("token_usage")
        return ArchitectGraphOutcome(
            state=state,  # type: ignore[arg-type]
            interrupted=bool(snapshot.next),
            interrupt=_find_interrupt(snapshot),
            thread_id=resolved_thread_id,
            usage=dict(usage) if isinstance(usage, dict) else self.usage_total(),
        )

    def usage_total(self) -> dict[str, Any]:
        """返回本 runner 调用的 Token 汇总（未合并历史 Checkpoint 时使用）。"""
        return self._usage_total.as_dict()

    # --------------------------------------------------------
    # 图构建
    # --------------------------------------------------------
    def _instrument(
        self,
        name: str,
        fn: Callable[[ArchitectState], dict[str, Any]],
    ) -> Callable[[ArchitectState], dict[str, Any]]:
        """为节点统一采集 Token 用量，并写入 ``architect.node.finished`` 事件。

        只有真正发生模型调用的节点才发事件，避免 RAG / 校验节点产生噪声；
        状态中的 ``token_usage`` 由 reducer 跨节点累加。
        """

        def wrapped(state: ArchitectState) -> dict[str, Any]:
            started = time.perf_counter()
            collector = UsageCollector()
            emitter: TokenStreamEmitter | None = None
            token_scope = nullcontext()
            if name in ARCHITECT_STREAMING_NODES:
                emitter = TokenStreamEmitter(
                    lambda text, task: self._emit(
                        "token",
                        {
                            "runId": state.get("run_id"),
                            "node": name,
                            "label": NODE_LABELS.get(name, name),
                            "task": task,
                            "text": text,
                        },
                    ),
                    reset=lambda task: self._emit(
                        "architect.token.reset",
                        {
                            "runId": state.get("run_id"),
                            "node": name,
                            "label": NODE_LABELS.get(name, name),
                            "task": task,
                        },
                    ),
                )
                token_scope = collect_tokens(emitter.feed, emitter.reset)
            try:
                with token_scope, collect_usage(collector):
                    update = fn(state)
            finally:
                if emitter is not None:
                    emitter.flush()
                    emitter.close()
            usage = collector.snapshot()
            self._usage_total.add(usage)
            if isinstance(update, dict) and usage.calls:
                update = {**update, "token_usage": usage.as_dict()}
                self._emit(
                    "architect.node.finished",
                    {
                        "runId": state.get("run_id"),
                        "node": name,
                        "label": NODE_LABELS.get(name, name),
                        "durationMs": int((time.perf_counter() - started) * 1000),
                        "usage": usage.as_dict(),
                    },
                )
            return update

        return wrapped

    def _build(self):
        builder = StateGraph(ArchitectState)
        nodes: dict[str, Callable[[ArchitectState], dict[str, Any]]] = {
            "summarize_history": self._summarize_history,
            "ingest_context": self._ingest_context,
            "retrieve_knowledge": self._retrieve_knowledge,
            "skeleton_gate": self._skeleton_gate,
            "architect_analyze": self._architect_analyze,
            "confidence_gate": self._confidence_gate,
            "clarify_gate": self._clarify_gate,
            "draft_contract": self._draft_contract,
            "validate_contract": self._validate_contract,
            "revise_contract": self._revise_contract,
            "conflict_gate": self._conflict_gate,
            "contract_ready": self._contract_ready,
            "contract_gate": self._contract_gate,
            "confirmed": self._confirmed,
            "cancelled": self._cancelled,
            "confidence_exhausted": self._confidence_exhausted,
            "failed": self._failed,
        }
        for name, fn in nodes.items():
            builder.add_node(name, self._instrument(name, fn))

        builder.add_edge(START, "summarize_history")
        # 契约确认后用户仍可追加需求：服务端会把已有 contract_draft 与新
        # 反馈写入初始状态，并标记 contract_decision=REVISE。此时直接从
        # summarize_history 跳到 revise_contract，复用已确认契约的上下文，
        # 不再重新做澄清 / 脑补一轮分析。
        builder.add_conditional_edges(
            "summarize_history",
            self._route_after_summary,
            {
                "ingest_context": "ingest_context",
                "revise_contract": "revise_contract",
            },
        )
        builder.add_edge("ingest_context", "retrieve_knowledge")
        builder.add_edge("retrieve_knowledge", "skeleton_gate")
        builder.add_conditional_edges(
            "skeleton_gate",
            self._route_skeleton,
            {
                "draft_contract": "draft_contract",
                "architect_analyze": "architect_analyze",
            },
        )
        builder.add_edge("architect_analyze", "confidence_gate")
        builder.add_conditional_edges(
            "confidence_gate",
            self._route_confidence,
            {
                "clarify_gate": "clarify_gate",
                "draft_contract": "draft_contract",
                "confidence_exhausted": "confidence_exhausted",
            },
        )
        # 澄清恢复后重新过一遍抽象需求闸门：用户可能仍然只说“没想好”，
        # 此时必须停在骨架，而不是继续让模型追问并最终脑补。
        builder.add_edge("clarify_gate", "skeleton_gate")
        builder.add_edge("draft_contract", "validate_contract")
        builder.add_conditional_edges(
            "validate_contract",
            self._route_validation,
            {
                "contract_ready": "contract_ready",
                "revise_contract": "revise_contract",
                "conflict_gate": "conflict_gate",
                "failed": "failed",
            },
        )
        builder.add_edge("revise_contract", "validate_contract")
        # 用户决策后重新跑确定性校验：A/B 修契约，C 直接取消。
        builder.add_conditional_edges(
            "conflict_gate",
            self._route_conflict_decision,
            {
                "validate_contract": "validate_contract",
                "cancelled": "cancelled",
                "failed": "failed",
            },
        )
        builder.add_edge("contract_ready", "contract_gate")
        builder.add_conditional_edges(
            "contract_gate",
            self._route_contract_decision,
            {
                "confirmed": "confirmed",
                "revise_contract": "revise_contract",
                "cancelled": "cancelled",
                "failed": "failed",
            },
        )
        builder.add_edge("confirmed", END)
        builder.add_edge("cancelled", END)
        builder.add_edge("confidence_exhausted", END)
        builder.add_edge("failed", END)
        return builder.compile(checkpointer=self._checkpointer_provider.get())

    def _route_after_summary(self, state: ArchitectState) -> str:
        """区分“首次澄清”与“已确认契约后的追加修订”。

        追加修订必须保留原契约与已收集的业务上下文，只让 LLM 重写受影响
        的部分；重新走 analyze 会覆盖 slots，导致修订后的契约丢失旧信息。
        """
        if state.get("contract_decision") == "REVISE" and state.get("contract_draft"):
            return "revise_contract"
        return "ingest_context"

    # --------------------------------------------------------
    # 节点：历史压缩
    # --------------------------------------------------------
    def _summarize_history(self, state: ArchitectState) -> dict[str, Any]:
        """在 Checkpointer 之前压缩 architect 的 ui_history。

        超过阈值时把多轮问答压成一条“核心需求与约束”；摘要会替换
        ui_history，后续 ``_context_from_state`` 只带摘要进入模型。
        压缩失败时保留原历史，不影响 Run。
        """
        history = list(state.get("ui_history") or [])
        result = compress_architect_history(
            history,
            self._llm.summarize_history,
            threshold=int(self._settings.history_summary_token_threshold),
            max_chars=int(self._settings.history_summary_max_chars),
            recent_turns=int(self._settings.history_recent_turns),
            classifier=IntentClassifier(self._settings),
            encoding=self._settings.context_token_encoding,
        )
        update: dict[str, Any] = {
            "history_tokens_before": result.tokens_before,
            "history_tokens_after": result.tokens_after,
        }
        if not result.compressed:
            return update
        update.update(
            {
                "ui_history": result.value,
                "history_summary": result.summary,
            }
        )
        self._emit(
            "architect.history.compressed",
            {
                "runId": state.get("run_id"),
                "tokensBefore": result.tokens_before,
                "tokensAfter": result.tokens_after,
                "summary": result.summary,
            },
            dedupe_key=f"architect.history.compressed:{state.get('run_id')}",
        )
        return update

    # --------------------------------------------------------
    # 节点：上下文与 RAG
    # --------------------------------------------------------
    def _ingest_context(self, state: ArchitectState) -> dict[str, Any]:
        self._check_cancelled()
        self._emit(
            "architect.context.ingested",
            {
                "runId": state["run_id"],
                "sourceHash": state.get("source_hash", ""),
                "origin": state.get("source_origin", "request"),
                "fileCount": len(state.get("source_files") or {}),
                "warnings": state.get("source_warnings") or [],
            },
            dedupe_key=f"architect.context.ingested:{state['run_id']}",
        )
        return {"status": "ANALYZING"}

    def _retrieve_knowledge(self, state: ArchitectState) -> dict[str, Any]:
        self._check_cancelled()
        query = _rag_query(state)
        try:
            provider = self._rag_provider_factory(state.get("rag_provider") or None)
            chunks = provider.retrieve(
                query,
                top_k=max(1, int(state.get("rag_top_k") or 5)),
            )
        except RAGProviderError as exc:
            # RAG 是增强能力，不是主流程单点；失败时继续生成并显式记录。
            logger.warning("architect rag retrieve failed: %s", exc)
            self._emit(
                "architect.rag.failed",
                {"message": str(exc)},
                dedupe_key=f"architect.rag.failed:{state['run_id']}",
            )
            return {"retrieved_chunks": []}

        payload = [
            {
                **chunk.to_citation(),
                "content": chunk.content,
                "metadata": chunk.metadata,
            }
            for chunk in chunks
        ]
        self._emit(
            "architect.rag.retrieved",
            {
                "query": query[:500],
                "provider": provider.name,
                "citations": [chunk.to_citation() for chunk in chunks],
            },
            dedupe_key=f"architect.rag.retrieved:{state['run_id']}",
        )
        return {"retrieved_chunks": payload}

    # --------------------------------------------------------
    # 节点：抽象需求 → 空白骨架
    # --------------------------------------------------------
    def _skeleton_gate(self, state: ArchitectState) -> dict[str, Any]:
        """在调用 LLM 之前拦截“做个工作台”这类抽象需求。

        这是防脑补的第一道确定性闸门：只要满足“空白新项目 + 抽象目标”，
        就不让模型分析、追问或起草契约，直接进入骨架契约。这样即使真实
        模型再想脑补 LoginForm / ResumeUploader，也完全没有机会输出。
        """
        use_skeleton = should_use_skeleton(
            history=list(state.get("ui_history") or []),
            slots=dict(state.get("slots") or {}),
            source_files=dict(state.get("source_files") or {}),
            project_summary=str(state.get("project_summary") or ""),
            latest_changes=str(state.get("latest_changes") or ""),
        )
        if not use_skeleton:
            return {"skeleton_mode": False}

        goal = latest_user_text(state.get("ui_history"))
        self._emit(
            "architect.skeleton.detected",
            {
                "runId": state.get("run_id"),
                "goal": goal,
                "reason": "abstract_brief",
                "layout": ["header", "sidebar", "main"],
            },
            dedupe_key=f"architect.skeleton.detected:{state.get('run_id')}",
        )
        return {
            "skeleton_mode": True,
            "app_shape": "lightweight_web",
            "confidence_score": 1.0,
            "missing_slots": [],
            "questions": [],
            "analysis": {
                "app_shape": "lightweight_web",
                "confidence_score": 1.0,
                "missing_slots": [],
                "slots": dict(state.get("slots") or {}),
                "business_flows": [],
                "entities": [],
                "assumptions": ["抽象需求，仅生成空白骨架。"],
                "reasoning": (
                    "用户只提供了抽象目标，未说明业务对象与操作；"
                    "按防脑补规则生成 Header + 侧边栏 + 空白主区域。"
                ),
            },
            "status": "DRAFTING_CONTRACT",
        }

    def _route_skeleton(self, state: ArchitectState) -> str:
        if state.get("skeleton_mode"):
            return "draft_contract"
        return "architect_analyze"

    # --------------------------------------------------------
    # 节点：应用形态与置信度
    # --------------------------------------------------------
    def _architect_analyze(self, state: ArchitectState) -> dict[str, Any]:
        self._check_cancelled()
        context = _context_from_state(state)
        analysis = self._llm.analyze(context)
        self._check_cancelled()
        review_first_round = (
            _has_semantic_review_packet(state) and int(state.get("round") or 0) == 0
        )
        if review_first_round:
            # 审查包首轮必须停在“业务绑定澄清”，不能被通用槽位问题挤掉，
            # 也不能直接跳进契约起草。后续轮次恢复正常槽位守卫。
            reasons = ["语义审查包：优先确认交互与数据绑定"]
            questions = list(analysis.questions)
            if not questions:
                questions = [
                    ArchitectQuestion(
                        question_id="q_semantic_review",
                        slot="ui.interactions",
                        type="free_text",
                        prompt=(
                            "请说明虚拟界面中核心按钮/表单需要什么后端能力？"
                            "例如登录鉴权、订单 CRUD 或导出接口。"
                        ),
                        required=True,
                    )
                ]
            missing = list(analysis.missing_slots) or ["ui.interactions"]
            analysis = analysis.model_copy(
                update={
                    "confidence_score": min(
                        float(analysis.confidence_score or 0.0), 0.84
                    ),
                    "missing_slots": missing,
                    "questions": questions,
                }
            )
        else:
            analysis, guard = apply_confidence_guard(
                analysis,
                dict(state.get("slots") or {}),
                float(state.get("threshold") or 0.85),
                project_mode=str(state.get("project_mode") or "NEW_PROJECT"),
            )
            reasons = list(guard.reasons)
        shape = analysis.app_shape
        questions = [item.model_dump(by_alias=True) for item in analysis.questions]
        update: dict[str, Any] = {
            "app_shape": shape or "",
            "slots": analysis.slots,
            "missing_slots": analysis.missing_slots,
            "questions": questions,
            "confidence_score": analysis.confidence_score,
            "analysis": analysis.model_dump(mode="json", by_alias=True),
            "status": "ANALYZING",
        }
        if shape:
            self._emit(
                "architect.app_shape.detected",
                {
                    "appShape": shape,
                    "label": APP_SHAPE_LABELS.get(shape, shape),
                    "confidenceScore": analysis.confidence_score,
                    "reasons": reasons,
                },
                dedupe_key=f"architect.app_shape:{state['run_id']}:{shape}",
            )
        self._emit(
            "architect.analysis.completed",
            {
                "round": int(state.get("round") or 0),
                "appShape": shape,
                "confidenceScore": analysis.confidence_score,
                "threshold": float(state.get("threshold") or 0.85),
                "missingSlots": analysis.missing_slots,
                "reasons": reasons,
            },
            dedupe_key=(
                f"architect.analysis:{state['run_id']}:{state.get('round', 0)}"
            ),
        )
        return update

    def _confidence_gate(self, state: ArchitectState) -> dict[str, Any]:
        # 纯路由节点：保留命名，方便观测与状态机文档对齐。
        return {}

    def _route_confidence(self, state: ArchitectState) -> str:
        # 用户显式选择“采用建议并跳过剩余提问”：跳过后续澄清。
        # 该标记只可能来自澄清 API 的 adoptSuggestions，模型无法写入。
        if bool(state.get("adopt_suggestions")):
            return "draft_contract"
        shape = state.get("app_shape") or ""
        missing = state.get("missing_slots") or []
        confidence = float(state.get("confidence_score") or 0.0)
        threshold = float(state.get("threshold") or 0.85)
        if shape and not missing and confidence >= threshold:
            return "draft_contract"
        if int(state.get("round") or 0) >= int(state.get("max_rounds") or 10):
            return "confidence_exhausted"
        return "clarify_gate"

    def _clarify_gate(self, state: ArchitectState) -> dict[str, Any]:
        clarification = _build_clarification(state)
        payload = clarification.model_dump(mode="json", by_alias=True)
        # 必须先发暂停事件再 interrupt：前端依赖该事件结束当前 SSE 并渲染
        # 澄清表单；否则连接会一直挂着心跳，用户看不到问题。
        self._emit(
            "architect.clarification.required",
            payload,
            dedupe_key=(
                "architect.clarification.required:" f"{clarification.clarification_id}"
            ),
        )
        # interrupt 会暂停当前节点；恢复后返回用户在 API 提交的答案。
        response = interrupt(payload)
        answers = _normalize_resume_answers(response)
        slots = dict(state.get("slots") or {})
        questions = {
            str(item.get("questionId") or item.get("question_id")): item
            for item in state.get("questions") or []
        }
        asked = list(state.get("asked_slots") or [])
        messages = list(state.get("messages") or [])
        history = list(state.get("ui_history") or [])

        for answer in answers["answers"]:
            question_id = str(
                answer.get("questionId") or answer.get("question_id") or ""
            )
            question = questions.get(question_id)
            value = answer.get("value")
            text = answer.get("text")
            slot = str((question or {}).get("slot") or answer.get("slot") or "").strip()
            if slot == "app.shape":
                shape = _normalize_shape(value) or _normalize_shape(text)
                if shape:
                    slots["app.shape"] = shape
            elif slot:
                resolved = _answer_value(value, text)
                if resolved not in (None, ""):
                    slots[slot] = resolved
            # 结构化回答里常混入其它槽位信息（例如后端问题里同时写了
            # “数据库用 MySQL”）。只在槽位为空时回填，绝不覆盖已确认值。
            resolved = _answer_value(value, text)
            if isinstance(resolved, (str, int, float)):
                slots = merge_inferred_slots(slots, str(resolved))
            if slot and slot not in asked:
                asked.append(slot)
            content = _answer_content(question, value, text)
            if content:
                messages.append(
                    {
                        "role": "user",
                        "content": content,
                        "round": int(state.get("round") or 0),
                        "questionId": question_id,
                    }
                )
                history.append({"role": "user", "content": content})

        comment = str(answers.get("comment") or "").strip()
        if comment:
            # “补充说明”是自由文本，同样做确定性槽位回填。这样即使 LLM
            # 没有把用户回答写入 slots，系统也不会反复追问同一问题。
            slots = merge_inferred_slots(slots, comment)
            messages.append(
                {
                    "role": "user",
                    "content": comment,
                    "round": int(state.get("round") or 0),
                    "questionId": None,
                }
            )
            history.append({"role": "user", "content": comment})

        return {
            "slots": slots,
            "asked_slots": asked,
            "messages": messages,
            "ui_history": history[-30:],
            "round": int(state.get("round") or 0) + 1,
            "questions": [],
            "adopt_suggestions": bool(answers.get("adopt_suggestions")),
            "status": "ANALYZING",
        }

    # --------------------------------------------------------
    # 节点：契约起草 / 校验 / 修订
    # --------------------------------------------------------
    def _draft_contract(self, state: ArchitectState) -> dict[str, Any]:
        self._check_cancelled()
        if state.get("skeleton_mode"):
            # 骨架模式完全跳过 LLM：契约内容由确定性代码生成，结构、
            # 文案与唯一占位组件都不可被模型的“创作欲”改变。
            draft = build_skeleton_draft(
                project_name=str(state.get("project_name") or ""),
                goal=latest_user_text(state.get("ui_history")),
            )
            self._emit(
                "architect.skeleton.contract",
                {
                    "runId": state.get("run_id"),
                    "components": len(draft.components),
                    "layout": [item["region"] for item in draft.page_layout],
                    "placeholder": True,
                },
                dedupe_key=f"architect.skeleton.contract:{state.get('run_id')}",
            )
        else:
            context = _context_from_state(state)
            draft = self._llm.draft_contract(context)
        self._check_cancelled()
        contract_id = state.get("contract_id") or f"ctr_{uuid.uuid4().hex[:20]}"
        self._emit(
            "architect.contract.drafting",
            {
                "contractId": contract_id,
                "appShape": state.get("app_shape"),
                "confidenceScore": float(state.get("confidence_score") or 0.0),
            },
            dedupe_key=f"architect.contract.drafting:{state['run_id']}",
        )
        return {
            "contract_id": contract_id,
            "contract_draft": draft_to_dict(draft),
            "contract_issues": [],
            "repair_count": 0,
            "status": "DRAFTING_CONTRACT",
        }

    def _validate_contract(self, state: ArchitectState) -> dict[str, Any]:
        self._check_cancelled()
        raw = state.get("contract_draft") or {}
        if not raw:
            return {
                "status": "FAILED",
                "error": "契约草稿为空",
                "error_code": "CONTRACT_DRAFT_MISSING",
                "contract_issues": ["契约草稿为空"],
            }
        draft = draft_from_dict(raw)
        shape = str(state.get("app_shape") or "")
        project_mode = str(state.get("project_mode") or "NEW_PROJECT")
        validation = validate_draft(
            shape,
            draft,
            project_mode=project_mode,
        )
        issues = list(validation.issues)
        audit_problems: list[Any] = []
        if validation.valid and validation.draft is not None:
            # 契约确认前的最后一道自检：DDL 表、relations、apiRoutes 与
            # 组件交互必须彼此自洽。这里与运行期 audit_contract 共用同一
            # 实现（只读、无副作用），避免把矛盾带进代码生成。
            # 存量项目：已有真实数据库与前端，跳过“凭空生成 DDL 并自检”，
            # 否则会把增量 DDL 误判为全量契约（父键不在父表等假错误）。
            if shape == "full_stack_system" and project_mode != "EXISTING_SOURCE":
                # 延迟导入：fullstack 包会反向依赖 architect.service，
                # 顶层导入会造成循环依赖。
                from ai_core.fullstack.contract_audit import validate_contract

                audit = validate_contract(
                    self._contract_view(state, validation.draft),
                    mode="fullstack",
                )
                audit_problems = list(audit.problems)
                issues.extend(problem.message for problem in audit.problems)
            if not issues:
                return {
                    "contract_draft": draft_to_dict(validation.draft),
                    "contract_issues": [],
                    "status": "DRAFTING_CONTRACT",
                }

        repair_limit = int(state.get("repair_limit") or 1)
        repair_count = int(state.get("repair_count") or 0)
        if repair_count < repair_limit:
            self._emit(
                "architect.contract.repair.requested",
                {
                    "contractId": state.get("contract_id"),
                    "issues": issues,
                    "attempt": repair_count + 1,
                },
                dedupe_key=(
                    f"architect.contract.repair:"
                    f"{state.get('contract_id')}:{repair_count + 1}"
                ),
            )
            return {
                "contract_draft": raw,
                "contract_issues": issues,
                "contract_feedback": self._repair_feedback(issues, audit_problems),
                "repair_count": repair_count + 1,
                "status": "REPAIRING_CONTRACT",
            }

        # 自动修复已耗尽：如果剩余问题属于“集合根与 DDL 表名矛盾”，
        # 不再直接 FAILED，而是把选择权交给用户（映射已有表 / 新建表 /
        # 取消）。这是死锁唯一正确的出口：继续重试只会产出同一错误。
        if (
            validation.valid
            and validation.draft is not None
            and audit_problems
            and not bool(state.get("conflict_resolution_applied"))
        ):
            conflict = self._build_contract_conflict(
                state, validation.draft, audit_problems, issues
            )
            if conflict is not None:
                return {
                    "contract_draft": raw,
                    "contract_issues": issues,
                    "contract_conflict": conflict,
                    "status": "AWAITING_DECISION",
                }

        return {
            "contract_draft": raw,
            "contract_issues": issues,
            "status": "FAILED",
            "error": "实施契约未通过分级校验：" + "；".join(issues),
            "error_code": "CONTRACT_VALIDATION_FAILED",
        }

    @staticmethod
    def _repair_feedback(
        issues: list[str],
        audit_problems: list[Any],
    ) -> str:
        """把审计 hint 与命名规范一起回灌模型，避免二次修仍然盲目。"""
        parts = list(dict.fromkeys(str(item) for item in issues if item))
        hints = [
            str(problem.hint)
            for problem in audit_problems
            if getattr(problem, "hint", "")
        ]
        if hints:
            parts.append("修复提示：" + "；".join(dict.fromkeys(hints)))
        naming_codes = {
            "nested_table_missing",
            "collection_table_missing",
        }
        if any(
            getattr(problem, "code", "") in naming_codes for problem in audit_problems
        ):
            parts.append(
                "命名规范：URL 路由使用 kebab-case，DDL 表名必须使用 "
                "snake_case，且语义一一对应；例如 /tailored-resumes 对应 "
                "tailored_resumes，/job-descriptions 对应 job_descriptions。"
                "请在 DDL 中创建对应子表，或改为父资源详情查询。"
            )
        return "；".join(dict.fromkeys(parts))

    def _build_contract_conflict(
        self,
        state: ArchitectState,
        draft: ContractDraft,
        audit_problems: list[Any],
        issues: list[str],
    ) -> dict[str, Any] | None:
        """把“集合根 → 表名”矛盾转换为用户可决策的结构化冲突。

        只针对 ``collection_table_missing``：这类矛盾有两个确定性出路
        （映射到已有表、或新建缺失表），适合交给用户拍板。其它契约
        问题（关联字段不存在等）仍按原逻辑失败，避免把复杂矛盾误包装成
        三选一。
        """
        problem = next(
            (
                item
                for item in audit_problems
                if item.code == "collection_table_missing"
            ),
            None,
        )
        if problem is None:
            return None

        from ai_core.domain.relations import parse_ddl_table_columns
        from ai_core.fullstack.coders import (
            action_resource_table,
            collection_path,
            core_table_for_contract,
        )
        from ai_core.fullstack.contract_audit import collection_resource

        tables_map = parse_ddl_table_columns(draft.database_ddl or "")
        tables = list(tables_map)
        if not tables:
            return None
        collection = collection_path(draft.api_routes or [])
        resource = collection_resource(collection)
        if not resource:
            return None

        # 认证类动作优先映射 users；普通业务资源映射到契约选出的核心表。
        suggested = action_resource_table(resource, tables)
        if not suggested:
            core = core_table_for_contract(self._contract_view(state, draft))
            if core.name.lower() in {name.lower() for name in tables}:
                suggested = core.name
        options: list[dict[str, Any]] = []
        if suggested:
            options.append(
                {
                    "optionId": "map_to_table",
                    "action": "MAP_TO_TABLE",
                    "label": f"自动将 {collection} 映射到 {suggested} 表（推荐）",
                    "description": (
                        f"保留现有接口，把集合根 {collection} 改写到真实表 "
                        f"{suggested} 对应的路径，不新增冗余表。"
                    ),
                    "targetTable": suggested,
                }
            )
        if resource.lower() not in {name.lower() for name in tables}:
            options.append(
                {
                    "optionId": "create_table",
                    "action": "CREATE_TABLE",
                    "label": f"在 DDL 中新建 {resource} 表",
                    "description": (
                        f"新增一张最小可用的 {resource} 表（id / name / "
                        "时间戳），保持现有 /api 集合路径不变。"
                    ),
                    "targetTable": resource,
                }
            )
        options.append(
            {
                "optionId": "cancel",
                "action": "CANCEL",
                "label": "取消生成，我重新修改契约",
                "description": "停止本次架构流程，回到需求澄清重新描述数据模型。",
                "targetTable": "",
            }
        )

        contract_id = str(state.get("contract_id") or "")
        return {
            "conflictId": f"ccf_{contract_id or state.get('run_id')}",
            "runId": str(state.get("run_id") or ""),
            "contractId": contract_id,
            "kind": "contract_naming_conflict",
            "status": "PENDING",
            "appShape": str(state.get("app_shape") or ""),
            "reason": problem.code,
            "message": problem.message,
            "apiCollection": collection,
            "resource": resource,
            "missingTable": resource,
            "suggestedTable": suggested,
            "tables": tables,
            "issues": list(issues),
            "options": options,
        }

    def _conflict_gate(self, state: ArchitectState) -> dict[str, Any]:
        """暂停图执行，让用户选择如何处理契约命名冲突。"""
        self._check_cancelled()
        conflict = dict(state.get("contract_conflict") or {})
        if not conflict:
            return {
                "status": "FAILED",
                "error": "契约冲突决策缺少上下文",
                "error_code": "CONTRACT_CONFLICT_MISSING",
            }
        self._emit(
            "architect.contract.conflict",
            conflict,
            dedupe_key=f"architect.contract.conflict:{conflict.get('conflictId')}",
        )
        response = interrupt(conflict)
        option = _normalize_conflict_option(conflict, response)
        if option is None:
            return {
                "status": "FAILED",
                "error": "契约冲突决策无效：请选择服务端下发的 optionId",
                "error_code": "CONTRACT_CONFLICT_OPTION_INVALID",
            }
        comment = ""
        if isinstance(response, dict):
            comment = str(response.get("comment") or "").strip()
        self._emit(
            "architect.contract.conflict.answered",
            {
                "conflictId": conflict.get("conflictId"),
                "optionId": option.get("optionId"),
                "action": option.get("action"),
                "targetTable": option.get("targetTable"),
                "comment": comment,
            },
            dedupe_key=(
                "architect.contract.conflict.answered:" f"{conflict.get('conflictId')}"
            ),
        )
        if option.get("action") == "CANCEL":
            return {
                "contract_conflict": {
                    **conflict,
                    "status": "ANSWERED",
                    "answeredAt": datetime.now(timezone.utc).isoformat(),
                },
                "contract_decision": "CANCEL",
                "contract_feedback": comment or "用户取消生成，准备重新修改契约",
                "status": "CANCELLED",
            }

        raw = state.get("contract_draft") or {}
        try:
            draft = draft_from_dict(raw)
            resolved = _apply_conflict_resolution(draft, conflict, option)
        except Exception as exc:  # noqa: BLE001 - 决策应用失败必须收敛
            logger.warning("apply contract conflict resolution failed", exc_info=True)
            return {
                "status": "FAILED",
                "error": f"应用契约冲突决策失败: {exc}",
                "error_code": "CONTRACT_CONFLICT_RESOLUTION_FAILED",
            }

        repair_limit = int(state.get("repair_limit") or 1)
        return {
            "contract_draft": draft_to_dict(resolved),
            "contract_issues": [],
            "contract_feedback": "",
            "contract_conflict": {
                **conflict,
                "status": "ANSWERED",
                "answeredAt": datetime.now(timezone.utc).isoformat(),
            },
            "conflict_resolution_applied": True,
            # 用户已拍板，不再让模型自动修一次；直接确定性复验。
            "repair_count": repair_limit,
            "status": "DRAFTING_CONTRACT",
        }

    @staticmethod
    def _contract_view(
        state: ArchitectState, draft: ContractDraft
    ) -> ArchitectContract:
        """把待确认草稿包装成只读契约视图，供 ``validate_contract`` 自检。

        这里不落库、不改状态；审计函数只读取 DDL / relations / 路由 /
        组件交互四个维度。
        """
        return ArchitectContract(
            contract_id=str(state.get("contract_id") or "ctr_draft"),
            run_id=str(state.get("run_id") or ""),
            app_shape="full_stack_system",
            confidence_score=min(
                max(float(state.get("confidence_score") or 0.0), 0.0), 1.0
            ),
            contract_markdown=draft.contract_markdown,
            tech_stack=draft.tech_stack,
            api_routes=draft.api_routes,
            database_ddl=draft.database_ddl,
            relations=draft.relations,
            component_interactions=draft.component_interactions,
        )

    def _revise_contract(self, state: ArchitectState) -> dict[str, Any]:
        self._check_cancelled()
        raw = state.get("contract_draft") or {}
        if not raw:
            return {
                "status": "FAILED",
                "error": "待修订的契约不存在",
                "error_code": "CONTRACT_DRAFT_MISSING",
            }
        previous = draft_from_dict(raw)
        feedback = str(
            state.get("contract_feedback")
            or "；".join(state.get("contract_issues") or [])
            or "按规范修正契约"
        )
        context = _context_from_state(state)
        skeleton_mode = bool(state.get("skeleton_mode"))
        # 校验修复也走本节点。骨架契约由确定性代码生成，若仍被校验
        # 拒绝，直接重建而不是交给 LLM“自由发挥”，否则会重新引入脑补。
        if skeleton_mode and state.get("status") == "REPAIRING_CONTRACT":
            draft = build_skeleton_draft(
                project_name=str(state.get("project_name") or ""),
                goal=latest_user_text(state.get("ui_history")),
            )
            self._check_cancelled()
            return {
                "contract_draft": draft_to_dict(draft),
                "contract_feedback": "",
                "status": "DRAFTING_CONTRACT",
            }

        if skeleton_mode and is_abstract_brief(feedback):
            # 用户仍然只给了抽象反馈：继续确定性生成骨架，避免模型借
            # “修订”之名补出业务组件。
            draft = build_skeleton_draft(
                project_name=str(state.get("project_name") or ""),
                goal=feedback or latest_user_text(state.get("ui_history")),
            )
            self._check_cancelled()
            return {
                "contract_draft": draft_to_dict(draft),
                "contract_feedback": "",
                "status": "DRAFTING_CONTRACT",
            }

        if skeleton_mode:
            # 用户开始补充具体业务（例如“需要登录和订单列表”）：带着
            # 反馈进入正常契约修订，同时永久关闭骨架模式。
            context = replace(
                context,
                ui_history=list(context.ui_history)
                + [{"role": "user", "content": feedback}],
            )
        draft = self._llm.revise_contract(context, previous, feedback)
        draft = replace(draft, skeleton_mode=False)
        self._check_cancelled()
        return {
            "contract_draft": draft_to_dict(draft),
            "contract_feedback": "",
            "skeleton_mode": False,
            "status": "DRAFTING_CONTRACT",
        }

    def _contract_ready(self, state: ArchitectState) -> dict[str, Any]:
        self._check_cancelled()
        raw = state.get("contract_draft") or {}
        draft = draft_from_dict(raw)
        # 质量分级在契约确认时写入；修订重算契约时必须沿用用户已选分级，
        # 不能被重新装配悄悄重置回默认 L2。
        existing = state.get("contract") or {}
        quality_tier = (
            normalize_quality_tier(state.get("quality_tier"))
            or normalize_quality_tier(existing.get("qualityTier"))
            or "L2_STANDARD"
        )
        contract = assemble_contract(
            contract_id=str(state.get("contract_id") or ""),
            run_id=str(state.get("run_id") or ""),
            shape=str(state.get("app_shape") or ""),
            confidence_score=float(state.get("confidence_score") or 0.0),
            threshold=float(state.get("threshold") or 0.85),
            source_hash=str(state.get("source_hash") or ""),
            draft=draft,
            citations=_citations(state),
            quality_tier=quality_tier,
            project_mode=str(state.get("project_mode") or "NEW_PROJECT"),
        )
        contract_payload = contract.model_dump(mode="json", by_alias=True)
        self._emit(
            "architect.contract.ready",
            {
                "contract": contract_payload,
                "citations": [
                    item.model_dump(by_alias=True) for item in contract.citations
                ],
            },
            dedupe_key=(
                f"architect.contract.ready:{contract.contract_id}:"
                f"{int(state.get('contract_revision_count') or 0)}"
            ),
        )
        return {
            "contract": contract_payload,
            "quality_tier": quality_tier,
            "status": "AWAITING_CONTRACT_CONFIRMATION",
        }

    def _contract_gate(self, state: ArchitectState) -> dict[str, Any]:
        self._check_cancelled()
        contract = state.get("contract") or {}
        payload = {
            "kind": "contract_confirmation",
            "contractId": contract.get("contractId") or state.get("contract_id"),
            "runId": state.get("run_id"),
            "appShape": state.get("app_shape"),
            "confidenceScore": float(state.get("confidence_score") or 0.0),
            "threshold": float(state.get("threshold") or 0.85),
            "contract": contract,
            "decisions": ["CONFIRM", "REVISE", "CANCEL"],
            # 风险自适应门禁：契约确认前必须由用户显式选择质量分级。
            # 该问题不可跳过；前端在未选择时禁用 CONFIRM 按钮，
            # 旧客户端未传时后端按 L2 兜底以保持协议向后兼容。
            "qualityTierRequired": True,
            "qualityTierPrompt": QUALITY_TIER_PROMPT,
            "qualityTierOptions": QUALITY_TIER_OPTIONS,
            "qualityTier": contract.get("qualityTier")
            or state.get("quality_tier")
            or "L2_STANDARD",
        }
        response = interrupt(payload)
        decision = _normalize_decision(response)
        comment = ""
        selected_tier = ""
        if isinstance(response, dict):
            comment = str(response.get("comment") or "").strip()
            selected_tier = normalize_quality_tier(
                response.get("qualityTier") or response.get("quality_tier")
            )
        # 未显式选择时沿用已知分级；首次确认且旧客户端未传则落 L2。
        quality_tier = (
            selected_tier
            or normalize_quality_tier(contract.get("qualityTier"))
            or normalize_quality_tier(state.get("quality_tier"))
            or "L2_STANDARD"
        )
        if selected_tier:
            # 把用户选择写回契约快照，_confirmed / 前端事件 / 后续
            # FullstackGraph 都读取同一份 contract.qualityTier。
            contract = dict(contract)
            contract["qualityTier"] = selected_tier

        if decision == "REVISE":
            revision_count = int(state.get("contract_revision_count") or 0)
            if revision_count >= CONTRACT_REVISION_SOFT_LIMIT:
                return {
                    "status": "FAILED",
                    "error": "契约修订次数超过上限，请重新发起架构澄清",
                    "error_code": "CONTRACT_REVISION_EXHAUSTED",
                }
            self._emit(
                "architect.contract.revision_requested",
                {
                    "contractId": state.get("contract_id"),
                    "comment": comment,
                    "revision": revision_count + 1,
                },
                dedupe_key=(
                    f"architect.contract.revision:"
                    f"{state.get('contract_id')}:{revision_count + 1}"
                ),
            )
            return {
                "contract": contract,
                "quality_tier": quality_tier,
                "contract_decision": "REVISE",
                "contract_feedback": comment,
                "contract_revision_count": revision_count + 1,
                "repair_count": 0,
                "status": "REVISING_CONTRACT",
            }
        return {
            "contract": contract,
            "quality_tier": quality_tier,
            "contract_decision": decision,
            "contract_feedback": comment,
            "status": (
                "CONFIRMING_CONTRACT" if decision == "CONFIRM" else "CANCELLING"
            ),
        }

    def _route_validation(self, state: ArchitectState) -> str:
        status = state.get("status")
        if status == "FAILED":
            return "failed"
        if status == "AWAITING_DECISION":
            return "conflict_gate"
        if state.get("contract_issues"):
            repair_limit = int(state.get("repair_limit") or 1)
            if int(state.get("repair_count") or 0) <= repair_limit:
                return "revise_contract"
            return "failed"
        return "contract_ready"

    def _route_conflict_decision(self, state: ArchitectState) -> str:
        status = state.get("status")
        if status == "CANCELLED":
            return "cancelled"
        if status == "FAILED":
            return "failed"
        return "validate_contract"

    def _route_contract_decision(self, state: ArchitectState) -> str:
        if state.get("status") == "FAILED":
            return "failed"
        decision = state.get("contract_decision")
        if decision == "CONFIRM":
            return "confirmed"
        if decision == "REVISE":
            return "revise_contract"
        if decision == "CANCEL":
            return "cancelled"
        return "failed"

    # --------------------------------------------------------
    # 终态
    # --------------------------------------------------------
    def _confirmed(self, state: ArchitectState) -> dict[str, Any]:
        contract = _contract_from_state(state)
        if contract is not None:
            # 双保险：用户选择的质量分级以 state 为准，避免旧 checkpoint
            # 里的 contract 快照缺字段时回退 L2。
            tier = normalize_quality_tier(state.get("quality_tier"))
            if tier:
                contract.quality_tier = tier  # type: ignore[assignment]
            contract.status = "CONFIRMED"
            contract.decided_at = datetime.now(timezone.utc)
            payload = contract.model_dump(mode="json", by_alias=True)
        else:
            payload = {}
        self._emit(
            "architect.contract.confirmed",
            {
                "contractId": state.get("contract_id"),
                "contract": payload,
                "comment": state.get("contract_feedback") or "",
            },
            # 同一 contractId 可以被追加需求多次修订并再次确认；dedupe key
            # 必须带 revision，否则第二次 CONFIRMED 会被事件缓冲区丢弃，
            # 前端 SSE 永远等不到终态。
            dedupe_key=(
                f"architect.contract.confirmed:{state.get('contract_id')}:"
                f"{int(state.get('contract_revision_count') or 0)}"
            ),
        )
        return {"contract": payload, "status": "CONFIRMED"}

    def _cancelled(self, state: ArchitectState) -> dict[str, Any]:
        contract = _contract_from_state(state)
        if contract is not None:
            contract.status = "CANCELLED"
            contract.decided_at = datetime.now(timezone.utc)
            payload = contract.model_dump(mode="json", by_alias=True)
        else:
            payload = {}
        self._emit(
            "architect.cancelled",
            {
                "contractId": state.get("contract_id"),
                "reason": state.get("contract_feedback") or "",
            },
            dedupe_key=f"architect.cancelled:{state.get('run_id')}",
        )
        return {"contract": payload, "status": "CANCELLED"}

    def _confidence_exhausted(self, state: ArchitectState) -> dict[str, Any]:
        error = (
            f"经过 {state.get('max_rounds')} 轮澄清仍未达到置信度阈值 "
            f"{state.get('threshold')}，请补充需求后重新发起"
        )
        run_id = str(state.get("run_id") or "")
        envelope = envelope_from_code(
            "CONFIDENCE_EXHAUSTED",
            technical_message=error,
            trace_id=f"trc_{run_id}" if run_id else None,
            run_id=run_id,
            stage="architect.confidence_exhausted",
        )
        self._emit(
            "architect.failed",
            attach_error_envelope(
                {
                    "error": envelope.user_message,
                    "errorCode": "CONFIDENCE_EXHAUSTED",
                    "round": state.get("round"),
                    "confidenceScore": state.get("confidence_score"),
                    "missingSlots": state.get("missing_slots") or [],
                },
                envelope,
            ),
            dedupe_key=f"architect.failed:{state.get('run_id')}",
        )
        return {
            "status": "FAILED",
            "error": error,
            "error_code": "CONFIDENCE_EXHAUSTED",
        }

    def _failed(self, state: ArchitectState) -> dict[str, Any]:
        error = state.get("error") or "架构澄清失败"
        code = state.get("error_code") or "ARCHITECT_FAILED"
        run_id = str(state.get("run_id") or "")
        envelope = envelope_from_code(
            code,
            technical_message=error,
            trace_id=f"trc_{run_id}" if run_id else None,
            run_id=run_id,
            stage="architect.failed",
        )
        self._emit(
            "architect.failed",
            attach_error_envelope(
                {
                    "error": envelope.user_message,
                    "errorCode": code,
                    "round": state.get("round"),
                    "confidenceScore": state.get("confidence_score"),
                },
                envelope,
            ),
            dedupe_key=f"architect.failed:{run_id}:{code}",
        )
        return {"status": "FAILED", "error": error, "error_code": code}

    # --------------------------------------------------------
    # 工具
    # --------------------------------------------------------
    def _check_cancelled(self) -> None:
        if self._cancel_check is not None and self._cancel_check():
            raise ArchitectRunCancelledError("Architect Run 已被用户取消")

    def _emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
    ) -> None:
        if self._pause_hook is not None and event_type in {
            "architect.clarification.required",
            "architect.contract.ready",
            # 确认事件也必须在 SSE 可见前预热内存状态：否则前端收到
            # confirmed 后立刻追加需求，会命中仍为 CONFIRMING_CONTRACT
            # 的 409 竞态。
            "architect.contract.confirmed",
        }:
            try:
                self._pause_hook(event_type, payload)
            except Exception:  # noqa: BLE001 - 状态预热失败不能阻断 SSE
                logger.warning(
                    "failed to prepare pause state for %s",
                    event_type,
                    exc_info=True,
                )
        try:
            self._event_sink.emit(event_type, payload, dedupe_key=dedupe_key)
        except Exception:  # noqa: BLE001 - 事件失败不能打断主流程
            logger.warning("failed to emit %s", event_type, exc_info=True)


# ------------------------------------------------------------
# 状态 / 槽位 / RAG 工具
# ------------------------------------------------------------
def _has_semantic_review_packet(state: ArchitectState) -> bool:
    """冻结画布审查包只影响首轮分析：不压缩、不被通用槽位问题挤掉。"""
    return any(
        "【SEMANTIC_REVIEW_PACKET】" in str(item.get("content") or "")
        for item in (state.get("ui_history") or [])
        if isinstance(item, dict)
    )


def _context_from_state(state: ArchitectState) -> ArchitectContext:
    return ArchitectContext(
        project_id=str(state.get("project_id") or ""),
        project_name=str(state.get("project_name") or "未命名项目"),
        source_files=dict(state.get("source_files") or {}),
        ui_history=list(state.get("ui_history") or []),
        slots=dict(state.get("slots") or {}),
        app_shape=state.get("app_shape") or None,
        round=int(state.get("round") or 0),
        threshold=float(state.get("threshold") or 0.85),
        retrieved_chunks=list(state.get("retrieved_chunks") or []),
        source_hash=str(state.get("source_hash") or ""),
        project_summary=str(state.get("project_summary") or ""),
        latest_changes=str(state.get("latest_changes") or ""),
        project_mode=str(state.get("project_mode") or "NEW_PROJECT"),
        direct_changelog=list(state.get("direct_changelog") or []),
    )


def _rag_query(state: ArchitectState) -> str:
    shape = state.get("app_shape") or "未知形态"
    slots = json.dumps(
        state.get("slots") or {},
        ensure_ascii=False,
        default=str,
    )
    history = " ".join(
        str(item.get("content") or "")
        for item in (state.get("ui_history") or [])[-6:]
        if isinstance(item, dict)
    )
    return (f"{state.get('project_name') or ''} {shape} {slots} {history}").strip()[
        :2000
    ]


def _build_clarification(state: ArchitectState) -> ArchitectClarification:
    round_no = int(state.get("round") or 0) + 1
    missing = list(state.get("missing_slots") or [])
    questions = [
        ArchitectQuestion.model_validate(item)
        for item in state.get("questions") or []
        if isinstance(item, dict)
    ]
    return ArchitectClarification(
        clarification_id=f"clr_{state.get('run_id')}_{round_no}",
        run_id=str(state.get("run_id") or ""),
        round=round_no,
        max_rounds=int(state.get("max_rounds") or 10),
        status="PENDING",
        app_shape=state.get("app_shape") or None,  # type: ignore[arg-type]
        confidence_score=float(state.get("confidence_score") or 0.0),
        threshold=float(state.get("threshold") or 0.85),
        missing_slots=missing,
        reason=(
            "app_shape_required"
            if not state.get("app_shape")
            else "missing_required_slots"
        ),
        questions=questions,
    )


def _normalize_resume_answers(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        raw_answers = response.get("answers") or []
        comment = response.get("comment")
        adopt_suggestions = bool(
            response.get("adopt_suggestions") or response.get("adoptSuggestions")
        )
    elif isinstance(response, list):
        raw_answers = response
        comment = None
        adopt_suggestions = False
    else:
        raw_answers = []
        comment = None
        adopt_suggestions = False
    answers = []
    for item in raw_answers:
        if isinstance(item, dict):
            answers.append(item)
    return {
        "answers": answers,
        "comment": comment,
        "adopt_suggestions": adopt_suggestions,
    }


def _answer_value(value: Any, text: Any) -> Any:
    if value not in (None, ""):
        return value
    if text not in (None, ""):
        return text
    return None


def _answer_content(
    question: dict[str, Any] | None,
    value: Any,
    text: Any,
) -> str:
    resolved = _answer_value(value, text)
    if resolved in (None, ""):
        return ""
    prompt = str((question or {}).get("prompt") or (question or {}).get("slot") or "")
    return f"{prompt} {resolved}".strip()[:2000]


def _normalize_shape(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip().lower()
    if raw in {
        "lightweight_web",
        "lightweight",
        "轻量网页工具",
        "轻量应用",
    }:
        return "lightweight_web"
    if raw in {
        "script_automation",
        "script",
        "自动化脚本",
        "脚本自动化",
    }:
        return "script_automation"
    if raw in {
        "full_stack_system",
        "fullstack",
        "full-stack",
        "全栈系统",
        "全栈",
    }:
        return "full_stack_system"
    return None


def _normalize_decision(response: Any) -> str:
    if isinstance(response, dict):
        raw = response.get("decision") or response.get("action") or ""
    else:
        raw = response or ""
    value = str(raw).strip().upper()
    if value in {"CONFIRM", "APPROVE", "APPROVED"}:
        return "CONFIRM"
    if value in {"REVISE", "REVISION", "CHANGES"}:
        return "REVISE"
    if value in {"CANCEL", "CANCELLED"}:
        return "CANCEL"
    return ""


def _citations(state: ArchitectState) -> list[ArchitectCitation]:
    citations: list[ArchitectCitation] = []
    for item in state.get("retrieved_chunks") or []:
        if not isinstance(item, dict):
            continue
        try:
            citations.append(
                ArchitectCitation(
                    doc_id=str(item.get("docId") or ""),
                    title=str(item.get("title") or ""),
                    section=str(item.get("section") or ""),
                    path=str(item.get("path") or ""),
                    score=float(item.get("score") or 0.0),
                )
            )
        except Exception:  # noqa: BLE001
            continue
    return citations


def _contract_from_state(state: ArchitectState) -> ArchitectContract | None:
    payload = state.get("contract") or {}
    if not payload:
        return None
    try:
        return ArchitectContract.model_validate(payload)
    except Exception:  # noqa: BLE001
        return None


def _normalize_conflict_option(
    conflict: dict[str, Any],
    response: Any,
) -> dict[str, Any] | None:
    """只接受服务端下发的 optionId / action，拒绝模型或客户端自造选项。"""
    if not isinstance(response, dict):
        return None
    options = [item for item in conflict.get("options") or [] if isinstance(item, dict)]
    option_id = str(response.get("optionId") or response.get("option_id") or "").strip()
    if option_id:
        return next(
            (item for item in options if item.get("optionId") == option_id),
            None,
        )
    action = str(response.get("action") or "").strip().upper()
    if action:
        return next(
            (item for item in options if str(item.get("action")).upper() == action),
            None,
        )
    return None


def _rewrite_collection_path(path: str, old: str, new: str) -> str:
    """把集合根及其子路径的公共前缀从 old 替换为 new，保留查询串。

    component_interactions 里是 ``GET /api/v1/orders`` 这类“方法 + 路径”
    写法，必须保留方法前缀后只改路径，否则交互引用会与 api_routes
    失配，审计会继续报 undeclared_interaction_route。
    """
    raw = str(path or "")
    head, space, rest = raw.partition(" ")
    if space and head.upper() in {
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "HEAD",
        "OPTIONS",
    }:
        return f"{head.upper()} {_rewrite_collection_path(rest, old, new)}"
    base, sep, query = raw.partition("?")
    if base == old or base.startswith(old + "/"):
        return new + base[len(old) :] + sep + query
    return raw


def _sanitize_table_name(value: str) -> str:
    name = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower())
    name = re.sub(r"_+", "_", name).strip("_")
    if not name or not re.match(r"^[a-z_]", name):
        name = f"app_{name}" if name else "app_records"
    return name[:48]


def _minimal_table_ddl(table: str, engine: str) -> str:
    """生成最小可用表结构；只用于用户在冲突决策中显式选择“新建表”。"""
    name = _sanitize_table_name(table)
    normalized = str(engine or "").strip().lower()
    if "postgres" in normalized:
        body = (
            "  id BIGSERIAL PRIMARY KEY,\n"
            "  name VARCHAR(255) NOT NULL,\n"
            "  created_at TIMESTAMP(3) NOT NULL,\n"
            "  updated_at TIMESTAMP(3) NOT NULL\n"
        )
    elif "sqlite" in normalized:
        body = (
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
            "  name TEXT NOT NULL,\n"
            "  created_at TEXT NOT NULL,\n"
            "  updated_at TEXT NOT NULL\n"
        )
    else:
        body = (
            "  id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,\n"
            "  name VARCHAR(255) NOT NULL,\n"
            "  created_at DATETIME(3) NOT NULL,\n"
            "  updated_at DATETIME(3) NOT NULL\n"
        )
    return f"CREATE TABLE IF NOT EXISTS {name} (\n{body});"


def _apply_conflict_resolution(
    draft: ContractDraft,
    conflict: dict[str, Any],
    option: dict[str, Any],
) -> ContractDraft:
    """把用户选择确定性地写回契约草稿。

    两条路径都必须产出“可重新通过审计”的契约：
      - MAP_TO_TABLE：重写集合根及其子路由 / 交互路径到目标真实表；
      - CREATE_TABLE：向 DDL 追加最小表，保留现有 API 路径。
    """
    action = str(option.get("action") or "").upper()
    label = str(option.get("label") or action)
    markdown = draft.contract_markdown.rstrip() + (
        f"\n\n## 命名冲突决策\n\n- 用户选择：{label}\n"
    )

    if action == "MAP_TO_TABLE":
        old_collection = str(conflict.get("apiCollection") or "").strip()
        target = _sanitize_table_name(str(option.get("targetTable") or ""))
        if not old_collection or not target:
            raise ValueError("映射决策缺少集合路径或目标表")
        segments = [item for item in old_collection.split("/") if item]
        if not segments:
            raise ValueError("集合路径无效")
        segments[-1] = target
        new_collection = "/" + "/".join(segments)

        seen: set[tuple[str, str]] = set()
        routes = []
        for route in draft.api_routes:
            path = _rewrite_collection_path(
                str(route.path or ""), old_collection, new_collection
            )
            key = (str(route.method or "GET").upper(), path)
            if key in seen:
                continue
            seen.add(key)
            routes.append(route.model_copy(update={"path": path}))
        interactions = [
            item.model_copy(
                update={
                    "api_route": _rewrite_collection_path(
                        str(item.api_route or ""), old_collection, new_collection
                    )
                }
            )
            for item in draft.component_interactions
        ]
        return replace(
            draft,
            contract_markdown=markdown,
            api_routes=routes,
            component_interactions=interactions,
        )

    if action == "CREATE_TABLE":
        table = _sanitize_table_name(
            str(option.get("targetTable") or conflict.get("missingTable") or "")
        )
        engine = str(draft.tech_stack.get("database") or "mysql")
        ddl = _minimal_table_ddl(table, engine)
        separator = "\n\n" if draft.database_ddl.strip() else ""
        return replace(
            draft,
            contract_markdown=markdown,
            database_ddl=f"{draft.database_ddl.rstrip()}{separator}{ddl}",
        )

    raise ValueError(f"不支持的冲突决策: {action}")


def _find_interrupt(snapshot: Any) -> dict[str, Any] | None:
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or []:
            value = getattr(item, "value", None)
            if isinstance(value, dict):
                return value
    return None
