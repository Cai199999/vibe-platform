"""全流程原型编排：一条命令跑完契约到飞轮。"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_core.a2a import build_feedback
from ai_core.collaboration.a2a_bridge import CollaborationBridge
from ai_core.collaboration.clarification import ClarificationBridge
from ai_core.collaboration.risk import RiskGate
from ai_core.prototype.delivery import package_delivery
from ai_core.prototype.generator import generate_workspace
from ai_core.prototype.planner import plan_topology
from ai_core.prototype.runtime import (
    applied_migrations,
    node_syntax_check,
    run_consumer,
    run_migrations,
    schema_columns,
    ticket_row,
    write_event,
)
from ai_core.prototype.spec import ApplicationSpec, default_ticket_spec

_PARENTS = Path(__file__).resolve().parents
# 仓库内：services/orchestrator/ai_core/prototype → parents[4] 是仓库根；
# 容器内：/app/ai_core/prototype → parents[2] 是 /app。
ROOT = _PARENTS[4] if len(_PARENTS) > 4 else _PARENTS[2]


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event_type: str, payload: dict, *, dedupe_key=None):
        self.events.append(
            {
                "type": event_type,
                "dedupeKey": dedupe_key,
                "payload": payload,
            }
        )
        return None


def run_full_flow(
    *,
    output_root: Path | None = None,
    run_id: str | None = None,
    spec: ApplicationSpec | None = None,
) -> dict[str, Any]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = run_id or f"ff_{stamp}_{uuid.uuid4().hex[:8]}"
    run_dir = (output_root or (ROOT / "reports" / "full-flow")) / run_id
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    spec = spec or default_ticket_spec()

    sink = RecordingSink()
    bridge = CollaborationBridge(sink=sink)
    clarifications = ClarificationBridge()
    risk = RiskGate()
    steps: list[dict[str, Any]] = []

    def step(name: str, **details: Any) -> None:
        steps.append({"name": name, "at": _now(), **details})

    def assign(agent: str, task_id: str, summary: str) -> None:
        sink.emit(
            "agent.task.assigned",
            {
                "agent": agent,
                "taskId": task_id,
                "assignedBy": "Architect",
                "summary": summary,
            },
            dedupe_key=f"agent.task.assigned:{task_id}:{agent}",
        )

    # 1. Architect 接收需求，补齐场景
    assign("DBACoder", "task_db", "准备表结构与迁移")
    assign("BackendCoder", "task_api", "生成业务接口")
    assign("Consumer", "task_worker", "接入事件消费与幂等")
    assign("Agent", "task_agent", "实现分类与处置建议")
    assign("InfraCoder", "task_delivery", "打包镜像、Helm 与迁移包")
    step("intake", goal=spec.goal, scenarios=spec.usage_scenarios.__dict__)

    request = clarifications.request(
        run_id=run_id,
        asked_by="DBACoder",
        task_id="task_db",
        context_summary="历史工单需要迁移到新表结构",
        question="迁移期间允许停服吗？",
        options=[
            {"id": "yes", "label": "可以停服", "impact": "迁移更简单"},
            {"id": "no", "label": "必须在线", "impact": "需要双写与回填"},
        ],
    )
    clarifications.relay(request.request_id)
    clarifications.answer(request.request_id, option_id="yes")
    resumed = clarifications.resume(request.request_id)
    step("clarification", askedBy=request.asked_by, resume=resumed)

    allowed = risk.evaluate("scope", {"pages": 4, "entities": 2})
    blocked = risk.evaluate("scope", {"pages": 20, "entities": 16})
    sink.emit("risk.decision", allowed.to_dict())
    sink.emit("risk.decision", blocked.to_dict())
    step(
        "risk_gate",
        allowed=allowed.to_dict(),
        blocked=blocked.to_dict(),
    )

    # 2. 契约 + 拓扑 + 回写
    topology = plan_topology(spec)
    contract_dir = workspace / "contract"
    contract_dir.mkdir(parents=True, exist_ok=True)
    rev1 = {"revision": 1, "spec": spec.to_dict()}
    rev2 = {"revision": 2, "spec": spec.to_dict(), "topology": topology.to_dict()}
    (contract_dir / "revision-1.json").write_text(
        json.dumps(rev1, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (contract_dir / "revision-2.json").write_text(
        json.dumps(rev2, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (contract_dir / "revisions.json").write_text(
        json.dumps(
            {"revisions": [rev1, rev2]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    step("contract", revision=2, services=len(spec.services), flows=len(spec.events))
    step("topology", planned=topology.to_dict())

    # 3. 多产物生成
    files = generate_workspace(workspace, spec, topology)
    for relative in (
        "db/migrations/001_init.sql",
        "db/migrations/002_ai_fields.sql",
        "apps/api/server.mjs",
        "services/agent/agent.py",
        "services/consumer/consumer.py",
        f"helm/{spec.name}/Chart.yaml",
    ):
        bridge.record_action(
            run_id=run_id,
            task_id="task_generate",
            actor="CodeGenerator",
            tool="write_file",
            target=relative,
            after_hash=_sha256(workspace / relative),
            risk="low",
        )
    step("generate", files=len(files))

    # 4. 快速通道：标准链路直连，但每一步仍向 Architect 报摘要
    grant = bridge.issue_fast_path(
        run_id=run_id,
        task_id="task_db",
        edges=[
            (
                "DBACoder",
                "BackendCoder",
                "schema_ready",
                "REBIND_ORM",
            )
        ],
    )
    fast_report = bridge.ingest_feedback(
        build_feedback(
            run_id=run_id,
            from_agent="DBACoder",
            to_agent="BackendCoder",
            kind="schema_ready",
            requested_action="REBIND_ORM",
            summary="迁移脚本已就绪，接口可以按新表结构生成",
            payload={"taskId": "task_db"},
        ),
        task_id="task_db",
        grant_id=grant.grant_id,
    )
    bridge.ingest_feedback(
        build_feedback(
            run_id=run_id,
            from_agent="InfraCoder",
            to_agent="Architect",
            kind="delivery_ready",
            requested_action="PACKAGE",
            summary="交付清单已生成，等待运行验收",
            payload={"taskId": "task_delivery"},
        ),
        task_id="task_delivery",
    )
    step(
        "fast_path",
        grantId=grant.grant_id,
        deliveryMode=fast_report.delivery_mode,
        originalTarget=fast_report.original_target,
    )

    # 5. 迁移与运行验收
    db_path = workspace / "data" / "app.db"
    applied = run_migrations(workspace, db_path)
    columns = schema_columns(db_path, "tickets")
    if "ai_category" not in columns:
        raise RuntimeError("迁移后缺少 ai_category 列")
    node_check = node_syntax_check(workspace)

    event1 = workspace / "events" / "ticket.created.json"
    first = run_consumer(
        workspace, db_path, event1, workspace / "services/agent/agent.py"
    )
    second = run_consumer(
        workspace, db_path, event1, workspace / "services/agent/agent.py"
    )
    row1 = ticket_row(db_path, "t_001")

    event2 = write_event(
        workspace,
        "ticket.created.finance.json",
        {
            "eventId": "evt_002",
            "ticketId": "t_002",
            "title": "报销单退款迟迟未到账",
            "content": "员工反馈退款和报销款项未到账",
        },
    )
    run_consumer(workspace, db_path, event2, workspace / "services/agent/agent.py")
    row2 = ticket_row(db_path, "t_002")

    if first.get("duplicate") is not False:
        raise RuntimeError("首次消费不应是重复事件")
    if second.get("duplicate") is not True:
        raise RuntimeError("重复事件未命中幂等")
    if row1["aiCategory"] != "IT":
        raise RuntimeError(f"分类不符合预期: {row1['aiCategory']}")
    if row2["aiCategory"] != "财务":
        raise RuntimeError(f"第二个分类不符合预期: {row2['aiCategory']}")
    if row1["processedEvents"] != 1:
        raise RuntimeError("重复事件被重复写回")
    step(
        "runtime_verification",
        migrations=applied,
        columns=columns,
        node=node_check,
        first=first,
        duplicate=second,
        ticket1=row1,
        ticket2=row2,
    )

    # 6. 可观测
    trace_path = workspace / "observability" / "trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "source": "prototype",
                "runId": run_id,
                "spans": [
                    {
                        "name": item["name"],
                        "startTime": item["at"],
                        "service": "vibe-platform-prototype",
                    }
                    for item in steps
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # 7. 交付打包
    delivery = package_delivery(workspace, spec)
    bridge.record_action(
        run_id=run_id,
        task_id="task_delivery",
        actor="InfraCoder",
        tool="package_delivery",
        target="dist/manifest.json",
        after_hash=delivery["manifest"]["imageDigest"],
        risk="low",
    )
    step("delivery", delivery=delivery)

    # 8. 应用层飞轮：只调提示词，不训练
    baseline = 0.72
    candidate = 0.81
    threshold = 0.75
    decision = "canary" if candidate >= threshold > baseline else "reject"
    flywheel = {
        "ticketId": "t_001",
        "humanDecision": "accepted",
        "candidate": {"promptVersion": "B", "changes": ["补充财务关键词"]},
        "eval": {"baseline": baseline, "candidate": candidate, "threshold": threshold},
        "decision": decision,
        "training": "not-performed",
    }
    flywheel_dir = workspace / "flywheel"
    flywheel_dir.mkdir(parents=True, exist_ok=True)
    (flywheel_dir / "registry.json").write_text(
        json.dumps(flywheel, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    step("flywheel", decision=decision)

    # 9. 证据清单
    evidence = {
        "runId": run_id,
        "generatedAt": _now(),
        "migrations": applied,
        "artifacts": [
            {"path": path.relative_to(workspace).as_posix(), "sha256": _sha256(path)}
            for path in sorted(workspace.rglob("*"))
            if path.is_file() and "data" not in path.parts
        ],
    }
    evidence_path = run_dir / "evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    bridge.revoke_fast_path(grant.grant_id, "任务完成，收回快速通道")
    report = {
        "runId": run_id,
        "status": "ok",
        "startedAt": steps[0]["at"],
        "finishedAt": _now(),
        "runDir": str(run_dir),
        "workspace": str(workspace),
        "spec": spec.to_dict(),
        "topology": topology.to_dict(),
        "steps": steps,
        "verification": {
            "migrations": applied,
            "schemaColumns": columns,
            "node": node_check,
            "idempotency": {
                "firstDuplicate": first.get("duplicate"),
                "secondDuplicate": second.get("duplicate"),
                "processedEvents": row1["processedEvents"],
            },
            "tickets": [row1, row2],
        },
        "delivery": delivery,
        "flywheel": flywheel,
        "agent": {
            "inbox": bridge.inbox.summaries(run_id),
            "actions": bridge.ledger.list_for_run(run_id),
            "events": sink.events,
        },
        "artifacts": len(evidence["artifacts"]),
    }
    report_path = run_dir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "summary.md").write_text(_summary(report), encoding="utf-8")
    report["reportPath"] = str(report_path)
    return report


def _summary(report: dict[str, Any]) -> str:
    verification = report["verification"]
    delivery = report["delivery"]
    return f"""# 全流程原型报告

- Run：`{report['runId']}`
- 状态：**{report['status']}**
- 迁移：{", ".join(verification['migrations'])}
- 幂等：首次重复={verification['idempotency']['firstDuplicate']}，第二次重复={verification['idempotency']['secondDuplicate']}
- 工单分类：{[item['aiCategory'] for item in verification['tickets']]}
- 交付：镜像 {delivery['manifest']['imageDigest'][:19]}…，Helm {delivery['manifest']['helmChart']}
- 飞轮：{report['flywheel']['decision']}（未训练）
- 产物：`{report['runDir']}`
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["RecordingSink", "run_full_flow"]
