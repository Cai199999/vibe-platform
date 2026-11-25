"""应用层飞轮：真实评测、闸门与不训练约束。"""

from __future__ import annotations

import unittest

from ai_core.prototype.flywheel import (
    BASELINE_RULES,
    CANDIDATE_RULES,
    accuracy,
    run_flywheel_evaluation,
)


class FlywheelTest(unittest.TestCase):
    def test_candidate_improves_measured_accuracy(self) -> None:
        baseline = accuracy(BASELINE_RULES)
        candidate = accuracy(CANDIDATE_RULES)
        self.assertEqual(baseline["score"], 0.75)
        self.assertEqual(candidate["score"], 1.0)
        self.assertGreater(candidate["score"], baseline["score"])

    def test_gate_allows_canary_only_above_threshold(self) -> None:
        result = run_flywheel_evaluation(threshold=0.85)
        self.assertEqual(result["decision"], "canary")
        self.assertEqual(result["training"], "not-performed")
        self.assertGreaterEqual(result["candidateEval"]["score"], 0.85)

        hold = run_flywheel_evaluation(threshold=1.1)
        self.assertEqual(hold["decision"], "hold")


if __name__ == "__main__":
    unittest.main()
