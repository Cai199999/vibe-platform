"""构建期 data-oid / vibe-source-map / oid-only 解析的回归测试。

这些测试直接运行真实的 `inspect_plugin.js` 与 `resolve_target.mjs`，
不使用 mock：证明 DOM→源码的映射来自构建期结构，而不是运行时猜行号。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ORCHESTRATOR_DIR = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ORCHESTRATOR_DIR / "sandbox_template"

SOURCE = """export default function App() {
  return (
    <main className="wrap">
      <section className="box">
        <h1>Title</h1>
        <p>Hello</p>
      </section>
    </main>
  )
}
"""

PLUGIN_RUNNER = """
import fs from 'node:fs'
import { sourceInspectPlugin } from './inspect_plugin.js'

const root = process.env.VIBE_TEST_ROOT
const plugin = sourceInspectPlugin()
plugin.configResolved({ root })
const file = root + '/src/App.jsx'
const code = fs.readFileSync(file, 'utf8')
const result = plugin.transform(code, file)
if (!result || !result.code.includes('data-oid=')) {
  console.error('plugin did not inject data-oid')
  process.exit(3)
}
await new Promise((resolve) => setTimeout(resolve, 250))
"""


class SourceMapPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="vibe-oid-"))
        (self.root / "src").mkdir(parents=True, exist_ok=True)
        (self.root / "src" / "App.jsx").write_text(SOURCE, encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _run_plugin(self) -> dict:
        env = {"VIBE_TEST_ROOT": str(self.root)}
        subprocess.run(
            ["node", "--input-type=module", "-e", PLUGIN_RUNNER],
            cwd=TEMPLATE_DIR,
            env={**os.environ, **env},
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(
            (self.root / "vibe-source-map.json").read_text(encoding="utf-8")
        )

    def _resolve(self, oid: str) -> dict:
        spec = self.root / "spec.json"
        spec.write_text(
            json.dumps(
                {
                    "workdir": str(self.root),
                    "parserRoot": str(TEMPLATE_DIR),
                    "anchors": [
                        {
                            "anchorId": "anc_1",
                            "annotationId": "ann_1",
                            "oid": oid,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        output = subprocess.run(
            ["node", "resolve_target.mjs", str(spec)],
            cwd=TEMPLATE_DIR,
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(output.stdout)["anchors"][0]

    def test_plugin_injects_oid_and_parent_map(self) -> None:
        source_map = self._run_plugin()
        self.assertEqual(source_map["version"], 1)
        self.assertEqual(len(source_map["nodes"]), 4)

        by_tag = {item["tag"]: item for item in source_map["nodes"].values()}
        self.assertEqual(by_tag["main"]["siblingIndex"], 0)
        self.assertTrue(by_tag["main"]["isContainer"])
        self.assertEqual(by_tag["section"]["parentOid"], by_tag["main"]["oid"])
        self.assertEqual(by_tag["p"]["parentOid"], by_tag["section"]["oid"])
        self.assertEqual(by_tag["p"]["siblingIndex"], 1)
        self.assertEqual(by_tag["p"]["siblingCount"], 2)
        self.assertGreater(by_tag["p"]["start"], 0)

    def test_oid_is_stable_across_line_shifts(self) -> None:
        first = self._run_plugin()
        first_oids = set(first["nodes"])

        source = (self.root / "src" / "App.jsx").read_text(encoding="utf-8")
        (self.root / "src" / "App.jsx").write_text(
            "// leading comment\n\n" + source,
            encoding="utf-8",
        )
        second = self._run_plugin()

        # 行号整体后移，但结构路径不变，oid 必须保持一致。
        self.assertEqual(set(second["nodes"]), first_oids)
        first_p = next(item for item in first["nodes"].values() if item["tag"] == "p")
        second_p = next(item for item in second["nodes"].values() if item["tag"] == "p")
        self.assertGreater(second_p["line"], first_p["line"])
        self.assertEqual(
            second_p["start"], first_p["start"] + len("// leading comment\n\n")
        )

    def test_inline_map_callbacks_get_distinct_oids(self) -> None:
        source = """export default function App({ first, second }) {
  return (
    <main>
      <ul>{first.map((item) => <li>{item}</li>)}</ul>
      <ul>{second.map((item) => <li>{item}</li>)}</ul>
    </main>
  )
}
"""
        (self.root / "src" / "App.jsx").write_text(source, encoding="utf-8")
        source_map = self._run_plugin()
        oids = list(source_map["nodes"])
        self.assertEqual(len(oids), len(set(oids)))
        li_oids = [
            item["oid"] for item in source_map["nodes"].values() if item["tag"] == "li"
        ]
        self.assertEqual(len(li_oids), 2)
        self.assertEqual(len(set(li_oids)), 2)

    def test_resolver_resolves_oid_without_file_line_or_selector(self) -> None:
        source_map = self._run_plugin()
        oid = next(
            key for key, item in source_map["nodes"].items() if item["tag"] == "p"
        )

        anchor = self._resolve(oid)

        self.assertEqual(anchor["status"], "RESOLVED")
        self.assertEqual(anchor["oid"], oid)
        self.assertEqual(anchor["file"], "src/App.jsx")
        self.assertEqual(anchor["nodeSource"], "<p>Hello</p>")

    def test_resolver_fails_closed_when_source_map_is_stale(self) -> None:
        source_map = self._run_plugin()
        oid = next(
            key for key, item in source_map["nodes"].items() if item["tag"] == "p"
        )
        # 只改源码、不刷新 manifest：oid 区间失效，必须拒绝猜测。
        source = (self.root / "src" / "App.jsx").read_text(encoding="utf-8")
        (self.root / "src" / "App.jsx").write_text(
            "// shifted\n" + source,
            encoding="utf-8",
        )

        anchor = self._resolve(oid)

        self.assertEqual(anchor["status"], "TARGET_NOT_FOUND")
        self.assertIn("source map", anchor["reason"])


if __name__ == "__main__":
    unittest.main()
