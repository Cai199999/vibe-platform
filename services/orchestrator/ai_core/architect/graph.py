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
    ArchitectCitation,
    ArchitectClarification,
    ArchitectContract,
    ArchitectQuestion,
)
from ai_core.domain.state import ArchitectState
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
                    )
                )
                token_scope = collect_tokens(emitter.feed)
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
                "failed": "failed",
            },
        )
        builder.add_edge("revise_contract", "validate_contract")
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
        analysis, guard = apply_confidence_guard(
            analysis,
            dict(state.get("slots") or {}),
            float(state.get("threshold") or 0.85),
        )
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
                    "reasons": guard.reasons,
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
                "reasons": guard.reasons,
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
        validation = validate_draft(shape, draft)
        issues = list(validation.issues)
        if validation.valid and validation.draft is not None:
            # 契约确认前的最后一道自检：DDL 表、relations、apiRoutes 与
            # 组件交互必须彼此自洽。这里与运行期 audit_contract 共用同一
            # 实现（只读、无副作用），避免把矛盾带进代码生成。
            if shape == "full_stack_system":
                # 延迟导入：fullstack 包会反向依赖 architect.service，
                # 顶层导入会造成循环依赖。
                from ai_core.fullstack.contract_audit import validate_contract

                audit = validate_contract(
                    self._contract_view(state, validation.draft),
                    mode="fullstack",
                )
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
                "contract_feedback": "；".join(issues),
                "repair_count": repair_count + 1,
                "status": "REPAIRING_CONTRACT",
            }
        return {
            "contract_draft": raw,
            "contract_issues": issues,
            "status": "FAILED",
            "error": "实施契约未通过分级校验：" + "；".join(issues),
            "error_code": "CONTRACT_VALIDATION_FAILED",
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
        contract = assemble_contract(
            contract_id=str(state.get("contract_id") or ""),
            run_id=str(state.get("run_id") or ""),
            shape=str(state.get("app_shape") or ""),
            confidence_score=float(state.get("confidence_score") or 0.0),
            threshold=float(state.get("threshold") or 0.85),
            source_hash=str(state.get("source_hash") or ""),
            draft=draft,
            citations=_citations(state),
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
        }
        response = interrupt(payload)
        decision = _normalize_decision(response)
        comment = ""
        if isinstance(response, dict):
            comment = str(response.get("comment") or "").strip()

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
                "contract_decision": "REVISE",
                "contract_feedback": comment,
                "contract_revision_count": revision_count + 1,
                "repair_count": 0,
                "status": "REVISING_CONTRACT",
            }
        return {
            "contract_decision": decision,
            "contract_feedback": comment,
            "status": (
                "CONFIRMING_CONTRACT" if decision == "CONFIRM" else "CANCELLING"
            ),
        }

    def _route_validation(self, state: ArchitectState) -> str:
        if state.get("status") == "FAILED":
            return "failed"
        if state.get("contract_issues"):
            repair_limit = int(state.get("repair_limit") or 1)
            if int(state.get("repair_count") or 0) <= repair_limit:
                return "revise_contract"
            return "failed"
        return "contract_ready"

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
        self._emit(
            "architect.failed",
            {
                "error": error,
                "errorCode": "CONFIDENCE_EXHAUSTED",
                "round": state.get("round"),
                "confidenceScore": state.get("confidence_score"),
                "missingSlots": state.get("missing_slots") or [],
            },
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
        self._emit(
            "architect.failed",
            {
                "error": error,
                "errorCode": code,
                "round": state.get("round"),
                "confidenceScore": state.get("confidence_score"),
            },
            dedupe_key=f"architect.failed:{state.get('run_id')}:{code}",
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


def _find_interrupt(snapshot: Any) -> dict[str, Any] | None:
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or []:
            value = getattr(item, "value", None)
            if isinstance(value, dict):
                return value
    return None
