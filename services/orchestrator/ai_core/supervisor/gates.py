"""Supervisor ``both`` 模式强门控注册表。

门控解决的是“架构契约未确认，UI 修改已经写入沙盒”的乱序冲突：

    AWAITING_CONTRACT ──release──▶ RELEASING ──ui started──▶ RELEASED
            │                           │
            └──── architect 取消/失败 ───┴──▶ FAILED

关键约束：
  1. 同一项目同时只允许一个活跃门控（或正在创建门控的预留）；
  2. UI 子图在 dispatch 阶段绝不启动，只有 release 才启动且仅启动一次；
  3. 门控是进程内状态，与 Architect / UI Run 一样由 AI Core 单实例持有。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ai_core.domain.supervisor import (
    GateStatus,
    SubGraphDispatch,
    SupervisorGate,
    SupervisorRequest,
)

ACTIVE_GATE_STATUSES = {"AWAITING_CONTRACT", "RELEASING"}


class SupervisorGateError(Exception):
    code = "SUPERVISOR_GATE_ERROR"
    status_code = 409


class SupervisorGateNotFoundError(SupervisorGateError):
    code = "SUPERVISOR_GATE_NOT_FOUND"
    status_code = 404


class SupervisorProjectGateActiveError(SupervisorGateError):
    code = "SUPERVISOR_PROJECT_GATE_ACTIVE"
    status_code = 409


class SupervisorGateStateError(SupervisorGateError):
    code = "SUPERVISOR_GATE_STATE_INVALID"
    status_code = 409


class SupervisorGateReleasingError(SupervisorGateStateError):
    code = "SUPERVISOR_GATE_RELEASING"


@dataclass
class GateRecord:
    gate_id: str
    project_id: str
    architect_run_id: str
    request: SupervisorRequest
    status: GateStatus = "AWAITING_CONTRACT"
    ui_dispatch: SubGraphDispatch | None = None
    message: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    released_at: datetime | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_GATE_STATUSES

    def to_view(self) -> SupervisorGate:
        return SupervisorGate(
            gate_id=self.gate_id,
            project_id=self.project_id,
            status=self.status,
            architect_run_id=self.architect_run_id,
            ui_run_id=self.ui_dispatch.run_id if self.ui_dispatch else None,
            ui_stream_path=self.ui_dispatch.stream_path if self.ui_dispatch else None,
            message=self.message,
            created_at=self.created_at,
            released_at=self.released_at,
            updated_at=self.updated_at,
        )


class SupervisorGateRegistry:
    """线程安全的门控表 + 项目级互斥预留。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._gates: dict[str, GateRecord] = {}
        self._project_gate: dict[str, str] = {}
        # projectId -> reservation token；Architect 启动完成前也占住项目，
        # 防止并发的 UI 派发绕过门控。
        self._reservations: dict[str, str] = {}

    # --------------------------------------------------------
    # 项目级互斥
    # --------------------------------------------------------
    def reserve_project(self, project_id: str) -> str:
        with self._lock:
            self._raise_if_project_busy(project_id)
            token = uuid.uuid4().hex
            self._reservations[project_id] = token
            return token

    def release_reservation(self, project_id: str, token: str) -> None:
        with self._lock:
            if self._reservations.get(project_id) == token:
                self._reservations.pop(project_id, None)

    def ensure_project_available(self, project_id: str) -> None:
        with self._lock:
            self._raise_if_project_busy(project_id)

    def _raise_if_project_busy(self, project_id: str) -> None:
        if project_id in self._reservations:
            raise SupervisorProjectGateActiveError(
                f"项目 {project_id} 正在创建 both 门控，请稍后重试"
            )
        gate_id = self._project_gate.get(project_id)
        if gate_id:
            gate = self._gates.get(gate_id)
            if gate is not None and gate.active:
                raise SupervisorProjectGateActiveError(
                    "项目存在待确认契约的 both 门控，"
                    f"必须先确认契约或取消架构澄清：{gate.gate_id}"
                )
            self._project_gate.pop(project_id, None)

    # --------------------------------------------------------
    # 生命周期
    # --------------------------------------------------------
    def create(
        self,
        project_id: str,
        architect_run_id: str,
        request: SupervisorRequest,
        *,
        reservation: str | None = None,
    ) -> GateRecord:
        with self._lock:
            if reservation is not None:
                if self._reservations.get(project_id) != reservation:
                    raise SupervisorGateStateError("门控预留已失效")
                self._reservations.pop(project_id, None)
            self._raise_if_project_busy(project_id)
            gate = GateRecord(
                gate_id=f"gate_{uuid.uuid4().hex[:20]}",
                project_id=project_id,
                architect_run_id=architect_run_id,
                request=request.model_copy(deep=True),
            )
            self._gates[gate.gate_id] = gate
            self._project_gate[project_id] = gate.gate_id
            return gate

    def get(self, gate_id: str) -> GateRecord:
        with self._lock:
            gate = self._gates.get(gate_id)
            if gate is None:
                raise SupervisorGateNotFoundError(f"门控不存在: {gate_id}")
            return gate

    def active_for_project(self, project_id: str) -> GateRecord | None:
        with self._lock:
            gate_id = self._project_gate.get(project_id)
            if not gate_id:
                return None
            gate = self._gates.get(gate_id)
            return gate if gate and gate.active else None

    def begin_release(self, gate_id: str) -> GateRecord:
        """抢占 release 权；同一门控只允许一个调用方真正启动 UI。"""
        with self._lock:
            gate = self.get(gate_id)
            if gate.status == "RELEASING":
                raise SupervisorGateReleasingError("门控正在释放，请稍后查询状态")
            if gate.status != "AWAITING_CONTRACT":
                raise SupervisorGateStateError(
                    f"门控当前状态为 {gate.status}，不可释放"
                )
            gate.status = "RELEASING"
            gate.updated_at = datetime.now(timezone.utc)
            return gate

    def complete_release(
        self,
        gate_id: str,
        dispatch: SubGraphDispatch,
    ) -> GateRecord:
        with self._lock:
            gate = self.get(gate_id)
            if gate.status == "RELEASED":
                # 幂等：保留第一次的 UI Run，不重复启动。
                if gate.ui_dispatch is None:
                    gate.ui_dispatch = dispatch
                return gate
            if gate.status != "RELEASING":
                raise SupervisorGateStateError(
                    f"门控当前状态为 {gate.status}，不可完成释放"
                )
            now = datetime.now(timezone.utc)
            gate.ui_dispatch = dispatch
            gate.status = "RELEASED"
            gate.released_at = now
            gate.updated_at = now
            return gate

    def fail(self, gate_id: str, message: str) -> GateRecord:
        """门控失败 / 中止；失败后项目可重新发起请求。"""
        with self._lock:
            gate = self.get(gate_id)
            if gate.status == "RELEASED":
                return gate
            gate.status = "FAILED"
            gate.message = message
            gate.updated_at = datetime.now(timezone.utc)
            if self._project_gate.get(gate.project_id) == gate_id:
                self._project_gate.pop(gate.project_id, None)
            return gate

    def restore_awaiting(self, gate_id: str, message: str | None = None) -> None:
        """release 条件未满足（如契约尚未确认）时回退，允许用户稍后重试。"""
        with self._lock:
            gate = self.get(gate_id)
            if gate.status != "RELEASING":
                return
            gate.status = "AWAITING_CONTRACT"
            gate.message = message
            gate.updated_at = datetime.now(timezone.utc)

    def snapshot(self, gate: GateRecord) -> dict[str, Any]:
        """测试 / 调试用的只读快照。"""
        with self._lock:
            return {
                "gateId": gate.gate_id,
                "status": gate.status,
                "projectId": gate.project_id,
                "architectRunId": gate.architect_run_id,
                "uiRunId": gate.ui_dispatch.run_id if gate.ui_dispatch else None,
            }


_GATE_REGISTRY = SupervisorGateRegistry()


def get_gate_registry() -> SupervisorGateRegistry:
    return _GATE_REGISTRY
