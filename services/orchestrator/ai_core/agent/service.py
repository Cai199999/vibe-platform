"""Run 执行服务：创建沙盒、装配 Graph、汇总执行结果。"""

from __future__ import annotations

import logging
import uuid
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
    ExecuteRunRequest,
    ExecuteRunResponse,
    VerifyResultModel,
)
from ai_core.config import Settings, get_settings
from ai_core.sandbox import get_sandbox_provider
from ai_core.sandbox.base import SandboxError, SandboxProvider

logger = logging.getLogger(__name__)

DEFAULT_TARGET_FILE = "src/App.jsx"


class FullStackNotSupportedError(Exception):
    """M1 尚未实现全栈生成。"""


class TargetFileNotFoundError(Exception):
    """标注指向的文件不存在。"""


class RunExecutionService:
    """M1 同步执行入口。

    M2 会改为：先落 checkpoint -> 返回 202 -> 后台执行 + 事件流。
    """

    def __init__(
        self,
        sandbox: SandboxProvider | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._sandbox = sandbox or get_sandbox_provider()
        self._settings = settings or get_settings()

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

        # 先构造 LLM，避免密钥缺失时已经创建了沙盒。
        llm = build_agent_llm(request, self._settings)

        handle = self._sandbox.create(
            project_id=request.project_id,
            files=request.files,
            template="react-vite",
        )

        # 批量标注可能指向不同文件；一次性读取，交给同一个 Agent 综合处理。
        current_files: dict[str, str] = {}
        try:
            for target_file in target_files:
                current_files[target_file] = self._sandbox.read_file(
                    handle.sandbox_id, target_file
                )
        except SandboxError as exc:
            self._sandbox.destroy(handle.sandbox_id)
            raise TargetFileNotFoundError(
                f"读取目标文件失败 {list(target_files)}: {exc}"
            ) from exc

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

        annotation_context = self._build_annotation_context(request, target_files)
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
            self._maybe_destroy(handle.sandbox_id, request)
            return ExecuteRunResponse(
                run_id=run_id,
                status="FAILED",
                sandbox_id=handle.sandbox_id,
                preview_url=handle.preview_url,
                error=f"模型执行失败: {exc}",
            )
        except SandboxError as exc:
            logger.exception("agent sandbox failed run_id=%s", run_id)
            self._maybe_destroy(handle.sandbox_id, request)
            return ExecuteRunResponse(
                run_id=run_id,
                status="FAILED",
                sandbox_id=handle.sandbox_id,
                preview_url=handle.preview_url,
                error=f"沙盒执行失败: {exc}",
            )
        except Exception:
            logger.exception("agent unexpected failure run_id=%s", run_id)
            self._maybe_destroy(handle.sandbox_id, request)
            raise

        response = self._to_response(
            run_id, handle.sandbox_id, handle.preview_url, final_state
        )
        self._maybe_destroy(handle.sandbox_id, request)
        return response

    # --------------------------------------------------------
    # 内部工具
    # --------------------------------------------------------
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
    ) -> dict[str, Any]:
        """构造 Planner / Coder 需要的逐标注上下文。

        每个标注保留自己的目标文件、来源组件、选择器、验收标准与自然语言
        指令；Graph 会把整个列表一次性交给模型，而不是循环调用模型。
        """
        contexts: list[dict[str, Any]] = []
        fallback_file = target_files[0] if target_files else DEFAULT_TARGET_FILE
        for index, annotation in enumerate(request.effective_annotations, start=1):
            intent = annotation.intent
            ui = intent.ui if intent else None
            target = annotation.target
            source = target.source if target else None
            contexts.append(
                {
                    "index": index,
                    "annotation_id": annotation.annotation_id,
                    "annotation": annotation.model_dump(
                        by_alias=True, exclude_none=True
                    ),
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
            plan=state.get("plan", ""),
            generated_patch=state.get("generated_patch", ""),
            generated_files=state.get("generated_files", {}),
            verify_result=verify_result,
            retry_count=int(state.get("retry_count", 0)),
            messages=state.get("messages", []),
            error=state.get("error"),
        )

    def _maybe_destroy(self, sandbox_id: str, request: ExecuteRunRequest) -> None:
        if request.options.keep_sandbox:
            return
        try:
            self._sandbox.destroy(sandbox_id)
        except SandboxError:
            logger.warning("failed to destroy sandbox %s", sandbox_id, exc_info=True)
