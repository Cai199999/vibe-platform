"""多服务生成产品入口：把全流程原型暴露为平台 API。"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ai_core.config import get_settings
from ai_core.prototype.pipeline import run_full_flow
from ai_core.prototype.spec import default_ticket_spec

router = APIRouter(prefix="/api/v1/internal/generated-apps", tags=["generated-apps"])


class GenerateMultiServiceRequest(BaseModel):
    name: str = Field(default="ticket-copilot", min_length=1, max_length=64)


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    verification = report.get("verification") or {}
    return {
        "runId": report.get("runId"),
        "status": report.get("status"),
        "runDir": report.get("runDir"),
        "reportPath": report.get("reportPath"),
        "stages": [
            {"name": item.get("name"), "at": item.get("at")}
            for item in report.get("steps") or []
        ],
        "verification": {
            "migrations": verification.get("migrations"),
            "idempotency": verification.get("idempotency"),
            "tickets": verification.get("tickets"),
        },
        "delivery": report.get("delivery"),
        "flywheel": report.get("flywheel"),
        "artifacts": report.get("artifacts"),
        "agent": {
            "inbox": len((report.get("agent") or {}).get("inbox") or []),
            "actions": len((report.get("agent") or {}).get("actions") or []),
        },
    }


@router.post("/generate")
def generate_multi_service(
    request: GenerateMultiServiceRequest | None = None,
) -> dict[str, Any]:
    """生成多服务应用原型（当前为固定工单蓝图 + 可覆盖名称）。

    真实产物写入 ``generated_apps_root``；接口只返回摘要与报告路径，
    避免把完整契约、拓扑和源码塞进响应体。
    """
    name = (request.name if request else "ticket-copilot").strip()
    spec = dataclasses.replace(default_ticket_spec(), name=name)
    settings = get_settings()
    report = run_full_flow(
        output_root=Path(settings.generated_apps_root),
        spec=spec,
    )
    return _summary(report)


@router.get("/runs/{run_id}")
def get_generated_app(run_id: str) -> dict[str, Any]:
    root = Path(get_settings().generated_apps_root) / run_id
    report_path = root / "report.json"
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="生成记录不存在")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return _summary(report)


__all__ = ["router"]
