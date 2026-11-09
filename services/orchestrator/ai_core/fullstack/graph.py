"""FullstackGraph：契约确认后的多 Agent 代码生成状态机。

拓扑::

    START -> DBACoder -> BackendCoder -> FrontendCoder -> InfraCoder
          -> BuildVerifier -> SandboxRestart -> END

设计说明：

  - 每个节点只操作沙盒文件与本次 Run 的临时状态，不依赖 LangGraph
    Checkpoint：生成失败时重新点一次“开始生成全栈代码”即可幂等重跑；
  - DBACoder / BackendCoder / FrontendCoder / InfraCoder 使用确定性模板，
    保证离线、Mock 与真实模型环境行为一致；
  - DBACoder 生成 Sequelize ORM（生产走 DATABASE_URL，沙盒回退 SQLite）；
  - InfraCoder 生成 Dockerfile / render.yaml / .env.example 等一键部署配置；
  - 构建校验与沙盒重启放在图内，失败会带节点名进入 ``fullstack.failed``，
    前端可准确定位是“代码生成失败”还是“服务启动失败”。
"""

from __future__ import annotations

import logging
import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ai_core.agent.events import EventSink, NullEventSink
from ai_core.domain.architect import ArchitectContract
from ai_core.domain.fullstack import FullstackRunOptions
from ai_core.fullstack.coders import (
    GeneratedBundle,
    generate_backend_bundle,
    generate_dba_bundle,
    generate_frontend_bundle,
    generate_infra_bundle,
)
from ai_core.ports.sandbox import SandboxProvider

logger = logging.getLogger(__name__)

# 节点总数用于日志与 SSE 的 index/total；新增节点时只需改这一处。
GRAPH_NODE_TOTAL = 6

NODE_LABELS = {
    "DBACoder": "数据库工程师",
    "BackendCoder": "后端工程师",
    "FrontendCoder": "前端工程师",
    "InfraCoder": "部署配置工程师",
    "BuildVerifier": "构建校验",
    "SandboxRestart": "沙盒重启",
}


class FullstackGenerationError(Exception):
    """全栈生成失败；携带失败节点，便于事件归因。"""

    code = "FULLSTACK_GENERATION_FAILED"

    def __init__(self, message: str, *, node: str | None = None) -> None:
        super().__init__(message)
        self.node = node


class FullstackBuildError(FullstackGenerationError):
    code = "FULLSTACK_BUILD_FAILED"

    def __init__(self, message: str) -> None:
        super().__init__(message, node="BuildVerifier")


class FullstackState(TypedDict, total=False):
    """FullstackGraph 的一次执行状态。"""

    run_id: str
    project_id: str
    sandbox_id: str
    preview_url: str
    contract: ArchitectContract
    options: FullstackRunOptions
    table: str
    collection_path: str
    files: list[str]
    dba_files: list[str]
    backend_files: list[str]
    frontend_files: list[str]
    infra_files: list[str]


class FullstackGraphRunner:
    """构建并执行全栈生成状态机。"""

    def __init__(
        self,
        sandbox: SandboxProvider,
        *,
        event_sink: EventSink | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._event_sink = event_sink or NullEventSink()
        self._graph = self._build()

    def run(self, initial_state: FullstackState) -> FullstackState:
        logger.info(
            "FullstackGraph 启动 run_id=%s project_id=%s sandbox_id=%s",
            initial_state.get("run_id"),
            initial_state.get("project_id"),
            initial_state.get("sandbox_id"),
        )
        result = self._graph.invoke(dict(initial_state))
        logger.info(
            "FullstackGraph 结束 run_id=%s changed_files=%d table=%s preview=%s",
            initial_state.get("run_id"),
            len(result.get("files") or []),
            result.get("table"),
            result.get("preview_url"),
        )
        return dict(result)  # type: ignore[return-value]

    # --------------------------------------------------------
    # 图构建
    # --------------------------------------------------------
    def _build(self):
        builder = StateGraph(FullstackState)
        builder.add_node("DBACoder", self._dba_coder)
        builder.add_node("BackendCoder", self._backend_coder)
        builder.add_node("FrontendCoder", self._frontend_coder)
        builder.add_node("InfraCoder", self._infra_coder)
        builder.add_node("BuildVerifier", self._build_verifier)
        builder.add_node("SandboxRestart", self._sandbox_restart)

        builder.add_edge(START, "DBACoder")
        builder.add_edge("DBACoder", "BackendCoder")
        builder.add_edge("BackendCoder", "FrontendCoder")
        builder.add_edge("FrontendCoder", "InfraCoder")
        builder.add_edge("InfraCoder", "BuildVerifier")
        builder.add_edge("BuildVerifier", "SandboxRestart")
        builder.add_edge("SandboxRestart", END)
        return builder.compile()

    # --------------------------------------------------------
    # 节点：DBACoder
    # --------------------------------------------------------
    def _dba_coder(self, state: FullstackState) -> dict[str, Any]:
        contract = state["contract"]
        started = time.monotonic()
        self._node_started("DBACoder", state, 1)
        try:
            bundle = generate_dba_bundle(contract)
            files = self._write_bundle(state, bundle)
            table = str(bundle.metadata.get("table") or "app_records")
            summary = bundle.summary
            self._emit_outputs("DBACoder", bundle, state)
            self._node_completed("DBACoder", state, summary, bundle.files, started)
            return {
                "dba_files": sorted(bundle.files),
                "table": table,
                "files": files,
            }
        except Exception as exc:
            self._node_failed("DBACoder", state, exc)
            raise FullstackGenerationError(
                f"DBACoder 生成失败: {exc}", node="DBACoder"
            ) from exc

    # --------------------------------------------------------
    # 节点：BackendCoder
    # --------------------------------------------------------
    def _backend_coder(self, state: FullstackState) -> dict[str, Any]:
        contract = state["contract"]
        started = time.monotonic()
        self._node_started("BackendCoder", state, 2)
        try:
            bundle = generate_backend_bundle(contract)
            files = self._write_bundle(state, bundle)
            collection = str(bundle.metadata.get("collectionPath") or "")
            self._emit_outputs("BackendCoder", bundle, state)
            self._node_completed(
                "BackendCoder", state, bundle.summary, bundle.files, started
            )
            return {
                "backend_files": sorted(bundle.files),
                "collection_path": collection,
                "files": files,
            }
        except Exception as exc:
            self._node_failed("BackendCoder", state, exc)
            raise FullstackGenerationError(
                f"BackendCoder 生成失败: {exc}", node="BackendCoder"
            ) from exc

    # --------------------------------------------------------
    # 节点：FrontendCoder
    # --------------------------------------------------------
    def _frontend_coder(self, state: FullstackState) -> dict[str, Any]:
        contract = state["contract"]
        sandbox_id = state["sandbox_id"]
        started = time.monotonic()
        self._node_started("FrontendCoder", state, 3)
        try:
            bundle = generate_frontend_bundle(
                contract,
                existing_main=self._read_optional(sandbox_id, "src/main.jsx"),
                existing_vite=self._read_optional(sandbox_id, "vite.config.js"),
                existing_package=self._read_optional(sandbox_id, "package.json"),
            )
            files = self._write_bundle(state, bundle)
            self._emit_outputs("FrontendCoder", bundle, state)
            self._node_completed(
                "FrontendCoder", state, bundle.summary, bundle.files, started
            )
            return {
                "frontend_files": sorted(bundle.files),
                "files": files,
            }
        except Exception as exc:
            self._node_failed("FrontendCoder", state, exc)
            raise FullstackGenerationError(
                f"FrontendCoder 生成失败: {exc}", node="FrontendCoder"
            ) from exc

    # --------------------------------------------------------
    # 节点：InfraCoder
    # --------------------------------------------------------
    def _infra_coder(self, state: FullstackState) -> dict[str, Any]:
        contract = state["contract"]
        started = time.monotonic()
        self._node_started("InfraCoder", state, 4)
        try:
            bundle = generate_infra_bundle(contract)
            files = self._write_bundle(state, bundle)
            self._emit_outputs("InfraCoder", bundle, state)
            self._node_completed(
                "InfraCoder", state, bundle.summary, bundle.files, started
            )
            return {
                "infra_files": sorted(bundle.files),
                "files": files,
            }
        except Exception as exc:
            self._node_failed("InfraCoder", state, exc)
            raise FullstackGenerationError(
                f"InfraCoder 生成失败: {exc}", node="InfraCoder"
            ) from exc

    # --------------------------------------------------------
    # 节点：BuildVerifier
    # --------------------------------------------------------
    def _build_verifier(self, state: FullstackState) -> dict[str, Any]:
        options = state.get("options") or FullstackRunOptions()
        started = time.monotonic()
        self._node_started("BuildVerifier", state, 5)
        if not options.verify_build:
            self._node_completed(
                "BuildVerifier",
                state,
                "已跳过构建校验（options.verifyBuild=false）",
                [],
                started,
            )
            return {}
        result = self._sandbox.verify(state["sandbox_id"], "build")
        if not result.success:
            logs = result.logs or "构建失败且没有日志"
            self._emit(
                "fullstack.node.output",
                {
                    "runId": state["run_id"],
                    "node": "BuildVerifier",
                    "path": "npm run build",
                    "language": "text",
                    "content": logs[-4000:],
                    "truncated": result.logs_truncated,
                },
            )
            self._node_failed(
                "BuildVerifier",
                state,
                FullstackBuildError(f"npm run build 退出码 {result.exit_code}"),
            )
            raise FullstackBuildError("生成代码未通过 npm run build：\n" + logs[-4000:])
        self._node_completed(
            "BuildVerifier",
            state,
            f"npm run build 通过（{result.duration_ms}ms）",
            [],
            started,
        )
        return {}

    # --------------------------------------------------------
    # 节点：SandboxRestart
    # --------------------------------------------------------
    def _sandbox_restart(self, state: FullstackState) -> dict[str, Any]:
        options = state.get("options") or FullstackRunOptions()
        sandbox_id = state["sandbox_id"]
        started = time.monotonic()
        self._node_started("SandboxRestart", state, 6)
        if not options.restart_sandbox:
            self._node_completed(
                "SandboxRestart",
                state,
                "已跳过重启（options.restartSandbox=false）",
                [],
                started,
            )
            return {}
        self._emit(
            "fullstack.sandbox.restarting",
            {
                "runId": state["run_id"],
                "sandboxId": sandbox_id,
                "healthPath": "/api/health",
            },
        )
        try:
            self._sandbox.restart(sandbox_id, ready_path="/api/health")
            handle = self._sandbox.get(sandbox_id)
        except Exception as exc:
            self._node_failed("SandboxRestart", state, exc)
            raise FullstackGenerationError(
                f"沙盒重启失败: {exc}", node="SandboxRestart"
            ) from exc
        self._emit(
            "fullstack.sandbox.ready",
            {
                "runId": state["run_id"],
                "sandboxId": sandbox_id,
                "previewUrl": handle.preview_url,
                "healthPath": "/api/health",
            },
        )
        self._node_completed("SandboxRestart", state, "前后端双服务已就绪", [], started)
        return {"preview_url": handle.preview_url}

    # --------------------------------------------------------
    # 文件 / 事件工具
    # --------------------------------------------------------
    def _write_bundle(
        self, state: FullstackState, bundle: GeneratedBundle
    ) -> list[str]:
        changed = self._sandbox.apply_patch(state["sandbox_id"], files=bundle.files)
        merged = set(state.get("files") or [])
        merged.update(changed)
        files = sorted(merged)
        state["files"] = files
        return files

    def _read_optional(self, sandbox_id: str, path: str) -> str | None:
        try:
            return self._sandbox.read_file(sandbox_id, path)
        except Exception:
            return None

    def _node_started(self, node: str, state: FullstackState, index: int) -> None:
        logger.info(
            "FullstackGraph 节点开始 [%d/%d] %s run_id=%s sandbox_id=%s",
            index,
            GRAPH_NODE_TOTAL,
            node,
            state.get("run_id"),
            state.get("sandbox_id"),
        )
        self._emit(
            "fullstack.node.started",
            {
                "runId": state["run_id"],
                "node": node,
                "label": NODE_LABELS.get(node, node),
                "index": index,
                "total": GRAPH_NODE_TOTAL,
            },
        )

    def _node_completed(
        self,
        node: str,
        state: FullstackState,
        summary: str,
        files: dict[str, str] | list[str],
        started: float,
    ) -> None:
        paths = sorted(files) if isinstance(files, dict) else sorted(files)
        logger.info(
            "FullstackGraph 节点完成 %s run_id=%s files=%s duration_ms=%d",
            node,
            state.get("run_id"),
            ",".join(paths),
            int((time.monotonic() - started) * 1000),
        )
        self._emit(
            "fullstack.node.completed",
            {
                "runId": state["run_id"],
                "node": node,
                "label": NODE_LABELS.get(node, node),
                "summary": summary,
                "files": paths,
                "durationMs": int((time.monotonic() - started) * 1000),
            },
        )

    def _node_failed(self, node: str, state: FullstackState, exc: Exception) -> None:
        logger.warning(
            "FullstackGraph 节点失败 %s run_id=%s error=%s",
            node,
            state.get("run_id"),
            exc,
        )
        self._emit(
            "fullstack.node.failed",
            {
                "runId": state["run_id"],
                "node": node,
                "label": NODE_LABELS.get(node, node),
                "error": str(exc),
            },
        )

    def _emit_outputs(
        self,
        node: str,
        bundle: GeneratedBundle,
        state: FullstackState,
    ) -> None:
        options = state.get("options") or FullstackRunOptions()
        limit = max(500, int(options.max_output_chars))
        language_by_suffix = {
            ".sql": "sql",
            ".mjs": "javascript",
            ".jsx": "jsx",
            ".json": "json",
            ".md": "markdown",
            ".yaml": "yaml",
            ".yml": "yaml",
        }
        for path, content in sorted(bundle.files.items()):
            truncated = len(content) > limit
            suffix = path[path.rfind(".") :] if "." in path else ""
            language = (
                "dockerfile"
                if path.rsplit("/", 1)[-1] == "Dockerfile"
                else language_by_suffix.get(suffix, "text")
            )
            self._emit(
                "fullstack.node.output",
                {
                    "runId": state["run_id"],
                    "node": node,
                    "path": path,
                    "language": language,
                    "content": content[:limit],
                    "truncated": truncated,
                    "bytes": len(content.encode("utf-8")),
                },
            )

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self._event_sink.emit(event_type, payload)


__all__ = [
    "GRAPH_NODE_TOTAL",
    "FullstackBuildError",
    "FullstackGenerationError",
    "FullstackGraphRunner",
    "FullstackState",
    "NODE_LABELS",
]
