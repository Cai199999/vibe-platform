"""Architect 协作状态查询：收件箱、动作台账、快速通道授权。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ai_core.collaboration.hub import ARCHITECT_HUB

router = APIRouter(prefix="/api/v1/internal/collaboration", tags=["collaboration"])


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
    return {
        "runId": run_id,
        "inbox": inbox[-limit:],
        "actions": actions[-limit:],
        "fastPathGrants": grants,
        "counts": {
            "inbox": len(inbox),
            "actions": len(actions),
            "fastPathGrants": len(grants),
        },
    }


__all__ = ["router"]
