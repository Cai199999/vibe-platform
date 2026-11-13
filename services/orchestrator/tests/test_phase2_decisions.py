"""Phase 2：多轮决策分流（Human-in-the-loop Decisions）。

覆盖三层：
  1. 纯函数：安全选项构造 / optionId 校验；
  2. 状态机节点：Resolver 挂起、decision_gate 选择、重新解析、轮次上限；
  3. Service + API：恢复值只携带 optionId，cancel 直接回滚并清理草稿。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_core.agent import graph as graph_module
from ai_core.agent import service as service_module
from ai_core.agent.events import RunEventBuffer
from ai_core.agent.graph import AgentGraphRunner
from ai_core.agent.service import (
    ActiveRun,
    DecisionOptionInvalidError,
    DecisionStateConflictError,
    RunExecutionService,
)
from ai_core.api import runs as runs_api
from ai_core.domain.agent import AnnotationEnvelope, ExecuteRunRequest
from ai_core.domain.decision import (
    ACTION_CANCEL,
    ACTION_MODIFY_CALL_SITE,
    ACTION_SELECT_TARGET,
    DECISION_KIND_AMBIGUOUS,
    DECISION_KIND_PROTECTED,
    FORBIDDEN_DECISION_ACTIONS,
    DecisionAnswerResponse,
    DecisionOption,
    DecisionRunStatus,
    DecisionStatus,
    PendingDecision,
)
from ai_core.domain.resolution import TARGET_AMBIGUOUS, RESOLVED
from test_phase1_resolver_ast_edit import (
    EDIT_SOURCE,
    _Phase1Sandbox,
    _ScriptedEditLLM,
    _annotation as _e2e_annotation,
    _context_payload,
    _e2e_state,
    _resolved_anchor as _e2e_resolved_anchor,
)

BUTTON_RANGE = {
    "startLine": 10,
    "startColumn": 5,
    "endLine": 10,
    "endColumn": 30,
    "startOffset": 100,
    "endOffset": 140,
}


def _annotation():
    return {
        "annotationId": "ann_1",
        "target": {
            "source": {
                "file": "src/App.jsx",
                "line": 10,
                "column": 5,
                "component": "Card",
                "instance": 1,
                "instanceCount": 2,
            }
        },
        "threadId": "thread_1",
    }


def _state(**overrides):
    state = {
        "run_id": "run_test",
        "sandbox_id": "sbx_test",
        "annotation_context": {"annotations": [{"annotation": _annotation()}]},
        "target_files": ["src/App.jsx"],
        "clarification_mode": "auto",
        "clarification_ttl_hours": 24,
        "decision_round": 0,
        "resolver_round": 0,
    }
    state.update(overrides)
    return state


def _ambiguous_anchor():
    return {
        "anchorId": "anc_ann_1",
        "annotationId": "ann_1",
        "status": TARGET_AMBIGUOUS,
        "file": "src/App.jsx",
        "reason": "选择器命中 2 个节点",
        "candidates": [
            {
                "candidateId": "anc_ann_1_c1",
                "file": "src/App.jsx",
                "range": BUTTON_RANGE,
                "nodeType": "JSXElement",
                "component": "App",
                "preview": "<button>登录</button>",
            },
            {
                "candidateId": "anc_ann_1_c2",
                "file": "src/App.jsx",
                "range": {**BUTTON_RANGE, "startOffset": 200, "endOffset": 240},
                "nodeType": "JSXElement",
                "component": "App",
                "preview": "<button>注册</button>",
            },
        ],
    }


def _resolved_anchor(**overrides):
    anchor = {
        "anchorId": "anc_ann_1",
        "annotationId": "ann_1",
        "status": RESOLVED,
        "file": "src/App.jsx",
        "range": BUTTON_RANGE,
        "nodeType": "JSXElement",
        "component": "App",
        "isShared": False,
        "protected": False,
        "allowedFiles": ["src/App.jsx"],
        "forbiddenRanges": [],
        "allowedOperations": ["insert", "delete", "modify"],
    }
    anchor.update(overrides)
    return anchor


def _protected_anchor():
    return _resolved_anchor(
        file="src/components/Card.jsx",
        component="Card",
        isShared=True,
        protected=True,
        sharedReason="被 2 个文件引用",
        callSiteFile="src/App.jsx",
        allowedFiles=["src/App.jsx"],
        callSiteAnchor={
            "anchorId": "anc_ann_1",
            "annotationId": "ann_1",
            "sourceAnchorId": "anc_ann_1",
            "status": RESOLVED,
            "file": "src/App.jsx",
            "range": BUTTON_RANGE,
            "nodeType": "JSXElement",
            "component": "App",
            "isShared": False,
            "protected": False,
            "allowedFiles": ["src/App.jsx"],
            "forbiddenRanges": [],
            "allowedOperations": ["insert", "delete", "modify"],
            "decisionAction": ACTION_MODIFY_CALL_SITE,
        },
    )


class _Sandbox:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def resolve_targets(self, sandbox_id, anchors, draft_id=None):
        self.calls.append([dict(item) for item in anchors])
        index = min(len(self.calls) - 1, len(self.results) - 1)
        return self.results[index]


def _runner(sandbox):
    runner = AgentGraphRunner.__new__(AgentGraphRunner)
    runner._sandbox = sandbox
    runner._emit = lambda *args, **kwargs: None  # type: ignore[method-assign]
    return runner


class DecisionPayloadTests(unittest.TestCase):
    def test_ambiguous_options_are_server_generated_and_safe(self) -> None:
        payload = graph_module._build_pending_decision_payload(
            _state(), DECISION_KIND_AMBIGUOUS, _ambiguous_anchor(), 1
        )
        decision = PendingDecision.model_validate(payload)
        self.assertEqual(decision.kind, "target_ambiguous")
        self.assertEqual(decision.round, 1)
        self.assertEqual(decision.max_rounds, 3)
        self.assertEqual(
            [item.option_id for item in decision.options],
            ["anc_ann_1_c1", "anc_ann_1_c2", "cancel_anc_ann_1"],
        )
        self.assertTrue(
            all(
                item.action in {ACTION_SELECT_TARGET, ACTION_CANCEL}
                for item in decision.options
            )
        )
        self.assertEqual(decision.options[-1].action, ACTION_CANCEL)
        self.assertEqual(
            decision.options[0].payload["range"]["startOffset"],
            BUTTON_RANGE["startOffset"],
        )

    def test_protected_component_offers_only_callsite_or_cancel(self) -> None:
        payload = graph_module._build_pending_decision_payload(
            _state(), DECISION_KIND_PROTECTED, _protected_anchor(), 2
        )
        decision = PendingDecision.model_validate(payload)
        actions = [item.action for item in decision.options]
        self.assertIn(ACTION_MODIFY_CALL_SITE, actions)
        self.assertEqual(actions[-1], ACTION_CANCEL)
        self.assertNotIn("force_modify_shared", actions)
        labels = " ".join(item.label for item in decision.options)
        self.assertNotIn("强行", labels)
        self.assertNotIn("公共组件定义", labels)

    def test_select_option_rejects_unknown_or_stale_ids(self) -> None:
        decision = PendingDecision.model_validate(
            graph_module._build_pending_decision_payload(
                _state(), DECISION_KIND_PROTECTED, _protected_anchor(), 1
            )
        )
        valid_id = decision.options[0].option_id
        self.assertIsNotNone(
            graph_module._select_decision_option(decision, {"optionId": valid_id})
        )
        self.assertIsNone(
            graph_module._select_decision_option(
                decision, {"optionId": valid_id, "decisionId": "dec_other"}
            )
        )
        self.assertIsNone(
            graph_module._select_decision_option(
                decision, {"optionId": "force_modify_shared"}
            )
        )
        forged = {"optionId": valid_id, "action": "force_modify_shared"}
        self.assertEqual(
            graph_module._select_decision_option(decision, forged).action,
            ACTION_MODIFY_CALL_SITE,
        )

    def test_forbidden_actions_have_no_option_id(self) -> None:
        decision = PendingDecision.model_validate(
            graph_module._build_pending_decision_payload(
                _state(), DECISION_KIND_AMBIGUOUS, _ambiguous_anchor(), 1
            )
        )
        option_ids = {item.option_id for item in decision.options}
        self.assertTrue(option_ids.isdisjoint(FORBIDDEN_DECISION_ACTIONS))


class ResolverDecisionTests(unittest.TestCase):
    def test_resolver_returns_pending_instead_of_failing(self) -> None:
        runner = _runner(_Sandbox([[_ambiguous_anchor()]]))
        update = runner._resolver(_state())
        self.assertEqual(update["resolver_outcome"], "ambiguous")
        self.assertIsNotNone(update["pending_decision"])
        self.assertEqual(update["pending_decision"]["kind"], "target_ambiguous")
        self.assertEqual(update["status"], "PLANNING")

    def test_round_limit_force_closes_run(self) -> None:
        runner = _runner(_Sandbox([[_ambiguous_anchor()]]))
        update = runner._resolver(_state(decision_round=3))
        self.assertEqual(update["resolver_outcome"], "exhausted")
        self.assertEqual(update["decision_outcome"], "exhausted")
        self.assertEqual(update["status"], "FAILED")
        self.assertEqual(update["error_code"], "DECISION_LIMIT")
        self.assertIn("无法确定安全路径", update["error"])
        self.assertIsNone(update["pending_decision"])

    def test_select_target_writes_range_then_resolves(self) -> None:
        sandbox = _Sandbox([[_ambiguous_anchor()], [_resolved_anchor()]])
        runner = _runner(sandbox)
        state = _state()
        first = runner._resolver(state)
        state.update(first)

        decision = first["pending_decision"]
        selected = next(
            item
            for item in decision["options"]
            if item["action"] == ACTION_SELECT_TARGET
        )
        with patch.object(
            graph_module,
            "interrupt",
            return_value={
                "decisionId": decision["decisionId"],
                "optionId": selected["optionId"],
            },
        ):
            gate = runner._decision_gate(state)

        self.assertEqual(gate["decision_outcome"], "decided")
        self.assertEqual(gate["decision_round"], 1)
        self.assertEqual(
            gate["decision_selections"]["anc_ann_1"]["range"]["startOffset"],
            BUTTON_RANGE["startOffset"],
        )
        state.update(gate)
        second = runner._resolver(state)
        self.assertEqual(second["resolver_outcome"], "resolved")
        self.assertEqual(
            sandbox.calls[1][0]["selectedRange"]["startOffset"],
            BUTTON_RANGE["startOffset"],
        )

    def test_modify_call_site_swaps_anchor_and_clears_protection(self) -> None:
        sandbox = _Sandbox([[_protected_anchor()], [_protected_anchor()]])
        runner = _runner(sandbox)
        state = _state()
        first = runner._resolver(state)
        self.assertEqual(first["resolver_outcome"], "unresolved")
        state.update(first)

        decision = first["pending_decision"]
        assert decision is not None
        self.assertEqual(decision["kind"], "protected_component")
        option = next(
            item
            for item in decision["options"]
            if item["action"] == ACTION_MODIFY_CALL_SITE
        )
        with patch.object(
            graph_module,
            "interrupt",
            return_value={
                "decisionId": decision["decisionId"],
                "optionId": option["optionId"],
            },
        ):
            gate = runner._decision_gate(state)
        override = gate["decision_overrides"]["anc_ann_1"]
        self.assertEqual(override["file"], "src/App.jsx")
        self.assertFalse(override["protected"])

        state.update(gate)
        second = runner._resolver(state)
        self.assertEqual(second["resolver_outcome"], "resolved")
        anchor = second["target_resolution"]["anchors"][0]
        self.assertEqual(anchor["file"], "src/App.jsx")
        self.assertFalse(anchor["protected"])
        self.assertEqual(anchor["allowedFiles"], ["src/App.jsx"])

    def test_invalid_option_consumes_round_and_loops(self) -> None:
        sandbox = _Sandbox([[_ambiguous_anchor()], [_ambiguous_anchor()]])
        runner = _runner(sandbox)
        state = _state()
        state.update(runner._resolver(state))
        decision = state["pending_decision"]

        with patch.object(
            graph_module, "interrupt", return_value={"optionId": "forged"}
        ):
            gate = runner._decision_gate(state)
        self.assertEqual(gate["decision_round"], decision["round"])
        self.assertEqual(gate["decision_outcome"], "decided")
        self.assertIsNone(gate["pending_decision"])
        self.assertNotIn("decision_selections", gate)

        state.update(gate)
        second = runner._resolver(state)
        self.assertEqual(second["resolver_outcome"], "ambiguous")
        self.assertEqual(second["pending_decision"]["round"], 2)

    def test_cancel_action_fails_closed_if_it_reaches_graph(self) -> None:
        runner = _runner(_Sandbox([[_ambiguous_anchor()]]))
        state = _state()
        state.update(runner._resolver(state))
        decision = state["pending_decision"]
        cancel = next(
            item for item in decision["options"] if item["action"] == ACTION_CANCEL
        )
        with patch.object(
            graph_module,
            "interrupt",
            return_value={
                "decisionId": decision["decisionId"],
                "optionId": cancel["optionId"],
            },
        ):
            gate = runner._decision_gate(state)
        self.assertEqual(gate["status"], "FAILED")
        self.assertEqual(gate["error_code"], "DECISION_CANCELLED")
        self.assertEqual(gate["decision_outcome"], "cancelled")


class _SyncThread:
    """测试用同步线程：start() 立即执行 target，便于断言恢复调用。"""

    def __init__(self, target=None, args=(), **_kwargs):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


class DecisionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run_id = "run_decision_test"
        self.request = ExecuteRunRequest(
            project_id="proj_1",
            annotations=[AnnotationEnvelope(annotation_id="ann_1")],
        )
        self.active = ActiveRun(
            run_id=self.run_id,
            fingerprint="fp",
            request=self.request,
            buffer=RunEventBuffer(self.run_id),
            thread_ids=["thread_1"],
        )
        self.service = RunExecutionService(
            sandbox=Mock(),
            settings=SimpleNamespace(),
            thread_store=Mock(),
            checkpointer_provider=Mock(),
        )
        with service_module._ACTIVE_LOCK:
            service_module._ACTIVE_RUNS[self.run_id] = self.active

    def tearDown(self) -> None:
        with service_module._ACTIVE_LOCK:
            service_module._ACTIVE_RUNS.pop(self.run_id, None)

    def _pause(self, kind=DECISION_KIND_PROTECTED, anchor=None):
        payload = graph_module._build_pending_decision_payload(
            _state(run_id=self.run_id),
            kind,
            anchor or _protected_anchor(),
            1,
        )
        decision = PendingDecision.model_validate(payload)
        self.active.pending_decision = decision
        self.active.pending_decisions.append(decision)
        self.active.status = "AWAITING_DECISION"
        self.active.prepared = SimpleNamespace()
        return decision

    def test_cancel_cleans_draft_without_resuming_graph(self) -> None:
        decision = self._pause(DECISION_KIND_AMBIGUOUS, _ambiguous_anchor())
        cancel = next(item for item in decision.options if item.action == ACTION_CANCEL)
        self.service._complete_cancelled = Mock()  # type: ignore[method-assign]
        self.service._execute_prepared = Mock()  # type: ignore[method-assign]

        response = self.service.answer_decision(
            self.run_id,
            decision.decision_id,
            service_module.DecisionRequest(option_id=cancel.option_id),
        )

        self.assertEqual(response.decision.status, "CANCELLED")
        self.assertEqual(response.run.status, "CANCELLED")
        self.service._complete_cancelled.assert_called_once()
        self.service._execute_prepared.assert_not_called()
        self.assertIsNone(self.active.pending_decision)
        self.assertEqual(decision.status, "CANCELLED")

    def test_safe_option_resumes_with_only_option_id(self) -> None:
        decision = self._pause()
        option = next(
            item for item in decision.options if item.action == ACTION_MODIFY_CALL_SITE
        )
        self.service._execute_prepared = Mock()  # type: ignore[method-assign]

        with patch.object(service_module.threading, "Thread", _SyncThread):
            response = self.service.answer_decision(
                self.run_id,
                decision.decision_id,
                service_module.DecisionRequest(
                    option_id=option.option_id,
                    comment="只在调用点改",
                ),
            )

        self.assertEqual(response.decision.status, "ANSWERED")
        self.assertEqual(response.decision.option_id, option.option_id)
        self.service._execute_prepared.assert_called_once()
        _, kwargs = self.service._execute_prepared.call_args
        self.assertEqual(
            kwargs["resume"],
            {
                "decisionId": decision.decision_id,
                "optionId": option.option_id,
            },
        )
        self.assertEqual(decision.status, "ANSWERED")
        self.assertEqual(decision.selected_option_id, option.option_id)

    def test_forged_option_is_rejected(self) -> None:
        decision = self._pause()
        with self.assertRaises(DecisionOptionInvalidError):
            self.service.answer_decision(
                self.run_id,
                decision.decision_id,
                service_module.DecisionRequest(option_id="force_modify_shared"),
            )
        self.assertEqual(self.active.status, "AWAITING_DECISION")
        self.assertIsNotNone(self.active.pending_decision)

    def test_state_conflict_when_no_pending_decision(self) -> None:
        with self.assertRaises(DecisionStateConflictError):
            self.service.answer_decision(
                self.run_id,
                "dec_missing",
                service_module.DecisionRequest(option_id="x"),
            )

    def test_expired_decision_cannot_be_answered(self) -> None:
        decision = self._pause()
        decision.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        with self.assertRaises(service_module.DecisionExpiredError):
            self.service.answer_decision(
                self.run_id,
                decision.decision_id,
                service_module.DecisionRequest(option_id=decision.options[0].option_id),
            )
        self.assertEqual(self.active.status, "EXPIRED")


class DecisionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(runs_api.router)
        self.client = TestClient(app)

    def test_answer_decision_returns_202(self) -> None:
        response_model = DecisionAnswerResponse(
            decision=DecisionStatus(
                decision_id="dec_1",
                status="ANSWERED",
                option_id="anc_ann_1_c1",
                answered_at=datetime.now(timezone.utc),
            ),
            run=DecisionRunStatus(run_id="run_1", status="RUNNING"),
        )
        with patch.object(
            runs_api.RunExecutionService,
            "answer_decision",
            return_value=response_model,
        ) as mocked:
            response = self.client.post(
                "/api/v1/internal/runs/run_1/decisions/dec_1",
                json={"optionId": "anc_ann_1_c1", "expectedVersion": 1},
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["decision"]["optionId"], "anc_ann_1_c1")
        mocked.assert_called_once()


class DecisionGraphE2ETest(unittest.TestCase):
    """真实 LangGraph invoke / Command(resume) 验证 interrupt 自环。"""

    def test_ambiguous_interrupt_then_resume_reaches_coder(self) -> None:
        from ai_core.adapters.checkpoint import MemoryCheckpointerProvider
        from ai_core.agent.llm import MockAgentLLM  # noqa: F401

        resolved = _e2e_resolved_anchor(EDIT_SOURCE)
        button_range = dict(resolved["range"])
        ambiguous = {
            "anchorId": resolved["anchorId"],
            "annotationId": "ann_e2e",
            "status": TARGET_AMBIGUOUS,
            "file": "src/App.jsx",
            "reason": "选择器命中 2 个节点",
            "candidates": [
                {
                    "candidateId": "anc_ann_e2e_c1",
                    "file": "src/App.jsx",
                    "range": button_range,
                    "nodeType": "JSXElement",
                    "component": "App",
                    "preview": "<button>登录</button>",
                },
                {
                    "candidateId": "anc_ann_e2e_c2",
                    "file": "src/App.jsx",
                    "range": button_range,
                    "nodeType": "JSXElement",
                    "component": "App",
                    "preview": "<button>注册</button>",
                },
            ],
        }
        sandbox = _Phase1Sandbox(
            {"src/App.jsx": EDIT_SOURCE},
            {"status": RESOLVED, "anchors": [ambiguous]},
        )
        llm = _ScriptedEditLLM(
            [
                {
                    "name": "apply_ast_edit",
                    "arguments": {
                        "anchor_id": resolved["anchorId"],
                        "operation": "modify",
                        "target_range": resolved["range"],
                        "jsx_code": (
                            '<button className="primary bg-blue-600">登录</button>'
                        ),
                    },
                }
            ]
        )
        runner = AgentGraphRunner(
            sandbox=sandbox,
            llm=llm,
            max_retries=0,
            checkpointer_provider=MemoryCheckpointerProvider(),
        )
        state = _e2e_state(_context_payload(_e2e_annotation()), EDIT_SOURCE)
        state["clarification_mode"] = "auto"
        thread_id = "thread_phase2_e2e"

        paused = runner.run(state, thread_id=thread_id)
        self.assertTrue(paused.interrupted)
        payload = paused.interrupt
        self.assertEqual(payload["kind"], "target_ambiguous")
        self.assertEqual(payload["round"], 1)

        sandbox.resolution = {"status": RESOLVED, "anchors": [resolved]}
        option_id = next(
            item["optionId"]
            for item in payload["options"]
            if item["action"] == ACTION_SELECT_TARGET
        )
        final = runner.run(
            state,
            thread_id=thread_id,
            resume={
                "decisionId": payload["decisionId"],
                "optionId": option_id,
            },
        )
        self.assertFalse(final.interrupted)
        self.assertEqual(
            final.state.get("status"), "SUCCEEDED", final.state.get("error")
        )
        self.assertEqual(final.state.get("decision_round"), 1)
        self.assertIsNone(final.state.get("pending_decision"))
        self.assertIn("bg-blue-600", sandbox.files["src/App.jsx"])


if __name__ == "__main__":
    unittest.main()
