"""Agent 协作协议原型：收件箱、澄清桥、风险门禁与动作台账。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ai_core.a2a import build_feedback
from ai_core.collaboration.a2a_bridge import CollaborationBridge
from ai_core.collaboration.clarification import (
    ClarificationBridge,
    ClarificationError,
)
from ai_core.collaboration.fastpath import FastPathError, FastPathPolicy
from ai_core.collaboration.hub import ARCHITECT_HUB
from ai_core.collaboration.inbox import ArchitectInbox, InboxError
from ai_core.collaboration.ledger import ActionLedger, LedgerError
from ai_core.collaboration.protocol import (
    AgentReport,
    ReportError,
    route_a2a_to_architect,
)
from ai_core.collaboration.risk import RiskGate
from ai_core.collaboration.user_language import (
    UserLanguageError,
    ensure_user_language,
    to_user_language,
    violations,
)


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict, str | None]] = []

    def emit(self, event_type, payload, *, dedupe_key=None):
        self.events.append((event_type, payload, dedupe_key))
        return None


def _feedback(to_agent: str = "BackendCoder", **overrides):
    data = {
        "run_id": "run_proto",
        "from_agent": "Applier",
        "to_agent": to_agent,
        "kind": "code_repair",
        "requested_action": "REPAIR_CODE",
        "summary": "服务端缺少关联查询，需要修复",
        "payload": {"taskId": "task_1", "decisions": ["补充关联"]},
    }
    data.update(overrides)
    return build_feedback(**data)


class AgentReportTest(unittest.TestCase):
    def test_summary_and_task_are_required(self) -> None:
        with self.assertRaises(ReportError):
            AgentReport(
                report_id="rep_1",
                run_id="run",
                agent="BackendCoder",
                task_id="task",
                status="done",
                summary=" ",
            )
        with self.assertRaises(ReportError):
            AgentReport(
                report_id="rep_2",
                run_id="run",
                agent="BackendCoder",
                task_id="",
                status="done",
                summary="已完成",
            )

    def test_architect_cannot_report_to_self(self) -> None:
        with self.assertRaises(ReportError):
            AgentReport(
                report_id="rep_3",
                run_id="run",
                agent="Architect",
                task_id="task",
                status="done",
                summary="自报",
            )

    def test_a2a_feedback_is_rerouted_and_clipped(self) -> None:
        feedback = _feedback(summary="问题" * 80)
        report = route_a2a_to_architect(feedback)
        self.assertEqual(report.agent, "Applier")
        self.assertEqual(report.original_target, "BackendCoder")
        self.assertEqual(report.status, "blocked")
        self.assertLessEqual(len(report.summary), 200)
        self.assertTrue(report.full_payload_ref.startswith("a2a://"))


class ArchitectInboxTest(unittest.TestCase):
    def test_summary_default_and_full_on_demand(self) -> None:
        inbox = ArchitectInbox()
        report = route_a2a_to_architect(
            _feedback(),
            task_id="task_1",
        )
        inbox.submit(report, payload={"prompt": "完整内容"})

        summaries = inbox.summaries("run_proto")
        self.assertEqual(len(summaries), 1)
        self.assertNotIn("payload", summaries[0])

        full = inbox.relay("run_proto", report.report_id, mode="full")
        self.assertEqual(full.payload, {"prompt": "完整内容"})
        self.assertEqual(full.mode, "full")

    def test_rejects_architect_self_report(self) -> None:
        inbox = ArchitectInbox()
        # AgentReport 构造阶段已拒绝 Architect；这里验证收件箱侧兜底。
        with self.assertRaises(InboxError):
            inbox.submit(SimpleNamespace(agent="Architect"))  # type: ignore[arg-type]


class ClarificationBridgeTest(unittest.TestCase):
    def test_multi_round_and_resume(self) -> None:
        bridge = ClarificationBridge()
        request = bridge.request(
            run_id="run",
            asked_by="DBACoder",
            task_id="task_1",
            context_summary="需要确定历史数据如何迁移",
            question="历史数据要保留吗？",
            options=[
                {"id": "keep", "label": "保留并迁移"},
                {"id": "drop", "label": "不保留"},
            ],
        )
        self.assertEqual(request.state, "requested")
        bridge.relay(request.request_id)

        next_round = bridge.answer(
            request.request_id,
            option_id="keep",
            followup_question="迁移期间允许停服吗？",
            followup_options=[
                {"id": "yes", "label": "可以停服"},
                {"id": "no", "label": "必须在线"},
            ],
        )
        self.assertEqual(next_round.round, 2)
        self.assertEqual(next_round.state, "awaiting_user")

        answered = bridge.answer(request.request_id, option_id="yes")
        self.assertEqual(answered.state, "answered")
        resumed = bridge.resume(request.request_id)
        self.assertTrue(resumed["resumeToken"].startswith("ckpt_"))
        self.assertEqual(len(resumed["answers"]), 2)

    def test_requires_two_options_and_valid_choice(self) -> None:
        bridge = ClarificationBridge()
        with self.assertRaises(ClarificationError):
            bridge.request(
                run_id="run",
                asked_by="DBACoder",
                task_id="task",
                context_summary="上下文",
                question="问题",
                options=[{"id": "only", "label": "唯一"}],
            )
        request = bridge.request(
            run_id="run",
            asked_by="DBACoder",
            task_id="task",
            context_summary="上下文",
            question="问题",
            options=[
                {"id": "a", "label": "A"},
                {"id": "b", "label": "B"},
            ],
        )
        bridge.relay(request.request_id)
        with self.assertRaises(ClarificationError):
            bridge.answer(request.request_id, option_id="missing")


class RiskGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = RiskGate()

    def test_high_risk_categories_are_blocked(self) -> None:
        self.assertEqual(
            self.gate.evaluate("consistency", {"errors": 2}).decision,
            "regenerate",
        )
        self.assertEqual(
            self.gate.evaluate("security", {"issues": 1}).decision,
            "reject",
        )
        self.assertEqual(
            self.gate.evaluate("scope", {"pages": 20}).decision,
            "ask_user",
        )
        self.assertEqual(
            self.gate.evaluate("scope", {"growth": 0.8}).decision,
            "ask_user",
        )
        self.assertEqual(
            self.gate.evaluate("data", {"destructive": True}).decision,
            "reject",
        )

    def test_low_risk_is_allowed(self) -> None:
        decision = self.gate.evaluate("scope", {"pages": 3, "entities": 2})
        self.assertEqual(decision.decision, "allow")
        self.assertFalse(decision.blocked)


class ActionLedgerTest(unittest.TestCase):
    def test_task_context_is_mandatory(self) -> None:
        ledger = ActionLedger()
        with self.assertRaises(LedgerError):
            ledger.record(
                run_id="run",
                task_id="",
                actor="BackendCoder",
                tool="write_file",
                target="server/index.js",
            )

    def test_records_are_listed_per_run(self) -> None:
        ledger = ActionLedger()
        record = ledger.record(
            run_id="run_a",
            task_id="task_1",
            actor="BackendCoder",
            tool="write_file",
            target="server/index.js",
            before_hash="a",
            after_hash="b",
            risk="medium",
        )
        self.assertEqual(record.risk, "medium")
        self.assertEqual(len(ledger.list_for_run("run_a")), 1)
        self.assertEqual(ledger.list_for_run("run_b"), [])


class UserLanguageTest(unittest.TestCase):
    def test_code_details_are_rejected(self) -> None:
        text = "请修改 server/index.js 里的 getUserById()"
        self.assertTrue(violations(text))
        with self.assertRaises(UserLanguageError):
            ensure_user_language(text)
        self.assertEqual(
            to_user_language(text),
            "这一步需要你确认业务范围。",
        )

    def test_business_language_passes(self) -> None:
        text = "请选择审批流程：先主管审批，还是直接由财务确认。"
        self.assertEqual(violations(text), [])
        ensure_user_language(text)


class FastPathPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = FastPathPolicy()

    def test_standard_edge_with_clean_state_is_allowed(self) -> None:
        grant = self.policy.issue(run_id="run", task_id="task")
        decision = self.policy.check(
            grant.grant_id,
            from_agent="DBACoder",
            to_agent="BackendCoder",
            kind="schema_ready",
            requested_action="REBIND_ORM",
            step_state={"errors": 0, "retries": 0},
        )
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.revoked)

    def test_missing_grant_and_non_standard_edge_are_denied(self) -> None:
        missing = self.policy.check(
            "",
            from_agent="DBACoder",
            to_agent="BackendCoder",
            kind="schema_ready",
            requested_action="REBIND_ORM",
        )
        self.assertFalse(missing.allowed)

        grant = self.policy.issue(run_id="run", task_id="task")
        denied = self.policy.check(
            grant.grant_id,
            from_agent="Applier",
            to_agent="BackendCoder",
            kind="code_repair",
            requested_action="REPAIR_CODE",
        )
        self.assertFalse(denied.allowed)
        self.assertFalse(denied.revoked)
        self.assertFalse(self.policy.get(grant.grant_id).revoked)

    def test_any_error_revokes_the_grant(self) -> None:
        grant = self.policy.issue(run_id="run", task_id="task")
        decision = self.policy.check(
            grant.grant_id,
            from_agent="DBACoder",
            to_agent="BackendCoder",
            kind="schema_ready",
            requested_action="REBIND_ORM",
            step_state={"retries": 1},
        )
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.revoked)
        self.assertTrue(self.policy.get(grant.grant_id).revoked)

    def test_expiry_and_step_limit(self) -> None:
        expired = self.policy.issue(
            run_id="run",
            task_id="task",
            ttl_seconds=1,
            now=100,
        )
        decision = self.policy.check(
            expired.grant_id,
            from_agent="DBACoder",
            to_agent="BackendCoder",
            kind="schema_ready",
            requested_action="REBIND_ORM",
            now=200,
        )
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.revoked)

        limited = self.policy.issue(run_id="run", task_id="task2", max_steps=1)
        self.policy.record_step(limited.grant_id)
        self.assertTrue(self.policy.get(limited.grant_id).revoked)


class ContractPolicyRequestTest(unittest.TestCase):
    def test_decision_request_accepts_agent_policy(self) -> None:
        from ai_core.domain.architect import ArchitectDecisionRequest

        request = ArchitectDecisionRequest(
            contract_id="ctr_1",
            decision="CONFIRM",
            agent_policy={
                "fastPathEdges": [
                    ["DBACoder", "BackendCoder", "schema_ready", "REBIND_ORM"]
                ],
                "clarification": "required",
                "riskTolerance": "strict",
                "userLanguage": "business",
            },
        )
        self.assertIsNotNone(request.agent_policy)
        self.assertEqual(len(request.agent_policy.fast_path_edges), 1)


class FastPathConfigTest(unittest.TestCase):
    def test_yaml_edges_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "edges.yaml"
            path.write_text(
                "version: 1\n"
                "edges:\n"
                "  - from: Planner\n"
                "    to: Coder\n"
                "    kind: plan_ready\n"
                "    requestedAction: START\n",
                encoding="utf-8",
            )
            policy = FastPathPolicy(config_path=path)
            grant = policy.issue(run_id="run", task_id="task")
            decision = policy.check(
                grant.grant_id,
                from_agent="Planner",
                to_agent="Coder",
                kind="plan_ready",
                requested_action="START",
            )
            self.assertTrue(decision.allowed)
            with self.assertRaises(FastPathError):
                policy.issue(
                    run_id="run",
                    task_id="task",
                    edges=[("DBACoder", "BackendCoder", "schema_ready", "REBIND_ORM")],
                )

    def test_config_hot_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "edges.yaml"
            path.write_text(
                "version: 1\n"
                "edges:\n"
                "  - from: Planner\n"
                "    to: Coder\n"
                "    kind: plan_ready\n"
                "    requestedAction: START\n",
                encoding="utf-8",
            )
            policy = FastPathPolicy(config_path=path)
            policy.issue(
                run_id="run",
                task_id="task",
                edges=[("Planner", "Coder", "plan_ready", "START")],
            )

            path.write_text(
                "version: 1\n"
                "edges:\n"
                "  - from: Planner\n"
                "    to: Reviewer\n"
                "    kind: plan_ready\n"
                "    requestedAction: REVIEW\n",
                encoding="utf-8",
            )
            stat = path.stat()
            os.utime(
                path,
                ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000),
            )
            self.assertTrue(policy.reload())
            with self.assertRaises(FastPathError):
                policy.issue(
                    run_id="run",
                    task_id="task",
                    edges=[("Planner", "Coder", "plan_ready", "START")],
                )
            policy.issue(
                run_id="run",
                task_id="task",
                edges=[("Planner", "Reviewer", "plan_ready", "REVIEW")],
            )

    def test_missing_config_falls_back_to_defaults(self) -> None:
        policy = FastPathPolicy(config_path="/nonexistent/edges.yaml")
        grant = policy.issue(run_id="run", task_id="task")
        decision = policy.check(
            grant.grant_id,
            from_agent="DBACoder",
            to_agent="BackendCoder",
            kind="schema_ready",
            requested_action="REBIND_ORM",
        )
        self.assertTrue(decision.allowed)


class ArchitectHubTest(unittest.TestCase):
    def setUp(self) -> None:
        ARCHITECT_HUB.reset()

    def tearDown(self) -> None:
        ARCHITECT_HUB.reset()

    def test_feedback_enters_shared_inbox_and_emits_event(self) -> None:
        sink = RecordingSink()
        report = ARCHITECT_HUB.ingest_feedback(
            _feedback(),
            task_id="task_1",
            sink=sink,
        )
        self.assertIsNotNone(report)
        summaries = ARCHITECT_HUB.summaries("run_proto")
        self.assertEqual(summaries[0]["agent"], "Applier")
        self.assertEqual(summaries[0]["originalTarget"], "BackendCoder")
        self.assertEqual(sink.events[0][0], "agent.report")

    def test_snapshot_survives_memory_reset(self) -> None:
        run_id = "run_persist"
        ARCHITECT_HUB.ingest_feedback(
            _feedback(run_id=run_id),
            task_id="task_persist",
        )
        ARCHITECT_HUB.record_action(
            run_id=run_id,
            task_id="task_persist",
            actor="Applier",
            tool="apply_patch",
            target="server/index.js",
        )
        ARCHITECT_HUB.issue_fast_path(run_id=run_id, task_id="task_persist")

        # 模拟进程重启：只清内存，不清 RunStore 快照
        ARCHITECT_HUB.inbox.reset()
        ARCHITECT_HUB.ledger.reset()
        ARCHITECT_HUB.fastpath.reset()
        ARCHITECT_HUB._loaded_runs.clear()  # noqa: SLF001

        snapshot = ARCHITECT_HUB.snapshot(run_id)
        self.assertEqual(len(snapshot["inbox"]), 1)
        self.assertEqual(len(snapshot["actions"]), 1)
        self.assertEqual(len(snapshot["grants"]), 1)

    def test_architect_origin_is_ignored(self) -> None:
        feedback = build_feedback(
            run_id="run_proto",
            from_agent="Architect",
            to_agent="BackendCoder",
            kind="contract_confirmed",
            requested_action="GENERATE_WITH_CONTRACT",
            summary="契约已确认",
        )
        self.assertIsNone(ARCHITECT_HUB.ingest_feedback(feedback))
        self.assertEqual(ARCHITECT_HUB.summaries("run_proto"), [])


class FullstackIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        ARCHITECT_HUB.reset()

    def tearDown(self) -> None:
        ARCHITECT_HUB.reset()

    def test_publish_a2a_enters_architect_inbox(self) -> None:
        from ai_core.fullstack.graph import FullstackGraphRunner

        sink = RecordingSink()
        runner = FullstackGraphRunner(sandbox=object(), event_sink=sink)  # type: ignore[arg-type]
        runner._publish_a2a(  # noqa: SLF001 - 原型接线验证
            {"run_id": "run_fs"},
            from_agent="BackendCoder",
            to_agent="Architect",
            kind="contract_review",
            requested_action="REVIEW_CONTRACT",
            summary="契约需要复核",
            payload={"taskId": "task_fs"},
        )
        summaries = ARCHITECT_HUB.summaries("run_fs")
        self.assertEqual(summaries[0]["agent"], "BackendCoder")
        self.assertEqual(summaries[0]["taskId"], "task_fs")
        self.assertTrue(any(event[0] == "agent.report" for event in sink.events))

    def test_a2a_uses_pre_authorized_fast_path(self) -> None:
        from ai_core.fullstack.graph import FullstackGraphRunner

        sink = RecordingSink()
        runner = FullstackGraphRunner(sandbox=object(), event_sink=sink)  # type: ignore[arg-type]
        grant = ARCHITECT_HUB.issue_fast_path(
            run_id="run_fs",
            task_id="task_fs",
            edges=[
                (
                    "DBACoder",
                    "BackendCoder",
                    "schema_ready",
                    "REBIND_ORM",
                )
            ],
        )
        runner._publish_a2a(  # noqa: SLF001
            {
                "run_id": "run_fs",
                "fast_path_grant_id": grant.grant_id,
                "llm_repair_count": 0,
                "build_retry_count": 0,
            },
            from_agent="DBACoder",
            to_agent="BackendCoder",
            kind="schema_ready",
            requested_action="REBIND_ORM",
            summary="表结构就绪，继续生成接口",
            payload={"taskId": "task_fs"},
        )
        summaries = ARCHITECT_HUB.summaries("run_fs")
        self.assertEqual(summaries[0]["deliveryMode"], "fast_path")
        self.assertEqual(summaries[0]["originalTarget"], "BackendCoder")

    def test_contract_policy_freezes_fast_path_edges(self) -> None:
        from ai_core.domain.architect import ArchitectAgentPolicy
        from ai_core.domain.fullstack import FullstackRunOptions
        from ai_core.fullstack.graph import FullstackGraphRunner

        policy = ArchitectAgentPolicy(
            fast_path_edges=[
                ["DBACoder", "BackendCoder", "schema_ready", "REBIND_ORM"],
                ["incomplete"],
            ]
        )
        self.assertEqual(len(policy.fast_path_edges), 1)

        runner = FullstackGraphRunner(sandbox=object())  # type: ignore[arg-type]
        grant_id = runner._issue_fast_path(  # noqa: SLF001
            {
                "run_id": "run_policy",
                "contract": SimpleNamespace(
                    contract_id="ctr_policy",
                    agent_policy=policy,
                ),
            },
            FullstackRunOptions(),
        )
        self.assertTrue(grant_id)
        grant = ARCHITECT_HUB.fastpath.get(grant_id)
        self.assertIn(
            ("DBACoder", "BackendCoder", "schema_ready", "REBIND_ORM"),
            grant.allowed_edges,
        )

    def test_applier_writes_enter_action_ledger(self) -> None:
        from ai_core.fullstack.graph import FullstackGraphRunner

        sink = RecordingSink()
        runner = FullstackGraphRunner(sandbox=object(), event_sink=sink)  # type: ignore[arg-type]
        runner._record_applier_actions(  # noqa: SLF001
            {"run_id": "run_fs", "contract": SimpleNamespace(contract_id="ctr_1")},
            ["server/index.js"],
            {"server/index.js": "export const x = 1\n"},
        )
        actions = ARCHITECT_HUB.ledger.list_for_run("run_fs")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["actor"], "Applier")
        self.assertEqual(actions[0]["taskId"], "ctr_1")
        self.assertTrue(actions[0]["afterHash"])
        self.assertTrue(any(event[0] == "agent.action" for event in sink.events))


class CollaborationBridgeTest(unittest.TestCase):
    def test_feedback_is_ingested_and_action_emitted(self) -> None:
        sink = RecordingSink()
        bridge = CollaborationBridge(sink=sink)
        report = bridge.ingest_feedback(_feedback())
        self.assertEqual(report.original_target, "BackendCoder")
        self.assertEqual(
            bridge.inbox.summaries("run_proto")[0]["agent"],
            "Applier",
        )
        self.assertEqual(sink.events[0][0], "agent.report")
        self.assertTrue(sink.events[0][1]["rerouted"])

        bridge.record_action(
            run_id="run_proto",
            task_id="task_1",
            actor="BackendCoder",
            tool="write_file",
            target="server/index.js",
        )
        self.assertEqual(sink.events[-1][0], "agent.action")
        self.assertEqual(len(bridge.ledger.list_for_run("run_proto")), 1)

    def test_fast_path_reports_progress_and_revokes_on_error(self) -> None:
        sink = RecordingSink()
        bridge = CollaborationBridge(sink=sink)
        grant = bridge.issue_fast_path(
            run_id="run_fp",
            task_id="task_1",
            edges=[
                (
                    "DBACoder",
                    "BackendCoder",
                    "schema_ready",
                    "REBIND_ORM",
                )
            ],
        )
        feedback = _feedback(
            run_id="run_fp",
            from_agent="DBACoder",
            to_agent="BackendCoder",
            kind="schema_ready",
            requested_action="REBIND_ORM",
            summary="表结构已就绪，开始生成接口",
            payload={"taskId": "task_1"},
        )
        report = bridge.ingest_feedback(
            feedback,
            task_id="task_1",
            grant_id=grant.grant_id,
        )
        self.assertEqual(report.delivery_mode, "fast_path")
        self.assertEqual(report.original_target, "BackendCoder")
        self.assertEqual(
            bridge.inbox.summaries("run_fp")[0]["deliveryMode"],
            "fast_path",
        )

        second = _feedback(
            run_id="run_fp",
            from_agent="DBACoder",
            to_agent="BackendCoder",
            kind="schema_ready",
            requested_action="REBIND_ORM",
            summary="第二次推进",
            payload={"taskId": "task_1"},
        )
        rerouted = bridge.ingest_feedback(
            second,
            task_id="task_1",
            grant_id=grant.grant_id,
            step_state={"errors": 1},
        )
        self.assertEqual(rerouted.delivery_mode, "hub")
        self.assertEqual(
            [item["deliveryMode"] for item in bridge.inbox.summaries("run_fp")],
            ["fast_path", "hub"],
        )
        kinds = [event[0] for event in sink.events]
        self.assertEqual(kinds.count("agent.fastpath.granted"), 1)
        self.assertEqual(kinds.count("agent.fastpath.revoked"), 1)
        reports = [event[1] for event in sink.events if event[0] == "agent.report"]
        self.assertTrue(reports[0]["direct"])
        self.assertFalse(reports[1]["direct"])


if __name__ == "__main__":
    unittest.main()
