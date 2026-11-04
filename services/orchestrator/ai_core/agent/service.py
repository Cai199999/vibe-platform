"""Run 执行服务：创建沙盒、装配 Graph、汇总执行结果。"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from ai_core.agent.context_doc import (
    VIBE_CONTEXT_FILENAME,
    render_vibe_context,
)
from ai_core.agent.graph import AgentGraphRunner
from ai_core.agent.llm import (
    AgentLLMError,
    build_agent_llm,
)
from ai_core.agent.schemas import (
    AnnotationEnvelope,
    ExecuteRunRequest,
    ExecuteRunResponse,
    RollbackRunRequest,
    RollbackRunResponse,
    RunCommitRecord,
    VerifyResultModel,
)
from ai_core.agent.thread_memory import ThreadMemoryStore, get_thread_store
from ai_core.config import Settings, get_settings
from ai_core.sandbox import get_sandbox_provider
from ai_core.sandbox.base import SandboxError, SandboxHandle, SandboxProvider

logger = logging.getLogger(__name__)

DEFAULT_TARGET_FILE = "src/App.jsx"


class FullStackNotSupportedError(Exception):
    """M1 尚未实现全栈生成。"""


class TargetFileNotFoundError(Exception):
    """标注指向的文件不存在。"""


class RollbackTargetMismatchError(Exception):
    """前端提交的 commit 与服务端 thread checkpoint 不一致。"""


class RunExecutionService:
    """M1 同步执行入口。

    M2 会改为：先落 checkpoint -> 返回 202 -> 后台执行 + 事件流。
    """

    def __init__(
        self,
        sandbox: SandboxProvider | None = None,
        settings: Settings | None = None,
        thread_store: ThreadMemoryStore | None = None,
    ) -> None:
        self._sandbox = sandbox or get_sandbox_provider()
        self._settings = settings or get_settings()
        self._threads = thread_store or get_thread_store()

    def execute(self, request: ExecuteRunRequest) -> ExecuteRunResponse:
        annotations = request.effective_annotations
        intent_kinds = {
            annotation.intent.kind if annotation.intent else "UI_ONLY"
            for annotation in annotations
        }
        if "FULLSTACK" in intent_kinds:
            raise FullStackNotSupportedError(
                "M1 只支持 UI_ONLY / UI_AND_BUSINESS，FULLSTACK 需等待 M2"
            )

        target_files = self._resolve_target_files(request)
        run_id = request.run_id or f"run_{uuid.uuid4().hex[:20]}"
        thread_ids = [
            self._thread_id(annotation, index)
            for index, annotation in enumerate(annotations, start=1)
        ]
        instructions = [
            self._annotation_instruction(annotation) for annotation in annotations
        ]

        # 先构造 LLM，避免密钥缺失时已经创建了沙盒。
        llm = build_agent_llm(request, self._settings)

        # 每个 thread 本轮成功后对应的轮次坐标。批量提交时各圈可能处于不同
        # 轮次；Git 只有一个全局 commit，因此 commit message 使用最大轮次。
        turn_counts = {
            thread_id: self._threads.next_turn(thread_id, annotation.chat_history)
            for annotation, thread_id in zip(annotations, thread_ids)
        }
        run_turn_count = max(turn_counts.values(), default=1)

        # 多轮改同一个圈时复用当前项目沙盒；只有首次 / 沙盒丢失才创建新容器。
        handle, reused_sandbox, current_files = self._open_sandbox(
            request, target_files, thread_ids
        )

        # 把全部画布标注渲染成 VIBE_CONTEXT.md 写进沙盒，导出源码包时自动带上：
        # 本地 Cursor / Claude Code 打开项目即可看到本次修改意图。
        # 写入失败只记录日志，不影响 Run 本身的执行与校验。
        try:
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

        # 合并前端 chatHistory 与服务端 thread 记忆，注入每条标注的
        # threadContext；Planner / Coder 必须结合历史理解增量修改。
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
            "status": "RUNNING",
            "error": None,
            "error_code": None,
        }

        runner = AgentGraphRunner(
            sandbox=self._sandbox,
            llm=llm,
            max_retries=initial_state["max_retries"],
            recursion_limit=self._settings.agent_graph_recursion_limit,
        )

        try:
            final_state = runner.run(initial_state)  # type: ignore[arg-type]
        except AgentLLMError as exc:
            logger.exception("agent llm failed run_id=%s", run_id)
            error = f"模型执行失败: {exc}"
            assistant_message = self._assistant_message(
                instructions, status="FAILED", error=error, retry_count=0
            )
            self._record_threads(
                annotations,
                thread_ids,
                run_id=run_id,
                assistant_message=assistant_message,
                status="FAILED",
                sandbox_id=handle.sandbox_id,
                files=current_files,
                patch="",
                changed_files=[],
                retry_count=0,
            )
            self._maybe_destroy(handle.sandbox_id, request)
            return ExecuteRunResponse(
                run_id=run_id,
                status="FAILED",
                sandbox_id=handle.sandbox_id,
                preview_url=handle.preview_url,
                reused_sandbox=reused_sandbox,
                assistant_message=assistant_message,
                error=error,
            )
        except SandboxError as exc:
            logger.exception("agent sandbox failed run_id=%s", run_id)
            error = f"沙盒执行失败: {exc}"
            assistant_message = self._assistant_message(
                instructions, status="FAILED", error=error, retry_count=0
            )
            self._record_threads(
                annotations,
                thread_ids,
                run_id=run_id,
                assistant_message=assistant_message,
                status="FAILED",
                sandbox_id=handle.sandbox_id,
                files=current_files,
                patch="",
                changed_files=[],
                retry_count=0,
            )
            self._maybe_destroy(handle.sandbox_id, request)
            return ExecuteRunResponse(
                run_id=run_id,
                status="FAILED",
                sandbox_id=handle.sandbox_id,
                preview_url=handle.preview_url,
                reused_sandbox=reused_sandbox,
                assistant_message=assistant_message,
                error=error,
            )
        except Exception:
            logger.exception("agent unexpected failure run_id=%s", run_id)
            self._maybe_destroy(handle.sandbox_id, request)
            raise

        status = str(final_state.get("status") or "FAILED")
        error = final_state.get("error")
        retry_count = int(final_state.get("retry_count", 0))

        # 只有 Verifier 通过、进入 AWAITING_APPROVAL 的 Run 才落 Git
        # checkpoint。一个批量 Run = 一个全局 commit；每个参与 thread 记录
        # 自己的 turnCount，前端据此渲染每条助手消息的回滚按钮。
        commit_hash: str | None = None
        baseline_commit: str | None = None
        commits: list[RunCommitRecord] = []
        if status == "AWAITING_APPROVAL":
            try:
                commit_hash = self._sandbox.git_commit(
                    handle.sandbox_id,
                    f"run: {run_id} - turn: {run_turn_count}",
                )
                commits = [
                    RunCommitRecord(
                        thread_id=thread_id,
                        turn_count=turn_counts[thread_id],
                        commit_hash=commit_hash,
                        run_id=run_id,
                    )
                    for thread_id in thread_ids
                ]
                try:
                    baseline_commit = self._sandbox.git_baseline_commit(
                        handle.sandbox_id
                    )
                except SandboxError:
                    logger.warning(
                        "failed to read git baseline run_id=%s", run_id, exc_info=True
                    )
            except SandboxError as exc:
                # 代码已经构建通过，但没有可回滚的 checkpoint 不能宣称成功：
                # 否则前端会生成一个无法 Undo 的“成功轮次”。
                logger.exception("git checkpoint failed run_id=%s", run_id)
                status = "FAILED"
                error = f"Git checkpoint 失败: {exc}"
                commit_hash = None
                commits = []

        assistant_message = self._assistant_message(
            instructions,
            status=status,
            error=error,
            retry_count=retry_count,
        )
        final_files = dict(final_state.get("current_files") or current_files)
        generated_files = final_state.get("generated_files") or {}
        generated_patch = str(final_state.get("generated_patch") or "")
        changed_files = sorted(generated_files) or list(target_files)
        response_state = {**final_state, "status": status, "error": error}
        response = self._to_response(
            run_id,
            handle.sandbox_id,
            handle.preview_url,
            response_state,
            reused_sandbox=reused_sandbox,
            assistant_message=assistant_message,
            commit_hash=commit_hash,
            turn_count=run_turn_count if commit_hash else 0,
            baseline_commit=baseline_commit,
            commits=commits,
        )
        self._record_threads(
            annotations,
            thread_ids,
            run_id=run_id,
            assistant_message=assistant_message,
            status=status,
            sandbox_id=handle.sandbox_id,
            files=final_files,
            patch=generated_patch,
            changed_files=changed_files,
            retry_count=retry_count,
            turn_counts=turn_counts if commit_hash else None,
            commit_hash=commit_hash,
            baseline_commit=baseline_commit,
        )
        self._maybe_destroy(handle.sandbox_id, request)
        return response

    def rollback(self, request: RollbackRunRequest) -> RollbackRunResponse:
        """把沙盒代码与单个 thread 的上下文回滚到指定成功轮次。

        顺序很重要：
          1. 先校验 thread checkpoint 与前端 commit 是否指向同一轮；
          2. 再执行 ``git reset --hard``，失败时绝不修改 thread 记忆；
          3. 最后恢复 ThreadMemoryStore 的 chatHistory / runs / files 快照，
             丢弃该轮之后的所有上下文。

        当前 Git 历史是全局线性的：回滚线程 B 也会丢弃 B 之后的 A/C 提交。
        M1.5 接受该限制；M2 用事件溯源重放未撤销标注的意图。
        """
        handle = self._sandbox.get(request.sandbox_id)
        checkpoint = self._threads.checkpoint(
            request.thread_id,
            run_id=request.run_id,
            turn_count=request.turn_count,
            commit_hash=request.commit_hash,
        )
        if checkpoint and checkpoint.get("commitHash") != request.commit_hash:
            raise RollbackTargetMismatchError(
                "回滚目标与 thread checkpoint 不一致："
                f"request={request.commit_hash}, "
                f"checkpoint={checkpoint.get('commitHash')}"
            )

        self._sandbox.git_reset_hard(request.sandbox_id, request.commit_hash)
        restored = self._threads.rollback_to_checkpoint(
            request.thread_id,
            run_id=request.run_id,
            turn_count=request.turn_count,
            commit_hash=request.commit_hash,
        )
        if restored is None:
            # python-ai 重启后内存 checkpoint 可能不存在；Git 回滚仍然有效，
            # 前端会按消息坐标自行截断本地历史。
            logger.warning(
                "thread checkpoint missing thread_id=%s run_id=%s commit=%s",
                request.thread_id,
                request.run_id,
                request.commit_hash,
            )

        revision = str(int(time.time() * 1000))
        separator = "&" if "?" in handle.preview_url else "?"
        preview_url = f"{handle.preview_url}{separator}v={revision}"
        return RollbackRunResponse(
            sandbox_id=request.sandbox_id,
            thread_id=request.thread_id,
            commit_hash=request.commit_hash,
            turn_count=(restored or checkpoint or {}).get("turnCount")
            or request.turn_count,
            run_id=(restored or checkpoint or {}).get("runId") or request.run_id,
            checkpoint_restored=restored is not None,
            preview_url=preview_url,
            preview_revision=revision,
        )

    # --------------------------------------------------------
    # 内部工具
    # --------------------------------------------------------
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
        """优先复用当前沙盒；失败时根据 thread 源码快照重建。

        多轮对话必须基于“上一次修改后的源码”继续，否则第二轮会把第一轮
        的颜色 / 文案覆盖回初始模板。因此不能每次都 create 新沙盒。
        """
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

        # 沙盒已销毁（浏览器刷新 / python-ai 重启）时，用服务端保存的最近
        # 源码快照重建，保证多轮对话不会丢失上一轮修改。
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
        """构造 Planner / Coder 需要的逐标注上下文。

        每个标注保留自己的目标文件、来源组件、选择器、验收标准与自然语言
        指令；Graph 会把整个列表一次性交给模型，而不是循环调用模型。
        ``threadContext`` 携带同一圈的历史对话与上一轮修改记录。
        """
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
            # 前端发送的 target.snippet 是圈选时的初始源码；多轮对话时沙盒
            # 已经包含上一轮修改。这里用沙盒当前源码覆盖片段，避免模型同时
            # 看到“旧片段 + 新 current_files”后把第一轮修改回滚掉。
            target_file = source.file if source and source.file else fallback_file
            current_source = (current_files or {}).get(target_file)
            nested_target = annotation_payload.get("target")
            if current_source and isinstance(nested_target, dict):
                snippet = nested_target.get("snippet")
                if isinstance(snippet, dict):
                    snippet["content"] = current_source
                    snippet["startLine"] = 1
                    snippet["endLine"] = len(current_source.splitlines())
            # 直接挂在 annotation JSON 上：DeepSeek 在 Planner / Coder 两个
            # 阶段都能看到同一 thread 的历史，不需要额外调用参数。
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
    ) -> ExecuteRunResponse:
        verify_state = state.get("verify_result") or {}
        verify_result = VerifyResultModel(**verify_state) if verify_state else None
        status = state.get("status")
        if status not in {"AWAITING_APPROVAL", "FAILED"}:
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
            error=state.get("error"),
        )

    def _maybe_destroy(self, sandbox_id: str, request: ExecuteRunRequest) -> None:
        if request.options.keep_sandbox:
            return
        try:
            self._sandbox.destroy(sandbox_id)
        except SandboxError:
            logger.warning("failed to destroy sandbox %s", sandbox_id, exc_info=True)
