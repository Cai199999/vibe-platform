"""LangGraph 单 Agent 闭环 + HITL 澄清门。

拓扑：

    START -> summarize_history -> planner -> clarify_gate -> style_check -> coder
              -> applier -> verifier -> evaluator
                ▲              │           ▲                              │
                │              │(澄清)     └──── repair (<= 3 次) ◄───────┤
                │              ▼                                          │
                └──── replan / interrupt()                                ├─ success + 审批 -> approval_gate -> awaiting_approval
                                                                          ├─ success + 免审批 -> succeeded (SUCCEEDED)
                                                                          └─ exhausted -> failed

`style_check` 只对新增组件生效：用户未说明颜色 / 大小 / 主次时先追问，
把回答写回标注上下文后再进入 Coder。

节点内部通过 ``EventSink`` 推送 node.* 事件；执行与 SSE 连接解耦。
"""

from __future__ import annotations

import contextvars
import copy
import difflib
import json
import logging
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt

from ai_core.adapters.checkpoint import get_checkpointer_provider
from ai_core.adapters.rag import get_rag_provider
from ai_core.ports.checkpoint import CheckpointerProvider
from ai_core.ports.rag import RAGProvider, RAGProviderError
from ai_core.agent.error_context import (
    error_file_hint,
    extract_error_context,
    extract_error_fingerprint,
)
from ai_core.agent.events import EventSink, NullEventSink
from ai_core.agent.llm import (
    AgentLLM,
    AgentLLMError,
    CodeResult,
    PlanResult,
    annotation_selector,
    annotation_target_file,
    annotation_text,
    append_css_rule,
    detect_move_offset,
    is_move_only_instruction,
)
from ai_core.agent.memory import IntentClassifier, compress_annotation_context
from ai_core.agent.preferences import (
    CRITICAL,
    MINOR,
    build_clarification_options,
    extract_preference_observations,
    infer_decision_impact,
    normalize_decision_impact,
    should_interrupt,
)
from ai_core.context_memory import get_project_memory
from ai_core.domain.actions import is_create_action
from ai_core.domain.state import AgentState
from ai_core.config import get_settings
from ai_core.observability.streaming import (
    TokenStreamEmitter,
    collect_tokens,
)
from ai_core.observability.token_usage import TokenUsage, UsageCollector, collect_usage
from ai_core.editor import (
    CodeEdit,
    EditKind,
    default_editor_registry,
)
from ai_core.ports.sandbox import SandboxError, SandboxProvider
from ai_core.tools import ToolRegistry, build_default_toolbox

logger = logging.getLogger(__name__)

NODE_LABELS = {
    "summarize_history": "压缩对话记忆",
    "research": "检索外部资料",
    "planner": "理解修改意图",
    "clarify_gate": "检查信息完整性",
    "style_check": "确认视觉样式",
    "coder": "生成修改方案",
    "applier": "写入沙盒",
    "verifier": "构建与校验",
    "evaluator": "整理结果",
    "reflection": "反思设计偏好",
    "approval_gate": "等待人工审批",
    "awaiting_approval": "确认结果",
    "succeeded": "完成",
    "rejected": "用户拒绝",
    "failed": "执行失败",
}


class RunCancelledError(Exception):
    """用户主动取消 Run。

    Graph 在节点安全点抛出，Service 捕获后回滚沙盒并发送
    ``run.cancelled`` 终态事件。放在 graph 模块避免依赖 Service 层。
    """


class CoderTimeoutError(Exception):
    """Coder 节点超过硬超时。

    模型调用没有可用的同步取消手段；超时后立即熔断本次 Run，后台线程的
    迟到结果会被丢弃。绝不进入构建自愈循环，否则前端会继续阻塞。
    """


CODER_TIMEOUT_MESSAGE = "由于代码结构复杂，AI 生成超时，请尝试缩小标注范围"
CODER_GENERATION_FAILED_MESSAGE = "AI 未生成可落地的新增组件，请重新描述或缩小标注范围"
# 首次超时后自动降级重试；让用户看到“系统在自救”，而不是干等 30s 后失败。
CODER_FALLBACK_PROGRESS = "已精简上下文并切换快速模型重试"
# 降级仍失败：不再把用户钉死在失败页，而是提供“转后台运行”的可恢复出口。
CODER_BACKGROUND_AVAILABLE_MESSAGE = (
    "本次修改较大，已自动精简上下文重试仍未完成。"
    "可点击“转后台运行”继续生成，完成后会通知你；当前草稿已保留。"
)
# 只有真正调用模型的节点才需要 token 流；其余节点保持零开销。
STREAMING_NODES = {"planner", "coder"}


@dataclass
class GraphRunOutcome:
    """一次 graph.invoke 的结果，包含暂停信息。"""

    state: AgentState
    interrupted: bool
    interrupt: dict[str, Any] | None
    thread_id: str
    # 整次 Run 的 Token 用量 / 费用（含澄清恢复前的历史节点）。
    usage: dict[str, Any] = field(default_factory=dict)


def _explicit_move_offset(annotation: dict[str, Any]) -> tuple[float, float] | None:
    """读取直接拖拽写入的累计位移。

    与自然语言解析不同，``action="MOVE"`` 的 ``payload.move.dx/dy`` 是
    前端几何计算的事实值：无需猜测方向、距离，也不受措辞影响。只接受
    有限数字，并复用 move_component 的 ±2000px 边界。
    """
    if str(annotation.get("action") or "").upper() != "MOVE":
        return None
    payload = annotation.get("payload") or {}
    move = payload.get("move") if isinstance(payload, dict) else None
    if not isinstance(move, dict):
        return None
    try:
        dx = float(move.get("dx"))
        dy = float(move.get("dy"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(dx) or not math.isfinite(dy):
        return None
    if abs(dx) > 2000 or abs(dy) > 2000:
        return None
    return dx, dy


def _parse_move_records(raw_moves: Any) -> list[dict[str, Any]] | None:
    """校验并归一化 payload.moves。

    每条记录都必须带选择器与有限、有界的累计偏移。任何一条非法都整体
    返回 None，回退到原有流程，绝不静默丢弃用户的布局草稿。
    """
    if not isinstance(raw_moves, list) or not raw_moves:
        return None
    # 上限既保护 Prompt / 补丁体积，也避免异常客户端造成长循环。
    if len(raw_moves) > 200:
        return None

    moves: list[dict[str, Any]] = []
    for raw in raw_moves:
        if not isinstance(raw, dict):
            return None
        selector = str(raw.get("selector") or "").strip()
        if not selector:
            return None
        try:
            dx = float(raw.get("dx"))
            dy = float(raw.get("dy"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(dx) or not math.isfinite(dy):
            return None
        if abs(dx) > 2000 or abs(dy) > 2000:
            return None
        moves.append(
            {
                "selector": selector,
                "dx": dx,
                "dy": dy,
                "source": str(raw.get("source") or "").strip(),
                "file": str(raw.get("file") or "").strip(),
                "label": str(raw.get("label") or "").strip(),
            }
        )
    return moves


def _explicit_batch_moves(
    annotation: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """解析 BATCH_MOVE 的 payload.moves。"""
    if str(annotation.get("action") or "").upper() != "BATCH_MOVE":
        return None
    payload = annotation.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    return _parse_move_records(payload.get("moves"))


def _explicit_layout_batch(
    annotation: dict[str, Any],
) -> dict[str, Any] | None:
    """解析 BATCH_LAYOUT：视觉暂存的位移 + 新增组件统一提交。

    返回：
      - moves：[{selector, dx, dy, ...}]，可为空；
      - additions：每项包含完整 CREATE_NEW annotate 信封，供 Coder 直接
        复用现有 UI 上下文裁剪与 data-vibe-created 标记校验。
    任一新增项不合法都返回 None，回退到普通流程而不是悄悄丢掉用户草稿。
    """
    if str(annotation.get("action") or "").upper() != "BATCH_LAYOUT":
        return None
    payload = annotation.get("payload") or {}
    if not isinstance(payload, dict):
        return None

    raw_moves = payload.get("moves")
    moves: list[dict[str, Any]] = []
    if raw_moves:
        parsed_moves = _parse_move_records(raw_moves)
        if parsed_moves is None:
            return None
        moves = parsed_moves

    raw_additions = payload.get("additions")
    additions: list[dict[str, Any]] = []
    if raw_additions:
        if not isinstance(raw_additions, list) or len(raw_additions) > 20:
            return None
        for raw in raw_additions:
            if not isinstance(raw, dict):
                return None
            nested = raw.get("annotation")
            if not isinstance(nested, dict):
                return None
            annotation_id = str(
                nested.get("annotationId") or nested.get("annotation_id") or ""
            ).strip()
            intent = nested.get("intent")
            intent = intent if isinstance(intent, dict) else {}
            ui = intent.get("ui")
            ui = ui if isinstance(ui, dict) else {}
            nested_payload = nested.get("payload")
            nested_payload = nested_payload if isinstance(nested_payload, dict) else {}
            text = str(
                raw.get("intent")
                or ui.get("summary")
                or nested_payload.get("text")
                or ""
            ).strip()
            if not annotation_id or not text:
                return None
            # 强制嵌套信封走 CREATE_NEW：前端暂存的一定是“新增组件”。
            normalized = dict(nested)
            normalized["annotationId"] = annotation_id
            normalized["action"] = "CREATE_NEW"
            normalized.setdefault("type", "visual-draw")
            if not isinstance(normalized.get("intent"), dict):
                normalized["intent"] = {
                    "kind": "UI_ONLY",
                    "ui": {"summary": text, "acceptance": [], "elementRole": "element"},
                }
            additions.append(
                {
                    "annotationId": annotation_id,
                    "intent": text,
                    "annotation": normalized,
                }
            )

    if not moves and not additions:
        return None
    return {"moves": moves, "additions": additions}


def _is_explicit_layout_batch(annotations: list[dict[str, Any]]) -> bool:
    """单个 BATCH_LAYOUT 是确定性布局批次，跳过 RAG / Planner LLM。"""
    return len(annotations) == 1 and _explicit_layout_batch(annotations[0]) is not None


def _is_explicit_move_batch(annotations: list[dict[str, Any]]) -> bool:
    """所有标注都是确定性位移时，跳过 RAG / Planner LLM。

    支持两种协议：
      - 单个 BATCH_MOVE：payload.moves 包含多个组件的累计偏移；
      - 一个或多个旧版 MOVE：每个标注各自带 payload.move。
    """
    if not annotations:
        return False
    if len(annotations) == 1 and _explicit_batch_moves(annotations[0]) is not None:
        return True
    return all(
        _explicit_move_offset(annotation) is not None
        and bool(annotation_selector(annotation))
        for annotation in annotations
    )


class AgentGraphRunner:
    """构建并执行单 Agent 状态机。"""

    def __init__(
        self,
        sandbox: SandboxProvider,
        llm: AgentLLM,
        max_retries: int = 3,
        recursion_limit: int = 60,
        event_sink: EventSink | None = None,
        checkpointer_provider: CheckpointerProvider | None = None,
        cancel_check: Callable[[], bool] | None = None,
        rag_provider_factory: Callable[[str | None], RAGProvider] | None = None,
    ) -> None:
        # 硬限制：无论调用方传入什么，最多 3 次修复。
        self._max_retries = min(max(0, max_retries), 3)
        self._sandbox = sandbox
        self._llm = llm
        self._recursion_limit = recursion_limit
        self._event_sink = event_sink or NullEventSink()
        # 本次 runner 调用的 Token 汇总；最终以 Checkpointer 状态里的
        # token_usage 为准（可跨澄清 / 审批恢复累加）。
        self._usage_total = TokenUsage()
        self._checkpointer_provider = (
            checkpointer_provider or get_checkpointer_provider()
        )
        # 协作式取消：每个节点执行前后检查一次，无法打断正在进行的 LLM
        # 调用，但能保证写沙盒 / 构建之后不再继续推进。
        self._cancel_check = cancel_check

        # LangGraph 工具箱：本地 search_web / shell_tool 与 MCP 远端工具
        # 统一注册到 ToolRegistry。当前模型仍输出结构化 JSON，因此由
        # research 节点在计划前显式调用 search_web；ToolNode 保留给后续
        # tool-calling 模型，无需改业务代码即可挂载。
        #
        # MCP 配置只来自服务端 settings.mcp_servers，绝不接受浏览器输入；
        # 工具适配层见 ai_core/mcp/tools.py（stdio 子进程 + 白名单）。
        self._settings = get_settings()
        # Planner 的企业知识检索入口；测试可注入确定性 Provider。
        self._rag_provider_factory = rag_provider_factory or get_rag_provider
        self._toolbox: ToolRegistry = build_default_toolbox(self._settings)
        self._tool_node = ToolNode(self._toolbox.list_tools())
        self._graph = self._build()

    def run(
        self,
        initial_state: AgentState,
        *,
        thread_id: str | None = None,
        resume: Any | None = None,
    ) -> GraphRunOutcome:
        """执行或恢复一次 Run。

        ``resume`` 不为 None 时使用 ``Command(resume=...)``；此时
        ``initial_state`` 只用于兜底 thread_id，真实状态来自 checkpoint。
        """
        resolved_thread_id = (
            thread_id or str(initial_state.get("run_id") or "") or "run_unknown"
        )
        config = {
            "recursion_limit": self._recursion_limit,
            "configurable": {"thread_id": resolved_thread_id},
        }
        logger.info(
            "agent run start run_id=%s sandbox_id=%s resume=%s",
            initial_state.get("run_id"),
            initial_state.get("sandbox_id"),
            resume is not None,
        )
        if resume is None:
            self._graph.invoke(dict(initial_state), config=config)
        else:
            self._graph.invoke(Command(resume=resume), config=config)

        snapshot = self._graph.get_state(config)
        state = dict(snapshot.values or {})
        interrupt_payload = _find_interrupt(snapshot)
        usage = state.get("token_usage")
        outcome = GraphRunOutcome(
            state=state,  # type: ignore[arg-type]
            interrupted=bool(snapshot.next),
            interrupt=interrupt_payload,
            thread_id=resolved_thread_id,
            usage=dict(usage) if isinstance(usage, dict) else self.usage_total(),
        )
        logger.info(
            "agent run finish run_id=%s status=%s retry_count=%s interrupted=%s "
            "tokens=%s cost=%s %s",
            state.get("run_id"),
            state.get("status"),
            state.get("retry_count"),
            outcome.interrupted,
            outcome.usage.get("totalTokens"),
            outcome.usage.get("cost"),
            outcome.usage.get("currency"),
        )
        return outcome

    def usage_total(self) -> dict[str, Any]:
        """返回本 runner 调用的 Token 汇总（未合并历史 Checkpoint 时使用）。"""
        return self._usage_total.as_dict()

    # --------------------------------------------------------
    # 图构建
    # --------------------------------------------------------
    def _build(self):
        builder = StateGraph(AgentState)

        builder.add_node(
            "summarize_history",
            self._instrument(
                "summarize_history",
                NODE_LABELS["summarize_history"],
                self._summarize_history,
            ),
        )
        builder.add_node(
            "research",
            self._instrument("research", NODE_LABELS["research"], self._research),
        )
        builder.add_node(
            "planner",
            self._instrument("planner", NODE_LABELS["planner"], self._planner),
        )
        builder.add_node(
            "clarify_gate",
            self._instrument(
                "clarify_gate", NODE_LABELS["clarify_gate"], self._clarify_gate
            ),
        )
        builder.add_node(
            "style_check",
            self._instrument(
                "style_check", NODE_LABELS["style_check"], self._style_check
            ),
        )
        builder.add_node(
            "coder", self._instrument("coder", NODE_LABELS["coder"], self._coder)
        )
        builder.add_node(
            "applier",
            self._instrument("applier", NODE_LABELS["applier"], self._applier),
        )
        builder.add_node(
            "verifier",
            self._instrument("verifier", NODE_LABELS["verifier"], self._verifier),
        )
        builder.add_node(
            "evaluator",
            self._instrument("evaluator", NODE_LABELS["evaluator"], self._evaluator),
        )
        builder.add_node(
            "reflection",
            self._instrument("reflection", NODE_LABELS["reflection"], self._reflection),
        )
        builder.add_node(
            "approval_gate",
            self._instrument(
                "approval_gate", NODE_LABELS["approval_gate"], self._approval_gate
            ),
        )
        builder.add_node("awaiting_approval", self._awaiting_approval)
        builder.add_node(
            "succeeded",
            self._instrument("succeeded", NODE_LABELS["succeeded"], self._succeeded),
        )
        builder.add_node("rejected", self._rejected)
        builder.add_node("failed", self._failed)

        builder.add_edge(START, "summarize_history")
        builder.add_edge("summarize_history", "research")
        builder.add_edge("research", "planner")
        builder.add_edge("planner", "clarify_gate")
        builder.add_conditional_edges(
            "clarify_gate",
            self._route_after_clarify,
            {"planner": "planner", "style_check": "style_check", "failed": "failed"},
        )
        builder.add_conditional_edges(
            "style_check",
            self._route_after_style_check,
            {"coder": "coder", "failed": "failed"},
        )
        builder.add_conditional_edges(
            "coder",
            self._route_after_coder,
            {"applier": "applier", "evaluator": "evaluator", "failed": "failed"},
        )
        builder.add_conditional_edges(
            "applier",
            self._route_after_applier,
            {"verifier": "verifier", "evaluator": "evaluator"},
        )
        builder.add_edge("verifier", "evaluator")
        builder.add_conditional_edges(
            "evaluator",
            self._route_after_evaluator,
            {
                "coder": "coder",
                "reflection": "reflection",
                "failed": "failed",
            },
        )
        builder.add_conditional_edges(
            "reflection",
            self._route_after_reflection,
            {
                "approval_gate": "approval_gate",
                "succeeded": "succeeded",
            },
        )
        builder.add_conditional_edges(
            "approval_gate",
            self._route_after_approval,
            {
                "awaiting_approval": "awaiting_approval",
                "rejected": "rejected",
            },
        )
        builder.add_edge("awaiting_approval", END)
        builder.add_edge("succeeded", END)
        builder.add_edge("rejected", END)
        builder.add_edge("failed", END)
        return builder.compile(checkpointer=self._checkpointer_provider.get())

    def _instrument(
        self,
        name: str,
        label: str,
        fn: Callable[[AgentState], dict[str, Any]],
    ) -> Callable[[AgentState], dict[str, Any]]:
        """为节点统一增加耗时与 node.* 事件，避免每个节点重复代码。"""

        def wrapped(state: AgentState) -> dict[str, Any]:
            started = time.perf_counter()
            round_no = int(state.get("clarification_round", 0))
            attempt = int(state.get("retry_count", 0))
            key_base = f"{name}:{round_no}:{attempt}"
            # 进入节点前先响应取消，避免用户取消后仍启动新的模型调用 / 构建。
            self._check_cancelled(name, key_base, started)
            self._emit(
                "node.started",
                {
                    "node": name,
                    "label": label,
                    "attempt": attempt,
                    "round": round_no,
                },
                dedupe_key=f"node.started:{key_base}",
            )
            # 采集器只覆盖当前节点：模型适配器在每次真实调用后写入 usage，
            # 节点结束后聚合为 node.finished.usage / state.token_usage。
            collector = UsageCollector()
            # 模型流式输出：按节点聚合为低频 token 事件，前端据此做打字机。
            emitter: TokenStreamEmitter | None = None
            token_scope = nullcontext()
            if name in STREAMING_NODES:
                emitter = TokenStreamEmitter(
                    lambda text, task: self._emit(
                        "token",
                        {
                            "node": name,
                            "task": task,
                            "text": text,
                        },
                    )
                )
                token_scope = collect_tokens(emitter.feed)
            try:
                with token_scope, collect_usage(collector):
                    update = fn(state)
            except GraphInterrupt:
                if emitter is not None:
                    emitter.flush()
                    emitter.close()
                usage = collector.snapshot()
                self._usage_total.add(usage)
                self._emit(
                    "node.finished",
                    {
                        "node": name,
                        "status": "paused",
                        "durationMs": _duration_ms(started),
                        "summary": (
                            "等待人工审批"
                            if name == "approval_gate"
                            else "等待用户澄清"
                        ),
                        "usage": usage.as_dict(),
                    },
                    dedupe_key=f"node.finished:{key_base}:paused",
                )
                raise
            except Exception as exc:  # noqa: BLE001 - 事件后继续向上抛
                if emitter is not None:
                    emitter.flush()
                    emitter.close()
                usage = collector.snapshot()
                self._usage_total.add(usage)
                self._emit(
                    "node.finished",
                    {
                        "node": name,
                        "status": "error",
                        "durationMs": _duration_ms(started),
                        "summary": str(exc)[:300],
                        "usage": usage.as_dict(),
                    },
                    dedupe_key=f"node.finished:{key_base}:error",
                )
                raise

            if emitter is not None:
                emitter.flush()
                emitter.close()
            usage = collector.snapshot()
            self._usage_total.add(usage)
            # 节点执行期间用户可能已取消：返回值不写回状态，交给 Service
            # 统一回滚沙盒并发 run.cancelled。
            self._check_cancelled(name, key_base, started, usage=usage)

            status = "ok"
            if update.get("apply_failed"):
                status = "error"
            verify = update.get("verify_result")
            if (
                name == "verifier"
                and isinstance(verify, dict)
                and not verify.get("success", True)
            ):
                status = "error"
            self._emit(
                "node.finished",
                {
                    "node": name,
                    "status": status,
                    "durationMs": _duration_ms(started),
                    "summary": _node_summary(name, update, state),
                    "usage": usage.as_dict(),
                },
                dedupe_key=f"node.finished:{key_base}:{status}",
            )
            if isinstance(update, dict) and usage.calls:
                # 只写本节点增量；AgentState.token_usage 的 reducer 负责累加。
                update = {**update, "token_usage": usage.as_dict()}
            return update

        return wrapped

    def _check_cancelled(
        self,
        node: str,
        key_base: str,
        started: float,
        usage: TokenUsage | None = None,
    ) -> None:
        """在安全点检查协作式取消标志；已取消则发事件并抛出控制流异常。"""
        if self._cancel_check is None or not self._cancel_check():
            return
        payload: dict[str, Any] = {
            "node": node,
            "status": "cancelled",
            "durationMs": _duration_ms(started),
            "summary": "用户取消执行",
        }
        if usage is not None:
            payload["usage"] = usage.as_dict()
        self._emit(
            "node.finished",
            payload,
            dedupe_key=f"node.finished:{key_base}:cancelled",
        )
        raise RunCancelledError("用户取消执行")

    def _emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
    ) -> None:
        try:
            self._event_sink.emit(event_type, payload, dedupe_key=dedupe_key)
        except Exception:  # noqa: BLE001 - 事件失败不能影响 Agent 主流程
            logger.warning("failed to emit %s", event_type, exc_info=True)

    # --------------------------------------------------------
    # 节点
    # --------------------------------------------------------
    def _summarize_history(self, state: AgentState) -> dict[str, Any]:
        """在 Checkpointer 之前压缩历史。

        只有超过阈值才调用轻量模型，把每个标注的 chatHistory / previousRuns
        压成一条“核心需求与约束”。压缩失败时保留原上下文，绝不阻断 Run。
        """
        context = state.get("annotation_context") or {}
        result = compress_annotation_context(
            context,
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
                "annotation_context": result.value,
                "history_summary": result.summary,
                "messages": [
                    _message(
                        "summarize_history",
                        "历史已压缩："
                        f"{result.tokens_before} -> {result.tokens_after} tokens",
                    )
                ],
            }
        )
        self._emit(
            "node.progress",
            {
                "node": "summarize_history",
                "message": (
                    "对话历史超过阈值，已压缩为摘要："
                    f"{result.tokens_before} -> {result.tokens_after} tokens"
                ),
            },
            dedupe_key=f"node.progress:summarize_history:{state.get('run_id')}",
        )
        return update

    def _research(self, state: AgentState) -> dict[str, Any]:
        """在规划前按需调用 search_web，为模型补充外部事实。

        触发条件是用户指令里出现“搜索 / 查一下 / 最新版本 / 官网”等
        明确的外部资料诉求。没有触发词时直接跳过，不产生额外网络请求；
        搜索失败也只记录消息，绝不阻断主流程。
        """
        query = _extract_research_query(state.get("annotation_context") or {})
        if not query:
            return {}
        if not self._settings.tool_web_search_enabled:
            return {
                "research_query": query,
                "messages": [
                    _message(
                        "research",
                        "search_web 已禁用（TOOL_WEB_SEARCH_ENABLED=false）",
                    )
                ],
            }
        tool = self._toolbox.get("search_web")
        if tool is None:
            return {}

        self._emit(
            "node.progress",
            {
                "node": "research",
                "message": f"检索外部资料：{query}",
            },
            dedupe_key=f"node.progress:research:{query}",
        )
        try:
            output = tool.invoke(
                {
                    "query": query,
                    "max_results": self._settings.tool_web_search_max_results,
                }
            )
        except Exception as exc:  # noqa: BLE001 - 检索失败不阻断 Run
            logger.warning("search_web invocation failed: %s", exc)
            return {
                "research_query": query,
                "messages": [_message("research", f"外部检索失败: {exc}", error=True)],
            }
        text = str(output or "").strip()
        # 工具完整结果落盘到项目记忆目录；Prompt 只保留预览 + sha256，
        # 避免一次搜索把上下文窗口吃掉，也方便事后审计。
        try:
            memory = get_project_memory(
                self._settings,
                state.get("tenant_id"),
                state.get("project_id"),
                sandbox_id=state.get("sandbox_id"),
            )
            reference = memory.save_tool_output(
                "search_web",
                text,
                preview_chars=int(self._settings.tool_output_preview_chars),
            )
            research_context = reference.prompt_text()
        except Exception:  # noqa: BLE001 - 记忆落盘失败时退化为本地截断
            logger.warning("tool output memory failed", exc_info=True)
            research_context = (
                f"{text[: self._settings.tool_output_preview_chars]}\n...[已截断]"
            )
        return {
            "research_query": query,
            "research_context": research_context,
            "messages": [
                _message("research", f"搜索「{query}」完成：{research_context[:600]}")
            ],
        }

    def _retrieve_planner_knowledge(
        self,
        state: AgentState,
        annotations: list[dict[str, Any]],
    ) -> tuple[str, str, list[dict[str, Any]]]:
        """Planner 侧的企业知识检索。

        与 Architect 共用 ``HybridRAGProvider``（BM25 + Chroma + RRF），
        但召回数量更小，避免把长文档塞满计划 Prompt。RAG 失败只降级为
        无知识增强，绝不让一次代码修改整体失败。
        """
        provider_name = str(state.get("rag_provider") or "").strip()
        if not provider_name:
            return "", "", []
        query = _planner_rag_query(annotations, state.get("current_files") or {})
        if not query:
            return "", "", []
        top_k = max(
            1,
            int(state.get("rag_top_k") or self._settings.agent_rag_top_k),
        )
        try:
            provider = self._rag_provider_factory(provider_name)
            chunks = provider.retrieve(query[:800], top_k=top_k)
        except Exception as exc:  # noqa: BLE001 - RAG 是增强能力，必须降级
            logger.warning("planner rag retrieve failed: %s", exc)
            return query, "", []

        blocks: list[str] = []
        citations: list[dict[str, Any]] = []
        used_chars = 0
        for index, chunk in enumerate(chunks, start=1):
            content = chunk.content.strip()[:1200]
            if not content:
                continue
            if used_chars + len(content) > 3600:
                break
            used_chars += len(content)
            blocks.append(
                f"[{index}] 《{chunk.title}》 / {chunk.section}"
                f"（{chunk.path}，score={chunk.score:.4f}）\n{content}"
            )
            citation = chunk.to_citation()
            citation["retrievalSources"] = list(
                chunk.metadata.get("retrievalSources") or []
            )
            citations.append(citation)
        return query, "\n\n".join(blocks), citations

    def _planner(self, state: AgentState) -> dict[str, Any]:
        context = state["annotation_context"]
        answers = list(state.get("clarification_answers") or [])
        if answers:
            context = _augment_context_with_answers(context, answers)
            self._emit(
                "node.progress",
                {
                    "node": "planner",
                    "message": f"已合并 {len(answers)} 条澄清回答，重新规划",
                },
                dedupe_key=(
                    f"node.progress:planner:" f"{state.get('clarification_round', 0)}"
                ),
            )
        annotations = [item["annotation"] for item in context["annotations"]]
        # 只注入精准截断后的错误上下文，避免把完整构建日志塞回 Planner。
        previous_errors = _repair_feedback(state.get("verify_result") or {})
        # 内部知识库检索：Architect / Planner 统一走 HybridRAGProvider。
        # 纯拖拽是确定性几何操作，跳过 RAG，避免无意义的模型/索引延迟。
        rag_query = ""
        knowledge_context = ""
        rag_citations: list[dict[str, Any]] = []
        deterministic_layout = _is_explicit_move_batch(annotations) or (
            _is_explicit_layout_batch(annotations)
        )
        if not deterministic_layout:
            rag_query, knowledge_context, rag_citations = (
                self._retrieve_planner_knowledge(state, annotations)
            )
        if _is_explicit_layout_batch(annotations):
            # 视觉暂存统一提交：位移是几何事实，新增组件是用户明确意图，
            # 不需要规划模型。样式是否缺失交给后续 style_check 统一追问，
            # Planner 不在这里重复打断。
            plan = PlanResult(
                plan=(
                    "一次提交画布暂存变更：先用 move_component 的规则写入全部 "
                    "transform 位移，再为每个 additions 中的 CREATE_NEW 生成 "
                    "完整 JSX（带 data-vibe-created），合并为一份补丁。"
                ),
                confidence_score=0.99,
                decision_impact=MINOR,
                needs_clarification=False,
            )
        elif _is_explicit_move_batch(annotations):
            # 直接拖拽是确定性几何输入：不调用规划模型，避免把“拖了 12px”
            # 重新解释成自然语言需求，也避免任何澄清中断打断静默同步。
            plan = PlanResult(
                plan=(
                    "调用 move_component Tool，按 payload.move(s) 的累计 dx/dy "
                    "一次性写入所有 CSS transform 规则，合并为单份补丁；"
                    "禁止修改宽高与布局。"
                ),
                confidence_score=0.99,
                decision_impact=MINOR,
                needs_clarification=False,
            )
        else:
            plan = self._llm.plan(
                annotations=annotations,
                current_files=state["current_files"],
                previous_errors=previous_errors,
                clarification_answers=answers or None,
                knowledge_context=knowledge_context or None,
            )
        # 把 research 节点的外部检索结果并入计划，Coder 会从 state["plan"]
        # 读到它；要求模型引用时保留 URL，便于用户核查事实来源。
        plan_text = plan.plan
        research = (state.get("research_context") or "").strip()
        if research:
            plan_text = (
                f"{plan_text}\n\n外部检索资料（引用时注明来源 URL）：\n{research}"
            )
        if knowledge_context:
            # 内部知识库来自企业文档，优先级高于模型常识；Coder 从 plan
            # 读取并以引用编号为准，不允许改写来源路径。
            plan_text = (
                f"{plan_text}\n\n内部知识库检索结果（Hybrid RAG，按相关度排序）：\n"
                f"{knowledge_context}"
            )
        threshold = float(state.get("confidence_threshold", 0.7))
        # 分级决策：模型未输出 decision_impact 时，用确定性关键词兜底。
        # 只有 CRITICAL 才允许触发中断，MINOR 一律自主执行。
        impact = normalize_decision_impact(
            getattr(plan, "decision_impact", None)
        ) or infer_decision_impact(annotations)
        needs = should_interrupt(
            decision_impact=impact,
            needs_clarification=bool(plan.needs_clarification),
            confidence_score=float(plan.confidence_score),
            threshold=threshold,
            mode=str(state.get("clarification_mode", "auto")),
        )
        clarification = plan.clarification if needs else None
        if needs and clarification is None:
            clarification = _fallback_clarification(annotations)
        if clarification is not None:
            clarification = _ensure_clarification_annotation(clarification, annotations)
            # 统一以 Planner 的判定为准，防止模型在 payload 中写错影响面。
            clarification = {
                **clarification,
                "decisionImpact": impact,
                "decisionSummary": clarification.get("decisionSummary")
                or _decision_summary(annotations),
            }
        options = (clarification or {}).get("clarificationOptions") or []
        return {
            "plan": plan_text,
            "plan_confidence": float(plan.confidence_score),
            "plan_needs_clarification": needs,
            "plan_decision_impact": impact,
            "clarification_options": options,
            "clarification": clarification,
            "annotation_context": context,
            "rag_query": rag_query,
            "rag_context": knowledge_context,
            "rag_citations": rag_citations,
            "status": "PLANNING",
            "messages": [_message("planner", plan_text)],
        }

    def _clarify_gate(self, state: AgentState) -> dict[str, Any]:
        mode = state.get("clarification_mode", "auto")
        impact = normalize_decision_impact(state.get("plan_decision_impact")) or MINOR
        # 双保险：即使旧 checkpoint 残留 plan_needs_clarification=True，
        # 只要影响面不是核心业务，也绝不能在实现细节上打断用户。
        needs = (
            bool(state.get("plan_needs_clarification"))
            and mode != "off"
            and impact == CRITICAL
        )
        if not needs:
            return {
                "clarification_outcome": "proceed",
                "clarification": None,
                "status": "PLANNING",
            }

        round_no = int(state.get("clarification_round", 0))
        max_rounds = int(state.get("max_clarification_rounds", 3))
        if round_no >= max_rounds:
            error = f"连续 {max_rounds} 轮澄清后仍未获得可执行信息"
            return {
                "clarification_outcome": "exhausted",
                "status": "FAILED",
                "error_code": "CLARIFICATION_LIMIT",
                "error": error,
                "messages": [_message("clarify_gate", error, error=True)],
            }

        next_round = round_no + 1
        payload = _build_clarification_payload(state, next_round)
        # 暂停事件必须是本连接的最后一帧：先补 node.finished(paused)，
        # 再发 clarification.required；否则 Graph 外层包装器在 GraphInterrupt
        # 之后追加的 node.finished 会清除事件缓冲的 terminal 标记，导致带
        # afterSeq 重连的客户端永远等不到终态。
        attempt = int(state.get("retry_count", 0))
        self._emit(
            "node.finished",
            {
                "node": "clarify_gate",
                "status": "paused",
                "durationMs": 0,
                "summary": "等待用户澄清",
            },
            dedupe_key=(f"node.finished:clarify_gate:{round_no}:{attempt}:paused"),
        )
        self._emit(
            "clarification.required",
            payload,
            dedupe_key=f"clarification.required:{payload['clarificationId']}",
        )
        # interrupt 会暂停图并把 payload 交给前端；恢复时返回用户答案。
        answers = interrupt(payload)
        normalized_answers = _normalize_answers(answers)
        merged = list(state.get("clarification_answers") or []) + normalized_answers
        return {
            "clarification_answers": merged,
            "clarification_round": next_round,
            "clarification": None,
            "clarification_outcome": "replan",
            "status": "PLANNING",
            "messages": [
                _message(
                    "clarify_gate",
                    f"收到第 {next_round} 轮澄清回答，重新规划",
                )
            ],
        }

    def _style_check(self, state: AgentState) -> dict[str, Any]:
        """新增组件前的视觉样式门禁（HITL 强制追问）。

        只在“新增组件 + 用户没有说清样式”时打断：颜色、大小、圆角等
        视觉细节如果让模型自由发挥，最容易出现风格割裂。已有明确样式
        （例如“加一个红色按钮”“和登录按钮一样”）则直接放行，避免把
        每一次新增都变成问卷。
        """
        mode = str(state.get("clarification_mode", "auto"))
        targets = _style_targets(state.get("annotation_context") or {})
        if mode == "off" or not targets:
            return {"style_check_outcome": "skipped", "status": "PLANNING"}

        missing = [item for item in targets if not _has_style_hints(item["text"])]
        if not missing:
            return {"style_check_outcome": "skipped", "status": "PLANNING"}

        round_no = int(state.get("clarification_round", 0))
        max_rounds = int(state.get("max_clarification_rounds", 3))
        if round_no >= max_rounds:
            error = f"连续 {max_rounds} 轮澄清后仍未获得可执行的样式信息"
            return {
                "style_check_outcome": "exhausted",
                "status": "FAILED",
                "error_code": "STYLE_CLARIFICATION_LIMIT",
                "error": error,
                "messages": [_message("style_check", error, error=True)],
            }

        next_round = round_no + 1
        payload = _build_style_clarification_payload(state, missing, next_round)
        attempt = int(state.get("retry_count", 0))
        # 与 clarify_gate 相同：暂停帧必须先于 clarification.required，
        # 否则带 afterSeq 重连的客户端拿不到 terminal 标记。
        self._emit(
            "node.finished",
            {
                "node": "style_check",
                "status": "paused",
                "durationMs": 0,
                "summary": "等待用户确认视觉样式",
            },
            dedupe_key=(f"node.finished:style_check:{round_no}:{attempt}:paused"),
        )
        self._emit(
            "clarification.required",
            payload,
            dedupe_key=f"clarification.required:{payload['clarificationId']}",
        )
        answers = interrupt(payload)
        normalized_answers = _normalize_answers(answers)
        merged = list(state.get("clarification_answers") or []) + normalized_answers
        target_ids = {item["annotationId"] for item in missing}
        context = _augment_style_answers(
            state.get("annotation_context") or {},
            normalized_answers,
            target_ids,
        )
        style_text = "；".join(
            filter(None, (_answer_text(item) for item in normalized_answers))
        )
        plan = str(state.get("plan") or "")
        if style_text:
            plan = f"{plan}\n\n视觉样式约束（必须严格复用现有组件类名）：{style_text}"
        return {
            "annotation_context": context,
            "clarification_answers": merged,
            "clarification_round": next_round,
            "style_check_outcome": "proceed",
            "plan": plan,
            "status": "PLANNING",
            "messages": [
                _message(
                    "style_check",
                    f"收到第 {next_round} 轮样式回答，继续生成组件",
                )
            ],
        }

    def _try_move_tool(
        self,
        state: AgentState,
        annotations: list[dict[str, Any]],
        current_files: dict[str, str],
        retry_count: int,
    ) -> dict[str, Any] | None:
        """纯移动指令的确定性 Tool 短路。

        只有“所有标注都是纯移动 + 能拿到目标选择器”时才生效；任何一条
        不满足就返回 None，继续走原有 LLM 编码流程。调用的是 ToolRegistry
        中注册的标准 Tool `move_component`，不是提示词模拟。
        """
        if not annotations:
            return None
        # BATCH_MOVE 必须是唯一的输入信封；混入其他标注说明客户端协议
        # 异常，直接回退 LLM，避免只处理一部分组件造成布局错位。
        if len(annotations) > 1 and any(
            str(item.get("action") or "").upper() == "BATCH_MOVE"
            for item in annotations
        ):
            return None
        target_files = list(state.get("target_files") or [])
        if not target_files:
            return None
        tool = self._toolbox.get("move_component")
        if tool is None:
            return None
        rules: list[tuple[str, str, str]] = []
        for annotation in annotations:
            batch_moves = _explicit_batch_moves(annotation)
            if batch_moves is not None:
                # 批量布局：所有组件共用一次 Tool 循环，最终只调用一次
                # append_css_rule / 一次 _combined_diff，生成合并全量补丁。
                fallback_path = annotation_target_file(
                    annotation, current_files, target_files[0]
                )
                for move_index, move in enumerate(batch_moves):
                    selector = move["selector"]
                    offset = (move["dx"], move["dy"])
                    candidate = move["file"]
                    path = (
                        candidate
                        if candidate and candidate in current_files
                        else fallback_path
                    )
                    if path not in current_files:
                        return None
                    try:
                        raw = tool.invoke(
                            {
                                "element_id": selector,
                                "x": offset[0],
                                "y": offset[1],
                            }
                        )
                        payload = json.loads(raw) if isinstance(raw, str) else raw
                        rule = str(payload.get("rule") or "")
                    except Exception as exc:  # noqa: BLE001 - 失败则回退 LLM
                        logger.warning("move_component tool skipped: %s", exc)
                        return None
                    if not rule:
                        return None
                    rules.append((path, rule, selector))
                    self._emit(
                        "node.progress",
                        {
                            "node": "coder",
                            "message": (
                                f"批量布局 [{move_index + 1}/{len(batch_moves)}] "
                                f"move_component({selector}, "
                                f"{offset[0]:g}, {offset[1]:g})"
                            ),
                        },
                        dedupe_key=(
                            "node.progress:coder:batch-move:"
                            f"{annotation.get('annotationId')}:"
                            f"{move_index}:{retry_count}"
                        ),
                    )
                continue
            if str(annotation.get("action") or "").upper() == "BATCH_MOVE":
                # 声明了 BATCH_MOVE 但 payload 非法：不猜测、不执行单个
                # 移动，交给原有 LLM 流程处理。
                return None

            selector = annotation_selector(annotation)
            # 直接拖拽：payload.move 是前端算好的累计偏移，直接使用；
            # 自然语言移动仍走原来的解析 + 纯移动指令校验。
            offset = _explicit_move_offset(annotation)
            if offset is None:
                text = annotation_text(annotation)
                offset = detect_move_offset(text)
                if offset is None or not selector or not is_move_only_instruction(text):
                    return None
            elif not selector:
                return None
            path = annotation_target_file(annotation, current_files, target_files[0])
            if path not in current_files:
                return None
            try:
                raw = tool.invoke(
                    {
                        "element_id": selector,
                        "x": offset[0],
                        "y": offset[1],
                    }
                )
                payload = json.loads(raw) if isinstance(raw, str) else raw
                rule = str(payload.get("rule") or "")
            except Exception as exc:  # noqa: BLE001 - 失败则回退 LLM
                logger.warning("move_component tool skipped: %s", exc)
                return None
            if not rule:
                return None
            rules.append((path, rule, selector))
            self._emit(
                "node.progress",
                {
                    "node": "coder",
                    "message": (
                        f"调用 Tool move_component({selector}, "
                        f"{offset[0]:g}, {offset[1]:g})"
                    ),
                },
                dedupe_key=(
                    f"node.progress:coder:move:"
                    f"{annotation.get('annotationId')}:{retry_count}"
                ),
            )

        updated = dict(current_files)
        for path, rule, _selector in rules:
            updated[path] = append_css_rule(updated[path], rule, path)
        changed_paths = sorted({path for path, _rule, _selector in rules})
        generated_files = {path: updated[path] for path in changed_paths}
        patch = _combined_diff(current_files, generated_files)
        round_no = int(state.get("clarification_round", 0))
        patch_id = f"pat_{state.get('run_id')}_{round_no}_{retry_count}"
        selector_labels = ", ".join(selector for _p, _r, selector in rules)
        self._emit(
            "patch.proposed",
            {
                "patchId": patch_id,
                "files": changed_paths,
                "insertions": sum(
                    1
                    for path in changed_paths
                    for line in generated_files[path].splitlines()
                    if "transform:translate3d" in line.replace(" ", "")
                ),
                "deletions": 0,
                "summary": f"通过 move_component 移动 {selector_labels}",
                "diffPreview": _tail(patch, 2000) if patch else "",
            },
            dedupe_key=f"patch.proposed:{patch_id}",
        )
        return {
            "generated_patch": patch,
            "generated_files": generated_files,
            "patch_id": patch_id,
            "apply_failed": False,
            "status": "CODING",
            "messages": [
                _message(
                    "coder",
                    f"Tool move_component 完成 {len(rules)} 次位移；"
                    "仅写入 transform，未修改宽高。",
                )
            ],
        }

    def _coder_layout_batch(
        self,
        state: AgentState,
        layout: dict[str, Any],
        current_files: dict[str, str],
        verify_result: dict[str, Any],
        retry_count: int,
    ) -> dict[str, Any]:
        """BATCH_LAYOUT 的统一编码：位移确定性写入 + 新增组件一次生成。

        顺序很重要：先让模型基于**原始文件**生成新增组件的 diff，再用
        Python 侧的最小补丁应用器合并到工作副本，最后追加位移 CSS。
        这样模型补丁的上下文始终与当前沙盒一致，不会因为提前插入 CSS
        规则而 hunk 匹配失败。
        """
        target_files = list(state.get("target_files") or [])
        if not target_files:
            return self._coder_failure_result(
                code="CODER_INTERNAL_ERROR",
                message="布局批次缺少目标文件",
            )
        additions = layout.get("additions") or []
        moves = layout.get("moves") or []
        updated = dict(current_files)
        changed: set[str] = set()
        summaries: list[str] = []

        if additions:
            synthetic = [item["annotation"] for item in additions]
            timeout_seconds = float(
                state.get("coder_timeout_seconds")
                or getattr(self._settings, "agent_coder_timeout_seconds", 30.0)
            )
            generate_kwargs = {
                "annotations": synthetic,
                "plan": state.get("plan", ""),
                "current_files": current_files,
                "target_files": target_files,
                "verify_result": _repair_payload(verify_result),
                "retry_count": retry_count,
            }
            self._emit(
                "node.progress",
                {
                    "node": "coder",
                    "message": f"批量生成 {len(additions)} 个新增组件的 JSX",
                },
                dedupe_key=f"node.progress:coder:layout-add:{retry_count}",
            )
            try:
                result = self._generate_with_coder_timeout(
                    **generate_kwargs,
                    compact=False,
                    prefer_fast=False,
                    timeout_seconds=timeout_seconds,
                )
            except CoderTimeoutError:
                try:
                    result = self._generate_with_coder_timeout(
                        **generate_kwargs,
                        compact=True,
                        prefer_fast=True,
                        timeout_seconds=float(
                            getattr(
                                self._settings,
                                "agent_coder_retry_timeout_seconds",
                                20.0,
                            )
                        ),
                    )
                except (CoderTimeoutError, AgentLLMError) as exc:
                    logger.warning("layout coder fallback failed: %s", exc)
                    return self._coder_failure_result(
                        code="CODER_TIMEOUT_RECOVERABLE",
                        message=CODER_BACKGROUND_AVAILABLE_MESSAGE,
                        background_available=True,
                    )
            except AgentLLMError as exc:
                detail = str(exc).strip()
                return self._coder_failure_result(
                    code="CODER_GENERATION_FAILED",
                    message=(
                        f"{CODER_GENERATION_FAILED_MESSAGE}（{detail[:200]}）"
                        if detail
                        else CODER_GENERATION_FAILED_MESSAGE
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("layout coder crashed")
                return self._coder_failure_result(
                    code="CODER_INTERNAL_ERROR",
                    message=f"Coder 生成新增组件失败: {exc}",
                )

            generated = _materialize_generated_files(current_files, result.files)
            if not generated and result.patch:
                applied = _apply_unified_patch(current_files, result.patch)
                if applied is None:
                    return self._coder_failure_result(
                        code="CODER_PATCH_UNPARSEABLE",
                        message="新增组件补丁无法合并，已保留画布草稿，请重试",
                    )
                generated = {
                    path: content
                    for path, content in applied.items()
                    if content != current_files.get(path)
                }
            if not generated:
                return self._coder_failure_result(
                    code="CODER_GENERATION_FAILED",
                    message="模型没有生成任何新增组件代码，画布草稿已保留",
                )
            updated.update(generated)
            changed.update(generated)
            summaries.append(result.summary or f"新增 {len(additions)} 个组件")

        if moves:
            tool = self._toolbox.get("move_component")
            if tool is None:
                return self._coder_failure_result(
                    code="CODER_INTERNAL_ERROR",
                    message="move_component Tool 不可用，布局草稿已保留",
                )
            for move_index, move in enumerate(moves):
                selector = move["selector"]
                offset = (move["dx"], move["dy"])
                fallback_path = annotation_target_file(
                    state["annotation_context"]["annotations"][0]["annotation"],
                    current_files,
                    target_files[0],
                )
                candidate = move.get("file") or ""
                path = (
                    candidate if candidate and candidate in updated else fallback_path
                )
                if path not in updated:
                    return self._coder_failure_result(
                        code="CODER_INTERNAL_ERROR",
                        message=f"位移目标文件不存在: {path}，布局草稿已保留",
                    )
                try:
                    raw = tool.invoke(
                        {
                            "element_id": selector,
                            "x": offset[0],
                            "y": offset[1],
                        }
                    )
                    payload = json.loads(raw) if isinstance(raw, str) else raw
                    rule = str(payload.get("rule") or "")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("layout move tool failed: %s", exc)
                    return self._coder_failure_result(
                        code="CODER_INTERNAL_ERROR",
                        message=f"位移规则生成失败: {exc}，布局草稿已保留",
                    )
                if not rule:
                    return self._coder_failure_result(
                        code="CODER_INTERNAL_ERROR",
                        message=f"位移规则为空: {selector}，布局草稿已保留",
                    )
                updated[path] = append_css_rule(updated[path], rule, path)
                changed.add(path)
                self._emit(
                    "node.progress",
                    {
                        "node": "coder",
                        "message": (
                            f"批量布局位移 [{move_index + 1}/{len(moves)}] "
                            f"move_component({selector}, {offset[0]:g}, {offset[1]:g})"
                        ),
                    },
                    dedupe_key=(
                        f"node.progress:coder:layout-move:{move_index}:{retry_count}"
                    ),
                )
            summaries.append(f"调整 {len(moves)} 个组件位置")

        generated_files = {
            path: updated[path]
            for path in sorted(changed)
            if path in updated and updated[path] != current_files.get(path)
        }
        if not generated_files:
            return self._coder_failure_result(
                code="CODER_GENERATION_FAILED",
                message="画布变更没有产生任何代码差异，草稿已保留",
            )
        patch = _combined_diff(current_files, generated_files)
        round_no = int(state.get("clarification_round", 0))
        patch_id = f"pat_{state.get('run_id')}_{round_no}_{retry_count}"
        insertions, deletions = _diff_stat(patch)
        summary = "；".join(summaries) or "统一提交画布变更"
        self._emit(
            "patch.proposed",
            {
                "patchId": patch_id,
                "files": sorted(generated_files),
                "insertions": insertions,
                "deletions": deletions,
                "summary": summary,
                "diffPreview": _tail(patch, 2000) if patch else "",
            },
            dedupe_key=f"patch.proposed:{patch_id}",
        )
        return {
            "generated_patch": patch,
            "generated_files": generated_files,
            "patch_id": patch_id,
            "apply_failed": False,
            "status": "CODING",
            "messages": [_message("coder", summary)],
        }

    def _generate_with_coder_timeout(
        self,
        *args: Any,
        timeout_seconds: float | None = None,
        **kwargs: Any,
    ) -> CodeResult:
        """在硬超时内执行 Coder 模型调用。

        ``ThreadPoolExecutor`` 无法强杀阻塞的 HTTP 请求，但能保证 Graph
        立即收到超时并进入降级 / 后台分支；后台线程的迟到结果被丢弃。用
        ``copy_context()`` 把当前节点的 TokenSink 带进线程，流式输出不丢。

        ``timeout_seconds`` 为空时读取服务端默认（普通 30s / 后台 240s）；
        降级重试会显式传入更短预算，确保用户尽快得到反馈。
        """
        timeout = max(
            1.0,
            float(
                timeout_seconds
                if timeout_seconds is not None
                else getattr(self._settings, "agent_coder_timeout_seconds", 30.0)
            ),
        )
        context = contextvars.copy_context()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="coder")
        future = executor.submit(context.run, self._llm.generate, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as exc:
            future.cancel()
            logger.warning("coder timeout after %.1fs", timeout)
            raise CoderTimeoutError(CODER_TIMEOUT_MESSAGE) from exc
        finally:
            # 绝不能 wait=True：超时的 HTTP 调用仍可能在后台阻塞。
            executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _coder_failure_result(
        *,
        code: str,
        message: str,
        background_available: bool = False,
    ) -> dict[str, Any]:
        """构造 Coder 失败增量。

        ``CODER_TIMEOUT_RECOVERABLE`` 表示本次失败可以通过“转后台运行”
        继续；其余错误仍按不可恢复处理，避免把格式错误无限重试。
        """
        return {
            "generated_patch": "",
            "generated_files": {},
            "apply_failed": True,
            "fatal_error": True,
            "next_action": "fail",
            "status": "FAILED",
            "error_code": code,
            "error": message,
            "background_available": background_available,
            "verify_result": _failure(message),
            "messages": [_message("coder", message, error=True)],
        }

    def _coder(self, state: AgentState) -> dict[str, Any]:
        context = state["annotation_context"]
        annotations = [item["annotation"] for item in context["annotations"]]
        current_files = state["current_files"]
        verify_result = state.get("verify_result") or {}
        retry_count = int(state.get("retry_count", 0))

        self._emit(
            "node.progress",
            {
                "node": "coder",
                "message": f"调用模型生成 {len(annotations)} 个标注的修改",
            },
            dedupe_key=f"node.progress:coder:{retry_count}",
        )
        # 视觉暂存统一提交：位移 + 新增组件必须一次落盘，避免用户每画
        # 一个组件就触发一次 Coder / HMR。
        if len(annotations) == 1:
            layout = _explicit_layout_batch(annotations[0])
            if layout is not None:
                return self._coder_layout_batch(
                    state, layout, current_files, verify_result, retry_count
                )
        # 纯移动请求走标准 Tool，而不是让模型自由写 CSS：
        # 1) 位移规则由 move_component 统一生成（只允许 transform）；
        # 2) 杜绝模型改 width / height / left / top 造成“按钮瞬间变长”；
        # 3) 规则可测试、可审计，和 Mock Coder 完全一致。
        move_result = self._try_move_tool(
            state, annotations, current_files, retry_count
        )
        if move_result is not None:
            return move_result
        # 普通请求 30s 熔断；用户显式选择“转后台运行”时使用 240s。
        timeout_seconds = float(
            state.get("coder_timeout_seconds")
            or getattr(self._settings, "agent_coder_timeout_seconds", 30.0)
        )
        background = bool(state.get("background_run"))
        generate_kwargs = {
            "annotations": annotations,
            "plan": state.get("plan", ""),
            "current_files": current_files,
            "target_files": state["target_files"],
            # 只把 Error Message + 报错行附近代码交给模型，
            # 完整 stdout/stderr 仅保留在状态里供 UI 与审计使用。
            "verify_result": _repair_payload(verify_result),
            "retry_count": retry_count,
        }
        fallback_used = False
        try:
            result = self._generate_with_coder_timeout(
                **generate_kwargs,
                compact=False,
                prefer_fast=False,
                timeout_seconds=timeout_seconds,
            )
        except CoderTimeoutError as exc:
            logger.warning("coder timeout: %s", exc)
            if background:
                return self._coder_failure_result(
                    code="CODER_TIMEOUT_RECOVERABLE",
                    message=CODER_BACKGROUND_AVAILABLE_MESSAGE,
                    background_available=True,
                )
            # 第一次超时先自救：压缩到目标节点窗口 + FAST 模型 + 更短预算。
            # 这能覆盖大多数“标注范围略大 / 强模型偶发卡顿”的情况。
            self._emit(
                "node.progress",
                {"node": "coder", "message": CODER_FALLBACK_PROGRESS},
                dedupe_key=f"node.progress:coder:fallback:{retry_count}",
            )
            try:
                result = self._generate_with_coder_timeout(
                    **generate_kwargs,
                    compact=True,
                    prefer_fast=True,
                    timeout_seconds=float(
                        getattr(
                            self._settings,
                            "agent_coder_retry_timeout_seconds",
                            20.0,
                        )
                    ),
                )
                fallback_used = True
            except (CoderTimeoutError, AgentLLMError) as retry_exc:
                # 降级仍失败：保留可恢复出口，前端可一键转后台，而不是
                # 把整轮需求直接判死。草稿由前端自行保留。
                logger.warning("coder fallback failed: %s", retry_exc)
                return self._coder_failure_result(
                    code="CODER_TIMEOUT_RECOVERABLE",
                    message=CODER_BACKGROUND_AVAILABLE_MESSAGE,
                    background_available=True,
                )
        except AgentLLMError as exc:
            # 结构化输出连续失败：不做后台重试（换更长超时也无济于事），
            # 让用户缩小标注范围或重新表述。
            logger.warning("coder failed without repair loop: %s", exc)
            # 透出具体校验原因（如“缺少 data-vibe-created 标记”），
            # 不能把“模型只改了 import”误报成超时。
            detail = str(exc).strip()
            return self._coder_failure_result(
                code="CODER_GENERATION_FAILED",
                message=(
                    f"{CODER_GENERATION_FAILED_MESSAGE}（{detail[:200]}）"
                    if detail
                    else CODER_GENERATION_FAILED_MESSAGE
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 未预期异常同样不能再阻塞
            logger.exception("coder crashed")
            return self._coder_failure_result(
                code="CODER_INTERNAL_ERROR",
                message=f"Coder 生成失败: {exc}",
            )

        # files 模式必须经过 FileCreator 落地：已有文件 REWRITE_FILE，
        # 新文件 CREATE_FILE。路径校验 / 覆盖保护由 CodeEditor 负责。
        generated_files = _materialize_generated_files(current_files, result.files)

        # files 模式：用修改前后的完整内容合成展示用 diff（一次性展示全部改动）。
        patch = result.patch or ""
        if not patch and generated_files:
            patch = _combined_diff(current_files, generated_files)

        changed_files = sorted(generated_files) or _diff_paths(patch)
        round_no = int(state.get("clarification_round", 0))
        patch_id = f"pat_{state.get('run_id')}_{round_no}_{retry_count}"
        insertions, deletions = _diff_stat(patch)
        self._emit(
            "patch.proposed",
            {
                "patchId": patch_id,
                "files": changed_files,
                "insertions": insertions,
                "deletions": deletions,
                "summary": result.summary,
                "diffPreview": _tail(patch, 2000) if patch else "",
            },
            dedupe_key=f"patch.proposed:{patch_id}",
        )
        return {
            "generated_patch": patch,
            "generated_files": generated_files,
            "patch_id": patch_id,
            "apply_failed": False,
            "status": "CODING",
            # 后台 / 降级状态透传给 UI：前端可展示“已精简上下文重试成功”。
            "coder_fallback_used": fallback_used,
            "background_available": False,
            "error_code": None,
            "messages": [
                _message(
                    "coder",
                    f"{result.summary}\n"
                    f"mode={'diff' if result.patch else 'files'} "
                    f"annotations={len(annotations)} changed={changed_files}",
                )
            ],
        }

    def _applier(self, state: AgentState) -> dict[str, Any]:
        sandbox_id = state["sandbox_id"]
        files = state.get("generated_files") or {}
        patch = state.get("generated_patch") or None

        try:
            # 全量文件与 unified diff 只能二选一：
            # generated_patch 在 files 模式下只是用于展示/审计的 diff，
            # 若同时应用，会对已更新的文件再次打补丁而失败。
            # 批量标注必须一次写入，避免多次 HMR / 构建。
            if files:
                changed = self._sandbox.apply_patch(sandbox_id=sandbox_id, files=files)
            elif patch:
                changed = self._sandbox.apply_patch(sandbox_id=sandbox_id, patch=patch)
            else:
                changed = []

            # 以沙盒真实文件为准更新全部目标文件；diff 模式可能修改额外文件。
            paths = set(state["target_files"])
            paths.update(files.keys())
            paths.update(_diff_paths(patch or ""))
            current_files = dict(state.get("current_files") or {})
            for path in sorted(paths):
                try:
                    current_files[path] = self._sandbox.read_file(sandbox_id, path)
                except SandboxError:
                    # 删除的文件或模型误报路径：不阻塞本次执行，构建阶段会暴露问题。
                    logger.warning("failed to reread %s after apply", path)
            current_code = current_files.get(state["target_file"], "")
        except SandboxError as exc:
            logger.warning("applier failed: %s", exc)
            return {
                "apply_failed": True,
                "verify_result": _failure(f"应用补丁失败: {exc}"),
                "status": "APPLYING",
                "messages": [_message("applier", str(exc), error=True)],
            }

        patch_id = str(state.get("patch_id") or "")
        if patch_id:
            self._emit(
                "patch.applied",
                {"patchId": patch_id, "changedFiles": changed},
                dedupe_key=f"patch.applied:{patch_id}",
            )
        return {
            "current_code": current_code,
            "current_files": current_files,
            "apply_failed": False,
            "status": "APPLYING",
            "messages": [_message("applier", f"已一次写入沙盒，变更文件: {changed}")],
        }

    def _verifier(self, state: AgentState) -> dict[str, Any]:
        round_no = int(state.get("clarification_round", 0))
        retry_count = int(state.get("retry_count", 0))
        key = f"{state.get('run_id')}:{round_no}:{retry_count}"
        self._emit(
            "verify.started",
            {"checks": ["npm run build", "created-markers"]},
            dedupe_key=f"verify.started:{key}",
        )
        try:
            result = self._sandbox.verify(state["sandbox_id"], check="build")
        except SandboxError as exc:
            logger.warning("verifier execution failed: %s", exc)
            self._emit(
                "verify.failed",
                {
                    "exitCode": -1,
                    "durationMs": 0,
                    "errorSummary": str(exc)[:1000],
                    "fingerprint": "",
                },
                dedupe_key=f"verify.failed:{key}",
            )
            return {
                "apply_failed": False,
                "verify_result": _failure(f"执行构建失败: {exc}"),
                "status": "VERIFYING",
                "messages": [_message("verifier", f"构建执行失败: {exc}", error=True)],
            }

        verify_state = {
            "success": result.success,
            "error_logs": "" if result.success else result.logs,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "error_context": "",
            "error_fingerprint": "",
        }
        # 构建通过 ≠ 需求完成：真实事故中 Coder 只新增了一行 import，
        # Vite 构建成功，前端却宣告“已添加按钮”。这里回读沙盒文件，
        # 校验每个 CREATE_NEW 标注的 data-vibe-created 标记是否真实落地。
        missing_markers = self._missing_created_markers(state) if result.success else []
        if result.success and missing_markers:
            detail = "；".join(
                f'{item["annotationId"]} 未出现在 {item["file"]}'
                for item in missing_markers
            )
            verify_state["success"] = False
            verify_state["error_logs"] = (
                f"npm run build 通过，但新增组件未写入文件: {detail}"
            )
            verify_state["error_context"] = verify_state["error_logs"]
            self._emit(
                "verify.failed",
                {
                    "exitCode": result.exit_code,
                    "durationMs": result.duration_ms,
                    "errorSummary": verify_state["error_context"][:1000],
                    "fingerprint": "",
                },
                dedupe_key=f"verify.failed:{key}",
            )
        elif result.success:
            self._emit(
                "verify.passed",
                {"exitCode": result.exit_code, "durationMs": result.duration_ms},
                dedupe_key=f"verify.passed:{key}",
            )
        else:
            # 精准截断：提取报错行号 + Error Message + 前后 N 行代码。
            error_context = extract_error_context(
                result.logs,
                _select_error_source(result.logs, state),
                context_lines=int(self._settings.error_context_lines),
                max_chars=int(self._settings.error_context_max_chars),
            )
            fingerprint = extract_error_fingerprint(result.logs)
            verify_state["error_context"] = error_context
            verify_state["error_fingerprint"] = fingerprint
            self._emit(
                "verify.failed",
                {
                    "exitCode": result.exit_code,
                    "durationMs": result.duration_ms,
                    "errorSummary": error_context[:1000],
                    "fingerprint": fingerprint,
                },
                dedupe_key=f"verify.failed:{key}",
            )
        if result.success and missing_markers:
            summary = "构建通过，但新增组件未写入文件: " + ", ".join(
                item["annotationId"] for item in missing_markers
            )
        elif result.success:
            summary = f"npm run build 成功，用时 {result.duration_ms}ms"
        else:
            summary = (
                f"npm run build 失败 (exit={result.exit_code})，"
                f"用时 {result.duration_ms}ms"
            )
        return {
            "apply_failed": False,
            "verify_result": verify_state,
            "status": "VERIFYING",
            "messages": [
                _message("verifier", summary, error=not verify_state["success"])
            ],
        }

    def _missing_created_markers(self, state: AgentState) -> list[dict[str, str]]:
        """回读沙盒文件，找出没有真正落地的 CREATE_NEW 标注。

        Coder 的解析层已经拦截了“只改 import”的补丁，但 Applier 仍可能
        因为 hunk 偏移、文件写错等原因丢掉新增节点。Verifier 是最后一道
        防线：构建成功但标记不存在时，必须判失败并让 Coder 带上上下文重试。
        """
        context = state.get("annotation_context") or {}
        target_files = [str(path) for path in (state.get("target_files") or [])]
        missing: list[dict[str, str]] = []
        pending: list[dict[str, Any]] = []
        for item in context.get("annotations") or []:
            if not isinstance(item, dict):
                continue
            annotation = item.get("annotation")
            if not isinstance(annotation, dict):
                continue
            layout = _explicit_layout_batch(annotation)
            if layout is not None:
                # BATCH_LAYOUT 的新增组件藏在 payload.additions 里，必须
                # 逐个校验真实标记，否则构建通过仍可能“假成功”。
                pending.extend(
                    entry["annotation"] for entry in layout.get("additions") or []
                )
                continue
            pending.append(annotation)

        for annotation in pending:
            action = (
                annotation.get("action")
                or annotation.get("intentAction")
                or annotation.get("intent_action")
            )
            if not is_create_action(action):
                continue
            marker = annotation.get("annotationId") or annotation.get("annotation_id")
            if not marker:
                continue
            target = annotation.get("target")
            target = target if isinstance(target, dict) else {}
            source = target.get("source")
            source = source if isinstance(source, dict) else {}
            path = str(
                source.get("file")
                or annotation.get("targetFile")
                or annotation.get("target_file")
                or ""
            ).strip()
            if not path:
                path = target_files[0] if target_files else ""
            if not path:
                missing.append({"annotationId": str(marker), "file": "未知文件"})
                continue
            try:
                content = self._sandbox.read_file(state["sandbox_id"], path)
            except SandboxError as exc:
                # 读取失败无法证明“没生成”，宁可跳过也不误杀；构建日志
                # 仍保留在 verify_result 中供人工排查。
                logger.warning(
                    "verifier 读取标注目标文件失败 marker=%s path=%s: %s",
                    marker,
                    path,
                    exc,
                )
                continue
            marker = str(marker)
            if (
                f'data-vibe-created="{marker}"' not in content
                and f"data-vibe-created='{marker}'" not in content
            ):
                missing.append({"annotationId": marker, "file": path})
        return missing

    def _evaluator(self, state: AgentState) -> dict[str, Any]:
        verify = state.get("verify_result") or {}
        retry_count = int(state.get("retry_count", 0))
        max_retries = int(state.get("max_retries", self._max_retries))

        if verify.get("success"):
            # 构建通过。是否暂停完全由调用方开关决定：
            #   true  -> approval_gate 等待逐文件审批；
            #   false -> 直接结束图，由 Service 提交 Git 并发送 run.succeeded。
            # 重试耗尽后的 FAILED 分支不受该开关影响。
            if state.get("approval_required"):
                return {
                    "next_action": "approve",
                    "status": "AWAITING_APPROVAL",
                    "error": None,
                    "messages": [
                        _message(
                            "evaluator",
                            f"验证通过（重试 {retry_count} 次），" "等待人工审批",
                        )
                    ],
                }
            return {
                "next_action": "succeed",
                "status": "SUCCEEDED",
                "error": None,
                "messages": [
                    _message(
                        "evaluator",
                        f"验证通过（重试 {retry_count} 次），"
                        "未开启人工审批，直接完成",
                    )
                ],
            }

        error_logs = str(verify.get("error_logs") or "未知构建错误")
        error_context = _repair_feedback(verify) or _tail(error_logs, 1500)
        fingerprint = str(verify.get("error_fingerprint") or "")
        last_fingerprint = str(state.get("last_repair_error_fingerprint") or "")

        # 同一错误已经修过一次仍然复现：继续重试只会重复烧钱，直接终止。
        if retry_count >= 1 and fingerprint and fingerprint == last_fingerprint:
            logger.warning("repeated error %s after repair, aborting", fingerprint)
            self._emit(
                "repair.aborted",
                {
                    "attempt": retry_count,
                    "fingerprint": fingerprint,
                    "reason": "连续两次自愈失败且错误相同",
                },
                dedupe_key=(
                    f"repair.aborted:{state.get('run_id')}:{retry_count}:{fingerprint}"
                ),
            )
            return {
                "next_action": "fail",
                "status": "FAILED",
                "error_code": "REPEATED_ERROR",
                "error": "连续两次自愈失败且错误相同，已终止以避免无效重试",
                "messages": [
                    _message(
                        "evaluator",
                        "检测到与上次修复相同的错误，终止重试。"
                        f"错误指纹: {fingerprint}",
                        error=True,
                    )
                ],
            }

        if retry_count < max_retries:
            logger.info(
                "verification failed, scheduling repair %s/%s",
                retry_count + 1,
                max_retries,
            )
            self._emit(
                "repair.started",
                {
                    "attempt": retry_count + 1,
                    "maxAttempts": max_retries,
                    "reason": error_context[:1000],
                },
                dedupe_key=(f"repair.started:{state.get('run_id')}:{retry_count + 1}"),
            )
            return {
                "retry_count": retry_count + 1,
                # 记录本次错误指纹，下一轮若原样复现则终止。
                "last_repair_error_fingerprint": fingerprint,
                "next_action": "retry",
                "status": "REPAIRING",
                "messages": [
                    _message(
                        "evaluator",
                        f"构建失败，开始第 {retry_count + 1}/{max_retries} 次修复。"
                        f"错误摘要:\n{error_context}",
                        error=True,
                    )
                ],
            }

        return {
            "next_action": "fail",
            "status": "FAILED",
            "error_code": "VERIFY_FAILED",
            "error": f"连续 {max_retries} 次修复后构建仍失败",
            "messages": [
                _message(
                    "evaluator",
                    f"重试预算耗尽（{max_retries} 次），标记 FAILED",
                    error=True,
                )
            ],
        }

    def _reflection(self, state: AgentState) -> dict[str, Any]:
        """成功路径上的确定性反思：只提取证据，不调用模型。

        这里不直接写偏好账本：Service 只有在确认本次运行 SUCCEEDED 后
        才落盘，失败 / 拒绝 / 取消的运行不会污染长期偏好。
        """
        annotations = [
            item["annotation"]
            for item in (state.get("annotation_context") or {}).get("annotations", [])
        ]
        observations = extract_preference_observations(
            annotations, run_id=str(state.get("run_id") or "")
        )
        if not observations:
            return {"reflection_preferences": []}
        labels = "、".join(
            f"{item.get('key')}={item.get('value')}" for item in observations
        )
        return {
            "reflection_preferences": observations,
            "messages": [_message("reflection", f"记录到实现偏好修改：{labels}")],
        }

    def _approval_gate(self, state: AgentState) -> dict[str, Any]:
        """构建通过后暂停，等待用户逐文件查看 Diff 并批准 / 拒绝。

        与澄清相同的终态帧规则：先补 ``node.finished(paused)``，再发
        ``approval.required``，保证暂停事件是该连接最后一帧；用户决策后
        通过 ``Command(resume=...)`` 恢复同一状态机。
        """
        round_no = int(state.get("clarification_round", 0))
        attempt = int(state.get("retry_count", 0))
        key_base = f"approval_gate:{round_no}:{attempt}"
        payload = _build_approval_payload(state)
        self._emit(
            "node.finished",
            {
                "node": "approval_gate",
                "status": "paused",
                "durationMs": 0,
                "summary": "等待人工审批",
            },
            dedupe_key=f"node.finished:{key_base}:paused",
        )
        self._emit(
            "approval.required",
            payload,
            dedupe_key=f"approval.required:{payload['approvalId']}",
        )
        decision = interrupt(payload)
        normalized = _normalize_approval_decision(decision)
        if normalized == "reject":
            comment = _approval_comment(decision)
            return {
                "approval_decision": "reject",
                "approval_id": payload["approvalId"],
                "approval": None,
                "approval_comment": comment,
                "status": "REJECTED",
                "messages": [
                    _message(
                        "approval_gate",
                        f"用户拒绝了本次修改{f'：{comment}' if comment else ''}",
                    )
                ],
            }
        return {
            "approval_decision": "approve",
            "approval_id": payload["approvalId"],
            "approval": None,
            "approval_comment": _approval_comment(decision),
            "status": "AWAITING_APPROVAL",
            "messages": [_message("approval_gate", "用户已批准本次修改")],
        }

    def _awaiting_approval(self, state: AgentState) -> dict[str, Any]:
        if state.get("approval_required"):
            content = "用户已批准，正在写入 Git checkpoint"
        else:
            content = "代码已通过构建验证，进入结果确认"
        return {
            "status": "AWAITING_APPROVAL",
            "messages": [_message("awaiting_approval", content)],
        }

    def _succeeded(self, state: AgentState) -> dict[str, Any]:
        """免审批成功终态。

        Git checkpoint 与 ``run.succeeded`` 由 Service 统一提交 / 发送；
        这里只负责把最终状态收敛为 SUCCEEDED。
        """
        return {
            "status": "SUCCEEDED",
            "error": None,
            "error_code": None,
        }

    def _rejected(self, state: AgentState) -> dict[str, Any]:
        return {
            "status": "REJECTED",
            "error": None,
            "error_code": "APPROVAL_REJECTED",
            "messages": [
                _message(
                    "rejected",
                    "已拒绝本次修改，沙盒将回滚到执行前状态",
                )
            ],
        }

    def _failed(self, state: AgentState) -> dict[str, Any]:
        return {
            "status": "FAILED",
            "error": state.get("error") or "Agent 执行失败",
            "messages": [
                _message("failed", state.get("error") or "Agent 执行失败", error=True)
            ],
        }

    # --------------------------------------------------------
    # 路由
    # --------------------------------------------------------
    def _route_after_clarify(self, state: AgentState) -> str:
        outcome = state.get("clarification_outcome")
        if outcome == "replan":
            return "planner"
        if outcome == "exhausted":
            return "failed"
        # 业务澄清完成后先做样式完整性检查，再进入 Coder；纯位移 /
        # 已明确样式的请求会在 style_check 内直接跳过。
        return "style_check"

    def _route_after_style_check(self, state: AgentState) -> str:
        if state.get("style_check_outcome") == "exhausted":
            return "failed"
        return "coder"

    def _route_after_coder(self, state: AgentState) -> str:
        # Coder 自身超时 / 结构化输出失败属于终态错误，直接 failed；
        # 只有“已生成代码但应用失败”才交给 evaluator 做修复预算判断。
        if state.get("fatal_error") or state.get("next_action") == "fail":
            return "failed"
        return "evaluator" if state.get("apply_failed") else "applier"

    def _route_after_applier(self, state: AgentState) -> str:
        return "evaluator" if state.get("apply_failed") else "verifier"

    def _route_after_evaluator(self, state: AgentState) -> str:
        action = state.get("next_action")
        # 成功 / 待审批都先经过 reflection 提取偏好证据，再进入后续节点。
        if action in {"approve", "succeed"}:
            return "reflection"
        if action == "retry":
            return "coder"
        return "failed"

    def _route_after_reflection(self, state: AgentState) -> str:
        if state.get("next_action") == "approve" and state.get("approval_required"):
            return "approval_gate"
        return "succeeded"

    def _route_after_approval(self, state: AgentState) -> str:
        return (
            "rejected"
            if state.get("approval_decision") == "reject"
            else "awaiting_approval"
        )


# ------------------------------------------------------------
# HITL 工具函数
# ------------------------------------------------------------
def _find_interrupt(snapshot: Any) -> dict[str, Any] | None:
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            value = getattr(item, "value", None)
            if isinstance(value, dict):
                return value
    return None


# 外部检索触发词：只有用户明确要求查资料时才联网，避免每条标注都搜索。
_RESEARCH_PATTERN = re.compile(
    r"搜索|搜一下|查一下|查查|查询|检索|最新资料|最新版本|官网|官方文档|"
    r"外部资料|联网|search\s+the\s+web|look\s+up|latest\s+version|documentation",
    re.IGNORECASE,
)


def _extract_research_query(context: dict[str, Any]) -> str | None:
    """从标注指令中提取检索 query；没有触发词时返回 None。"""
    texts: list[str] = []
    for item in context.get("annotations") or []:
        annotation = item.get("annotation") if isinstance(item, dict) else None
        if not isinstance(annotation, dict):
            continue
        intent = annotation.get("intent") or {}
        ui = intent.get("ui") if isinstance(intent, dict) else {}
        if isinstance(ui, dict) and ui.get("summary"):
            texts.append(str(ui["summary"]))
        payload = annotation.get("payload") or {}
        if isinstance(payload, dict) and payload.get("text"):
            texts.append(str(payload["text"]))
    combined = " ".join(item.strip() for item in texts if item.strip())
    if not combined or not _RESEARCH_PATTERN.search(combined):
        return None
    # 去掉换行并限制长度，避免把整段验收标准当作 query。
    return re.sub(r"\s+", " ", combined).strip()[:200]


APPROVAL_DIFF_LIMIT = 20000


def _build_approval_payload(state: AgentState) -> dict[str, Any]:
    """构造审批证据：逐文件 unified diff + 汇总统计 + 构建结果。

    以前端传入时的 ``initial_files`` 为基线，而不是用一个可能已被其它节点
    覆盖的 current_files；这样“拒绝”回滚和 Diff 展示使用同一份事实。
    """
    initial = dict(state.get("initial_files") or {})
    current = dict(state.get("current_files") or {})
    generated = dict(state.get("generated_files") or {})
    patch = str(state.get("generated_patch") or "")

    paths: set[str] = set(generated) | set(_diff_paths(patch))
    for path in set(initial) | set(current):
        if initial.get(path) != current.get(path):
            paths.add(path)

    files: list[dict[str, Any]] = []
    total_insertions = 0
    total_deletions = 0
    for path in sorted(paths):
        old = initial.get(path)
        new = current.get(path)
        if new is None:
            new = generated.get(path, "")
        if old is None:
            change_type = "added"
            old = ""
        else:
            change_type = "modified"
        if old == new:
            continue
        diff = _unified_diff(old, str(new), path)
        insertions, deletions = _diff_stat(diff)
        total_insertions += insertions
        total_deletions += deletions
        truncated = len(diff) > APPROVAL_DIFF_LIMIT
        files.append(
            {
                "path": path,
                "changeType": change_type,
                "insertions": insertions,
                "deletions": deletions,
                "diff": (
                    f"{diff[:APPROVAL_DIFF_LIMIT]}\n...[truncated]..."
                    if truncated
                    else diff
                ),
                "truncated": truncated,
            }
        )

    total_changes = total_insertions + total_deletions
    if len(files) > 3 or total_changes > 400:
        risk_level = "HIGH"
    elif len(files) > 1 or total_changes > 80:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"

    annotations = (state.get("annotation_context") or {}).get("annotations", [])
    summaries = [
        str(item.get("ui_summary") or item.get("instruction") or "").strip()
        for item in annotations
    ]
    summary = "；".join(item for item in summaries if item) or "应用本轮代码修改"
    verify = state.get("verify_result") or {}
    expires_at = datetime.now(timezone.utc) + timedelta(
        hours=int(state.get("approval_ttl_hours", 24))
    )
    return {
        "kind": "approval",
        "approvalId": f"apr_{state.get('run_id')}",
        "runId": state.get("run_id"),
        "status": "PENDING",
        "summary": summary[:300],
        "riskLevel": risk_level,
        "options": ["approve", "reject"],
        "evidence": {
            "filesChanged": len(files),
            "insertions": total_insertions,
            "deletions": total_deletions,
            "files": files,
            "build": {
                "status": "PASSED" if verify.get("success") else "FAILED",
                "durationMs": verify.get("duration_ms", 0),
                "exitCode": verify.get("exit_code", 0),
            },
        },
        "sandboxId": state.get("sandbox_id"),
        "previewUrl": state.get("preview_url"),
        "expiresAt": expires_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def _normalize_approval_decision(raw: Any) -> str:
    """把前端 / BFF 的多种写法归一为 approve / reject。

    未知决策一律按 reject 处理：宁可回滚，也不能在用户没有明确批准时
    把代码写入 Git checkpoint。
    """
    value: Any = raw
    if isinstance(raw, dict):
        value = raw.get("decision") or raw.get("approvalDecision") or raw.get("action")
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"approve", "approved", "accept", "accepted", "ok"}:
            return "approve"
        if normalized in {
            "reject",
            "rejected",
            "deny",
            "denied",
            "refuse",
            "takeover",
        }:
            return "reject"
    return "reject"


def _approval_comment(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    comment = raw.get("comment") or raw.get("reason")
    return str(comment)[:1000] if comment else None


# 用户已明确说出的视觉线索：命中任意一项就不再追问样式。
# 注意不要把“按钮 / 组件”本身当样式，否则“加个按钮”永远不会触发澄清。
_STYLE_HINT_PATTERN = re.compile(
    r"("
    r"主色|次要色|次色|主题色|主次|颜色|红色|蓝色|绿色|黄色|黑色|白色|灰色|紫色|橙色|"
    r"圆角|rounded|字体|字号|字重|加粗|阴影|shadow|边框|描边|"
    r"大小|尺寸|宽高|宽度|高度|内边距|padding|间距|margin|gap|"
    r"primary|secondary|tailwind|class(?:name)?|样式|风格|"
    r"复用|沿用|一致|一样|相同|"
    r"\d+\s*(?:px|rem|em)"
    r")",
    re.IGNORECASE,
)

_COMPONENT_NOUNS = (
    ("button", "按钮"),
    ("按钮", "按钮"),
    ("input", "输入框"),
    ("输入框", "输入框"),
    ("select", "下拉框"),
    ("下拉", "下拉框"),
    ("card", "卡片"),
    ("卡片", "卡片"),
    ("badge", "标签"),
    ("标签", "标签"),
    ("icon", "图标"),
    ("图标", "图标"),
)


def _has_style_hints(text: str) -> bool:
    """判断用户是否已经明确描述过颜色 / 尺寸 / 主次 / Tailwind 类名。"""
    return bool(_STYLE_HINT_PATTERN.search(str(text or "")))


def _annotation_style_text(annotation: dict[str, Any]) -> str:
    payload = annotation.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    intent = annotation.get("intent")
    intent = intent if isinstance(intent, dict) else {}
    ui = intent.get("ui")
    ui = ui if isinstance(ui, dict) else {}
    return str(payload.get("text") or ui.get("summary") or "").strip()


def _component_noun(target: dict[str, Any]) -> str:
    raw = f"{target.get('componentType') or ''} {target.get('text') or ''}".lower()
    for token, noun in _COMPONENT_NOUNS:
        if token in raw:
            return noun
    return "组件"


def _style_targets(context: dict[str, Any]) -> list[dict[str, Any]]:
    """收集所有真正会“新增组件”的目标（含 BATCH_LAYOUT 嵌套新增项）。

    纯位移、纯修改不参与样式追问：它们应复用既有元素样式，强行打断
    只会制造噪音。
    """
    targets: list[dict[str, Any]] = []
    for item in context.get("annotations") or []:
        annotation = item.get("annotation") or {}
        layout = _explicit_layout_batch(annotation)
        if layout is not None:
            for addition in layout["additions"]:
                nested = addition["annotation"]
                targets.append(
                    {
                        "annotationId": addition["annotationId"],
                        "text": addition["intent"],
                        "componentType": nested.get("componentType")
                        or nested.get("component_type")
                        or "",
                    }
                )
            continue
        if not is_create_action(annotation.get("action")):
            continue
        annotation_id = (
            annotation.get("annotationId")
            or annotation.get("annotation_id")
            or item.get("annotation_id")
        )
        if not annotation_id:
            continue
        targets.append(
            {
                "annotationId": str(annotation_id),
                "text": _annotation_style_text(annotation)
                or str(item.get("instruction") or ""),
                "componentType": annotation.get("componentType")
                or annotation.get("component_type")
                or "",
            }
        )
    return targets


def _merge_style_text(value: Any, style_text: str) -> str:
    original = str(value or "").strip()
    if not style_text:
        return original
    if style_text in original:
        return original
    return f"{original}（样式要求：{style_text}）" if original else style_text


def _augment_style_answers(
    context: dict[str, Any],
    answers: list[dict[str, Any]],
    target_ids: set[str],
) -> dict[str, Any]:
    """把样式回答写回标注上下文，Coder 从同一份 annotation_context 读取。

    与业务澄清不同，本次只增强“样式缺失的新增项”，不能污染已有元素
    的修改指令；BATCH_LAYOUT 需要同时更新嵌套信封与 entry.intent，
    因为 `_explicit_layout_batch` 优先读取后者作为展示与合成指令。
    """
    style_text = "；".join(filter(None, (_answer_text(item) for item in answers)))
    if not style_text or not target_ids:
        return context

    updated = copy.deepcopy(context)
    for item in updated.get("annotations") or []:
        annotation = item.get("annotation") or {}
        layout = _explicit_layout_batch(annotation)
        if layout is not None:
            raw_additions = (annotation.get("payload") or {}).get("additions") or []
            for addition in raw_additions:
                if not isinstance(addition, dict):
                    continue
                nested = addition.get("annotation")
                if not isinstance(nested, dict):
                    continue
                annotation_id = str(
                    nested.get("annotationId") or nested.get("annotation_id") or ""
                )
                if annotation_id not in target_ids:
                    continue
                merged = _merge_style_text(_annotation_style_text(nested), style_text)
                nested.setdefault("payload", {})["text"] = merged
                intent = nested.get("intent")
                if not isinstance(intent, dict):
                    intent = {}
                    nested["intent"] = intent
                ui = intent.get("ui")
                if not isinstance(ui, dict):
                    ui = {}
                    intent["ui"] = ui
                ui["summary"] = merged
                addition["intent"] = merged
            continue

        annotation_id = str(
            annotation.get("annotationId")
            or annotation.get("annotation_id")
            or item.get("annotation_id")
            or ""
        )
        if annotation_id not in target_ids:
            continue
        merged = _merge_style_text(
            _annotation_style_text(annotation) or str(item.get("instruction") or ""),
            style_text,
        )
        annotation.setdefault("payload", {})["text"] = merged
        intent = annotation.get("intent")
        if not isinstance(intent, dict):
            intent = {}
            annotation["intent"] = intent
        ui = intent.get("ui")
        if not isinstance(ui, dict):
            ui = {}
            intent["ui"] = ui
        ui["summary"] = merged
        item["instruction"] = merged
        item["ui_summary"] = merged
    return updated


def _build_style_clarification_payload(
    state: AgentState,
    targets: list[dict[str, Any]],
    round_no: int,
) -> dict[str, Any]:
    """构造样式澄清帧。

    问题与选项都刻意引用“现有按钮”而不是给具体色值：目标是让用户做
    选择，而不是让 Agent 猜一个十六进制颜色。
    """
    first = targets[0]
    noun = _component_noun(first)
    question = {
        "questionId": "style.appearance",
        "type": "single_choice",
        "prompt": (
            f"请问您希望这个{noun}是主色还是次要色？" "大小与现有的提交按钮一致吗？"
        ),
        "required": True,
        "options": [
            {
                "value": "和现有主按钮一致",
                "label": "和现有主按钮一致",
                "description": "复用主色、圆角与内边距",
                "recommended": True,
            },
            {
                "value": "和现有次要按钮一致",
                "label": "和现有次要按钮一致",
                "description": "复用描边 / 幽灵按钮样式",
            },
            {
                "value": "和登录按钮一样",
                "label": "和登录按钮一样",
                "description": "严格复用登录按钮的 Tailwind 类名",
            },
        ],
    }
    clarification = {
        "annotationId": first["annotationId"],
        "reason": "style_detail_missing",
        "decisionImpact": "MAJOR",
        "decisionSummary": (f"新增{noun}需要先确认视觉样式，避免与现有页面风格割裂。"),
        "questions": [question],
    }
    # 借用标准澄清帧构造：统一 clarificationId / expiresAt / 一键选项，
    # 前端无需为样式追问新增一套协议。
    return _build_clarification_payload(
        {
            **state,
            "clarification": clarification,
            "plan_decision_impact": "MAJOR",
        },
        round_no,
    )


def _build_clarification_payload(
    state: AgentState,
    round_no: int,
) -> dict[str, Any]:
    clarification = state.get("clarification") or {}
    annotation_id = clarification.get("annotationId")
    thread_id = None
    for item in (state.get("annotation_context") or {}).get("annotations", []):
        if not annotation_id or item.get("annotation_id") == annotation_id:
            thread_id = item.get("thread_id")
            break
        # BATCH_LAYOUT 的澄清目标可能是嵌套新增项，其 annotationId 不在
        # 顶层 context 的 annotation_id 上；沿 additions 查找并复用同一
        # 布局批次的 threadId，前端才能把问题投递到正确的迷你聊天框。
        layout = _explicit_layout_batch(item.get("annotation") or {})
        if layout is None:
            continue
        matched = next(
            (
                addition
                for addition in layout["additions"]
                if addition["annotationId"] == annotation_id
            ),
            None,
        )
        if matched is not None:
            nested = matched["annotation"]
            thread_id = nested.get("threadId") or nested.get("thread_id")
            break
    expires_at = datetime.now(timezone.utc) + timedelta(
        hours=int(state.get("clarification_ttl_hours", 24))
    )
    return {
        "kind": "clarification",
        "clarificationId": f"clr_{state.get('run_id')}_{round_no}",
        "runId": state.get("run_id"),
        "annotationId": annotation_id,
        "threadId": thread_id,
        "round": round_no,
        "maxRounds": int(state.get("max_clarification_rounds", 3)),
        "reason": clarification.get("reason") or "core_business_decision",
        "decisionImpact": clarification.get("decisionImpact")
        or state.get("plan_decision_impact")
        or CRITICAL,
        "decisionSummary": clarification.get("decisionSummary") or "",
        "questions": clarification.get("questions") or [],
        # 前端一键决策按钮；questions[].options 作为兼容回退。
        "clarificationOptions": clarification.get("clarificationOptions")
        or build_clarification_options(clarification.get("questions") or []),
        "expiresAt": expires_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def _normalize_answers(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    answers: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        question_id = item.get("questionId") or item.get("question_id")
        if not question_id:
            continue
        answers.append(
            {
                "questionId": str(question_id),
                "value": item.get("value", item.get("answer")),
                "text": item.get("text"),
            }
        )
    return answers


def _augment_context_with_answers(
    context: dict[str, Any],
    answers: list[dict[str, Any]],
) -> dict[str, Any]:
    """把澄清答案合并进标注文本，Planner / Coder 使用同一份增强上下文。

    LangGraph 恢复时不会重新执行 Service 的初始状态构造，因此必须在
    Planner 内把答案写回 annotation_context，Coder 才能看到“改成红色”。
    """
    answer_text = "；".join(
        f"{item.get('questionId')}: {_answer_text(item)}"
        for item in answers
        if _answer_text(item)
    )
    if not answer_text:
        return context

    updated = copy.deepcopy(context)
    for item in updated.get("annotations", []):
        annotation = item.get("annotation") or {}
        payload = annotation.setdefault("payload", {})
        original = str(payload.get("text") or "").strip()
        merged = f"{original}（澄清回答：{answer_text}）" if original else answer_text
        payload["text"] = merged
        intent = annotation.setdefault("intent", {})
        ui = intent.setdefault("ui", {})
        ui["summary"] = merged
        item["instruction"] = merged
        item["ui_summary"] = merged
        # M2 一次澄清默认只作用于当前单标注；批量标注的逐条答案可在 M3
        # 通过 answer.annotationId 扩展。
        break
    return updated


def _answer_text(item: dict[str, Any]) -> str:
    value = item.get("value")
    # 多选用列表表达，拼成自然语言再注入 Planner，避免出现 Python repr。
    if isinstance(value, list):
        return "、".join(str(part) for part in value if str(part).strip())
    if isinstance(value, dict):
        return str(value)
    return str(value or item.get("text") or "").strip()


def _ensure_clarification_annotation(
    clarification: dict[str, Any],
    annotations: list[dict[str, Any]],
) -> dict[str, Any]:
    if clarification.get("annotationId") or not annotations:
        return clarification
    first = annotations[0]
    annotation_id = first.get("annotationId") or first.get("annotation_id")
    if annotation_id:
        clarification = {**clarification, "annotationId": str(annotation_id)}
    return clarification


def _fallback_clarification(annotations: list[dict[str, Any]]) -> dict[str, Any]:
    """核心业务决策的兜底澄清模板。

    只有 Planner 已判定 CRITICAL 且模型未返回结构化澄清时才会走到
    这里；实现细节层不会调用本函数。
    """
    annotation_id = None
    if annotations:
        annotation_id = annotations[0].get("annotationId") or annotations[0].get(
            "annotation_id"
        )
    questions = [
        {
            "questionId": "q_core_scope",
            "type": "single_choice",
            "prompt": "这个需求涉及核心业务逻辑，你希望如何实现？",
            "required": True,
            "options": [
                {
                    "value": "ui_only",
                    "label": "只做前端演示",
                    "description": "不接后端，先看效果",
                },
                {
                    "value": "backend_api",
                    "label": "补齐后端接口",
                    "description": "新增或修改 API / 数据表",
                },
                {
                    "value": "existing_service",
                    "label": "复用已有服务",
                    "description": "接入现有接口，不改契约",
                },
            ],
            "defaultValue": "backend_api",
        }
    ]
    return {
        "reason": "core_business_decision",
        "decisionImpact": CRITICAL,
        "decisionSummary": _decision_summary(annotations),
        "annotationId": str(annotation_id) if annotation_id else None,
        "questions": questions,
        "clarificationOptions": build_clarification_options(questions),
    }


def _decision_summary(annotations: list[dict[str, Any]]) -> str:
    text = ""
    if annotations:
        payload = annotations[0].get("payload") or {}
        text = str(payload.get("text") or "").strip()
    if not text:
        return "该需求涉及核心业务决策，需要你确认实现范围"
    return f"该需求涉及核心业务决策：{text[:80]}"


def _node_summary(
    name: str,
    update: dict[str, Any],
    state: AgentState,
) -> str:
    if name == "summarize_history":
        before = update.get("history_tokens_before", 0)
        after = update.get("history_tokens_after", 0)
        if not before:
            return "无历史需要压缩"
        if before != after:
            return f"历史已压缩：{before} -> {after} tokens"
        return f"历史未超过阈值（{before} tokens）"
    if name == "planner":
        plan = str(update.get("plan") or "")
        confidence = update.get("plan_confidence")
        impact = update.get("plan_decision_impact")
        suffix = f"（置信度 {confidence:.2f}）" if isinstance(confidence, float) else ""
        impact_text = f"[{impact}] " if impact else ""
        return f"{impact_text}{plan[:120]}{suffix}"
    if name == "clarify_gate":
        outcome = update.get("clarification_outcome")
        if outcome == "replan":
            return "已收到澄清回答，重新规划"
        if outcome == "exhausted":
            return "澄清轮次耗尽"
        return "信息充分，继续执行"
    if name == "style_check":
        outcome = update.get("style_check_outcome")
        if outcome == "exhausted":
            return "样式澄清轮次耗尽"
        if outcome == "proceed":
            return "已确认视觉样式，继续生成"
        return "样式已明确或无需追问，直接生成"
    if name == "reflection":
        observations = update.get("reflection_preferences") or []
        if not observations:
            return "本轮没有新的实现偏好"
        return "记录 " + "、".join(
            f"{item.get('key')}={item.get('value')}" for item in observations
        )
    if name == "coder":
        files = sorted((update.get("generated_files") or {}).keys())
        return (
            f"生成 {len(files)} 个文件：{', '.join(files)}" if files else "生成修改方案"
        )
    if name == "applier":
        return "已写入沙盒" if not update.get("apply_failed") else "写入沙盒失败"
    if name == "verifier":
        verify = update.get("verify_result") or {}
        return (
            f"构建通过（{verify.get('duration_ms', 0)}ms）"
            if verify.get("success")
            else "构建失败"
        )
    if name == "evaluator":
        action = update.get("next_action")
        return {"approve": "验证通过", "retry": "安排自动修复"}.get(
            action, "重试预算耗尽"
        )
    if name == "approval_gate":
        return (
            "已拒绝，回滚沙盒"
            if update.get("approval_decision") == "reject"
            else "已批准"
        )
    if name == "rejected":
        return "本次修改已拒绝并回滚"
    return ""


def _duration_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


# ------------------------------------------------------------
# 通用工具函数
# ------------------------------------------------------------
def _planner_rag_query(
    annotations: list[dict[str, Any]],
    current_files: dict[str, str],
) -> str:
    """把画布标注压缩成一条检索查询。

    只取用户诉求与元素定位信息，不拼接完整源码，避免无关 token 稀释
    向量相似度；检索词上限由调用方再次截断。
    """
    parts: list[str] = []
    for annotation in annotations:
        text = annotation_text(annotation).strip()
        if text:
            parts.append(text)
        selector = annotation_selector(annotation).strip()
        if selector:
            parts.append(f"元素 {selector}")
        target = annotation_target_file(annotation, current_files, "")
        if target:
            parts.append(f"文件 {target}")
    return "；".join(dict.fromkeys(parts))


def _message(node: str, content: str, error: bool = False) -> dict[str, Any]:
    return {
        "role": "assistant",
        "node": node,
        "content": content,
        "level": "error" if error else "info",
    }


def _failure(error_logs: str) -> dict[str, Any]:
    """非构建类失败（Coder / Applier / 沙盒执行异常）。

    这类错误本身已经很短，error_context 直接复用截断后的消息。
    """
    return {
        "success": False,
        "error_logs": error_logs,
        "error_context": _tail(error_logs, 2000),
        "error_fingerprint": "",
        "exit_code": -1,
        "duration_ms": 0,
    }


def _select_error_source(logs: str, state: AgentState) -> str:
    """根据错误日志中的文件路径选择要截取的源码。

    依次回退：日志里的文件 -> target_files 第一个 -> target_file -> 任意文件。
    """
    files = dict(state.get("current_files") or {})
    hint = error_file_hint(logs)
    if hint:
        wanted = hint.replace("\\", "/").lstrip("./")
        for path, source in files.items():
            normalized = path.replace("\\", "/").lstrip("./")
            if (
                normalized == wanted
                or normalized.endswith(wanted)
                or wanted.endswith(normalized)
            ):
                return source
    for path in state.get("target_files") or []:
        if path in files:
            return files[path]
    target = state.get("target_file")
    if target and target in files:
        return files[target]
    return next(iter(files.values()), "")


def _repair_feedback(verify_result: dict[str, Any]) -> str | None:
    """返回注入 Planner / Coder 的精简错误；成功或空结果返回 None。

    Vite 的失败日志通常只有 1~3KB，此时完整日志比“日志 + 代码窗口”更短，
    直接复用完整日志，避免截断逻辑反而放大 Prompt；只有日志足够长时才
    使用 ``error_context``（最多 2000 字符）实现降本。
    """
    if not verify_result or verify_result.get("success", True):
        return None
    logs = str(verify_result.get("error_logs") or "").strip()
    context = str(verify_result.get("error_context") or "").strip()
    if logs and len(logs) <= 2000 and len(logs) <= len(context or ""):
        return logs
    if context:
        return context
    return _tail(logs, 2000) if logs else None


def _repair_payload(verify_result: dict[str, Any]) -> dict[str, Any]:
    """Coder 只看到必要字段，避免完整 stdout/stderr 重复进入 Prompt。"""
    if not verify_result:
        return {}
    return {
        "success": bool(verify_result.get("success")),
        "exit_code": verify_result.get("exit_code", -1),
        "error_context": _repair_feedback(verify_result) or "",
    }


_HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new>\d+)(?:,(?P<new_count>\d+))? @@"
)
_DIFF_FILE_RE = re.compile(r"^(?P<marker>---|\+\+\+)\s+(?P<path>.+?)\s*$")


def _normalize_patch_path(raw: str) -> str:
    value = raw.split("\t", 1)[0].strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    if value.startswith("b/") or value.startswith("a/"):
        value = value[2:]
    return value


def _apply_unified_patch(
    current_files: dict[str, str],
    patch: str,
) -> dict[str, str] | None:
    """把模型输出的 unified diff 合并回内存文件表。

    BATCH_LAYOUT 需要先拿到“新增组件后的完整内容”，再追加确定性 CSS
    位移规则；沙盒写盘仍由 Applier 用 ``git apply`` 完成。这里只处理
    标准 ``---/+++`` + ``@@`` 格式，容忍行号偏移（与 ``--recount`` 的
    行为一致）；遇到无法定位的上下文就返回 None，绝不猜测写错位置。
    """
    if not patch.strip():
        return None
    lines = patch.splitlines()
    result = dict(current_files)
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        if not line.startswith("--- "):
            index += 1
            continue
        if index + 1 >= total or not lines[index + 1].startswith("+++ "):
            return None
        old_path = _normalize_patch_path(line[4:])
        new_path = _normalize_patch_path(lines[index + 1][4:])
        path = new_path if new_path != "/dev/null" else old_path
        if not path or path == "/dev/null":
            return None
        content = result.get(path, "")
        file_lines = content.splitlines()
        trailing_newline = content.endswith("\n")
        index += 2
        line_offset = 0  # 同一文件内多个 hunk 的累计行号位移
        applied_any = False
        while index < total and lines[index].startswith("@@"):
            header = _HUNK_HEADER_RE.match(lines[index])
            if not header:
                return None
            old_start = int(header.group("old"))
            index += 1
            old_block: list[str] = []
            new_block: list[str] = []
            while index < total:
                body = lines[index]
                if body.startswith("@@"):
                    break
                if (
                    body.startswith("--- ")
                    and index + 1 < total
                    and lines[index + 1].startswith("+++ ")
                ):
                    break
                if body.startswith("\\"):  # \ No newline at end of file
                    index += 1
                    continue
                if not body:
                    # 空行在 diff 中表示一个空的上下文行。
                    old_block.append("")
                    new_block.append("")
                elif body[0] == " ":
                    old_block.append(body[1:])
                    new_block.append(body[1:])
                elif body[0] == "-":
                    old_block.append(body[1:])
                elif body[0] == "+":
                    new_block.append(body[1:])
                else:
                    # 非标准行：视为补丁结束，交给下一个文件头处理。
                    break
                index += 1

            expected = max(0, old_start - 1 + line_offset)
            position = _locate_block(file_lines, old_block, expected)
            if position is None:
                return None
            file_lines[position : position + len(old_block)] = new_block
            line_offset += len(new_block) - len(old_block)
            applied_any = True

        if not applied_any:
            return None
        updated = "\n".join(file_lines)
        if trailing_newline:
            updated += "\n"
        result[path] = updated
    return result


def _locate_block(
    file_lines: list[str],
    block: list[str],
    expected: int,
) -> int | None:
    """在期望位置附近搜索 hunk 的旧内容，容忍行号偏移。"""
    if not block:
        return min(max(0, expected), len(file_lines))
    last_start = len(file_lines) - len(block)
    if last_start < 0:
        return None
    # 补丁行号可能远大于实际文件（模型计数错误 / 上下文过短），先把期望
    # 位置夹到合法区间，再向外扩散；否则从越界位置出发永远扫不到。
    start = min(max(0, expected), last_start)
    for distance in range(0, len(file_lines) + 1):
        for candidate in (start - distance, start + distance):
            if candidate < 0 or candidate > last_start:
                continue
            if file_lines[candidate : candidate + len(block)] == block:
                return candidate
    return None


def _unified_diff(old: str, new: str, target_file: str) -> str:
    if old == new:
        return ""
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{target_file}",
            tofile=f"b/{target_file}",
        )
    )


def _materialize_generated_files(
    current_files: dict[str, str],
    generated: dict[str, str],
) -> dict[str, str]:
    """把 Coder 输出的完整文件经 ``CodeEditor`` 落地为变更集合。

    - 已存在文件：``REWRITE_FILE``，避免模型拼错路径却新建孤儿文件；
    - 新文件：``CREATE_FILE``，为全栈阶段的 Java / SQL / 配置生成预留；
    - 路径穿越、绝对路径、非法盘符由 ``FileCreator`` 统一拒绝。

    返回相对 ``current_files`` 的净变化，``apply_all`` 会丢弃内容未变的文件。
    """
    if not generated:
        return {}
    edits = [
        CodeEdit(
            kind=(
                EditKind.REWRITE_FILE if path in current_files else EditKind.CREATE_FILE
            ),
            path=path,
            content=content,
            label=f"{'重写' if path in current_files else '新建'} {path}",
        )
        for path, content in generated.items()
    ]
    result = default_editor_registry().apply_all(edits, current_files)
    return dict(result.files)


def _combined_diff(
    before: dict[str, str],
    after: dict[str, str],
) -> str:
    """把 files 模式下所有变更文件合成为一个 unified diff，用于前端展示。"""
    parts: list[str] = []
    for path in sorted(after):
        old = before.get(path)
        # 新增文件没有旧内容，不在这里生成 diff；构建仍以 files 全量写入为准。
        if old is None:
            continue
        diff = _unified_diff(old, after[path], path)
        if diff:
            parts.append(diff)
    return "".join(parts)


def _diff_paths(patch: str) -> list[str]:
    """从 unified diff 中提取目标文件路径（优先 +++ b/path）。"""
    paths: list[str] = []
    for line in patch.splitlines():
        if not line.startswith("+++ "):
            continue
        path = line[4:].strip()
        if path.startswith("b/"):
            path = path[2:]
        if path and path != "/dev/null" and path not in paths:
            paths.append(path)
    return paths


def _diff_stat(patch: str) -> tuple[int, int]:
    insertions = 0
    deletions = 0
    for line in patch.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            continue
        if line.startswith("+"):
            insertions += 1
        elif line.startswith("-"):
            deletions += 1
    return insertions, deletions


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"...[truncated]...\n{text[-limit:]}"
