"""全栈生成执行服务：契约解析、沙盒准备、后台 Graph 与 SSE 事件。

一个 Fullstack Run 的生命周期：

    POST /runs/stream
      -> 校验契约已 CONFIRMED（且必须是 full_stack_system）
      -> 复用 / 创建沙盒
      -> 后台线程执行 FullstackGraph
      -> SSE 输出 DBACoder / BackendCoder / FrontendCoder / InfraCoder /
         接口一致性检查 / 构建 / 重启
      -> fullstack.completed | fullstack.failed（终态）

事件 seq 由 ``RunEventBuffer`` 唯一分配；BFF 与前端只透传 / 重放。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ai_core.agent.events import RunEventBuffer, get_event_buffer
from ai_core.architect.service import find_architect_contract
from ai_core.codegen.visual import VisualCodegenError, compile_visual_document
from ai_core.codegen.visual_gate import VisualCodegenGateUnavailable
from ai_core.domain.architect import ArchitectContract
from ai_core.error_envelope import attach_error_envelope, envelope_from_code
from ai_core.project_lock import ProjectBusyError, project_write_lock
from ai_core.config import get_settings
from ai_core.direct_ops.changelog import get_change_log_store
from ai_core.domain.direct_ops import ChangeLogEntry
from ai_core.domain.fullstack import (
    FullstackRunRequest,
    FullstackRunSnapshot,
    FullstackStep,
)
from ai_core.domain.project_mode import EXISTING_SOURCE
from ai_core.fullstack.coders import (
    collection_path,
    contract_database_engine,
    core_table_for_contract,
)
from ai_core.fullstack.contract_audit import audit_contract
from ai_core.fullstack.graph import (
    MODE_NODE_ORDERS,
    FullstackGenerationError,
    FullstackGraphRunner,
    FullstackState,
)
from ai_core.fullstack.quality import (
    L1_PROTOTYPE,
    L1_WARNING_TEXT,
    quality_policy,
    resolve_quality_tier,
    warning_badge,
)
from ai_core.observability.fullstack_metrics import FULLSTACK_METRICS
from ai_core.ports.sandbox import SandboxError, SandboxProvider

logger = logging.getLogger(__name__)

TERMINAL_FULLSTACK_STATUSES = {"SUCCEEDED", "FAILED"}


# ------------------------------------------------------------
# 领域异常
# ------------------------------------------------------------
class FullstackServiceError(Exception):
    code = "FULLSTACK_ERROR"
    status_code = 500


class FullstackContractNotFoundError(FullstackServiceError):
    code = "FULLSTACK_CONTRACT_NOT_FOUND"
    status_code = 404


class FullstackContractNotConfirmedError(FullstackServiceError):
    code = "FULLSTACK_CONTRACT_NOT_CONFIRMED"
    status_code = 409


class FullstackContractInvalidError(FullstackServiceError):
    code = "FULLSTACK_CONTRACT_INVALID"
    status_code = 422


class FullstackVisualCodegenError(FullstackServiceError):
    """冻结 VisualDocument 无法安全编译为 React 工程。"""

    code = "VISUAL_CODEGEN_FAILED"
    status_code = 422


class FullstackSandboxRequiredError(FullstackServiceError):
    code = "FULLSTACK_SANDBOX_REQUIRED"
    status_code = 422


class FullstackRunNotFoundError(FullstackServiceError):
    code = "FULLSTACK_RUN_NOT_FOUND"
    status_code = 404


class FullstackRunIdConflictError(FullstackServiceError):
    code = "FULLSTACK_RUN_ID_CONFLICT"
    status_code = 409


# ------------------------------------------------------------
# 运行期数据
# ------------------------------------------------------------
@dataclass
class ActiveFullstackRun:
    run_id: str
    fingerprint: str
    request: FullstackRunRequest
    contract: ArchitectContract
    buffer: RunEventBuffer
    sandbox_id: str
    preview_url: str
    status: str = "RUNNING"
    files: list[str] = field(default_factory=list)
    steps: list[FullstackStep] = field(default_factory=list)
    error: str | None = None
    error_code: str | None = None
    # 失败归因：CODE（代码缺陷）/ CONTRACT（契约矛盾）/ ENVIRONMENT。
    failure_kind: str | None = None
    # 缺陷归因：CODE_DEFECT / CONTRACT_DEFECT（见 contract_audit）。
    defect_kind: str | None = None
    needs_clarification: bool = False
    clarification: dict[str, Any] | None = None
    # 前端骨架模式下生成的完整 src/App.jsx，用于前端把真实源码写回项目。
    frontend_entry: str = ""
    # 本次是否使用冻结 VisualDocument 做 CodeGen；前端据此切换画布模式。
    visual_document_id: str = ""
    visual_node_count: int = 0
    table: str = ""
    collection_path: str = ""
    # 风险自适应门禁：L1 的非阻塞告警 / L3 验收结论随快照返回。
    quality_tier: str = "L2_STANDARD"
    warnings: list[str] = field(default_factory=list)
    mysql_check: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_FULLSTACK_STATUSES

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)


_ACTIVE_RUNS: dict[str, ActiveFullstackRun] = {}
_ACTIVE_LOCK = threading.RLock()


def get_active_fullstack_run(run_id: str) -> ActiveFullstackRun | None:
    with _ACTIVE_LOCK:
        return _ACTIVE_RUNS.get(run_id)


def _request_fingerprint(request: FullstackRunRequest) -> str:
    raw = json.dumps(
        request.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class FullstackService:
    """全栈生成入口。"""

    def __init__(self, sandbox: SandboxProvider | None = None) -> None:
        # 延迟 import 组合根，避免测试注入 Fake Sandbox 时触发 Docker 初始化。
        if sandbox is None:
            from ai_core.sandbox import get_sandbox_provider

            sandbox = get_sandbox_provider()
        self._sandbox = sandbox
        self._settings = get_settings()

    # --------------------------------------------------------
    # 启动
    # --------------------------------------------------------
    def start_stream(self, request: FullstackRunRequest) -> ActiveFullstackRun:
        """同步校验与准备沙盒，随后启动后台 Graph。"""
        contract = self._resolve_contract(request)
        run_id = request.run_id or f"fsk_{uuid.uuid4().hex[:20]}"
        fingerprint = _request_fingerprint(request)

        with _ACTIVE_LOCK:
            existing = _ACTIVE_RUNS.get(run_id)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise FullstackRunIdConflictError(
                        f"Fullstack runId 已被其它请求占用: {run_id}"
                    )
                return existing

        sandbox_id, preview_url, created = self._ensure_sandbox(request, contract)
        logger.info(
            "fullstack start run_id=%s project_id=%s contract_id=%s "
            "sandbox_id=%s created=%s",
            run_id,
            request.project_id,
            contract.contract_id,
            sandbox_id,
            created,
        )
        active = ActiveFullstackRun(
            run_id=run_id,
            fingerprint=fingerprint,
            request=request,
            contract=contract,
            buffer=get_event_buffer(run_id),
            sandbox_id=sandbox_id,
            preview_url=preview_url,
            quality_tier=resolve_quality_tier(contract),
        )
        with _ACTIVE_LOCK:
            existing = _ACTIVE_RUNS.get(run_id)
            if existing is not None:
                # 并发请求同一 runId：后到者回放先到者，若本次新建了沙盒
                # 且未被复用，尽力清理，避免泄漏容器。
                if created:
                    self._destroy_quietly(sandbox_id)
                return existing
            _ACTIVE_RUNS[run_id] = active

        mode = request.options.mode
        # 每种模式只广播自己真实执行的节点，前端进度条不会出现“假节点”。
        nodes = MODE_NODE_ORDERS.get(mode, MODE_NODE_ORDERS["fullstack"])
        active.buffer.emit(
            "fullstack.started",
            {
                "runId": run_id,
                "projectId": request.project_id,
                "contractId": contract.contract_id,
                "sandboxId": sandbox_id,
                "previewUrl": preview_url,
                "appShape": contract.app_shape,
                "qualityTier": resolve_quality_tier(contract),
                "qualityPolicy": quality_policy(contract).to_dict(),
                "mode": mode,
                "databaseEngine": contract_database_engine(contract),
                "orm": "sequelize",
                "nodes": nodes,
            },
            dedupe_key=f"fullstack.started:{run_id}",
        )
        threading.Thread(
            target=self._run_worker,
            args=(active,),
            name=f"fullstack-{run_id}",
            daemon=True,
        ).start()
        return active

    def _run_worker(self, active: ActiveFullstackRun) -> None:
        """Fullstack 全程写主工作树，必须与 Direct Ops / Promote 串行。"""
        try:
            with project_write_lock.lease(
                active.request.project_id,
                owner=f"fullstack:{active.run_id}",
                timeout=self._settings.project_write_lock_timeout_seconds,
            ):
                self._run_worker_locked(active)
        except ProjectBusyError:
            logger.warning("fullstack project lock busy run_id=%s", active.run_id)
            self._complete_failure(
                active,
                "项目正在被其它操作写入，请稍后重试。",
                "PROJECT_BUSY",
            )

    def _run_worker_locked(self, active: ActiveFullstackRun) -> None:
        started = time.monotonic()
        policy = quality_policy(active.contract)
        # 契约结构审计先于任何生成节点：矛盾契约即使重跑一百次也会得到
        # 同一个错误。L1 已选择 Demo 级质量，降级为告警继续生成。
        audit = audit_contract(active.contract, mode=active.request.options.mode)
        warnings: list[str] = []
        if not audit.ok:
            if policy.blocking:
                self._complete_failure(
                    active,
                    audit.summary(),
                    "CONTRACT_REVISION_REQUIRED",
                    node="ConsistencyVerifier",
                    failure_kind="CONTRACT",
                    defect_kind=audit.defect_kind,
                    needs_clarification=True,
                    clarification=audit.payload(),
                )
                return
            warnings.append(f"L1 敏捷模式跳过契约审计阻断：{audit.summary()}")

        runner = FullstackGraphRunner(self._sandbox, event_sink=active.buffer)
        visual_document = (
            active.request.visual_document or active.contract.visual_document
        )
        if visual_document is not None:
            active.visual_document_id = visual_document.doc_id
        initial: FullstackState = {
            "run_id": active.run_id,
            "project_id": active.request.project_id,
            "sandbox_id": active.sandbox_id,
            "preview_url": active.preview_url,
            "contract": active.contract,
            "options": active.request.options,
            "visual_document": visual_document,
            "files": [],
            "quality_tier": policy.tier,
            "warnings": warnings,
            "build_retry_count": 0,
        }
        try:
            outcome = runner.run(initial)
            with _ACTIVE_LOCK:
                active.files = list(outcome.get("files") or [])
                active.frontend_entry = str(outcome.get("frontend_entry") or "")
                active.visual_node_count = int(outcome.get("visual_node_count") or 0)
                active.table = str(outcome.get("table") or "")
                active.collection_path = str(outcome.get("collection_path") or "")
                active.preview_url = str(
                    outcome.get("preview_url") or active.preview_url
                )
                active.quality_tier = resolve_quality_tier(active.contract)
                active.warnings = list(outcome.get("warnings") or [])
                # L1 成功也必须明确提示“未经过严格校验”，徽标文案由后端
                # 统一提供，避免前端各写一份。
                if (
                    active.quality_tier == L1_PROTOTYPE
                    and L1_WARNING_TEXT not in active.warnings
                ):
                    active.warnings.append(L1_WARNING_TEXT)
                active.mysql_check = dict(outcome.get("mysql_check") or {})
                active.status = "SUCCEEDED"
                active.touch()
            self._record_visual_codegen_changelog(active)
            active.buffer.emit(
                "fullstack.completed",
                {
                    "runId": active.run_id,
                    "projectId": active.request.project_id,
                    "contractId": active.contract.contract_id,
                    "sandboxId": active.sandbox_id,
                    "previewUrl": active.preview_url,
                    "status": "SUCCEEDED",
                    "mode": active.request.options.mode,
                    "table": active.table,
                    "collectionPath": active.collection_path,
                    "files": active.files,
                    "qualityTier": active.quality_tier,
                    "qualityLabel": quality_policy(active.contract).label,
                    "qualityBadge": warning_badge(active.quality_tier),
                    "warnings": list(active.warnings),
                    # L1/L2 不执行真实迁移验收，空字典必须回传 null，
                    # 否则前端会误以为“验收失败”。
                    "mysqlCheck": dict(active.mysql_check) or None,
                    "databaseEngine": contract_database_engine(active.contract),
                    "orm": "sequelize",
                    "dbaFiles": list(outcome.get("dba_files") or []),
                    "backendFiles": list(outcome.get("backend_files") or []),
                    "frontendFiles": list(outcome.get("frontend_files") or []),
                    # 前端骨架 / Visual CodeGen 携带完整入口源码；
                    # 源码不受 node.output 截断影响，前端可继续编辑。
                    "frontendEntry": (
                        active.frontend_entry
                        if active.request.options.mode == "frontend"
                        or active.visual_document_id
                        else ""
                    ),
                    "visualDocumentId": active.visual_document_id or None,
                    "visualNodeCount": active.visual_node_count or None,
                    "infraFiles": list(outcome.get("infra_files") or []),
                    # 四个核心指标的当前聚合快照，便于前端 / 监控直接消费。
                    "metrics": FULLSTACK_METRICS.snapshot(),
                    "elapsedMs": int((time.monotonic() - started) * 1000),
                },
                dedupe_key=f"fullstack.completed:{active.run_id}",
            )
        except FullstackGenerationError as exc:
            self._complete_failure(
                active,
                str(exc),
                exc.code,
                node=exc.node,
                failure_kind=getattr(exc, "failure_kind", "CODE"),
                defect_kind=getattr(exc, "defect_kind", "CODE_DEFECT"),
                needs_clarification=bool(getattr(exc, "needs_clarification", False)),
                clarification=getattr(exc, "clarification", None),
            )
        except SandboxError as exc:
            self._complete_failure(
                active, f"沙盒操作失败: {exc}", "FULLSTACK_SANDBOX_ERROR"
            )
        except Exception as exc:  # noqa: BLE001 - 后台线程必须收敛
            logger.exception("fullstack run failed run_id=%s", active.run_id)
            self._complete_failure(
                active,
                f"全栈生成异常: {exc}",
                "FULLSTACK_INTERNAL_ERROR",
            )

    def _record_visual_codegen_changelog(self, active: ActiveFullstackRun) -> None:
        """CodeGen 成功写入 ChangeLog；失败不能影响已完成的生成。"""
        if not active.visual_document_id:
            return
        document = active.request.visual_document or active.contract.visual_document
        content_hash = str(document.metadata.content_hash or "") if document else ""
        try:
            get_change_log_store().append(
                ChangeLogEntry(
                    change_id="chg_" + uuid.uuid4().hex[:20],
                    project_id=active.request.project_id,
                    sandbox_id=active.sandbox_id,
                    run_id=active.run_id,
                    actor="AI_RUN",
                    action="VISUAL_CODEGEN",
                    target={
                        "type": "VISUAL_DOCUMENT",
                        "documentId": active.visual_document_id,
                        "nodeCount": active.visual_node_count,
                        "mode": active.request.options.mode,
                    },
                    summary=(f"成功从冻结版本 {content_hash} 编译生成真实前端工程"),
                    diff_summary="；".join(active.files[:20]),
                    after_hash=content_hash,
                    risk="L1_VERIFY_DIRECT",
                    applied=True,
                    created_at=datetime.now(timezone.utc),
                )
            )
        except Exception:  # noqa: BLE001 - 日志失败不篡改生成结论
            logger.warning(
                "failed to record visual codegen changelog run_id=%s",
                active.run_id,
                exc_info=True,
            )

    def _complete_failure(
        self,
        active: ActiveFullstackRun,
        error: str,
        code: str,
        *,
        node: str | None = None,
        failure_kind: str = "CODE",
        defect_kind: str | None = None,
        needs_clarification: bool = False,
        clarification: dict[str, Any] | None = None,
    ) -> None:
        envelope = envelope_from_code(
            code,
            technical_message=error,
            trace_id=getattr(active.buffer, "trace_id", None),
            run_id=active.run_id,
            stage=f"fullstack.{node or 'unknown'}",
        )
        # 先发终态事件，再改快照状态：测试 / 前端一旦观察到 FAILED，
        # 就能确定对应的 fullstack.failed 已经在缓冲区内，避免竞态。
        active.buffer.emit(
            "fullstack.failed",
            attach_error_envelope(
                {
                    "runId": active.run_id,
                    "status": "FAILED",
                    "mode": active.request.options.mode,
                    "contractId": active.contract.contract_id,
                    "sandboxId": active.sandbox_id,
                    "qualityTier": active.quality_tier,
                    "warnings": list(active.warnings),
                    "node": node,
                    "error": envelope.user_message,
                    "errorCode": code,
                    # 前端据此决定是“重试生成”还是“回到契约澄清”。
                    "failureKind": failure_kind,
                    "defectKind": defect_kind,
                    "needsClarification": needs_clarification,
                    "clarification": clarification or {},
                    "metrics": FULLSTACK_METRICS.snapshot(),
                },
                envelope,
            ),
            dedupe_key=f"fullstack.failed:{active.run_id}:{code}",
        )
        with _ACTIVE_LOCK:
            active.status = "FAILED"
            active.error = error
            active.error_code = code
            active.failure_kind = failure_kind
            active.defect_kind = defect_kind
            active.needs_clarification = needs_clarification
            active.clarification = clarification
            active.touch()

    # --------------------------------------------------------
    # 契约 / 沙盒
    # --------------------------------------------------------
    def _resolve_contract(self, request: FullstackRunRequest) -> ArchitectContract:
        contract = request.contract or find_architect_contract(request.contract_id)
        if contract is None:
            raise FullstackContractNotFoundError(
                f"未找到已确认的实施契约: {request.contract_id}"
            )
        if contract.contract_id != request.contract_id:
            raise FullstackContractInvalidError("contractId 与契约内容不一致")
        if contract.status != "CONFIRMED":
            raise FullstackContractNotConfirmedError(
                f"契约当前状态为 {contract.status}，请先确认后再生成代码"
            )
        # 存量项目：源码是唯一事实源，绝不能触发前端 CodeGen。
        # 不能只信契约里的 project_mode（可能是旧契约 / 前端回传快照）：
        # 前端本次 Run 显式声明的 projectMode 优先级最高。
        #
        # 注意：不能用 request.source.files 反推模式——新建项目也会携带
        # 内置模板 / 当前入口源码，按 files 判定会把首次 CodeGen 误杀。
        declared_modes = {
            value
            for value in (request.options.project_mode, contract.project_mode)
            if value
        }
        if EXISTING_SOURCE in declared_modes and request.options.mode in {
            "frontend",
            "fullstack",
        }:
            raise FullstackContractInvalidError(
                "存量项目禁止前端 CodeGen；请使用 backend 增量模式"
            )
        if request.options.mode == "frontend":
            # 前端骨架只要求 Web 形态；脚本契约没有可生成的 React 页面。
            if contract.app_shape == "script_automation":
                raise FullstackContractInvalidError(
                    "脚本自动化契约不包含前端页面，无法初始化前端骨架"
                )
            # 离线优先链路：VisualDocument 就是前端唯一事实源，
            # frontendRoutes / components 只服务旧骨架生成，不能反过来
            # 阻断已经冻结的虚拟界面编译。
            visual = request.visual_document or contract.visual_document
            if visual is None and (
                not contract.frontend_routes or not contract.components
            ):
                raise FullstackContractInvalidError(
                    "契约缺少 frontendRoutes 或 components，无法生成前端骨架"
                )
            return contract
        if contract.app_shape != "full_stack_system":
            raise FullstackContractInvalidError(
                f"当前契约形态为 {contract.app_shape}，只有 full_stack_system "
                "需要生成后端与数据库代码"
            )
        # 专属子图按需校验：DBA 只要 DDL，BackendCoder 只要 API 路由，
        # 完整全栈两者都必须存在。避免纯建表请求被“缺少 apiRoutes”误拦。
        mode = request.options.mode
        if mode in {"fullstack", "backend"} and not contract.api_routes:
            raise FullstackContractInvalidError("契约缺少 apiRoutes，无法生成后端路由")
        if mode in {"fullstack", "dba"} and not (contract.database_ddl or "").strip():
            raise FullstackContractInvalidError(
                "契约缺少 databaseDdl，无法生成数据库层"
            )
        return contract

    def _ensure_sandbox(
        self,
        request: FullstackRunRequest,
        contract: ArchitectContract,
    ) -> tuple[str, str, bool]:
        """返回 ``(sandbox_id, preview_url, created)``。

        优先复用请求里的 sandboxId / source.sandboxId；容器已不存在时，
        若有源码则回退为新建，保证刷新后的旧页面仍可继续生成。

        离线优先链路：VisualDocument 的完整 Vite 工程必须先于沙盒启动
        写入容器，否则 package.json / index.html 缺失会让首轮 dev 失败。
        """
        source_files = dict(request.source.files)
        visual_document = request.visual_document or contract.visual_document
        if request.options.mode == "frontend" and visual_document is not None:
            try:
                compiled = compile_visual_document(visual_document)
            except VisualCodegenGateUnavailable as exc:
                raise FullstackVisualCodegenError(
                    f"CodeGen AST 门禁不可用: {exc}"
                ) from exc
            except VisualCodegenError as exc:
                raise FullstackVisualCodegenError(
                    f"Visual CodeGen 失败: {exc}"
                ) from exc
            # 生成工程是权威文件：覆盖模板里的旧 App.jsx / package.json。
            source_files.update(compiled.files)

        sandbox_id = request.sandbox_id or request.source.sandbox_id
        if sandbox_id:
            try:
                handle = self._sandbox.get(sandbox_id)
                return sandbox_id, handle.preview_url, False
            except SandboxError:
                if not source_files and request.options.mode != "frontend":
                    raise FullstackSandboxRequiredError(
                        f"沙盒不存在: {sandbox_id}，且请求未携带源码"
                    ) from None

        if not source_files and request.options.mode != "frontend":
            raise FullstackSandboxRequiredError(
                "全栈生成需要 sandboxId 或 source.files 中的项目源码"
            )
        try:
            # 前端骨架允许空 files：沙盒镜像自带 React/Vite 模板，
            # FrontendCoder 会立即写入按契约生成的 src/App.jsx。
            handle = self._sandbox.create(
                request.project_id,
                source_files,
            )
        except SandboxError as exc:
            raise FullstackServiceError(f"创建沙盒失败: {exc}") from exc
        return handle.sandbox_id, handle.preview_url, True

    def _destroy_quietly(self, sandbox_id: str) -> None:
        try:
            self._sandbox.destroy(sandbox_id)
        except Exception:  # noqa: BLE001 - 清理失败不改变主流程
            logger.warning("fullstack cleanup sandbox failed: %s", sandbox_id)

    # --------------------------------------------------------
    # 查询
    # --------------------------------------------------------
    def snapshot(self, run_id: str) -> FullstackRunSnapshot:
        active = get_active_fullstack_run(run_id)
        if active is None:
            raise FullstackRunNotFoundError(f"Fullstack Run 不存在: {run_id}")
        with _ACTIVE_LOCK:
            return FullstackRunSnapshot(
                run_id=active.run_id,
                project_id=active.request.project_id,
                contract_id=active.contract.contract_id,
                sandbox_id=active.sandbox_id,
                status=active.status,  # type: ignore[arg-type]
                preview_url=active.preview_url,
                steps=[step.model_copy(deep=True) for step in active.steps],
                files=list(active.files),
                error=active.error,
                error_code=active.error_code,
                failure_kind=active.failure_kind,
                defect_kind=active.defect_kind,
                needs_clarification=active.needs_clarification,
                clarification=active.clarification or {},
                quality_tier=active.quality_tier,
                quality_badge=warning_badge(active.quality_tier),
                warnings=list(active.warnings),
                mysql_check=dict(active.mysql_check),
                last_seq=active.buffer.last_seq,
                created_at=active.created_at,
                updated_at=active.updated_at,
            )

    def summary_for_request(self, contract: ArchitectContract) -> dict[str, Any]:
        """契约摘要：供诊断脚本 / 测试核对生成目标。"""
        table = core_table_for_contract(contract).name
        return {
            "contractId": contract.contract_id,
            "appShape": contract.app_shape,
            "databaseEngine": contract_database_engine(contract),
            "orm": "sequelize",
            "table": table,
            "collectionPath": collection_path(contract.api_routes),
            "routeCount": len(contract.api_routes),
        }


__all__ = [
    "ActiveFullstackRun",
    "FullstackContractInvalidError",
    "FullstackContractNotConfirmedError",
    "FullstackContractNotFoundError",
    "FullstackRunIdConflictError",
    "FullstackRunNotFoundError",
    "FullstackSandboxRequiredError",
    "FullstackService",
    "FullstackServiceError",
    "FullstackVisualCodegenError",
    "get_active_fullstack_run",
]
