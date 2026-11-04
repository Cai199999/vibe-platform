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
            annotations=[item["annotation"] for item in context["annotations"]],
            current_files=state["current_files"],
            previous_errors=previous_errors,
        )
        return {
            "plan": plan.plan,
            "status": "PLANNING",
            "messages": [_message("planner", plan.plan)],
        }

    def _coder(self, state: AgentState) -> dict[str, Any]:
        context = state["annotation_context"]
        annotations = [item["annotation"] for item in context["annotations"]]
        current_files = state["current_files"]
        verify_result = state.get("verify_result") or {}
        retry_count = int(state.get("retry_count", 0))

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

        return {
            "current_code": current_code,
            "current_files": current_files,
            "apply_failed": False,
            "status": "APPLYING",
            "messages": [_message("applier", f"已一次写入沙盒，变更文件: {changed}")],
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


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"...[truncated]...\n{text[-limit:]}"
