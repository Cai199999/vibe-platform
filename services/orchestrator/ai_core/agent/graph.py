"""LangGraph 单 Agent 闭环 + HITL 澄清门。

拓扑：

    START -> planner -> clarify_gate -> coder -> applier -> verifier -> evaluator
                ▲              │           ▲                              │
                │              │(澄清)     └──── repair (<= 3 次) ◄───────┤
                │              ▼                                          │
                └──── replan / interrupt()                                ├─ success -> awaiting_approval
                                                                          └─ exhausted -> failed

节点内部通过 ``EventSink`` 推送 node.* 事件；执行与 SSE 连接解耦。
"""

from __future__ import annotations

import copy
import difflib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ai_core.agent.checkpoint import (
    CheckpointerProvider,
    get_checkpointer_provider,
)
from ai_core.agent.events import EventSink, NullEventSink
from ai_core.agent.llm import AgentLLM
from ai_core.agent.state import AgentState
from ai_core.sandbox.base import SandboxError, SandboxProvider

logger = logging.getLogger(__name__)

NODE_LABELS = {
    "planner": "理解修改意图",
    "clarify_gate": "检查信息完整性",
    "coder": "生成修改方案",
    "applier": "写入沙盒",
    "verifier": "构建与校验",
    "evaluator": "整理结果",
    "awaiting_approval": "等待确认",
    "failed": "执行失败",
}


@dataclass
class GraphRunOutcome:
    """一次 graph.invoke 的结果，包含暂停信息。"""

    state: AgentState
    interrupted: bool
    interrupt: dict[str, Any] | None
    thread_id: str


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
    ) -> None:
        # 硬限制：无论调用方传入什么，最多 3 次修复。
        self._max_retries = min(max(0, max_retries), 3)
        self._sandbox = sandbox
        self._llm = llm
        self._recursion_limit = recursion_limit
        self._event_sink = event_sink or NullEventSink()
        self._checkpointer_provider = (
            checkpointer_provider or get_checkpointer_provider()
        )
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
        outcome = GraphRunOutcome(
            state=state,  # type: ignore[arg-type]
            interrupted=bool(snapshot.next),
            interrupt=interrupt_payload,
            thread_id=resolved_thread_id,
        )
        logger.info(
            "agent run finish run_id=%s status=%s retry_count=%s interrupted=%s",
            state.get("run_id"),
            state.get("status"),
            state.get("retry_count"),
            outcome.interrupted,
        )
        return outcome

    # --------------------------------------------------------
    # 图构建
    # --------------------------------------------------------
    def _build(self):
        builder = StateGraph(AgentState)

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
        builder.add_node("awaiting_approval", self._awaiting_approval)
        builder.add_node("failed", self._failed)

        builder.add_edge(START, "planner")
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
                "awaiting_approval": "awaiting_approval",
                "failed": "failed",
            },
        )
        builder.add_edge("awaiting_approval", END)
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
            try:
                update = fn(state)
            except GraphInterrupt:
                self._emit(
                    "node.finished",
                    {
                        "node": name,
                        "status": "paused",
                        "durationMs": _duration_ms(started),
                        "summary": "等待用户澄清",
                    },
                    dedupe_key=f"node.finished:{key_base}:paused",
                )
                raise
            except Exception as exc:  # noqa: BLE001 - 事件后继续向上抛
                self._emit(
                    "node.finished",
                    {
                        "node": name,
                        "status": "error",
                        "durationMs": _duration_ms(started),
                        "summary": str(exc)[:300],
                    },
                    dedupe_key=f"node.finished:{key_base}:error",
                )
                raise

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
                },
                dedupe_key=f"node.finished:{key_base}:{status}",
            )
            return update

        return wrapped

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
        previous_errors = (state.get("verify_result") or {}).get("error_logs")
        plan = self._llm.plan(
            annotations=annotations,
            current_files=state["current_files"],
            previous_errors=previous_errors,
            clarification_answers=answers or None,
        )
        threshold = float(state.get("confidence_threshold", 0.7))
        needs = plan.needs_clarification or plan.confidence_score < threshold
        clarification = plan.clarification if needs else None
        if needs and clarification is None:
            clarification = _fallback_clarification(annotations)
        if clarification is not None:
            clarification = _ensure_clarification_annotation(clarification, annotations)
        return {
            "plan": plan.plan,
            "plan_confidence": float(plan.confidence_score),
            "plan_needs_clarification": needs,
            "clarification": clarification,
            "annotation_context": context,
            "status": "PLANNING",
            "messages": [_message("planner", plan.plan)],
        }

    def _clarify_gate(self, state: AgentState) -> dict[str, Any]:
        mode = state.get("clarification_mode", "auto")
        needs = bool(state.get("plan_needs_clarification")) and mode != "off"
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
        try:
            result = self._llm.generate(
                annotations=annotations,
                plan=state.get("plan", ""),
                current_files=current_files,
                target_files=state["target_files"],
                verify_result=verify_result,
                retry_count=retry_count,
            )
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
        if not patch and result.files:
            patch = _combined_diff(current_files, result.files)

        changed_files = sorted(result.files) or _diff_paths(patch)
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
            "generated_files": result.files,
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
        }
        if result.success:
            self._emit(
                "verify.passed",
                {"exitCode": result.exit_code, "durationMs": result.duration_ms},
                dedupe_key=f"verify.passed:{key}",
            )
        else:
            self._emit(
                "verify.failed",
                {
                    "exitCode": result.exit_code,
                    "durationMs": result.duration_ms,
                    "errorSummary": _tail(result.logs, 1000),
                    "fingerprint": "",
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
            return {
                "next_action": "approve",
                "status": "AWAITING_APPROVAL",
                "error": None,
                "messages": [
                    _message(
                        "evaluator",
                        f"验证通过（重试 {retry_count} 次），流转 AWAITING_APPROVAL",
                    )
                ],
            }

        error_logs = str(verify.get("error_logs") or "未知构建错误")
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
                    "reason": _tail(error_logs, 1000),
                },
                dedupe_key=(f"repair.started:{state.get('run_id')}:{retry_count + 1}"),
            )
            return {
                "retry_count": retry_count + 1,
                "next_action": "retry",
                "status": "REPAIRING",
                "messages": [
                    _message(
                        "evaluator",
                        f"构建失败，开始第 {retry_count + 1}/{max_retries} 次修复。"
                        f"错误摘要:\n{_tail(error_logs, 1500)}",
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

    def _awaiting_approval(self, state: AgentState) -> dict[str, Any]:
        return {
            "status": "AWAITING_APPROVAL",
            "messages": [
                _message(
                    "awaiting_approval",
                    "代码已通过构建验证，等待人工审批或前端预览确认",
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
        if action == "approve":
            return "awaiting_approval"
        if action == "retry":
            return "coder"
        return "failed"


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
        "clarificationId": f"clr_{state.get('run_id')}_{round_no}",
        "runId": state.get("run_id"),
        "annotationId": annotation_id,
        "threadId": thread_id,
        "round": round_no,
        "maxRounds": int(state.get("max_clarification_rounds", 3)),
        "reason": clarification.get("reason") or "ambiguous_intent",
        "questions": clarification.get("questions") or [],
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
    annotation_id = None
    if annotations:
        annotation_id = annotations[0].get("annotationId") or annotations[0].get(
            "annotation_id"
        )
    return {
        "reason": "low_confidence",
        "annotationId": str(annotation_id) if annotation_id else None,
        "questions": [
            {
                "questionId": "q_confirm",
                "type": "single_choice",
                "prompt": "当前信息不足，你希望怎么继续？",
                "required": True,
                "options": [
                    {"value": "confirm", "label": "按当前最佳计划执行"},
                    {"value": "adjust", "label": "我再补充具体要求"},
                ],
                "defaultValue": "adjust",
            }
        ],
    }


def _node_summary(
    name: str,
    update: dict[str, Any],
    state: AgentState,
) -> str:
    if name == "planner":
        plan = str(update.get("plan") or "")
        confidence = update.get("plan_confidence")
        suffix = f"（置信度 {confidence:.2f}）" if isinstance(confidence, float) else ""
        return f"{plan[:120]}{suffix}"
    if name == "clarify_gate":
        outcome = update.get("clarification_outcome")
        if outcome == "replan":
            return "已收到澄清回答，重新规划"
        if outcome == "exhausted":
            return "澄清轮次耗尽"
        return "信息充分，继续执行"
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
    return {
        "success": False,
        "error_logs": error_logs,
        "exit_code": -1,
        "duration_ms": 0,
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
