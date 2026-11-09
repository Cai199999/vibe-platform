"""全栈生成执行服务：契约解析、沙盒准备、后台 Graph 与 SSE 事件。

一个 Fullstack Run 的生命周期：

    POST /runs/stream
      -> 校验契约已 CONFIRMED（且必须是 full_stack_system）
      -> 复用 / 创建沙盒
      -> 后台线程执行 FullstackGraph
      -> SSE 输出 DBACoder / BackendCoder / FrontendCoder / 构建 / 重启
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
from ai_core.domain.architect import ArchitectContract
from ai_core.domain.fullstack import (
    FullstackRunRequest,
    FullstackRunSnapshot,
    FullstackStep,
)
from ai_core.fullstack.coders import collection_path, extract_table_name
from ai_core.fullstack.graph import (
    FullstackGenerationError,
    FullstackGraphRunner,
    FullstackState,
)
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
    table: str = ""
    collection_path: str = ""
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

        sandbox_id, preview_url, created = self._ensure_sandbox(request)
        active = ActiveFullstackRun(
            run_id=run_id,
            fingerprint=fingerprint,
            request=request,
            contract=contract,
            buffer=get_event_buffer(run_id),
            sandbox_id=sandbox_id,
            preview_url=preview_url,
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

        active.buffer.emit(
            "fullstack.started",
            {
                "runId": run_id,
                "projectId": request.project_id,
                "contractId": contract.contract_id,
                "sandboxId": sandbox_id,
                "previewUrl": preview_url,
                "appShape": contract.app_shape,
                "nodes": [
                    "DBACoder",
                    "BackendCoder",
                    "FrontendCoder",
                    "BuildVerifier",
                    "SandboxRestart",
                ],
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
        started = time.monotonic()
        runner = FullstackGraphRunner(self._sandbox, event_sink=active.buffer)
        initial: FullstackState = {
            "run_id": active.run_id,
            "project_id": active.request.project_id,
            "sandbox_id": active.sandbox_id,
            "preview_url": active.preview_url,
            "contract": active.contract,
            "options": active.request.options,
            "files": [],
        }
        try:
            outcome = runner.run(initial)
            with _ACTIVE_LOCK:
                active.files = list(outcome.get("files") or [])
                active.table = str(outcome.get("table") or "")
                active.collection_path = str(outcome.get("collection_path") or "")
                active.preview_url = str(
                    outcome.get("preview_url") or active.preview_url
                )
                active.status = "SUCCEEDED"
                active.touch()
            active.buffer.emit(
                "fullstack.completed",
                {
                    "runId": active.run_id,
                    "projectId": active.request.project_id,
                    "contractId": active.contract.contract_id,
                    "sandboxId": active.sandbox_id,
                    "previewUrl": active.preview_url,
                    "status": "SUCCEEDED",
                    "table": active.table,
                    "collectionPath": active.collection_path,
                    "files": active.files,
                    "dbaFiles": list(outcome.get("dba_files") or []),
                    "backendFiles": list(outcome.get("backend_files") or []),
                    "frontendFiles": list(outcome.get("frontend_files") or []),
                    "elapsedMs": int((time.monotonic() - started) * 1000),
                },
                dedupe_key=f"fullstack.completed:{active.run_id}",
            )
        except FullstackGenerationError as exc:
            self._complete_failure(active, str(exc), exc.code, node=exc.node)
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

    def _complete_failure(
        self,
        active: ActiveFullstackRun,
        error: str,
        code: str,
        *,
        node: str | None = None,
    ) -> None:
        with _ACTIVE_LOCK:
            active.status = "FAILED"
            active.error = error
            active.error_code = code
            active.touch()
        active.buffer.emit(
            "fullstack.failed",
            {
                "runId": active.run_id,
                "status": "FAILED",
                "contractId": active.contract.contract_id,
                "sandboxId": active.sandbox_id,
                "node": node,
                "error": error,
                "errorCode": code,
            },
            dedupe_key=f"fullstack.failed:{active.run_id}:{code}",
        )

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
        if contract.app_shape != "full_stack_system":
            raise FullstackContractInvalidError(
                f"当前契约形态为 {contract.app_shape}，只有 full_stack_system "
                "需要生成后端与数据库代码"
            )
        if not contract.api_routes:
            raise FullstackContractInvalidError("全栈契约缺少 apiRoutes")
        if not (contract.database_ddl or "").strip():
            raise FullstackContractInvalidError("全栈契约缺少 databaseDdl")
        return contract

    def _ensure_sandbox(self, request: FullstackRunRequest) -> tuple[str, str, bool]:
        """返回 ``(sandbox_id, preview_url, created)``。

        优先复用请求里的 sandboxId / source.sandboxId；容器已不存在时，
        若有源码则回退为新建，保证刷新后的旧页面仍可继续生成。
        """
        sandbox_id = request.sandbox_id or request.source.sandbox_id
        if sandbox_id:
            try:
                handle = self._sandbox.get(sandbox_id)
                return sandbox_id, handle.preview_url, False
            except SandboxError:
                if not request.source.files:
                    raise FullstackSandboxRequiredError(
                        f"沙盒不存在: {sandbox_id}，且请求未携带源码"
                    ) from None

        if not request.source.files:
            raise FullstackSandboxRequiredError(
                "全栈生成需要 sandboxId 或 source.files 中的项目源码"
            )
        try:
            handle = self._sandbox.create(
                request.project_id,
                dict(request.source.files),
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
                last_seq=active.buffer.last_seq,
                created_at=active.created_at,
                updated_at=active.updated_at,
            )

    def summary_for_request(self, contract: ArchitectContract) -> dict[str, Any]:
        """契约摘要：供诊断脚本 / 测试核对生成目标。"""
        table = extract_table_name(contract.database_ddl)
        return {
            "contractId": contract.contract_id,
            "appShape": contract.app_shape,
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
    "get_active_fullstack_run",
]
