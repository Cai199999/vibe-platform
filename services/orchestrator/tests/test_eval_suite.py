"""Eval Suite 加载器、离线执行器与报告测试。

运行（宿主机没有 AI Core 依赖时用一次性容器）：

    docker run --rm --user "$(id -u):$(id -g)" \
      -e PYTHONDONTWRITEBYTECODE=1 \
      -v "$PWD:/workspace:ro" -w /workspace/services/orchestrator \
      --entrypoint python vibe-platform/python-ai:dev -m unittest \
      tests.test_eval_suite -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from eval_suite.engine import EvalEngine
from eval_suite.loader import EvalSuiteError, load_suite
from eval_suite.report import write_reports

REPO_ROOT = Path(__file__).resolve().parents[3]
ORCHESTRATOR_ROOT = Path(__file__).resolve().parents[1]
CORE_SUITE = REPO_ROOT / "config/eval_suite/core_cases.yaml"
CORE_FIXTURES = REPO_ROOT / "config/eval_suite/fixtures"
CUSTOM_AGENTS = ORCHESTRATOR_ROOT / "config/custom_agents"

_MINIMAL_CASE = """
- id: "{case_id}"
  name: "{name}"
  input:
    target_agent: "auto"
    text: "把按钮改成红色"
  assertions:
    expected_handoff: "handoff_ui"
"""


class LoaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write(self, text: str) -> Path:
        path = self.root / "suite.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_core_suite_loads(self) -> None:
        suite = load_suite(CORE_SUITE)
        self.assertEqual(len(suite.cases), 10)
        self.assertEqual(len({case.case_id for case in suite.cases}), 10)
        self.assertEqual(
            sum(1 for case in suite.cases if case.needs_code_generation),
            4,
        )
        # 新契约：HITL 与 both 门控必须各自有真实执行用例。
        self.assertEqual(sum(1 for case in suite.cases if case.needs_hitl), 1)
        self.assertEqual(sum(1 for case in suite.cases if case.needs_gate), 1)

    def test_duplicate_ids_are_rejected(self) -> None:
        path = self._write(
            _MINIMAL_CASE.format(case_id="case_dup", name="A")
            + _MINIMAL_CASE.format(case_id="case_dup", name="B")
        )
        with self.assertRaisesRegex(EvalSuiteError, "重复"):
            load_suite(path)

    def test_unknown_assertion_field_is_rejected(self) -> None:
        path = self._write("""
- id: "case_bad"
  name: "未知字段"
  input:
    text: "把按钮改成红色"
  assertions:
    expected_handoff: "handoff_ui"
    expected_magic: true
""")
        with self.assertRaisesRegex(EvalSuiteError, "未知字段"):
            load_suite(path)

    def test_fixture_traversal_is_rejected(self) -> None:
        path = self._write("""
- id: "case_traversal"
  name: "路径穿越"
  input:
    text: "把按钮改成红色"
    fixture: "../secret"
  assertions:
    expected_handoff: "handoff_ui"
""")
        with self.assertRaisesRegex(EvalSuiteError, "穿越"):
            load_suite(path)

    def test_single_annotation_inherits_case_text(self) -> None:
        path = self._write("""
- id: "case_inherit"
  name: "继承文案"
  input:
    text: "改成红色"
    annotation:
      annotationId: "ann_1"
      target:
        mode: "source"
        selector: "[data-source='primary-action']"
  assertions:
    expected_handoff: "handoff_ui"
""")
        suite = load_suite(path)
        annotation = suite.cases[0].input.annotations[0]
        self.assertEqual(annotation["payload"]["text"], "改成红色")


class EngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = EvalEngine(custom_agents_dir=CUSTOM_AGENTS)

    def test_core_suite_all_pass(self) -> None:
        suite = load_suite(CORE_SUITE)
        result = self.engine.run_suite(suite)
        self.assertEqual(result.summary.total, 10)
        self.assertEqual(result.summary.failed, 0, msg=self._failure_text(result))

    def test_assertion_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "suite.yaml"
            path.write_text(
                """
- id: "case_wrong_handoff"
  name: "错误期望"
  input:
    text: "把按钮改成红色"
    annotation:
      annotationId: "ann_1"
      target:
        mode: "source"
        selector: "[data-source='primary-action']"
  assertions:
    expected_handoff: "handoff_architect"
""",
                encoding="utf-8",
            )
            result = self.engine.run_suite(load_suite(path))
        self.assertEqual(result.summary.failed, 1)
        failed = result.cases[0]
        self.assertIn("handoff", [check.name for check in failed.failures])

    def test_code_assertion_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "suite.yaml"
            path.write_text(
                """
- id: "case_wrong_code"
  name: "错误代码片段"
  input:
    text: "改成红色"
    fixture: "react_dashboard"
    annotation:
      annotationId: "ann_1"
      target:
        mode: "source"
        selector: "[data-source='primary-action']"
      payload:
        text: "改成红色"
  assertions:
    expected_handoff: "handoff_ui"
    expected_code_contains: ["background-color:purple"]
""",
                encoding="utf-8",
            )
            suite = load_suite(path, fixtures_root=CORE_FIXTURES)
            result = self.engine.run_suite(suite)
        self.assertEqual(result.summary.failed, 1)
        names = [check.name for check in result.cases[0].failures]
        self.assertTrue(any(name.startswith("code_contains:") for name in names))

    def test_reports_are_written(self) -> None:
        suite = load_suite(CORE_SUITE)
        result = self.engine.run_suite(suite)
        with tempfile.TemporaryDirectory() as tempdir:
            written = write_reports(result, tempdir, ["json", "markdown", "junit"])
            paths = {path.name: path for path in written}
            self.assertEqual(
                set(paths),
                {"eval-report.json", "eval-report.md", "eval-junit.xml"},
            )
            payload = json.loads(paths["eval-report.json"].read_text("utf-8"))
            self.assertEqual(payload["summary"]["total"], 10)
            self.assertEqual(payload["summary"]["failed"], 0)
            root = ET.parse(paths["eval-junit.xml"]).getroot()
            self.assertEqual(root.tag, "testsuite")
            self.assertEqual(root.attrib["failures"], "0")

    @staticmethod
    def _failure_text(result) -> str:
        lines = []
        for case in result.cases:
            for check in case.failures:
                lines.append(
                    f"{case.case_id} {check.name}: {check.expected} != {check.actual}"
                )
        return "\n".join(lines)


if __name__ == "__main__":
    unittest.main()
