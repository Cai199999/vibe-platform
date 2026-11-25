"""快速通道：标准链路 + 零异常时允许子 Agent 直连。

约束：
- 授权只能由 Architect 预先发放，子 Agent 不能自授权；
- 只允许标准链路白名单内的边；
- 出现错误、重试、澄清、风险拦截或超步数，立即收回授权；
- 直连期间仍必须向 Architect 发送进度摘要（可见，但不阻塞）。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

from ai_core.a2a_contract import A2A_ESCALATION_KINDS, A2A_REPAIR_KINDS

try:  # PyYAML 在运行镜像中存在；纯测试环境缺失时回退默认白名单。
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

logger = logging.getLogger(__name__)

FastEdge = tuple[str, str, str, str]

DEFAULT_FAST_EDGES: frozenset[FastEdge] = frozenset(
    {
        ("DBACoder", "BackendCoder", "schema_ready", "REBIND_ORM"),
        ("BackendCoder", "FrontendCoder", "api_ready", "BIND_API"),
        ("BackendCoder", "BuildVerifier", "backend_ready", "VERIFY_BUILD"),
        ("FrontendCoder", "BuildVerifier", "code_ready", "VERIFY_BUILD"),
        ("Consumer", "Agent", "task_ready", "RUN_AGENT"),
        ("Agent", "Consumer", "result_ready", "WRITE_BACK"),
    }
)

_BLOCKING_STATE = (
    ("errors", "运行中出现错误"),
    ("retries", "出现重试"),
    ("clarifications", "出现澄清"),
)


class FastPathError(ValueError):
    """快速通道授权或使用非法。"""


@dataclass
class FastPathGrant:
    grant_id: str
    run_id: str
    task_id: str
    allowed_edges: frozenset[FastEdge]
    issued_by: str = "Architect"
    issued_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    max_steps: int = 50
    steps: int = 0
    revoked: bool = False
    revoke_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "grantId": self.grant_id,
            "runId": self.run_id,
            "taskId": self.task_id,
            "issuedBy": self.issued_by,
            "allowedEdges": [list(edge) for edge in sorted(self.allowed_edges)],
            "issuedAt": self.issued_at,
            "expiresAt": self.expires_at,
            "maxSteps": self.max_steps,
            "steps": self.steps,
            "revoked": self.revoked,
            "revokeReason": self.revoke_reason,
        }


@dataclass(frozen=True)
class FastPathDecision:
    allowed: bool
    reason: str
    grant_id: str = ""
    revoked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "grantId": self.grant_id,
            "revoked": self.revoked,
        }


def _resolve_config(path: str | Path | None) -> Path | None:
    if not path:
        return None
    file_path = Path(path)
    return file_path if file_path.is_absolute() else Path.cwd() / file_path


def load_fast_edges(path: str | Path | None) -> frozenset[FastEdge] | None:
    """从能力包 YAML 读取白名单；缺失或非法时返回 None。"""
    if not path or yaml is None:
        return None
    file_path = _resolve_config(path)
    if file_path is None or not file_path.exists():
        return None
    try:
        data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    except Exception:  # pragma: no cover - 配置错误回退默认
        logger.exception("快速通道白名单读取失败: %s", file_path)
        return None
    edges: set[FastEdge] = set()
    for item in data.get("edges") or []:
        if not isinstance(item, dict):
            continue
        edge = (
            str(item.get("from") or ""),
            str(item.get("to") or ""),
            str(item.get("kind") or ""),
            str(item.get("requestedAction") or ""),
        )
        if all(edge):
            edges.add(edge)
    return frozenset(edges) or None


class FastPathPolicy:
    """按 task 发放、检查、收回快速通道授权。"""

    def __init__(
        self,
        edges: Iterable[FastEdge] | None = None,
        *,
        config_path: str | Path | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._grants: dict[str, FastPathGrant] = {}
        self._config_path = ""
        self._edges_mtime_ns: int | None = None
        if edges is not None:
            self._edges = frozenset(tuple(str(part) for part in edge) for edge in edges)
            return
        path = config_path
        if path is None:
            try:
                from ai_core.config import get_settings

                path = get_settings().collaboration_fastpath_edges_path
            except Exception:  # pragma: no cover - 配置不可用时回退
                path = ""
        self._config_path = str(path or "")
        self._edges = load_fast_edges(path) or DEFAULT_FAST_EDGES
        resolved = _resolve_config(self._config_path)
        if resolved is not None:
            try:
                self._edges_mtime_ns = resolved.stat().st_mtime_ns
            except OSError:
                self._edges_mtime_ns = None

    def reload(self) -> bool:
        """能力包文件变化后热加载；失败时保留旧白名单。"""
        with self._lock:
            resolved = _resolve_config(self._config_path)
            if resolved is None:
                return False
            try:
                mtime = resolved.stat().st_mtime_ns
            except OSError:
                return False
            if mtime == self._edges_mtime_ns:
                return False
            self._edges_mtime_ns = mtime
            edges = load_fast_edges(self._config_path)
            if not edges:
                return False
            self._edges = edges
            return True

    def issue(
        self,
        *,
        run_id: str,
        task_id: str,
        edges: Iterable[FastEdge] | None = None,
        ttl_seconds: float = 600.0,
        max_steps: int = 50,
        now: float | None = None,
    ) -> FastPathGrant:
        self.reload()
        if not str(run_id or "").strip():
            raise FastPathError("runId 不能为空")
        if not str(task_id or "").strip():
            raise FastPathError("taskId 不能为空")
        allowed = frozenset(
            tuple(str(part) for part in edge) for edge in (edges or self._edges)
        )
        invalid = allowed - self._edges
        if invalid:
            raise FastPathError(f"包含未授权的链路: {sorted(invalid)}")
        current = time.time() if now is None else float(now)
        grant = FastPathGrant(
            grant_id=f"fp_{uuid.uuid4().hex[:20]}",
            run_id=str(run_id),
            task_id=str(task_id),
            allowed_edges=allowed,
            expires_at=current + max(1.0, float(ttl_seconds)),
            max_steps=max(1, int(max_steps)),
        )
        with self._lock:
            self._grants[grant.grant_id] = grant
        return _copy(grant)

    def check(
        self,
        grant_id: str,
        *,
        from_agent: str,
        to_agent: str,
        kind: str,
        requested_action: str,
        step_state: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> FastPathDecision:
        edge = (
            str(from_agent or ""),
            str(to_agent or ""),
            str(kind or ""),
            str(requested_action or ""),
        )
        with self._lock:
            grant = self._grants.get(str(grant_id or ""))
            if grant is None:
                return FastPathDecision(False, "没有 Architect 发放的快速通道授权")

            current = time.time() if now is None else float(now)
            if grant.revoked:
                return FastPathDecision(
                    False,
                    f"授权已收回：{grant.revoke_reason or '未知原因'}",
                    grant.grant_id,
                    revoked=True,
                )
            if current >= grant.expires_at:
                self._revoke(grant, "授权已过期")
                return FastPathDecision(
                    False, "授权已过期", grant.grant_id, revoked=True
                )
            if grant.steps >= grant.max_steps:
                self._revoke(grant, "直连步数超过上限")
                return FastPathDecision(
                    False, "直连步数超过上限", grant.grant_id, revoked=True
                )

            blocked = _blocking_reason(step_state or {})
            if blocked:
                self._revoke(grant, blocked)
                return FastPathDecision(False, blocked, grant.grant_id, revoked=True)

            if kind in A2A_REPAIR_KINDS or kind in A2A_ESCALATION_KINDS:
                return FastPathDecision(
                    False,
                    "修复/升级链路不允许直连",
                    grant.grant_id,
                )
            if edge not in grant.allowed_edges:
                return FastPathDecision(
                    False,
                    "不在标准链路白名单内",
                    grant.grant_id,
                )
            return FastPathDecision(True, "标准链路且无异常", grant.grant_id)

    def record_step(
        self,
        grant_id: str,
        *,
        now: float | None = None,
    ) -> FastPathGrant:
        with self._lock:
            grant = self._get(grant_id)
            grant.steps += 1
            if grant.steps >= grant.max_steps:
                self._revoke(grant, "直连步数超过上限")
            return _copy(grant)

    def revoke(self, grant_id: str, reason: str) -> FastPathGrant:
        with self._lock:
            grant = self._get(grant_id)
            self._revoke(grant, reason)
            return _copy(grant)

    def get(self, grant_id: str) -> FastPathGrant:
        with self._lock:
            return _copy(self._get(grant_id))

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                grant.to_dict()
                for grant in self._grants.values()
                if grant.run_id == str(run_id)
            ]

    def export_run(self, run_id: str) -> list[dict[str, Any]]:
        return self.list_for_run(run_id)

    def import_run(self, run_id: str, data: list[dict[str, Any]] | None) -> None:
        with self._lock:
            for item in data or []:
                try:
                    grant = FastPathGrant(
                        grant_id=str(item.get("grantId") or ""),
                        run_id=str(item.get("runId") or run_id),
                        task_id=str(item.get("taskId") or ""),
                        allowed_edges=frozenset(
                            tuple(str(part) for part in edge)
                            for edge in item.get("allowedEdges", [])
                        ),
                        issued_by=str(item.get("issuedBy") or "Architect"),
                        issued_at=float(item.get("issuedAt") or 0),
                        expires_at=float(item.get("expiresAt") or 0),
                        max_steps=int(item.get("maxSteps") or 1),
                        steps=int(item.get("steps") or 0),
                        revoked=bool(item.get("revoked")),
                        revoke_reason=str(item.get("revokeReason") or ""),
                    )
                except (TypeError, ValueError):
                    continue
                if grant.grant_id:
                    self._grants[grant.grant_id] = grant

    def _get(self, grant_id: str) -> FastPathGrant:
        grant = self._grants.get(str(grant_id or ""))
        if grant is None:
            raise FastPathError(f"快速通道授权不存在: {grant_id}")
        return grant

    def _revoke(self, grant: FastPathGrant, reason: str) -> None:
        if not grant.revoked:
            grant.revoked = True
            grant.revoke_reason = str(reason or "已收回")

    def reset(self) -> None:
        with self._lock:
            self._grants.clear()


def _blocking_reason(state: dict[str, Any]) -> str:
    if str(state.get("status") or "") in {"failed", "blocked"}:
        return "任务状态异常"
    if state.get("risk_blocked"):
        return "风险门禁已拦截"
    for key, reason in _BLOCKING_STATE:
        try:
            if int(state.get(key, 0) or 0) > 0:
                return reason
        except (TypeError, ValueError):
            continue
    return ""


def _copy(grant: FastPathGrant) -> FastPathGrant:
    return replace(grant, allowed_edges=frozenset(grant.allowed_edges))


__all__ = [
    "DEFAULT_FAST_EDGES",
    "FastEdge",
    "FastPathDecision",
    "FastPathError",
    "FastPathGrant",
    "FastPathPolicy",
    "load_fast_edges",
]
