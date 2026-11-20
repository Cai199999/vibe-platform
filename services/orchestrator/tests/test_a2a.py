"""A2A 反馈协议：信封字段、运行级日志与去重/上限。"""

from __future__ import annotations

import unittest

from ai_core.a2a import A2AFeedbackLog, build_feedback


class A2AFeedbackTest(unittest.TestCase):
    def test_build_feedback_serializes_camel_case(self) -> None:
        feedback = build_feedback(
            run_id="run_a2a",
            from_agent="BackendCoder",
            to_agent="Architect",
            kind="contract_revision",
            requested_action="REVISE_CONTRACT",
            summary="契约缺少 JOIN",
            evidence=["[server/index.js] 字段不存在", ""],
            contract_id="ctr_1",
            payload={"prompt": "请补 relations"},
        )

        item = feedback.to_dict()
        self.assertTrue(item["feedbackId"].startswith("a2a_"))
        self.assertEqual(item["runId"], "run_a2a")
        self.assertEqual(item["fromAgent"], "BackendCoder")
        self.assertEqual(item["toAgent"], "Architect")
        self.assertEqual(item["requestedAction"], "REVISE_CONTRACT")
        self.assertEqual(item["contractId"], "ctr_1")
        self.assertEqual(item["evidence"], ["[server/index.js] 字段不存在"])
        self.assertEqual(item["payload"]["prompt"], "请补 relations")
        self.assertTrue(item["createdAt"])

    def test_log_is_isolated_by_run_and_capped(self) -> None:
        log = A2AFeedbackLog(max_per_run=3)
        for index in range(5):
            log.publish(
                build_feedback(
                    run_id="run_cap",
                    from_agent="Applier",
                    to_agent="BackendCoder",
                    kind="code_repair",
                    requested_action="REPAIR_CODE",
                    summary=f"issue {index}",
                )
            )
        log.publish(
            build_feedback(
                run_id="run_other",
                from_agent="Applier",
                to_agent="DBACoder",
                kind="schema_repair",
                requested_action="REGENERATE_DB",
                summary="db only",
            )
        )

        capped = log.list_for_run("run_cap")
        self.assertEqual(len(capped), 3)
        self.assertEqual([item["summary"] for item in capped], [
            "issue 2",
            "issue 3",
            "issue 4",
        ])
        self.assertEqual(len(log.list_for_run("run_other")), 1)
        log.clear("run_cap")
        self.assertEqual(log.list_for_run("run_cap"), [])


if __name__ == "__main__":
    unittest.main()
