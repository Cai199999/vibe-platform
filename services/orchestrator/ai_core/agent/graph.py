"""LangGraph 单 Agent 闭环 + HITL 澄清门。

拓扑：

    START -> summarize_history -> planner -> clarify_gate -> coder -> applier
                                    -> verifier -> evaluator
                ▲              │           ▲                              │
                │              │(澄清)     └──── repair (<= 3 次) ◄───────┤
                │              ▼                                          │
                └──── replan / interrupt()                                ├─ success + 审批 -> approval_gate -> awaiting_approval
                                                                          ├─ success + 免审批 -> succeeded (SUCCEEDED)
                                                                          └─ exhausted -> failed

节点内部通过 ``EventSink`` 推送 node.* 事件；执行与 SSE 连接解耦。
"""

from __future__ import annotations

import copy
import difflib
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt

from ai_core.adapters.checkpoint import get_checkpointer_provider
from ai_core.ports.checkpoint import CheckpointerProvider
from ai_core.agent.error_context import (
    error_file_hint,
    extract_error_context,
    extract_error_fingerprint,
)
from ai_core.agent.events import EventSink, NullEventSink
from ai_core.agent.llm import (
    AgentLLM,
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
    CORE_BUSINESS,
    IMPLEMENTATION_DETAIL,
    build_clarification_options,
    extract_preference_observations,
    infer_decision_impact,
    normalize_decision_impact,
    should_interrupt,
)
from ai_core.context_memory import get_project_memory
from ai_core.domain.state import AgentState
from ai_core.config import get_settings
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


def _is_explicit_move_batch(annotations: list[dict[str, Any]]) -> bool:
    """所有标注都是带选择器的直接拖拽 MOVE 时，走完全确定性链路。"""
    if not annotations:
        return False
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
            {"planner": "planner", "coder": "coder", "failed": "failed"},
        )
        builder.add_conditional_edges(
            "coder",
            self._route_after_coder,
            {"applier": "applier", "evaluator": "evaluator"},
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
            try:
                with collect_usage(collector):
                    update = fn(state)
            except GraphInterrupt:
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
        if _is_explicit_move_batch(annotations):
            # 直接拖拽是确定性几何输入：不调用规划模型，避免把“拖了 12px”
            # 重新解释成自然语言需求，也避免任何澄清中断打断静默同步。
            plan = PlanResult(
                plan=(
                    "调用 move_component Tool，按 payload.move 的累计 dx/dy "
                    "写入 CSS transform；禁止修改宽高与布局。"
                ),
                confidence_score=0.99,
                decision_impact=IMPLEMENTATION_DETAIL,
                needs_clarification=False,
            )
        else:
            plan = self._llm.plan(
                annotations=annotations,
                current_files=state["current_files"],
                previous_errors=previous_errors,
                clarification_answers=answers or None,
            )
        # 把 research 节点的外部检索结果并入计划，Coder 会从 state["plan"]
        # 读到它；要求模型引用时保留 URL，便于用户核查事实来源。
        plan_text = plan.plan
        research = (state.get("research_context") or "").strip()
        if research:
            plan_text = (
                f"{plan_text}\n\n外部检索资料（引用时注明来源 URL）：\n{research}"
            )
        threshold = float(state.get("confidence_threshold", 0.7))
        # 分级决策：模型未输出 decision_impact 时，用确定性关键词兜底。
        # 只有 CORE_BUSINESS 才允许触发中断，IMPLEMENTATION_DETAIL 一律自主执行。
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
            "status": "PLANNING",
            "messages": [_message("planner", plan_text)],
        }

    def _clarify_gate(self, state: AgentState) -> dict[str, Any]:
        mode = state.get("clarification_mode", "auto")
        impact = (
            normalize_decision_impact(state.get("plan_decision_impact"))
            or IMPLEMENTATION_DETAIL
        )
        # 双保险：即使旧 checkpoint 残留 plan_needs_clarification=True，
        # 只要影响面不是核心业务，也绝不能在实现细节上打断用户。
        needs = (
            bool(state.get("plan_needs_clarification"))
            and mode != "off"
            and impact == CORE_BUSINESS
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
        target_files = list(state.get("target_files") or [])
        if not target_files:
            return None
        rules: list[tuple[str, str, str]] = []
        for annotation in annotations:
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
            tool = self._toolbox.get("move_component")
            if tool is None:
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
        # 纯移动请求走标准 Tool，而不是让模型自由写 CSS：
        # 1) 位移规则由 move_component 统一生成（只允许 transform）；
        # 2) 杜绝模型改 width / height / left / top 造成“按钮瞬间变长”；
        # 3) 规则可测试、可审计，和 Mock Coder 完全一致。
        move_result = self._try_move_tool(
            state, annotations, current_files, retry_count
        )
        if move_result is not None:
            return move_result
        try:
            result = self._llm.generate(
                annotations=annotations,
                plan=state.get("plan", ""),
                current_files=current_files,
                target_files=state["target_files"],
                # 只把 Error Message + 报错行附近代码交给模型，
                # 完整 stdout/stderr 仅保留在状态里供 UI 与审计使用。
                verify_result=_repair_payload(verify_result),
                retry_count=retry_count,
            )
            # files 模式必须经过 FileCreator 落地：已有文件 REWRITE_FILE，
            # 新文件 CREATE_FILE。路径校验 / 覆盖保护由 CodeEditor 负责。
            generated_files = _materialize_generated_files(current_files, result.files)
        except Exception as exc:  # noqa: BLE001 - 模型错误转入失败/重试
            logger.warning("coder failed: %s", exc)
            return {
                "generated_patch": "",
                "generated_files": {},
                "apply_failed": True,
                "verify_result": _failure(f"Coder 生成失败: {exc}"),
                "status": "CODING",
                "messages": [_message("coder", f"代码生成失败: {exc}", error=True)],
            }

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
            {"checks": ["npm run build"]},
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
        if result.success:
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
        summary = (
            f"npm run build 成功，用时 {result.duration_ms}ms"
            if result.success
            else f"npm run build 失败 (exit={result.exit_code})，用时 {result.duration_ms}ms"
        )
        return {
            "apply_failed": False,
            "verify_result": verify_state,
            "status": "VERIFYING",
            "messages": [_message("verifier", summary, error=not result.success)],
        }

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
        return "coder"

    def _route_after_coder(self, state: AgentState) -> str:
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


def _build_clarification_payload(
    state: AgentState,
    round_no: int,
) -> dict[str, Any]:
    clarification = state.get("clarification") or {}
    annotation_id = clarification.get("annotationId")
    thread_id = None
    for item in (state.get("annotation_context") or {}).get("annotations", []):
        if annotation_id and item.get("annotation_id") != annotation_id:
            continue
        thread_id = item.get("thread_id")
        if annotation_id:
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
        or CORE_BUSINESS,
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

    只有 Planner 已判定 CORE_BUSINESS 且模型未返回结构化澄清时才会走到
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
        "decisionImpact": CORE_BUSINESS,
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
