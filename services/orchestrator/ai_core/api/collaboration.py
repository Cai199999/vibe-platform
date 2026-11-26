"""Architect 协作状态查询：收件箱、动作台账、快速通道授权。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ai_core.collaboration.error_router import GATE_ERRORS
from ai_core.collaboration.hub import ARCHITECT_HUB

router = APIRouter(prefix="/api/v1/internal/collaboration", tags=["collaboration"])


@router.get("/errors/catalog")
def get_error_catalog() -> dict[str, Any]:
    """平台全部确定性错误码与负责 Agent，Architect 与运维共用。"""
    return GATE_ERRORS.catalog()


@router.get("/errors/stats")
def get_error_stats(
    top: int = Query(default=10, ge=1, le=50),
) -> dict[str, Any]:
    """高频门禁错误统计：按错误码、指纹、负责 Agent 聚合。"""
    return GATE_ERRORS.stats(top)


@router.get("/runs/{run_id}/errors")
def get_run_errors(
    run_id: str,
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    """某个 Run 的门禁错误时间线（含尝试次数与分派结果）。"""
    items = GATE_ERRORS.ledger.for_run(run_id, limit)
    return {"runId": run_id, "count": len(items), "errors": items}


@router.get("/runs/{run_id}")
def get_run_collaboration(
    run_id: str,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """返回某个 Run 的 Architect 视角协作状态。

    - 默认只返回摘要（不返回子 Agent 的完整载荷）；
    - 动作台账与快速通道授权按 run 隔离；
    - 进程重启后数据会丢失，持久化在后续阶段接入。
    """
    snapshot = ARCHITECT_HUB.snapshot(run_id)
    inbox = snapshot["inbox"]
    actions = snapshot["actions"]
    grants = snapshot["grants"]
    gate_errors = GATE_ERRORS.ledger.for_run(run_id, limit)
    return {
        "runId": run_id,
        "inbox": inbox[-limit:],
        "actions": actions[-limit:],
        "fastPathGrants": grants,
        "gateErrors": gate_errors,
        "counts": {
            "inbox": len(inbox),
            "actions": len(actions),
            "fastPathGrants": len(grants),
            "gateErrors": len(gate_errors),
        },
    }


__all__ = ["router"]
