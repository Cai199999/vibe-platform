"""LangGraph 单 Agent 闭环。

拓扑：

    START -> planner -> coder -> applier -> verifier -> evaluator
                            ^                              |
                            |                              +-- success --> awaiting_approval -> END
                            +---- retry (<= 3 次) <---------+
                                                           +-- exhausted --> failed -> END

Coder / Applier 失败同样进入 evaluator，复用同一套重试预算。
"""

from __future__ import annotations

import difflib
import logging
from typing import Any

from langgraph.graph import END, START, StateGraph

from ai_core.agent.llm import AgentLLM
from ai_core.agent.state import AgentState
from ai_core.sandbox.base import SandboxError, SandboxProvider

logger = logging.getLogger(__name__)


class AgentGraphRunner:
    """构建并执行单 Agent 状态机。"""

    def __init__(
        self,
        sandbox: SandboxProvider,
        llm: AgentLLM,
        max_retries: int = 3,
        recursion_limit: int = 60,
    ) -> None:
        # 硬限制：无论调用方传入什么，最多 3 次修复。
        self._max_retries = min(max(0, max_retries), 3)
        self._sandbox = sandbox
        self._llm = llm
        self._recursion_limit = recursion_limit
        self._graph = self._build()

    def run(self, initial_state: AgentState) -> AgentState:
        logger.info(
            "agent run start run_id=%s sandbox_id=%s",
            initial_state.get("run_id"),
            initial_state.get("sandbox_id"),
        )
        final_state = self._graph.invoke(
            initial_state,
            config={"recursion_limit": self._recursion_limit},
        )
        logger.info(
            "agent run finish run_id=%s status=%s retry_count=%s",
            final_state.get("run_id"),
            final_state.get("status"),
            final_state.get("retry_count"),
        )
        return final_state  # type: ignore[return-value]

    # --------------------------------------------------------
    # 图构建
    # --------------------------------------------------------
    def _build(self):
        builder = StateGraph(AgentState)

        builder.add_node("planner", self._planner)
        builder.add_node("coder", self._coder)
        builder.add_node("applier", self._applier)
        builder.add_node("verifier", self._verifier)
        builder.add_node("evaluator", self._evaluator)
        builder.add_node("awaiting_approval", self._awaiting_approval)
        builder.add_node("failed", self._failed)

        builder.add_edge(START, "planner")
        builder.add_edge("planner", "coder")
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
        return builder.compile()

    # --------------------------------------------------------
    # 节点
    # --------------------------------------------------------
    def _planner(self, state: AgentState) -> dict[str, Any]:
        context = state["annotation_context"]
        previous_errors = (state.get("verify_result") or {}).get("error_logs")
        plan = self._llm.plan(
            annotation=context["annotation"],
            current_code=state["current_code"],
            previous_errors=previous_errors,
        )
        return {
            "plan": plan.plan,
            "status": "PLANNING",
            "messages": [_message("planner", plan.plan)],
        }

    def _coder(self, state: AgentState) -> dict[str, Any]:
        target_file = state["target_file"]
        current_code = state["current_code"]
        verify_result = state.get("verify_result") or {}
        retry_count = int(state.get("retry_count", 0))

        try:
            result = self._llm.generate(
                annotation=state["annotation_context"]["annotation"],
                plan=state.get("plan", ""),
                current_code=current_code,
                target_file=target_file,
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

        patch = result.patch or ""
        if not patch and target_file in result.files:
            patch = _unified_diff(
                current_code,
                result.files[target_file],
                target_file,
            )

        return {
            "generated_patch": patch,
            "generated_files": result.files,
            "apply_failed": False,
            "status": "CODING",
            "messages": [
                _message(
                    "coder",
                    f"{result.summary}\n"
                    f"mode={'diff' if result.patch else 'files'} "
                    f"changed={sorted(result.files) or [target_file]}",
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
            if files:
                changed = self._sandbox.apply_patch(sandbox_id=sandbox_id, files=files)
            elif patch:
                changed = self._sandbox.apply_patch(sandbox_id=sandbox_id, patch=patch)
            else:
                changed = []
            # 以沙盒真实文件为准更新 current_code，兼容 unified diff。
            current_code = self._sandbox.read_file(sandbox_id, state["target_file"])
        except SandboxError as exc:
            logger.warning("applier failed: %s", exc)
            return {
                "apply_failed": True,
                "verify_result": _failure(f"应用补丁失败: {exc}"),
                "status": "APPLYING",
                "messages": [_message("applier", str(exc), error=True)],
            }

        return {
            "current_code": current_code,
            "apply_failed": False,
            "status": "APPLYING",
            "messages": [_message("applier", f"已写入沙盒，变更文件: {changed}")],
        }

    def _verifier(self, state: AgentState) -> dict[str, Any]:
        try:
            result = self._sandbox.verify(state["sandbox_id"], check="build")
        except SandboxError as exc:
            logger.warning("verifier execution failed: %s", exc)
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
# 工具函数
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


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"...[truncated]...\n{text[-limit:]}"
