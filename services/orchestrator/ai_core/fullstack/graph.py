"""FullstackGraph：契约确认后的多 Agent 代码生成状态机。

拓扑::

    START -> DBACoder -> BackendCoder -> FrontendCoder -> InfraCoder
          -> ConsistencyVerifier -[通过]-> BuildVerifier -> SandboxRestart -> END
                 ^                     |
                 |                     +-[缺失路由]-> BackendCoder（补齐后重检）
                 +-----------------------------------+

设计说明：

  - 每个节点只操作沙盒文件与本次 Run 的临时状态，不依赖 LangGraph
    Checkpoint：生成失败时重新点一次“开始生成全栈代码”即可幂等重跑；
  - DBACoder / BackendCoder / FrontendCoder / InfraCoder 使用确定性模板，
    保证离线、Mock 与真实模型环境行为一致；
  - BackendCoder 为契约中的每个业务动作注册独立路由，通用 CRUD 不能替代
    抓取 / 改写 / 投递 / 对话等语义端点；
  - ConsistencyVerifier 在构建前比对前端请求与后端实际路由；缺失路由时
    生成 FixAction 回传 BackendCoder 自动补齐，最多重试 2 次，仍不一致才
    向用户报错。它只做业务语义对齐，绝不建议前端改到无关路径；
  - 构建校验与沙盒重启放在图内，失败会带节点名进入 ``fullstack.failed``，
    前端可准确定位是“代码生成失败”还是“服务启动失败”。
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ai_core.agent.events import EventSink, NullEventSink
from ai_core.config import get_settings
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
    ConsistencyIssue,
    ConsistencyReport,
    verify_consistency,
)
from ai_core.fullstack.schema_mcp import verify_schema_with_mcp
from ai_core.ports.sandbox import SandboxProvider

logger = logging.getLogger(__name__)

# 节点总数用于日志与 SSE 的 index/total；新增节点时只需改这一处。
GRAPH_NODE_TOTAL = 7
# 一致性检查失败后，最多把缺失路由回传 BackendCoder 自愈 2 次。
MAX_CONSISTENCY_REPAIRS = 2
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
# Supervisor 激活的专属专家子图：只跑与目标 Agent 相关的节点。
BACKEND_NODE_ORDER = ["DBACoder", "BackendCoder", "BuildVerifier", "SandboxRestart"]
DBA_NODE_ORDER = ["DBACoder", "SandboxRestart"]
MODE_NODE_ORDERS = {
    "fullstack": FULLSTACK_NODE_ORDER,
    "frontend": FRONTEND_NODE_ORDER,
    "backend": BACKEND_NODE_ORDER,
    "dba": DBA_NODE_ORDER,
}

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
    # ---- 一致性自愈状态 ----
    consistency_fix_routes: list[dict[str, str]]
    consistency_applied_routes: list[dict[str, str]]
    consistency_repair_count: int
    consistency_ok: bool
    needs_backend_repair: bool


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
            mode: self._build(mode)
            for mode in ("fullstack", "frontend", "backend", "dba")
        }

    def run(self, initial_state: FullstackState) -> FullstackState:
        options = initial_state.get("options") or FullstackRunOptions()
        mode = options.mode if options.mode in MODE_NODE_ORDERS else "fullstack"
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

        if mode == "dba":
            # 只生成数据库层：DDL / Sequelize 模型 / 迁移；不碰前端与后端路由。
            builder.add_node("DBACoder", self._dba_coder)
            builder.add_edge(START, "DBACoder")
            builder.add_edge("DBACoder", "SandboxRestart")
            builder.add_edge("SandboxRestart", END)
            return builder.compile()

        if mode == "backend":
            # 后端专家：DBA 提供模型与迁移，BackendCoder 注册业务路由，
            # 构建校验后重启服务；FrontendCoder / InfraCoder 完全不运行。
            builder.add_node("DBACoder", self._dba_coder)
            builder.add_node("BackendCoder", self._backend_coder)
            builder.add_edge(START, "DBACoder")
            builder.add_edge("DBACoder", "BackendCoder")
            builder.add_edge("BackendCoder", "BuildVerifier")
            builder.add_edge("BuildVerifier", "SandboxRestart")
            builder.add_edge("SandboxRestart", END)
            return builder.compile()

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
        # 正常生成：BackendCoder -> FrontendCoder；
        # 一致性自愈：BackendCoder 补齐路由后直接回到 ConsistencyVerifier。
        builder.add_conditional_edges(
            "BackendCoder",
            self._route_after_backend_coder,
            {
                "frontend": "FrontendCoder",
                "consistency": "ConsistencyVerifier",
            },
        )
        builder.add_edge("FrontendCoder", "InfraCoder")
        # 全部代码生成完毕、进入沙盒构建前，先做跨端接口一致性校验。
        builder.add_edge("InfraCoder", "ConsistencyVerifier")
        # 通过 -> 构建；缺失路由 -> BackendCoder 自愈；重试耗尽在节点内抛错。
        builder.add_conditional_edges(
            "ConsistencyVerifier",
            self._route_after_consistency_verifier,
            {
                "build": "BuildVerifier",
                "repair": "BackendCoder",
            },
        )
        builder.add_edge("BuildVerifier", "SandboxRestart")
        builder.add_edge("SandboxRestart", END)
        return builder.compile()

    @staticmethod
    def _route_after_backend_coder(state: FullstackState) -> str:
        """修复模式只回一致性检查；正常模式继续生成前端。"""
        return "consistency" if state.get("needs_backend_repair") else "frontend"

    @staticmethod
    def _route_after_consistency_verifier(state: FullstackState) -> str:
        """一致性通过才允许构建，否则回到 BackendCoder 补齐缺失路由。"""
        return "build" if state.get("consistency_ok") else "repair"

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
            # 正常生成时 fix_routes 为空；自愈时把缺失路由合并进契约路由，
            # 重新生成 server/index.js 与 api-routes.json。
            fix_routes = list(state.get("consistency_fix_routes") or [])
            applied_routes = list(state.get("consistency_applied_routes") or [])
            for route in fix_routes:
                if route not in applied_routes:
                    applied_routes.append(route)
            bundle = generate_backend_bundle(contract, extra_routes=applied_routes)
            files = self._write_bundle(state, bundle)
            collection = str(bundle.metadata.get("collectionPath") or "")
            summary = bundle.summary
            if fix_routes:
                summary = f"一致性自愈：补齐 {len(fix_routes)} 条缺失路由；" + summary
            self._emit_outputs("BackendCoder", bundle, state)
            self._node_completed("BackendCoder", state, summary, bundle.files, started)
            return {
                "backend_files": sorted(bundle.files),
                "collection_path": collection,
                "files": files,
                "consistency_fix_routes": [],
                "consistency_applied_routes": applied_routes,
                "needs_backend_repair": False,
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
        """比对前端请求与后端路由；缺失路由时驱动 BackendCoder 自愈。

        读取顺序即“最终事实”：
          1. 前端：``src/App.jsx`` 的 INTERACTIONS + ``src/fullstack-api.jsx``
             的 COLLECTION_PATH；
          2. 后端：``server/api-routes.json``（BackendCoder 生成，含全部
             实际注册的业务路由）。
        校验只做静态解析，不启动服务，因此失败不会产生半启动的沙盒。
        缺失路由最多回传 BackendCoder 补齐 2 次；通过后才允许进入构建。
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
            report = verify_consistency(snapshot, contract)
            # 维度二：静态路由比对之外，再通过内置 SQLite MCP 把生成的
            # DDL 真正执行到临时数据库，查询 table / columns 是否存在。
            # 这一步能抓住“路由齐全但表根本建不起来”的产物。
            schema_check = verify_schema_with_mcp(snapshot, contract)
            checked_files = set(report.checked_files)
            if not schema_check.source.startswith("generated:"):
                checked_files.add(schema_check.source)
            report = replace(
                report,
                schema=schema_check.to_dict(),
                checked_files=tuple(sorted(checked_files)),
            )
            if not schema_check.ok and get_settings().schema_mcp_strict:
                report = replace(
                    report,
                    issues=report.issues
                    + (
                        ConsistencyIssue(
                            kind="schema",
                            component="DBACoder",
                            method="",
                            path="",
                            message=schema_check.summary(),
                            suggestion=(
                                "修正契约 DDL 或 DBACoder 生成的迁移文件，"
                                "确保目标表与全部字段可真实创建；"
                                "禁止绕过迁移直接上线"
                            ),
                            fix_action="FIX_SCHEMA",
                        ),
                    ),
                )
        except Exception as exc:
            self._node_failed("ConsistencyVerifier", state, exc)
            raise FullstackGenerationError(
                f"接口一致性检查失败: {exc}", node="ConsistencyVerifier"
            ) from exc

        # 差异报告单独推送一次，前端可以直接渲染 issue 列表与 MCP 结构结果。
        self._emit(
            "fullstack.consistency.report",
            {"runId": state["run_id"], **report.to_dict()},
        )
        if report.ok:
            self._node_completed(
                "ConsistencyVerifier",
                state,
                report.summary(),
                [],
                started,
            )
            return {"consistency_ok": True, "needs_backend_repair": False}

        repair_count = int(state.get("consistency_repair_count") or 0)
        missing_routes = list(report.missing_routes())
        if missing_routes and repair_count < MAX_CONSISTENCY_REPAIRS:
            # 自愈：把缺失路由作为 FixAction 回传 BackendCoder，补齐后重检。
            # 不向用户抛错，也不允许把前端请求改到无关的现有路径。
            next_count = repair_count + 1
            logger.warning(
                "ConsistencyVerifier 缺失 %d 条路由，转 BackendCoder 自愈 [%d/%d] run_id=%s",
                len(missing_routes),
                next_count,
                MAX_CONSISTENCY_REPAIRS,
                state.get("run_id"),
            )
            self._emit(
                "fullstack.consistency.repair",
                {
                    "runId": state["run_id"],
                    "attempt": next_count,
                    "maxAttempts": MAX_CONSISTENCY_REPAIRS,
                    "node": "BackendCoder",
                    "missingRoutes": missing_routes,
                    "report": report.to_dict(),
                },
            )
            self._node_completed(
                "ConsistencyVerifier",
                state,
                (
                    f"发现 {len(missing_routes)} 条缺失路由，转交 BackendCoder "
                    f"补齐（第 {next_count}/{MAX_CONSISTENCY_REPAIRS} 次）"
                ),
                [],
                started,
            )
            return {
                "consistency_ok": False,
                "needs_backend_repair": True,
                "consistency_fix_routes": missing_routes,
                "consistency_repair_count": next_count,
            }

        # 重试耗尽，或差异无法通过补齐路由解决（如 Payload 字段冲突）。
        self._node_failed("ConsistencyVerifier", state, ConsistencyError(report))
        raise FullstackConsistencyError(report)

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
        # 后端依赖可能随契约变化（例如上传需要 multer）。构建前先安装，
        # 否则沙盒重启时 server/index.js 会因缺少模块直接退出。
        if options.mode != "frontend":
            install = self._sandbox.verify(state["sandbox_id"], "install")
            if not install.success:
                logs = install.logs or "依赖安装失败且没有日志"
                self._emit(
                    "fullstack.node.output",
                    {
                        "runId": state["run_id"],
                        "node": "BuildVerifier",
                        "path": "npm install",
                        "language": "text",
                        "content": logs[-4000:],
                        "truncated": install.logs_truncated,
                    },
                )
                self._node_failed(
                    "BuildVerifier",
                    state,
                    FullstackBuildError(f"npm install 退出码 {install.exit_code}"),
                )
                raise FullstackBuildError("生成代码依赖安装失败：\n" + logs)
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
        # 前端骨架 / 纯 DBA 子图没有改动 Node 后端，只能探测 Vite 根路径；
        # 完整全栈与后端子图才探测 /api/health，确认代理与后端同时就绪。
        ready_path = "/api/health" if options.mode in {"fullstack", "backend"} else "/"
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
        summary = {
            "frontend": "前端开发服务已就绪",
            "dba": "数据库层已生成，开发服务已就绪",
            "backend": "后端服务已就绪",
        }.get(options.mode, "前后端双服务已就绪")
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
        mode = options.mode if options.mode in MODE_NODE_ORDERS else "fullstack"
        order = MODE_NODE_ORDERS[mode]
        total = len(order)
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
    "MAX_CONSISTENCY_REPAIRS",
    "FullstackBuildError",
    "FullstackConsistencyError",
    "FullstackGenerationError",
    "FullstackGraphRunner",
    "FullstackState",
    "NODE_LABELS",
]
