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
  - DBACoder / BackendCoder / FrontendCoder / InfraCoder 默认使用确定性
    模板，保证离线、Mock 与真实模型环境行为一致；
  - BackendCoder 被 Applier 拦截为 CODE_DEFECT 时，把完整诊断反馈给真实
    LLM 修复 ``server/index.js``；LLM 产物仍需再次通过 Applier AST 门禁；
  - BackendCoder 为契约中的每个业务动作注册独立路由，通用 CRUD 不能替代
    抓取 / 改写 / 投递 / 对话等语义端点；
  - ConsistencyVerifier 在构建前比对前端请求与后端实际路由；缺失路由时
    生成 FixAction 回传 BackendCoder 自动补齐，最多重试 2 次，仍不一致才
    向用户报错。它只做业务语义对齐，绝不建议前端改到无关路径；
  - 构建校验与沙盒重启放在图内，失败会带节点名进入 ``fullstack.failed``，
    前端可准确定位是“代码生成失败”还是“服务启动失败”。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import replace
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ai_core.a2a import A2A_LOG, build_feedback
from ai_core.agent.events import EventSink, NullEventSink
from ai_core.codegen.visual import (
    VisualCodegenError,
    compile_visual_document,
    generate_visual_fallback,
)
from ai_core.codegen.bindings import apply_contract_interactions
from ai_core.codegen.stack_dependencies import (
    contract_stack_text,
    resolve_stack_dependencies,
)
from ai_core.codegen.visual_extensions import (
    extension_dependencies,
    load_visual_extensions,
)
from ai_core.codegen.visual_gate import VisualCodegenGateUnavailable
from ai_core.config import get_settings
from ai_core.domain.architect import ArchitectContract
from ai_core.domain.fullstack import FullstackRunOptions
from ai_core.domain.visual_document import VisualDocument
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
from ai_core.fullstack.applier import Applier, ApplierSafetyBlock
from ai_core.fullstack.contract_audit import (
    DEFECT_CODE,
    DEFECT_CONTRACT,
    ContractAudit,
    audit_contract,
)
from ai_core.fullstack.diagnostics import DIAG_AST_UNAVAILABLE
from ai_core.fullstack.llm_backend import (
    LLMBackendCoder,
    LLMBackendRepairUnavailable,
)
from ai_core.fullstack.mysql_schema import verify_mysql_migration
from ai_core.fullstack.quality import (
    L1_WARNING_TEXT,
    QualityPolicy,
    quality_policy,
)
from ai_core.fullstack.schema_mcp import verify_schema_with_mcp
from ai_core.observability.fullstack_metrics import FULLSTACK_METRICS
from ai_core.ports.sandbox import SandboxProvider

logger = logging.getLogger(__name__)


def _merge_package_manifests(base: str, override: str) -> str:
    """保留全栈基座的 scripts / 名称，同时合并 CodeGen 派生的依赖。

    Visual CodeGen 的 package.json 已包含 techStack 解析出的依赖
    （例如第三方 UI 库 / 图表库）；全栈模式的 base bundle 只负责 dev 脚本与
    模板依赖。两者必须合并，不能由任意一方整体覆盖。
    """
    try:
        base_obj = json.loads(base)
        override_obj = json.loads(override)
    except (TypeError, ValueError):
        return base
    if not isinstance(base_obj, dict) or not isinstance(override_obj, dict):
        return base
    merged = dict(base_obj)
    for key in ("dependencies", "devDependencies"):
        values = dict(base_obj.get(key) or {})
        values.update(override_obj.get(key) or {})
        merged[key] = values
    scripts = dict(override_obj.get("scripts") or {})
    scripts.update(base_obj.get("scripts") or {})
    if scripts:
        merged["scripts"] = scripts
    return json.dumps(merged, ensure_ascii=False, indent=2) + "\n"

# 节点总数用于日志与 SSE 的 index/total；新增节点时只需改这一处。
GRAPH_NODE_TOTAL = 7
# 一致性检查失败后，最多把缺失路由回传 BackendCoder 自愈 2 次。
MAX_CONSISTENCY_REPAIRS = 2
# ORM 模型与迁移 DDL 字段错位时，最多让 DBACoder 按 DDL 重写 2 次。
MAX_ORM_REPAIRS = 2
# JOIN 别名 / include 关系与契约不一致时，最多让 BackendCoder 重建 2 次。
MAX_JOIN_REPAIRS = 2
# Applier AST 门禁拦截后，最多让 BackendCoder 重新生成 2 次。
MAX_APPLIER_REPAIRS = 2
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


def _diagnostic_fingerprint(messages: Any) -> str:
    """诊断指纹：同一组问题重复出现时，需要结合候选产物判断是否收敛。"""
    joined = "\n".join(
        str(item).strip() for item in (messages or ()) if str(item).strip()
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _candidate_fingerprint(files: Any) -> str:
    """被拦截候选产物的指纹；模型改了代码但诊断相同，仍允许再试一次。"""
    if not isinstance(files, dict) or not files:
        return ""
    joined = "\n".join(
        f"{path}:{content}"
        for path, content in sorted(files.items())
        if isinstance(content, str)
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
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
    """全栈生成失败；携带失败节点，便于事件归因。

    ``failure_kind`` 是给用户看的归因结论：
      - ``CODE``：契约没问题，生成代码违反契约，可自愈或重试；
      - ``CONTRACT``：契约自相矛盾，重试只会重复失败，必须重新澄清；
      - ``ENVIRONMENT``：构建 / 校验器环境不可用。
    """

    code = "FULLSTACK_GENERATION_FAILED"
    failure_kind = "CODE"
    defect_kind = DEFECT_CODE
    needs_clarification = False
    clarification: dict[str, Any] | None = None

    def __init__(self, message: str, *, node: str | None = None) -> None:
        super().__init__(message)
        self.node = node


class FullstackBuildError(FullstackGenerationError):
    code = "FULLSTACK_BUILD_FAILED"
    failure_kind = "ENVIRONMENT"
    # 构建 / 校验器环境问题不属于契约或代码归因。
    defect_kind = None

    def __init__(self, message: str) -> None:
        super().__init__(message, node="BuildVerifier")


class FullstackConsistencyError(FullstackGenerationError):
    """前后端接口契约不一致；携带结构化差异报告。"""

    code = "FULLSTACK_CONSISTENCY_FAILED"

    def __init__(self, report: ConsistencyReport) -> None:
        super().__init__(report.failure_message(), node="ConsistencyVerifier")
        self.report = report


class FullstackQualityEnvironmentError(FullstackGenerationError):
    """L3 真实 MySQL 验收不可用：属于环境问题，不能误判为代码缺陷。"""

    code = "FULLSTACK_L3_MYSQL_UNAVAILABLE"
    failure_kind = "ENVIRONMENT"
    defect_kind = None

    def __init__(self, message: str) -> None:
        super().__init__(message, node="ConsistencyVerifier")


class FullstackContractContradictionError(FullstackGenerationError):
    """契约自相矛盾：停止重试，要求用户回到 Architect 修改契约。"""

    code = "CONTRACT_REVISION_REQUIRED"
    failure_kind = "CONTRACT"
    defect_kind = DEFECT_CONTRACT
    needs_clarification = True

    def __init__(
        self, audit: ContractAudit, *, node: str = "ConsistencyVerifier"
    ) -> None:
        super().__init__(audit.summary(), node=node)
        self.audit = audit
        self.clarification = audit.payload()


class FullstackState(TypedDict, total=False):
    """FullstackGraph 的一次执行状态。"""

    run_id: str
    project_id: str
    sandbox_id: str
    preview_url: str
    contract: ArchitectContract
    options: FullstackRunOptions
    # 离线优先链路：冻结的 VisualDocument 是前端唯一事实源。
    visual_document: VisualDocument | None
    visual_fallback_applied: bool
    visual_node_count: int
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
    consistency_dba_repair_count: int
    consistency_join_repair_count: int
    consistency_ok: bool
    needs_backend_repair: bool
    needs_dba_repair: bool
    # Applier AST 门禁：拦截后先分类，再决定重生成 Backend 还是要求改契约。
    applier_retry_count: int
    needs_backend_retry: bool
    # 缺陷来源是 server/db.js（持久化层）时，BackendCoder 无权修复，
    # 必须回到 DBACoder 重建 ORM / 迁移后再绑定 HTTP 层。
    applier_dba_retry: bool
    # Applier / 一致性诊断原文：下一次 BackendCoder 把这段错误反馈给 LLM。
    applier_feedback: list[str]
    applier_feedback_attempt: int
    # 上一次 Applier 诊断指纹：连续两次完全一致说明模型没有改到根因，
    # 再重试也不会收敛，直接升级给 Architect 复核契约。
    applier_fingerprint: str
    # 上一次被拦截候选产物的指纹；诊断相同但产物变了，仍允许再迭代一次，
    # 避免把“模型正在逐步修”误判成死循环。
    applier_candidate_fingerprint: str
    # 上一轮被 Applier 拦截的候选产物。下一轮 LLM 修复以此为基线，
    # 避免每轮都从模板重置、反复引入同类缺陷。
    applier_previous_files: dict[str, str]
    llm_repair_count: int
    # ---- 风险自适应门禁 ----
    quality_tier: str
    # L1 不阻塞的校验结论：随 fullstack.completed 返回给前端展示风险徽标。
    warnings: list[str]
    build_retry_count: int
    # L3 真实 MySQL 迁移验收结果（MysqlMigrationCheck.to_dict）。
    mysql_check: dict[str, Any]


class FullstackGraphRunner:
    """构建并执行全栈生成状态机。"""

    def __init__(
        self,
        sandbox: SandboxProvider,
        *,
        event_sink: EventSink | None = None,
        backend_llm: LLMBackendCoder | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._event_sink = event_sink or NullEventSink()
        # 允许测试注入可观测的修复器；生产环境按配置惰性创建。
        self._backend_llm = backend_llm
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

    def _warn(self, state: FullstackState, message: str) -> list[str]:
        """记录 L1 非阻塞告警，并推送独立事件供前端即时展示。"""
        warnings = list(state.get("warnings") or [])
        if message not in warnings:
            warnings.append(message)
            self._emit(
                "fullstack.quality.warning",
                {
                    "runId": state.get("run_id"),
                    "tier": quality_policy(state.get("contract")).tier,
                    "message": message,
                },
            )
        # 同一节点内可能连续调用多次；同步写回状态，后续 _warn 才能在
        # 前一条告警基础上追加，而不是互相覆盖。
        state["warnings"] = warnings
        return warnings

    @staticmethod
    def _quality(state: FullstackState) -> QualityPolicy:
        return quality_policy(state.get("contract"))

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
            builder.add_conditional_edges(
                "BackendCoder",
                self._route_after_backend_coder,
                {
                    "retry": "BackendCoder",
                    # 持久化层缺陷回到 DBACoder，重建后再进 BackendCoder。
                    "retry_dba": "DBACoder",
                    "frontend": "BuildVerifier",
                },
            )
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
                # Applier 拦截了 JOIN/ORM 缺陷：只重跑 BackendCoder，
                # 不重新生成 DDL，也不进入注定失败的构建。
                "retry": "BackendCoder",
                # 缺陷来源是 server/db.js：BackendCoder 无权修改持久化层，
                # 回到 DBACoder 重建 ORM / 迁移。
                "retry_dba": "DBACoder",
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
                # ORM 模型与 DDL 字段错位：回到 DBACoder 重新生成持久化层，
                # 随后由既有 DBACoder -> BackendCoder 边重新绑定 HTTP 层。
                "regenerate": "DBACoder",
            },
        )
        builder.add_edge("BuildVerifier", "SandboxRestart")
        builder.add_edge("SandboxRestart", END)
        return builder.compile()

    @staticmethod
    def _route_after_backend_coder(state: FullstackState) -> str:
        """Applier 拦截优先重跑；一致性自愈回检查；正常继续前端。

        后端子图没有 ConsistencyVerifier 节点（构建后直接重启），因此
        只在 fullstack 模式下允许返回 ``consistency``。
        """
        if state.get("applier_dba_retry"):
            return "retry_dba"
        if state.get("needs_backend_retry"):
            return "retry"
        options = state.get("options") or FullstackRunOptions()
        if state.get("needs_backend_repair") and options.mode == "fullstack":
            return "consistency"
        return "frontend"

    @staticmethod
    def _has_retried(state: FullstackState) -> bool:
        """本次图执行是否已经发生过任意一次自愈重试。"""
        return any(
            int(state.get(key) or 0) > 0
            for key in (
                "applier_retry_count",
                "consistency_repair_count",
                "consistency_dba_repair_count",
                "consistency_join_repair_count",
            )
        )

    @staticmethod
    def _route_after_consistency_verifier(state: FullstackState) -> str:
        """一致性通过才允许构建。

        - ORM/DDL 字段错位：回到 DBACoder 重写持久化层；
        - 缺失路由：回到 BackendCoder 补齐端点。
        """
        if state.get("consistency_ok"):
            return "build"
        return "regenerate" if state.get("needs_dba_repair") else "repair"

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
            # DBACoder → BackendCoder：持久化层重建完成后回执，后端可以
            # 按新的 schema.core.json / DDL 重新绑定 ORM 与路由。
            if (
                state.get("needs_dba_repair")
                or int(state.get("consistency_dba_repair_count") or 0) > 0
            ):
                self._publish_a2a(
                    state,
                    from_agent="DBACoder",
                    to_agent="BackendCoder",
                    kind="schema_ready",
                    requested_action="REBIND_ORM",
                    summary="DDL / ORM 已按契约重建，后端可重新绑定",
                    evidence=sorted(bundle.files),
                    contract_id=contract.contract_id,
                    payload={
                        "dbaFiles": sorted(bundle.files),
                        "table": table,
                    },
                )
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
        sandbox_id = state["sandbox_id"]
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
            # BackendCoder 的唯一输入事实是 DBACoder 的 schema.core.json
            # （tables + relations）。缺失说明上游节点没有产出契约结构，
            # 此时禁止回退到“自己解析 DDL / 猜关系”，直接失败。
            dba_schema = self._read_optional(sandbox_id, "server/schema.core.json")
            if not dba_schema:
                raise FullstackGenerationError(
                    "BackendCoder 缺少 DBACoder 的 server/schema.core.json，"
                    "拒绝在无权威表结构的情况下生成后端",
                    node="BackendCoder",
                )
            bundle = generate_backend_bundle(
                contract,
                extra_routes=applied_routes,
                dba_schema=dba_schema,
            )
            feedback = [
                str(item).strip()
                for item in state.get("applier_feedback") or []
                if str(item).strip()
            ]
            llm_repaired = False
            if feedback:
                bundle, llm_repaired = self._repair_backend_with_llm(
                    state,
                    bundle,
                    feedback,
                    dba_schema,
                    # db.js 是 Applier 校验 JOIN 别名的真实事实源；把它
                    # 只读地放进修复 Prompt，模型才能对齐实际 RELATIONS。
                    authoritative_db_js=self._read_optional(sandbox_id, "server/db.js"),
                )
            try:
                files = self._write_bundle(state, bundle)
            except ApplierSafetyBlock as exc:
                blocked = self._handle_applier_block(
                    state, exc, started, bundle.files
                )
                # 只有回 BackendCoder 重写时才保留本次候选，作为下一轮
                # LLM 修复的基线；DBACoder 重建会重新生成持久化层。
                if blocked.get("needs_backend_retry"):
                    blocked["applier_previous_files"] = {
                        path: bundle.files[path]
                        for path in ("server/index.js",)
                        if isinstance(bundle.files.get(path), str)
                    }
                return blocked
            collection = str(bundle.metadata.get("collectionPath") or "")
            summary = bundle.summary
            if fix_routes:
                summary = f"一致性自愈：补齐 {len(fix_routes)} 条缺失路由；" + summary
            if int(state.get("applier_retry_count") or 0):
                summary = (
                    f"Applier AST 门禁复核通过（第 "
                    f"{int(state.get('applier_retry_count') or 0)} 次重生成）；"
                    + summary
                )
            self._emit_outputs("BackendCoder", bundle, state)
            self._node_completed("BackendCoder", state, summary, bundle.files, started)
            return {
                "backend_files": sorted(bundle.files),
                "collection_path": collection,
                "files": files,
                "consistency_fix_routes": [],
                "consistency_applied_routes": applied_routes,
                "needs_backend_repair": False,
                "needs_backend_retry": False,
                "applier_dba_retry": False,
                # 诊断已消费：下一次普通生成不应继续携带旧错误。
                "applier_feedback": [],
                "applier_feedback_attempt": 0,
                "applier_previous_files": {},
                "llm_repair_count": int(state.get("llm_repair_count") or 0)
                + (1 if llm_repaired else 0),
            }
        except FullstackGenerationError:
            raise
        except Exception as exc:
            self._node_failed("BackendCoder", state, exc)
            raise FullstackGenerationError(
                f"BackendCoder 生成失败: {exc}", node="BackendCoder"
            ) from exc

    def _repair_backend_with_llm(
        self,
        state: FullstackState,
        bundle: GeneratedBundle,
        feedback: list[str],
        dba_schema: str,
        *,
        authoritative_db_js: str | None = None,
    ) -> tuple[GeneratedBundle, bool]:
        """把 Applier 诊断交给 LLM，返回 ``(修复后产物, 是否成功)``。

        LLM 失败必须降级回模板产物，而不是把整个 Run 变成“环境失败”：
        Applier 仍是最终裁决者，下一轮仍可继续拦截并重试。
        """
        attempt = int(state.get("applier_feedback_attempt") or 1)
        max_attempts = max(MAX_APPLIER_REPAIRS, MAX_JOIN_REPAIRS)
        # 上一轮被拦截的候选优先作为修复基线：否则每轮都从模板重置，
        # 模型看不到自己上一次改了什么，修复过程不保证收敛。
        previous = state.get("applier_previous_files") or {}
        repair_bundle = bundle
        if isinstance(previous.get("server/index.js"), str):
            repair_files = dict(bundle.files)
            repair_files["server/index.js"] = previous["server/index.js"]
            repair_bundle = GeneratedBundle(
                files=repair_files,
                summary=bundle.summary,
                metadata={**bundle.metadata, "applierPreviousAttempt": True},
            )
        coder = self._resolve_backend_llm()
        if coder is None or not coder.available:
            self._emit(
                "fullstack.applier.llm_repair",
                {
                    "runId": state["run_id"],
                    "node": "BackendCoder",
                    "attempt": attempt,
                    "maxAttempts": max_attempts,
                    "status": "SKIPPED",
                    "reason": "LLM_REPAIR_DISABLED",
                    "diagnostics": feedback,
                },
            )
            return bundle, False
        try:
            repaired = coder.repair(
                contract=state["contract"],
                bundle=repair_bundle,
                dba_schema=dba_schema,
                diagnostics=feedback,
                attempt=attempt,
                max_attempts=max_attempts,
                authoritative_files=(
                    {"server/db.js": authoritative_db_js}
                    if authoritative_db_js
                    else None
                ),
            )
        except LLMBackendRepairUnavailable as exc:
            logger.warning(
                "LLM BackendCoder 不可用，回退模板 run_id=%s: %s",
                state.get("run_id"),
                exc,
            )
            self._emit(
                "fullstack.applier.llm_repair",
                {
                    "runId": state["run_id"],
                    "node": "BackendCoder",
                    "attempt": attempt,
                    "maxAttempts": max_attempts,
                    "status": "SKIPPED",
                    "reason": "LLM_REPAIR_UNAVAILABLE",
                    "error": str(exc),
                    "diagnostics": feedback,
                },
            )
            return bundle, False
        except Exception as exc:  # noqa: BLE001 - LLM 失败必须降级回模板
            logger.warning(
                "LLM BackendCoder 修复失败，回退模板 run_id=%s: %s",
                state.get("run_id"),
                exc,
            )
            self._emit(
                "fullstack.applier.llm_repair",
                {
                    "runId": state["run_id"],
                    "node": "BackendCoder",
                    "attempt": attempt,
                    "maxAttempts": max_attempts,
                    "status": "FAILED",
                    "error": str(exc),
                    "diagnostics": feedback,
                },
            )
            return bundle, False

        self._emit(
            "fullstack.applier.llm_repair",
            {
                "runId": state["run_id"],
                "node": "BackendCoder",
                "attempt": attempt,
                "maxAttempts": max_attempts,
                "status": "SUCCEEDED",
                "files": sorted(
                    (repaired.metadata.get("llmRepair") or {}).get("files") or []
                ),
                "diagnostics": feedback,
            },
        )
        logger.info(
            "LLM BackendCoder 已按诊断修复 run_id=%s attempt=%d/%d",
            state.get("run_id"),
            attempt,
            max_attempts,
        )
        return repaired, True

    def _resolve_backend_llm(self) -> LLMBackendCoder | None:
        if self._backend_llm is not None:
            return self._backend_llm
        try:
            self._backend_llm = LLMBackendCoder(get_settings())
        except Exception as exc:  # noqa: BLE001 - 配置损坏不应阻断模板主路径
            logger.warning("初始化 LLM BackendCoder 失败: %s", exc)
            return None
        return self._backend_llm

    def _handle_applier_block(
        self,
        state: FullstackState,
        exc: ApplierSafetyBlock,
        started: float,
        candidate_files: Any = None,
    ) -> dict[str, Any]:
        """Applier 拦截后的归因与重试决策。

        先让 ``audit_contract`` 判断“契约本身是否缺字段/缺 JOIN”：
          - CONTRACT_DEFECT：立即要求重修契约，不浪费一次重试；
          - CODE_DEFECT：BackendCoder 重新生成，最多 ``MAX_APPLIER_REPAIRS`` 次。
        """
        contract = state["contract"]
        options = state.get("options") or FullstackRunOptions()
        retry_count = int(state.get("applier_retry_count") or 0)
        report = exc.report
        # 只有 db.js 的问题属于持久化层：index.js 无论怎么改都消不掉，
        # 继续让 BackendCoder 重写只会浪费重试预算。
        db_only = bool(report.issues) and all(
            issue.source == "server/db.js" for issue in report.issues
        )
        audit = audit_contract(
            contract,
            mode=options.mode,
            code_issues=report.messages(),
        )
        # Applier → 具体 Coder：把可执行的诊断送到能修它的人手里。
        # db.js 只能由 DBACoder 重建；index.js 才回 BackendCoder。
        target_agent = "DBACoder" if db_only else "BackendCoder"
        self._publish_a2a(
            state,
            from_agent="Applier",
            to_agent=target_agent,
            kind="schema_repair" if db_only else "code_repair",
            requested_action="REGENERATE_DB" if db_only else "REPAIR_CODE",
            summary=report.summary(),
            evidence=report.messages(),
            contract_id=contract.contract_id,
            payload={
                "attempt": retry_count,
                "maxAttempts": MAX_APPLIER_REPAIRS,
                "defectKind": audit.defect_kind,
                "issues": [issue.to_dict() for issue in report.issues],
            },
        )
        self._emit(
            "fullstack.applier.blocked",
            {
                "runId": state["run_id"],
                "node": "BackendCoder",
                "attempt": retry_count,
                "maxAttempts": MAX_APPLIER_REPAIRS,
                "defectKind": audit.defect_kind,
                "route": "DBACoder" if db_only else "BackendCoder",
                "issues": [issue.to_dict() for issue in report.issues],
                "metrics": FULLSTACK_METRICS.snapshot(),
            },
        )
        if not audit.ok:
            if retry_count > 0:
                FULLSTACK_METRICS.record_contract_misretry()
            # BackendCoder → Architect：代码侧诊断已证明契约本身缺字段 /
            # 缺 JOIN，继续重试代码只会重复失败，自动发起契约修订请求。
            self._publish_a2a(
                state,
                from_agent="BackendCoder",
                to_agent="Architect",
                kind="contract_revision",
                requested_action="REVISE_CONTRACT",
                summary=audit.summary(),
                evidence=report.messages(),
                contract_id=contract.contract_id,
                payload=audit.payload(),
            )
            self._node_failed("BackendCoder", state, RuntimeError(audit.summary()))
            raise FullstackContractContradictionError(audit, node="BackendCoder")
        # 校验器本身不可用（缺 node / Babel）属于环境缺陷，重跑生成器
        # 不会恢复；直接按环境失败上报，禁止伪装成可自愈的代码缺陷。
        if report.issues and all(
            issue.code == DIAG_AST_UNAVAILABLE for issue in report.issues
        ):
            self._node_failed("BackendCoder", state, RuntimeError(report.summary()))
            raise FullstackBuildError("ORM AST 校验器不可用，Applier 拒绝落盘")
        # 诊断与候选产物都完全一致 = 模型没有改到任何有效内容；再花一次
        # LLM 重试也不会收敛。若候选变了（即使诊断相同），说明模型仍在
        # 迭代，允许消耗第二轮重试。
        fingerprint = _diagnostic_fingerprint(report.messages())
        candidate_fingerprint = _candidate_fingerprint(candidate_files)
        previous_fingerprint = str(state.get("applier_fingerprint") or "")
        previous_candidate = str(
            state.get("applier_candidate_fingerprint") or ""
        )
        if (
            retry_count > 0
            and previous_fingerprint == fingerprint
            and previous_candidate == candidate_fingerprint
        ):
            self._publish_a2a(
                state,
                from_agent="BackendCoder",
                to_agent="Architect",
                kind="contract_review",
                requested_action="REVIEW_CONTRACT",
                summary=(
                    "代码自愈未收敛：连续两次 Applier 诊断与候选产物完全一致，"
                    "重试无法修复，请复核契约中的嵌套路由 / relations"
                ),
                evidence=report.messages(),
                contract_id=contract.contract_id,
                payload={
                    "attempt": retry_count,
                    "maxAttempts": MAX_APPLIER_REPAIRS,
                    "fingerprint": fingerprint,
                    "candidateFingerprint": candidate_fingerprint,
                    "issues": [issue.to_dict() for issue in report.issues],
                },
            )
            self._node_failed(
                "BackendCoder",
                state,
                RuntimeError("Applier 诊断未收敛: " + report.summary()),
            )
            raise FullstackGenerationError(
                "Applier AST 门禁诊断连续两次完全一致，代码自愈无法收敛："
                + "；".join(report.messages()),
                node="BackendCoder",
            )
        if retry_count >= MAX_APPLIER_REPAIRS:
            # 代码自愈 2/2 耗尽：把完整诊断升级给 Architect，让架构师判断
            # 是契约字段/JOIN 仍不完整，还是需要调整契约约束；不能直接
            # 把“后端工程师修不动”当成无解错误。
            self._publish_a2a(
                state,
                from_agent="BackendCoder",
                to_agent="Architect",
                kind="contract_review",
                requested_action="REVIEW_CONTRACT",
                summary=(
                    f"Applier AST 门禁连续拦截，代码自愈 {MAX_APPLIER_REPAIRS}/"
                    f"{MAX_APPLIER_REPAIRS} 次耗尽：{report.summary()}"
                ),
                evidence=report.messages(),
                contract_id=contract.contract_id,
                payload={
                    "attempt": retry_count,
                    "maxAttempts": MAX_APPLIER_REPAIRS,
                    "audit": audit.payload(),
                    "issues": [issue.to_dict() for issue in report.issues],
                },
            )
            self._node_failed("BackendCoder", state, RuntimeError(report.summary()))
            raise FullstackGenerationError(
                "Applier AST 门禁连续拦截，代码自愈耗尽："
                + "；".join(report.messages()),
                node="BackendCoder",
            )
        next_count = retry_count + 1
        target = "DBACoder" if db_only else "BackendCoder"
        action = "重建持久化层" if db_only else "重新生成"
        logger.warning(
            "Applier 拦截 %d 处代码缺陷，转 %s %s [%d/%d] run_id=%s",
            len(report.issues),
            target,
            action,
            next_count,
            MAX_APPLIER_REPAIRS,
            state.get("run_id"),
        )
        self._node_completed(
            "BackendCoder",
            state,
            (
                f"Applier AST 门禁拦截 {len(report.issues)} 处代码缺陷，"
                f"转 {target} {action}（第 {next_count}/{MAX_APPLIER_REPAIRS} 次）"
            ),
            [],
            started,
        )
        return {
            # db.js 缺陷回到 DBACoder；index.js 缺陷才让 BackendCoder+LLM 修复。
            "needs_backend_retry": not db_only,
            "applier_dba_retry": db_only,
            "applier_retry_count": next_count,
            # 下一轮 BackendCoder 不再盲目重跑模板，而是携带这些诊断
            # 调用 LLM；模板仍作为 LLM 不可用时的降级产物。
            # 带 source 前缀：模型能区分 index.js / db.js，避免在错误
            # 的文件里反复“修”一个不属于它的缺陷。
            "applier_feedback": (
                []
                if db_only
                else [
                    (
                        f"[{issue.source}] {issue.message}"
                        if issue.source
                        else issue.message
                    )
                    for issue in report.issues
                ]
            ),
            "applier_feedback_attempt": 0 if db_only else next_count,
            "applier_previous_files": {},
            # 记录本轮诊断 + 候选产物指纹；下一次两者都一字不差时
            # 直接升级给 Architect，不再空转重试。
            "applier_fingerprint": fingerprint,
            "applier_candidate_fingerprint": candidate_fingerprint,
            "consistency_ok": False,
        }

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
            visual_document = state.get("visual_document")
            warnings = list(state.get("warnings") or [])
            if visual_document is not None:
                # 离线优先链路：冻结虚拟树一次性确定性编译。
                # 不调用 LLM、不猜测布局，基础节点全部走硬编码映射。
                # 先把契约里的 componentInteractions 映射成节点绑定，
                # CodeGen 才能生成真实的 API 调用（领域无关）。
                visual_document = apply_contract_interactions(
                    visual_document,
                    contract.component_interactions,
                )
                extensions = load_visual_extensions()
                stack = resolve_stack_dependencies(contract_stack_text(contract))
                dependencies = {
                    **stack.dependencies,
                    **extension_dependencies(extensions),
                }
                try:
                    compiled = compile_visual_document(
                        visual_document,
                        include_interactions=True,
                        extra_dependencies=dependencies,
                        extra_dev_dependencies=stack.dev_dependencies,
                        extra_imports=list(stack.imports),
                        visual_extensions=extensions,
                    )
                except VisualCodegenGateUnavailable as exc:
                    raise FullstackGenerationError(
                        f"CodeGen AST 门禁不可用: {exc}", node="FrontendCoder"
                    ) from exc
                except VisualCodegenError as exc:
                    raise FullstackGenerationError(
                        f"Visual CodeGen 失败: {exc}", node="FrontendCoder"
                    ) from exc

                for message in compiled.warnings:
                    if message not in warnings:
                        warnings.append(message)

                if options.mode == "frontend":
                    bundle = GeneratedBundle(
                        files=compiled.files,
                        summary=compiled.summary,
                        metadata={**compiled.metadata, "mode": "visual"},
                    )
                else:
                    # 全栈模式下仍保留 api-client / main.jsx / vite 代理注入，
                    # 只用 Visual CodeGen 覆盖 src/App.jsx 与视觉样式。
                    base_bundle = generate_frontend_bundle(
                        contract,
                        existing_main=self._read_optional(sandbox_id, "src/main.jsx"),
                        existing_vite=self._read_optional(sandbox_id, "vite.config.js"),
                        existing_package=self._read_optional(
                            sandbox_id, "package.json"
                        ),
                        # 冻结虚拟树是唯一前端产物；旧数据面板没有源码锚点，
                        # 会变成“看得见但删不掉、拖不动”的菜单元件。
                        include_panel=False,
                    )
                    # 全栈工程以 base_bundle 的 package.json / main.jsx /
                    # vite.config.js 为准；Visual CodeGen 只覆盖 App.jsx、
                    # visual.css 与 index.html。
                    merged_files = dict(compiled.files)
                    merged_files.update(base_bundle.files)
                    # base bundle 提供 dev.mjs / 后端依赖；compiled 提供
                    # techStack 派生依赖（第三方 UI 库、图表库等）。整体覆盖会
                    # 丢掉其中一方，必须按 manifest 合并。
                    if (
                        "package.json" in compiled.files
                        and "package.json" in base_bundle.files
                    ):
                        merged_files["package.json"] = _merge_package_manifests(
                            base_bundle.files["package.json"],
                            compiled.files["package.json"],
                        )
                    bundle = GeneratedBundle(
                        files=merged_files,
                        summary=f"{base_bundle.summary}；{compiled.summary}",
                        metadata={
                            **base_bundle.metadata,
                            **compiled.metadata,
                            "mode": "fullstack+visual",
                        },
                    )
                self._emit(
                    "fullstack.visual.codegen",
                    {
                        "runId": state.get("run_id"),
                        "node": "FrontendCoder",
                        "documentId": visual_document.doc_id,
                        "nodeCount": compiled.metadata.get("nodeCount", 0),
                        "interactionCount": compiled.metadata.get(
                            "interactionCount", 0
                        ),
                        "customNodeCount": compiled.metadata.get("customNodeCount", 0),
                        "repaired": compiled.repaired,
                        "warnings": list(compiled.warnings),
                    },
                )
            elif options.mode == "frontend":
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
                "warnings": warnings,
            }
            if visual_document is not None:
                result["visual_node_count"] = int(bundle.metadata.get("nodeCount") or 0)
            if options.mode == "frontend" or visual_document is not None:
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
        policy = self._quality(state)
        warnings = list(state.get("warnings") or [])
        # 契约审计先于任何修复：契约自相矛盾时，重试模板只会得到同一个
        # 错误。这里立即终止并把归因交给前端，引导用户重新澄清。
        # L1 已由用户显式选择 Demo 级质量：审计问题降级为告警，不阻塞。
        options = state.get("options") or FullstackRunOptions()
        contract_audit = audit_contract(contract, mode=options.mode)
        if not contract_audit.ok:
            if not policy.blocking:
                warnings = self._warn(
                    state,
                    f"L1 敏捷模式跳过契约审计阻断：{contract_audit.summary()}",
                )
            else:
                if self._has_retried(state):
                    # 已经跑过至少一次自愈仍暴露出契约问题：这是一次“本可
                    # 避免的误重试”，计入 Contract_Misretry_Count。
                    FULLSTACK_METRICS.record_contract_misretry()
                logger.warning(
                    "ConsistencyVerifier 契约自相矛盾，停止重试 run_id=%s problems=%s",
                    state.get("run_id"),
                    [problem.code for problem in contract_audit.problems],
                )
                self._publish_a2a(
                    state,
                    from_agent="ConsistencyVerifier",
                    to_agent="Architect",
                    kind="contract_revision",
                    requested_action="REVISE_CONTRACT",
                    summary=contract_audit.summary(),
                    evidence=[
                        problem.message for problem in contract_audit.problems
                    ],
                    contract_id=contract.contract_id,
                    payload=contract_audit.payload(),
                )
                self._node_failed(
                    "ConsistencyVerifier", state, RuntimeError(contract_audit.summary())
                )
                raise FullstackContractContradictionError(contract_audit)

        snapshot: dict[str, str] = {}
        for path in (
            "src/App.jsx",
            "src/fullstack-api.jsx",
            "server/api-routes.json",
            "server/index.js",
            # ORM ↔ 迁移 DDL 一致性校验所需的真实产物。
            "server/db.js",
            "server/schema.core.json",
            "server/migrations/001_init.sql",
        ):
            content = self._read_optional(sandbox_id, path)
            if content is not None:
                snapshot[path] = content

        try:
            report = verify_consistency(snapshot, contract)
            checked_files = set(report.checked_files)
            # 维度二：静态路由比对之外，再通过内置 SQLite MCP 把生成的
            # DDL 真正执行到临时数据库，查询 table / columns 是否存在。
            # 这一步能抓住“路由齐全但表根本建不起来”的产物。
            # L1 跳过；L2 / L3 保留。
            if policy.sqlite_schema_check:
                schema_check = verify_schema_with_mcp(snapshot, contract)
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
            # 维度三（仅 L3）：把迁移文件真实执行到临时 MySQL 库，并用
            # information_schema 核对核心表字段；连接失败是环境问题，
            # DDL / 字段问题则回退 DBACoder 重写迁移。
            if policy.mysql_migration_check:
                mysql_check = verify_mysql_migration(snapshot, contract)
                report = replace(report, mysql=mysql_check.to_dict())
                if mysql_check.environment_error:
                    raise FullstackQualityEnvironmentError(mysql_check.summary())
                if not mysql_check.ok:
                    report = replace(
                        report,
                        issues=report.issues
                        + (
                            ConsistencyIssue(
                                kind="mysql_migration",
                                component="DBACoder",
                                method="",
                                path="server/migrations/001_init.sql",
                                message=mysql_check.summary(),
                                suggestion=(
                                    "由 DBACoder 按契约 DDL 重写迁移文件，确保"
                                    "真实 MySQL 可执行且核心表字段完整；"
                                    "禁止跳过迁移验收直接交付"
                                ),
                                fix_action="REGENERATE_ORM",
                            ),
                        ),
                    )
        except FullstackQualityEnvironmentError as exc:
            # 环境问题必须原样上抛，保留 failureKind=ENVIRONMENT，避免
            # 被下面的通用包装误判成代码缺陷。
            self._node_failed("ConsistencyVerifier", state, exc)
            raise
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
        # L1：任何一致性问题都降级为告警，直接进入构建，不触发修复重试。
        if not policy.blocking and not report.ok:
            for issue in report.issues:
                warnings = self._warn(
                    state,
                    f"L1 一致性告警 [{issue.kind}] "
                    f"{issue.component} {issue.method} {issue.path}：{issue.message}",
                )
            warnings = self._warn(state, L1_WARNING_TEXT)
            self._node_completed(
                "ConsistencyVerifier",
                state,
                f"L1 敏捷模式：{len(report.issues)} 处一致性问题已降级为告警",
                [],
                started,
            )
            return {
                "consistency_ok": True,
                "needs_backend_repair": False,
                "needs_dba_repair": False,
                "warnings": warnings,
                "quality_tier": policy.tier,
                "mysql_check": report.mysql or {},
            }
        if report.ok:
            self._node_completed(
                "ConsistencyVerifier",
                state,
                report.summary(),
                [],
                started,
            )
            return {
                "consistency_ok": True,
                "needs_backend_repair": False,
                "needs_dba_repair": False,
                "warnings": warnings,
                "quality_tier": policy.tier,
                "mysql_check": report.mysql or {},
            }

        # 优先处理 ORM ↔ DDL 字段错位：这类问题会让所有 CRUD 直接 500，
        # 必须先由 DBACoder 按 DDL 重写持久化层，再让 BackendCoder 重新绑定。
        dba_repair_count = int(state.get("consistency_dba_repair_count") or 0)
        if report.needs_orm_repair and dba_repair_count < MAX_ORM_REPAIRS:
            next_count = dba_repair_count + 1
            logger.warning(
                "ConsistencyVerifier 发现 ORM/DDL 字段错位，转 DBACoder 重写 [%d/%d] run_id=%s",
                next_count,
                MAX_ORM_REPAIRS,
                state.get("run_id"),
            )
            self._publish_a2a(
                state,
                from_agent="ConsistencyVerifier",
                to_agent="DBACoder",
                kind="schema_repair",
                requested_action="REGENERATE_ORM",
                summary=(
                    "ORM 模型与迁移 DDL 字段错位，请按 DDL 重写持久化层"
                ),
                evidence=[
                    issue.message
                    for issue in report.issues
                    if issue.fix_action == "REGENERATE_ORM"
                ],
                contract_id=contract.contract_id,
                payload={
                    "attempt": next_count,
                    "maxAttempts": MAX_ORM_REPAIRS,
                    "report": report.to_dict(),
                },
            )
            self._emit(
                "fullstack.consistency.repair",
                {
                    "runId": state["run_id"],
                    "attempt": next_count,
                    "maxAttempts": MAX_ORM_REPAIRS,
                    "node": "DBACoder",
                    "reason": "ORM_SCHEMA_MISMATCH",
                    "issues": [
                        issue.to_dict()
                        for issue in report.issues
                        if issue.fix_action == "REGENERATE_ORM"
                    ],
                    "report": report.to_dict(),
                },
            )
            self._node_completed(
                "ConsistencyVerifier",
                state,
                (
                    "ORM 模型与迁移 DDL 字段不一致，转交 DBACoder 按 DDL 重写"
                    f"（第 {next_count}/{MAX_ORM_REPAIRS} 次）"
                ),
                [],
                started,
            )
            return {
                "consistency_ok": False,
                "needs_backend_repair": True,
                "needs_dba_repair": True,
                "consistency_dba_repair_count": next_count,
                "consistency_fix_routes": [],
                "warnings": warnings,
                "quality_tier": policy.tier,
            }

        # JOIN 别名 / include 关系错误：契约有 relations，但 BackendCoder
        # 把别名或外键写错。这类问题只需重跑 BackendCoder，不重写 DBACoder。
        join_repair_count = int(state.get("consistency_join_repair_count") or 0)
        if report.needs_join_repair and join_repair_count < MAX_JOIN_REPAIRS:
            next_count = join_repair_count + 1
            logger.warning(
                "ConsistencyVerifier 发现 JOIN 关系错位，转 BackendCoder 重建 [%d/%d] run_id=%s",
                next_count,
                MAX_JOIN_REPAIRS,
                state.get("run_id"),
            )
            self._publish_a2a(
                state,
                from_agent="ConsistencyVerifier",
                to_agent="BackendCoder",
                kind="route_repair",
                requested_action="REPAIR_BACKEND_JOIN",
                summary="JOIN 别名 / include 关系与契约不一致，请重建后端 JOIN",
                evidence=[
                    issue.message
                    for issue in report.issues
                    if issue.fix_action == "REPAIR_BACKEND_JOIN"
                ],
                contract_id=contract.contract_id,
                payload={
                    "attempt": next_count,
                    "maxAttempts": MAX_JOIN_REPAIRS,
                    "report": report.to_dict(),
                },
            )
            self._emit(
                "fullstack.consistency.repair",
                {
                    "runId": state["run_id"],
                    "attempt": next_count,
                    "maxAttempts": MAX_JOIN_REPAIRS,
                    "node": "BackendCoder",
                    "reason": "JOIN_SCHEMA_MISMATCH",
                    "issues": [
                        issue.to_dict()
                        for issue in report.issues
                        if issue.fix_action == "REPAIR_BACKEND_JOIN"
                    ],
                    "report": report.to_dict(),
                },
            )
            self._node_completed(
                "ConsistencyVerifier",
                state,
                (
                    "JOIN 别名 / include 关系与契约不一致，转交 BackendCoder "
                    f"重建（第 {next_count}/{MAX_JOIN_REPAIRS} 次）"
                ),
                [],
                started,
            )
            join_messages = [
                issue.message
                for issue in report.issues
                if issue.fix_action == "REPAIR_BACKEND_JOIN"
            ]
            return {
                "consistency_ok": False,
                "needs_backend_repair": True,
                "needs_dba_repair": False,
                "consistency_join_repair_count": next_count,
                "consistency_fix_routes": [],
                # JOIN 别名错位同样属于模板无法自我修复的代码缺陷，
                # 把诊断交给下一轮 LLM BackendCoder。
                "applier_feedback": join_messages,
                "applier_feedback_attempt": next_count,
                "warnings": warnings,
                "quality_tier": policy.tier,
            }

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
            self._publish_a2a(
                state,
                from_agent="ConsistencyVerifier",
                to_agent="BackendCoder",
                kind="route_repair",
                requested_action="ADD_BACKEND_ROUTE",
                summary=f"前端请求缺失 {len(missing_routes)} 条后端路由，请补齐",
                evidence=[str(item) for item in missing_routes],
                contract_id=contract.contract_id,
                payload={
                    "attempt": next_count,
                    "maxAttempts": MAX_CONSISTENCY_REPAIRS,
                    "missingRoutes": missing_routes,
                    "report": report.to_dict(),
                },
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
                "needs_dba_repair": False,
                "consistency_fix_routes": missing_routes,
                "consistency_repair_count": next_count,
                "warnings": warnings,
                "quality_tier": policy.tier,
            }

        # 重试耗尽，或差异无法通过补齐路由解决（如 Payload 字段冲突）。
        # 终止前再做一次契约审计：若矛盾来自契约，必须明确告知用户
        # “改契约”而不是继续重跑代码生成。
        final_audit = audit_contract(
            contract,
            mode=options.mode,
            code_issues=[issue.message for issue in report.issues],
        )
        if not final_audit.ok:
            if self._has_retried(state):
                FULLSTACK_METRICS.record_contract_misretry()
            logger.warning(
                "ConsistencyVerifier 重试耗尽且契约自相矛盾 run_id=%s problems=%s",
                state.get("run_id"),
                [problem.code for problem in final_audit.problems],
            )
            self._publish_a2a(
                state,
                from_agent="ConsistencyVerifier",
                to_agent="Architect",
                kind="contract_revision",
                requested_action="REVISE_CONTRACT",
                summary=final_audit.summary(),
                evidence=[problem.message for problem in final_audit.problems],
                contract_id=contract.contract_id,
                payload=final_audit.payload(),
            )
            self._node_failed(
                "ConsistencyVerifier", state, RuntimeError(final_audit.summary())
            )
            raise FullstackContractContradictionError(final_audit)
        # 契约本身能通过审计，但代码侧的自愈次数已耗尽：把剩余差异交给
        # Architect 复核契约约束是否有歧义，避免“无限重跑代码”。
        self._publish_a2a(
            state,
            from_agent="ConsistencyVerifier",
            to_agent="Architect",
            kind="contract_review",
            requested_action="REVIEW_CONTRACT",
            summary=(
                f"一致性问题自愈耗尽（{len(report.issues)} 处仍未解决）："
                f"{report.summary()}"
            ),
            evidence=[issue.message for issue in report.issues],
            contract_id=contract.contract_id,
            payload={
                "report": report.to_dict(),
                "attempt": repair_count,
            },
        )
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
        policy = self._quality(state)
        warnings = list(state.get("warnings") or [])
        # Visual CodeGen 降级产物必须显式回传给图状态；LangGraph 不会
        # 自动合并节点函数里对 state 字典的原地修改。
        visual_updates: dict[str, Any] = {}
        # L1 允许在失败后重试 1 次；L2 / L3 维持一次失败即终止的严格语义。
        max_attempts = 1 + policy.build_retry_limit

        def _run(kind: str, label: str):
            last = None
            for attempt in range(1, max_attempts + 1):
                result = self._sandbox.verify(state["sandbox_id"], kind)
                if result.success:
                    return result, attempt
                last = result
                logs = result.logs or f"{label}失败且没有日志"
                self._emit(
                    "fullstack.node.output",
                    {
                        "runId": state["run_id"],
                        "node": "BuildVerifier",
                        "path": label,
                        "language": "text",
                        "content": logs[-4000:],
                        "truncated": result.logs_truncated,
                    },
                )
                if attempt < max_attempts:
                    self._emit(
                        "fullstack.build.retry",
                        {
                            "runId": state["run_id"],
                            "node": "BuildVerifier",
                            "command": label,
                            "attempt": attempt,
                            "maxAttempts": max_attempts,
                            "exitCode": result.exit_code,
                        },
                    )
                    logger.warning(
                        "BuildVerifier %s 失败，重试 [%d/%d] run_id=%s",
                        label,
                        attempt,
                        max_attempts,
                        state.get("run_id"),
                    )
            return last, max_attempts

        # 后端依赖可能随契约变化（例如上传需要 multer）。构建前先安装，
        # 否则沙盒重启时 server/index.js 会因缺少模块直接退出。
        # Visual CodeGen 生成的是完整 Vite 工程（含 package.json），
        # 即使 mode=frontend 也必须执行 npm install，确保依赖与声明一致。
        if options.mode != "frontend" or state.get("visual_document") is not None:
            install, install_attempts = _run("install", "npm install")
            if not install.success:
                logs = install.logs or "依赖安装失败且没有日志"
                if policy.blocking:
                    self._node_failed(
                        "BuildVerifier",
                        state,
                        FullstackBuildError(f"npm install 退出码 {install.exit_code}"),
                    )
                    raise FullstackBuildError("生成代码依赖安装失败：\n" + logs)
                warnings = self._warn(
                    state,
                    f"{L1_WARNING_TEXT} 依赖安装失败（已重试 "
                    f"{install_attempts - 1} 次），继续启动沙盒："
                    f"{logs[-500:]}",
                )
                self._node_completed(
                    "BuildVerifier",
                    state,
                    "L1 敏捷模式：依赖安装失败已降级为告警",
                    [],
                    started,
                )
                return {
                    "warnings": warnings,
                    "build_retry_count": install_attempts - 1,
                    "quality_tier": policy.tier,
                }
        result, build_attempts = _run("build", "npm run build")
        # Visual CodeGen 的构建失败最多修复一次：重写为扁平安全占位页后
        # 再构建。占位页同样经过 data-node-id 完整性门禁，仍然可继续
        # Direct Ops 定位；绝不把不可编译源码留在沙盒里。
        visual_document = state.get("visual_document")
        if (
            not result.success
            and visual_document is not None
            and not state.get("visual_fallback_applied")
        ):
            try:
                fallback = generate_visual_fallback(
                    visual_document,
                    reason=f"npm run build 退出码 {result.exit_code}",
                )
                self._write_bundle(
                    state,
                    GeneratedBundle(
                        files=fallback.files,
                        summary=fallback.summary,
                        metadata=fallback.metadata,
                    ),
                )
                state["visual_fallback_applied"] = True
                # 完成事件回传的入口源码必须与沙盒最终内容一致。
                state["frontend_entry"] = fallback.files.get("src/App.jsx", "")
                state["visual_node_count"] = len(fallback.node_ids)
                visual_updates = {
                    "visual_fallback_applied": True,
                    "frontend_entry": state["frontend_entry"],
                    "visual_node_count": len(fallback.node_ids),
                }
                self._emit(
                    "fullstack.visual.codegen.fallback",
                    {
                        "runId": state.get("run_id"),
                        "node": "BuildVerifier",
                        "documentId": visual_document.doc_id,
                        "reason": f"npm run build 退出码 {result.exit_code}",
                        "summary": fallback.summary,
                    },
                )
                warnings = self._warn(state, fallback.summary)
                result, build_attempts = _run("build", "npm run build")
            except Exception as exc:  # noqa: BLE001 - 降级失败继续原始失败路径
                logger.warning(
                    "Visual CodeGen 安全占位页构建失败 run_id=%s: %s",
                    state.get("run_id"),
                    exc,
                )
        if not result.success:
            logs = result.logs or "构建失败且没有日志"
            if policy.blocking:
                self._node_failed(
                    "BuildVerifier",
                    state,
                    FullstackBuildError(f"npm run build 退出码 {result.exit_code}"),
                )
                raise FullstackBuildError(
                    "生成代码未通过 npm run build：\n" + logs[-4000:]
                )
            warnings = self._warn(
                state,
                f"{L1_WARNING_TEXT} 构建失败（已重试 {build_attempts - 1} 次），"
                f"继续启动沙盒：{logs[-500:]}",
            )
            self._node_completed(
                "BuildVerifier",
                state,
                "L1 敏捷模式：构建失败已降级为告警",
                [],
                started,
            )
            return {
                "warnings": warnings,
                "build_retry_count": build_attempts - 1,
                "quality_tier": policy.tier,
                **visual_updates,
            }
        self._node_completed(
            "BuildVerifier",
            state,
            f"npm run build 通过（{result.duration_ms}ms）",
            [],
            started,
        )
        return {
            "warnings": warnings,
            "build_retry_count": build_attempts - 1,
            "quality_tier": policy.tier,
            **visual_updates,
        }

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
        policy = self._quality(state)
        try:
            self._sandbox.restart(sandbox_id, ready_path=ready_path)
            handle = self._sandbox.get(sandbox_id)
        except Exception as exc:
            if not policy.blocking:
                # L1：沙盒启动失败也返回告警而非失败，前端仍可展示已生成
                # 的文件与后续修复入口。
                warnings = self._warn(
                    state,
                    f"{L1_WARNING_TEXT} 沙盒启动失败：{exc}",
                )
                self._node_completed(
                    "SandboxRestart",
                    state,
                    "L1 敏捷模式：沙盒启动失败已降级为告警",
                    [],
                    started,
                )
                return {"warnings": warnings, "quality_tier": policy.tier}
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
        # 所有产物统一经 Applier 落盘：AST 门禁是唯一写入口，Coder
        # 无法绕过，也不存在“先生成后检查”的半写盘状态。
        options = state.get("options") or FullstackRunOptions()
        applier = Applier(self._sandbox, state["contract"])
        changed = applier.apply(
            state["sandbox_id"],
            bundle.files,
            mode=options.mode,
        )
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

    def _publish_a2a(
        self,
        state: FullstackState,
        *,
        from_agent: str,
        to_agent: str,
        kind: str,
        requested_action: str,
        summary: str,
        evidence: Any = (),
        contract_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """发布一条统一的 Agent-to-Agent 反馈并写入运行级日志。"""
        feedback = build_feedback(
            run_id=str(state.get("run_id") or ""),
            from_agent=from_agent,
            to_agent=to_agent,
            kind=kind,
            requested_action=requested_action,
            summary=summary,
            evidence=evidence if isinstance(evidence, (list, tuple)) else (evidence,),
            contract_id=contract_id,
            payload=payload,
        )
        item = A2A_LOG.publish(feedback)
        self._emit("a2a.feedback", item)
        return item


__all__ = [
    "GRAPH_NODE_TOTAL",
    "MAX_CONSISTENCY_REPAIRS",
    "FullstackBuildError",
    "FullstackConsistencyError",
    "FullstackGenerationError",
    "FullstackQualityEnvironmentError",
    "FullstackGraphRunner",
    "FullstackState",
    "NODE_LABELS",
]
