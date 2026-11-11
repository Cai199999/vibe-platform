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
from ai_core.fullstack.schema_mcp import verify_schema_with_mcp
from ai_core.observability.fullstack_metrics import FULLSTACK_METRICS
from ai_core.ports.sandbox import SandboxProvider

logger = logging.getLogger(__name__)

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
    # Applier / 一致性诊断原文：下一次 BackendCoder 把这段错误反馈给 LLM。
    applier_feedback: list[str]
    applier_feedback_attempt: int
    llm_repair_count: int


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
                {"retry": "BackendCoder", "frontend": "BuildVerifier"},
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
                )
            try:
                files = self._write_bundle(state, bundle)
            except ApplierSafetyBlock as exc:
                return self._handle_applier_block(state, exc, started)
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
                # 诊断已消费：下一次普通生成不应继续携带旧错误。
                "applier_feedback": [],
                "applier_feedback_attempt": 0,
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
    ) -> tuple[GeneratedBundle, bool]:
        """把 Applier 诊断交给 LLM，返回 ``(修复后产物, 是否成功)``。

        LLM 失败必须降级回模板产物，而不是把整个 Run 变成“环境失败”：
        Applier 仍是最终裁决者，下一轮仍可继续拦截并重试。
        """
        attempt = int(state.get("applier_feedback_attempt") or 1)
        max_attempts = max(MAX_APPLIER_REPAIRS, MAX_JOIN_REPAIRS)
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
                bundle=bundle,
                dba_schema=dba_schema,
                diagnostics=feedback,
                attempt=attempt,
                max_attempts=max_attempts,
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
        audit = audit_contract(
            contract,
            mode=options.mode,
            code_issues=report.messages(),
        )
        self._emit(
            "fullstack.applier.blocked",
            {
                "runId": state["run_id"],
                "node": "BackendCoder",
                "attempt": retry_count,
                "maxAttempts": MAX_APPLIER_REPAIRS,
                "defectKind": audit.defect_kind,
                "issues": [issue.to_dict() for issue in report.issues],
                "metrics": FULLSTACK_METRICS.snapshot(),
            },
        )
        if not audit.ok:
            if retry_count > 0:
                FULLSTACK_METRICS.record_contract_misretry()
            self._node_failed("BackendCoder", state, RuntimeError(audit.summary()))
            raise FullstackContractContradictionError(audit, node="BackendCoder")
        # 校验器本身不可用（缺 node / Babel）属于环境缺陷，重跑生成器
        # 不会恢复；直接按环境失败上报，禁止伪装成可自愈的代码缺陷。
        if report.issues and all(
            issue.code == DIAG_AST_UNAVAILABLE for issue in report.issues
        ):
            self._node_failed("BackendCoder", state, RuntimeError(report.summary()))
            raise FullstackBuildError("ORM AST 校验器不可用，Applier 拒绝落盘")
        if retry_count >= MAX_APPLIER_REPAIRS:
            self._node_failed("BackendCoder", state, RuntimeError(report.summary()))
            raise FullstackGenerationError(
                "Applier AST 门禁连续拦截，代码自愈耗尽："
                + "；".join(report.messages()),
                node="BackendCoder",
            )
        next_count = retry_count + 1
        logger.warning(
            "Applier 拦截 %d 处代码缺陷，转 BackendCoder 重生成 [%d/%d] run_id=%s",
            len(report.issues),
            next_count,
            MAX_APPLIER_REPAIRS,
            state.get("run_id"),
        )
        self._node_completed(
            "BackendCoder",
            state,
            (
                f"Applier AST 门禁拦截 {len(report.issues)} 处代码缺陷，"
                f"转 BackendCoder 重新生成（第 {next_count}/{MAX_APPLIER_REPAIRS} 次）"
            ),
            [],
            started,
        )
        return {
            "needs_backend_retry": True,
            "applier_retry_count": next_count,
            # 下一轮 BackendCoder 不再盲目重跑模板，而是携带这些诊断
            # 调用 LLM；模板仍作为 LLM 不可用时的降级产物。
            "applier_feedback": report.messages(),
            "applier_feedback_attempt": next_count,
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
        # 契约审计先于任何修复：契约自相矛盾时，重试模板只会得到同一个
        # 错误。这里立即终止并把归因交给前端，引导用户重新澄清。
        options = state.get("options") or FullstackRunOptions()
        contract_audit = audit_contract(contract, mode=options.mode)
        if not contract_audit.ok:
            if self._has_retried(state):
                # 已经跑过至少一次自愈仍暴露出契约问题：这是一次“本可
                # 避免的误重试”，计入 Contract_Misretry_Count。
                FULLSTACK_METRICS.record_contract_misretry()
            logger.warning(
                "ConsistencyVerifier 契约自相矛盾，停止重试 run_id=%s problems=%s",
                state.get("run_id"),
                [problem.code for problem in contract_audit.problems],
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
            return {
                "consistency_ok": True,
                "needs_backend_repair": False,
                "needs_dba_repair": False,
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
            self._node_failed(
                "ConsistencyVerifier", state, RuntimeError(final_audit.summary())
            )
            raise FullstackContractContradictionError(final_audit)
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
