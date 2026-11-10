"""FullstackGraph：契约确认后的多 Agent 代码生成状态机。

拓扑::

    START -> DBACoder -> BackendCoder -> FrontendCoder -> InfraCoder
          -> ConsistencyVerifier -> BuildVerifier -> SandboxRestart -> END

设计说明：

  - 每个节点只操作沙盒文件与本次 Run 的临时状态，不依赖 LangGraph
    Checkpoint：生成失败时重新点一次“开始生成全栈代码”即可幂等重跑；
  - DBACoder / BackendCoder / FrontendCoder / InfraCoder 使用确定性模板，
    保证离线、Mock 与真实模型环境行为一致；
  - DBACoder 生成 Sequelize ORM（生产走 DATABASE_URL，沙盒回退 SQLite）；
  - InfraCoder 生成 Dockerfile / render.yaml / .env.example 等一键部署配置；
  - ConsistencyVerifier 在全部代码生成后、进入沙盒构建前比对前端请求
    与后端路由；不一致时直接阻断，并把 Method / Path / Payload 差异报告
    交给 Coder 修复；
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
    generate_frontend_skeleton_bundle,
    generate_infra_bundle,
)
from ai_core.fullstack.consistency import (
    ConsistencyError,
    ConsistencyReport,
    ensure_consistency,
)
from ai_core.ports.sandbox import SandboxProvider

logger = logging.getLogger(__name__)

# 节点总数用于日志与 SSE 的 index/total；新增节点时只需改这一处。
GRAPH_NODE_TOTAL = 7
# 对话前置模式的初始前端骨架只执行三个节点。
FRONTEND_GRAPH_NODE_TOTAL = 3
FULLSTACK_NODE_ORDER = [
    "DBACoder",
    "BackendCoder",
    "FrontendCoder",
    "InfraCoder",
    "ConsistencyVerifier",
    "BuildVerifier",
    "SandboxRestart",
]
FRONTEND_NODE_ORDER = ["FrontendCoder", "BuildVerifier", "SandboxRestart"]

NODE_LABELS = {
    "DBACoder": "数据库工程师",
    "BackendCoder": "后端工程师",
    "FrontendCoder": "前端工程师",
    "InfraCoder": "部署配置工程师",
    "ConsistencyVerifier": "接口一致性检查",
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


class FullstackConsistencyError(FullstackGenerationError):
    """前后端接口契约不一致；携带结构化差异报告。"""

    code = "FULLSTACK_CONSISTENCY_FAILED"

    def __init__(self, report: ConsistencyReport) -> None:
        super().__init__(report.failure_message(), node="ConsistencyVerifier")
        self.report = report


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
    frontend_entry: str


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
        # 两套拓扑共用节点实现：
        #  - fullstack：契约确认后的完整生成；
        #  - frontend：新建项目契约确认后的前端骨架初始化。
        self._graphs = {
            "fullstack": self._build("fullstack"),
            "frontend": self._build("frontend"),
        }

    def run(self, initial_state: FullstackState) -> FullstackState:
        options = initial_state.get("options") or FullstackRunOptions()
        mode = "frontend" if options.mode == "frontend" else "fullstack"
        logger.info(
            "FullstackGraph 启动 mode=%s run_id=%s project_id=%s sandbox_id=%s",
            mode,
            initial_state.get("run_id"),
            initial_state.get("project_id"),
            initial_state.get("sandbox_id"),
        )
        result = self._graphs[mode].invoke(dict(initial_state))
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
    def _build(self, mode: str):
        builder = StateGraph(FullstackState)
        builder.add_node("FrontendCoder", self._frontend_coder)
        builder.add_node("BuildVerifier", self._build_verifier)
        builder.add_node("SandboxRestart", self._sandbox_restart)

        if mode == "frontend":
            # 契约确认后的前端初始化：只生成真实页面骨架并完成构建 / 重启。
            builder.add_edge(START, "FrontendCoder")
            builder.add_edge("FrontendCoder", "BuildVerifier")
            builder.add_edge("BuildVerifier", "SandboxRestart")
            builder.add_edge("SandboxRestart", END)
            return builder.compile()

        builder.add_node("DBACoder", self._dba_coder)
        builder.add_node("BackendCoder", self._backend_coder)
        builder.add_node("InfraCoder", self._infra_coder)
        builder.add_node("ConsistencyVerifier", self._consistency_verifier)
        builder.add_edge(START, "DBACoder")
        builder.add_edge("DBACoder", "BackendCoder")
        builder.add_edge("BackendCoder", "FrontendCoder")
        builder.add_edge("FrontendCoder", "InfraCoder")
        # 全部代码生成完毕、进入沙盒构建前，先做跨端接口一致性校验。
        builder.add_edge("InfraCoder", "ConsistencyVerifier")
        builder.add_edge("ConsistencyVerifier", "BuildVerifier")
        builder.add_edge("BuildVerifier", "SandboxRestart")
        builder.add_edge("SandboxRestart", END)
        return builder.compile()

    # --------------------------------------------------------
    # 节点：DBACoder
    # --------------------------------------------------------
    def _dba_coder(self, state: FullstackState) -> dict[str, Any]:
        contract = state["contract"]
        started = time.monotonic()
        self._node_started("DBACoder", state)
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
        self._node_started("BackendCoder", state)
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
        options = state.get("options") or FullstackRunOptions()
        started = time.monotonic()
        self._node_started("FrontendCoder", state)
        try:
            if options.mode == "frontend":
                # 初始骨架：按契约生成 src/App.jsx，不接后端 API。
                bundle = generate_frontend_skeleton_bundle(contract)
            else:
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
            result: dict[str, Any] = {
                "frontend_files": sorted(bundle.files),
                "files": files,
            }
            if options.mode == "frontend":
                # 把完整入口源码随图状态返回，完成事件可直接带回前端。
                # 事件里的 node.output 受 maxOutputChars 限制，若被截断，
                # 工作区就无法保存可继续编辑的真实源码。
                result["frontend_entry"] = bundle.files.get("src/App.jsx", "")
            return result
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
        self._node_started("InfraCoder", state)
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
    # 节点：ConsistencyVerifier
    # --------------------------------------------------------
    def _consistency_verifier(self, state: FullstackState) -> dict[str, Any]:
        """比对前端请求与后端路由；不一致时阻断进入沙盒。

        读取顺序即“最终事实”：
          1. 前端：``src/App.jsx`` 的 INTERACTIONS + ``src/fullstack-api.jsx``
             的 COLLECTION_PATH；
          2. 后端：``server/api-routes.json``（BackendCoder 生成）。
        校验只做静态解析，不启动服务，因此失败不会产生半启动的沙盒。
        """
        contract = state["contract"]
        sandbox_id = state["sandbox_id"]
        started = time.monotonic()
        self._node_started("ConsistencyVerifier", state)
        snapshot: dict[str, str] = {}
        for path in (
            "src/App.jsx",
            "src/fullstack-api.jsx",
            "server/api-routes.json",
            "server/index.js",
        ):
            content = self._read_optional(sandbox_id, path)
            if content is not None:
                snapshot[path] = content

        try:
            report = ensure_consistency(snapshot, contract)
        except ConsistencyError as exc:
            # 差异报告单独推送一次，前端可以直接渲染 issue 列表。
            self._emit(
                "fullstack.consistency.report",
                {"runId": state["run_id"], **exc.report.to_dict()},
            )
            self._node_failed("ConsistencyVerifier", state, exc)
            raise FullstackConsistencyError(exc.report) from exc
        except Exception as exc:
            self._node_failed("ConsistencyVerifier", state, exc)
            raise FullstackGenerationError(
                f"接口一致性检查失败: {exc}", node="ConsistencyVerifier"
            ) from exc

        self._emit(
            "fullstack.consistency.report",
            {"runId": state["run_id"], **report.to_dict()},
        )
        self._node_completed(
            "ConsistencyVerifier",
            state,
            report.summary(),
            [],
            started,
        )
        return {}

    # --------------------------------------------------------
    # 节点：BuildVerifier
    # --------------------------------------------------------
    def _build_verifier(self, state: FullstackState) -> dict[str, Any]:
        options = state.get("options") or FullstackRunOptions()
        started = time.monotonic()
        self._node_started("BuildVerifier", state)
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
        # 前端骨架没有 Node 后端，只能探测 Vite 根路径；完整全栈才探测
        # /api/health，确认 Vite 代理与后端同时就绪。
        ready_path = "/" if options.mode == "frontend" else "/api/health"
        started = time.monotonic()
        self._node_started("SandboxRestart", state)
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
                "healthPath": ready_path,
            },
        )
        try:
            self._sandbox.restart(sandbox_id, ready_path=ready_path)
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
                "healthPath": ready_path,
            },
        )
        summary = (
            "前端开发服务已就绪" if options.mode == "frontend" else "前后端双服务已就绪"
        )
        self._node_completed("SandboxRestart", state, summary, [], started)
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

    def _node_started(self, node: str, state: FullstackState) -> None:
        options = state.get("options") or FullstackRunOptions()
        frontend_only = options.mode == "frontend"
        order = FRONTEND_NODE_ORDER if frontend_only else FULLSTACK_NODE_ORDER
        total = FRONTEND_GRAPH_NODE_TOTAL if frontend_only else GRAPH_NODE_TOTAL
        index = order.index(node) + 1 if node in order else 0
        logger.info(
            "FullstackGraph 节点开始 [%d/%d] %s run_id=%s sandbox_id=%s",
            index,
            total,
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
                "total": total,
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
    "FullstackConsistencyError",
    "FullstackGenerationError",
    "FullstackGraphRunner",
    "FullstackState",
    "NODE_LABELS",
]
