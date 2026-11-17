"""存量 React 源码只读分析服务测试。"""

from __future__ import annotations

import unittest

from ai_core.source_analysis.service import (
    SourceAnalysisInvalid,
    SourceAnalysisUnavailable,
    _script_path,
    analyze_existing_react,
)

SAMPLE_SOURCE = """\
export function Text({ children }) {
  return <p className="m-0 text-sm text-slate-700">{children}</p>
}

export function Header() {
  return (
    <header className="flex items-center border-b px-4 py-3">
      <Text>卡片标题</Text>
      <button className="rounded bg-blue-600 px-3 py-1 text-white">操作</button>
    </header>
  )
}

export default function Card() {
  return (
    <div className="rounded-xl border bg-white p-4 shadow">
      <Header />
      <section className="mt-3 grid grid-cols-2 gap-2">
        <Text>第一列</Text>
        <Text>第二列</Text>
      </section>
    </div>
  )
}
"""


def _script_available() -> bool:
    try:
        _script_path()
        return True
    except SourceAnalysisUnavailable:
        return False


@unittest.skipUnless(_script_available(), "analyzer script not mounted")
class SourceAnalysisTests(unittest.TestCase):
    def test_analyzes_nested_card_hierarchy(self) -> None:
        report = analyze_existing_react("Card.jsx", SAMPLE_SOURCE)

        self.assertIs(report["analysisOnly"], True)
        self.assertIs(report["writable"], False)
        self.assertEqual(report["file"], "Card.jsx")
        self.assertEqual(report["entryComponent"], "Card")
        self.assertIn("Card -> Header -> Text", report["componentHierarchies"])
        self.assertEqual(report["root"]["tag"], "div")
        self.assertIn("rounded-xl", report["root"]["classes"])
        self.assertEqual(report["stats"]["components"], 3)

    def test_rejects_unsupported_extension(self) -> None:
        with self.assertRaises(SourceAnalysisInvalid):
            analyze_existing_react("malware.exe", SAMPLE_SOURCE)

    def test_rejects_empty_and_oversized_source(self) -> None:
        with self.assertRaises(SourceAnalysisInvalid):
            analyze_existing_react("App.jsx", "   ")
        with self.assertRaises(SourceAnalysisInvalid):
            analyze_existing_react("App.jsx", "x" * 500_001)

    def test_syntax_errors_are_reported_as_invalid(self) -> None:
        with self.assertRaises(SourceAnalysisInvalid) as raised:
            analyze_existing_react("Broken.jsx", "export default function App( {")
        self.assertIn("解析失败", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
