#!/usr/bin/env python3
"""Agent 协作协议原型：跑通 Architect 收件箱、澄清桥、风险门禁与动作台账。

不接真实模型、不改现有图执行器；只验证协议闭环与复用边界。
产物：reports/collaboration/prototype-timeline.json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "orchestrator"))

from ai_core.a2a import build_feedback  # noqa: E402
from ai_core.collaboration.a2a_bridge import CollaborationBridge  # noqa: E402
from ai_core.collaboration.clarification import ClarificationBridge  # noqa: E402
from ai_core.collaboration.ledger import LedgerError  # noqa: E402
from ai_core.collaboration.risk import RiskGate  # noqa: E402
from ai_core.collaboration.user_language import (  # noqa: E402
    to_user_language,
    violations,
)


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, event_type, payload, *, dedupe_key=None):
        self.events.append(
            {
                "type": event_type,
                "dedupeKey": dedupe_key,
                "payload": payload,
            }
        )
        return None


def main() -> int:
    run_id = "run_proto_architect_hub"
    sink = RecordingSink()
    bridge = CollaborationBridge(sink=sink)
    risk_gate = RiskGate()
    clarifications = ClarificationBridge()
    steps: list[dict] = []

    # 1. 现有 A2A 直连：Applier -> BackendCoder，改道为 Architect 报告
    feedback = build_feedback(
        run_id=run_id,
        from_agent="Applier",
        to_agent="BackendCoder",
        kind="code_repair",
        requested_action="REPAIR_CODE",
        summary="服务端缺少关联查询，需要补一条关联记录读取",
        evidence=["[server/index.js] 关联字段不存在"],
        payload={
            "taskId": "task_1",
            "decisions": ["保持现有路由不变"],
            "prompt": "完整修复上下文：" + ("关联查询缺少别名；" * 30),
        },
    )
    report = bridge.ingest_feedback(feedback, task_id="task_1")
    steps.append(
        {
            "step": "a2a_rerouted",
            "from": "Applier",
            "originalTarget": report.original_target,
            "status": report.status,
            "summary": report.summary,
        }
    )

    # 2. Architect 先读摘要，再按需取全文
    summary_view = bridge.inbox.relay(
        run_id, report.report_id, mode="summary"
    ).to_dict()
    full_view = bridge.inbox.relay(run_id, report.report_id, mode="full").to_dict()
    steps.append(
        {
            "step": "architect_relay",
            "summaryMode": summary_view["mode"],
            "fullPayloadChars": len(full_view["payload"].get("prompt", "")),
        }
    )

    # 3. 快速通道：Architect 授权标准链路直连；出现异常立即收回
    grant = bridge.issue_fast_path(
        run_id=run_id,
        task_id="task_fast",
        edges=[
            (
                "DBACoder",
                "BackendCoder",
                "schema_ready",
                "REBIND_ORM",
            )
        ],
    )
    fast_feedback = build_feedback(
        run_id=run_id,
        from_agent="DBACoder",
        to_agent="BackendCoder",
        kind="schema_ready",
        requested_action="REBIND_ORM",
        summary="表结构已就绪，继续生成接口",
        payload={"taskId": "task_fast"},
    )
    fast_report = bridge.ingest_feedback(
        fast_feedback,
        task_id="task_fast",
        grant_id=grant.grant_id,
    )
    error_feedback = build_feedback(
        run_id=run_id,
        from_agent="DBACoder",
        to_agent="BackendCoder",
        kind="schema_ready",
        requested_action="REBIND_ORM",
        summary="第二次推进时出现错误",
        payload={"taskId": "task_fast"},
    )
    rerouted_fast = bridge.ingest_feedback(
        error_feedback,
        task_id="task_fast",
        grant_id=grant.grant_id,
        step_state={"errors": 1},
    )
    steps.append(
        {
            "step": "fast_path",
            "grantId": grant.grant_id,
            "firstDelivery": fast_report.delivery_mode,
            "afterErrorDelivery": rerouted_fast.delivery_mode,
            "architectVisible": [
                item["deliveryMode"]
                for item in bridge.inbox.summaries(run_id)
                if item["taskId"] == "task_fast"
            ],
        }
    )

    # 4. 子 Agent 卡住：强制澄清，多轮，原地恢复
    request = clarifications.request(
        run_id=run_id,
        asked_by="DBACoder",
        task_id="task_1",
        context_summary="需要确定历史库存流水是否迁移",
        question="历史流水要保留吗？",
        options=[
            {"id": "keep", "label": "保留并迁移", "impact": "迁移时间更长"},
            {"id": "drop", "label": "不保留", "impact": "丢失历史记录"},
        ],
    )
    clarifications.relay(request.request_id)
    next_round = clarifications.answer(
        request.request_id,
        option_id="keep",
        followup_question="迁移期间允许停服吗？",
        followup_options=[
            {"id": "yes", "label": "可以停服", "impact": "迁移更简单"},
            {"id": "no", "label": "必须在线", "impact": "需要双写方案"},
        ],
    )
    answered = clarifications.answer(request.request_id, option_id="yes")
    resumed = clarifications.resume(request.request_id)
    steps.append(
        {
            "step": "clarification",
            "askedBy": request.asked_by,
            "rounds": next_round.round,
            "finalState": answered.state,
            "resumeToken": resumed["resumeToken"],
            "answers": resumed["answers"],
        }
    )

    # 5. 高危操作：一致性、安全、范围、数据破坏
    risk_results = [
        risk_gate.evaluate("consistency", {"errors": 2}).to_dict(),
        risk_gate.evaluate("security", {"issues": 1}).to_dict(),
        risk_gate.evaluate("scope", {"pages": 20, "entities": 16}).to_dict(),
        risk_gate.evaluate("data", {"destructive": True}).to_dict(),
        risk_gate.evaluate("scope", {"pages": 4, "entities": 3}).to_dict(),
    ]
    steps.append({"step": "risk_gate", "results": risk_results})

    # 6. 动作台账：有 taskId 才允许记录
    action = bridge.record_action(
        run_id=run_id,
        task_id="task_1",
        actor="BackendCoder",
        tool="write_file",
        target="server/index.js",
        before_hash="sha256:before",
        after_hash="sha256:after",
        risk="medium",
    )
    rejected_action = None
    try:
        bridge.record_action(
            run_id=run_id,
            task_id="",
            actor="BackendCoder",
            tool="write_file",
            target="server/index.js",
        )
    except LedgerError as exc:
        rejected_action = str(exc)
    steps.append(
        {
            "step": "action_ledger",
            "recorded": action.to_dict(),
            "rejectedWithoutTask": rejected_action,
        }
    )

    # 7. 用户语言边界
    code_text = "请修改 server/index.js 里的 getUserById()"
    business_text = "请选择审批流程：先主管审批，还是直接由财务确认。"
    steps.append(
        {
            "step": "user_language",
            "codeTextViolations": violations(code_text),
            "codeTextToUser": to_user_language(code_text),
            "businessTextViolations": violations(business_text),
        }
    )

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "runId": run_id,
        "steps": steps,
        "events": sink.events,
        "inbox": bridge.inbox.summaries(run_id),
        "actions": bridge.ledger.list_for_run(run_id),
    }
    out_dir = ROOT / "reports" / "collaboration"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "prototype-timeline.json"
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"[prototype] run={run_id}")
    print(f"[prototype] a2a rerouted -> {report.original_target} / {report.status}")
    print(
        f"[prototype] fast path: direct={fast_report.delivery_mode} "
        f"-> {rerouted_fast.delivery_mode} after error"
    )
    print(
        f"[prototype] clarification rounds={next_round.round} "
        f"resume={resumed['resumeToken']}"
    )
    print(
        "[prototype] risk decisions="
        + ", ".join(item["decision"] for item in risk_results)
    )
    print(f"[prototype] ledger actions={len(payload['actions'])}")
    print(f"[prototype] timeline={out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
