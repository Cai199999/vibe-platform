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

GRAPH_PLUGIN_RUNNER = """
import fs from 'node:fs'
import { sourceInspectPlugin } from './inspect_plugin.js'

const root = process.env.VIBE_TEST_ROOT
const plugin = sourceInspectPlugin()
plugin.configResolved({ root })
for (const file of [root + '/src/UserCard.jsx', root + '/src/App.jsx']) {
  const code = fs.readFileSync(file, 'utf8')
  plugin.transform(code, file)
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

    def _run_graph_plugin(self) -> dict:
        (self.root / "src" / "UserCard.jsx").write_text(
            """export default function UserCard({ title }) {
  return (
    <section>
      <h2>{title}</h2>
      <p>UserCard 内部</p>
    </section>
  )
}
""",
            encoding="utf-8",
        )
        (self.root / "src" / "App.jsx").write_text(
            """import UserCard from './UserCard'

export default function App() {
  return (
    <main>
      <h1>跨文件调用点</h1>
      <UserCard title="原始标题" />
    </main>
  )
}
""",
            encoding="utf-8",
        )
        subprocess.run(
            ["node", "--input-type=module", "-e", GRAPH_PLUGIN_RUNNER],
            cwd=TEMPLATE_DIR,
            env={**os.environ, "VIBE_TEST_ROOT": str(self.root)},
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(
            (self.root / "vibe-source-map.json").read_text(encoding="utf-8")
        )

    def _run_ast_edit(self, spec: dict) -> dict:
        spec_path = self.root / "edit-spec.json"
        spec_path.write_text(
            json.dumps(
                {
                    "workdir": str(self.root),
                    "parserRoot": str(TEMPLATE_DIR),
                    **spec,
                }
            ),
            encoding="utf-8",
        )
        output = subprocess.run(
            ["node", "apply_ast_edit.mjs", str(spec_path)],
            cwd=TEMPLATE_DIR,
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(output.stdout)

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
        li_nodes = [
            item for item in source_map["nodes"].values() if item["tag"] == "li"
        ]
        self.assertEqual(len(li_nodes), 2)
        self.assertEqual(len({item["oid"] for item in li_nodes}), 2)
        self.assertTrue(
            all(item["containerScope"] == "repeated_template" for item in li_nodes)
        )
        self.assertTrue(all(item["deleteSafe"] is False for item in li_nodes))

    def test_update_class_ast_edit(self) -> None:
        source_map = self._run_plugin()
        h1 = next(item for item in source_map["nodes"].values() if item["tag"] == "h1")
        edited = self._run_ast_edit(
            {
                "file": "src/App.jsx",
                "operation": "update_props",
                "anchorRange": {
                    "startOffset": h1["start"],
                    "endOffset": h1["end"],
                },
                "props": {"className": "text-red-600 font-bold"},
            }
        )
        self.assertEqual(edited["status"], "OK", edited)
        self.assertIn('className="text-red-600 font-bold"', edited["content"])

    def test_static_map_source_and_delete_array_item(self) -> None:
        source = """const items = [
  { name: 'A' },
  { name: 'B' },
]

export default function App() {
  return (
    <ul>
      {items.map((item) => <li>{item.name}</li>)}
    </ul>
  )
}
"""
        (self.root / "src" / "App.jsx").write_text(source, encoding="utf-8")
        source_map = self._run_plugin()
        li = next(item for item in source_map["nodes"].values() if item["tag"] == "li")

        anchor = self._resolve(li["oid"])

        self.assertEqual(anchor["status"], "RESOLVED")
        self.assertTrue(anchor["inMap"])
        self.assertEqual(anchor["mapSource"]["kind"], "variable_array")
        self.assertEqual(anchor["mapSource"]["count"], 2)
        edited = self._run_ast_edit(
            {
                "file": "src/App.jsx",
                "operation": "delete_array_item",
                "anchorRange": anchor["range"],
                "arrayRange": {
                    "startOffset": anchor["mapSource"]["start"],
                    "endOffset": anchor["mapSource"]["end"],
                },
                "itemIndex": 0,
            }
        )
        self.assertEqual(edited["status"], "OK", edited)
        self.assertNotIn("name: 'A'", edited["content"])
        self.assertIn("name: 'B'", edited["content"])

    def test_multiple_call_sites_are_ordered_and_conditional_marked(self) -> None:
        (self.root / "src" / "UserCard.jsx").write_text(
            """export default function UserCard({ title }) {
  return <section>{title}</section>
}
""",
            encoding="utf-8",
        )
        (self.root / "src" / "App.jsx").write_text(
            """import UserCard from './UserCard'

export default function App({ show }) {
  return (
    <main>
      <UserCard title="A" />
      {show ? <UserCard title="B" /> : null}
    </main>
  )
}
""",
            encoding="utf-8",
        )
        subprocess.run(
            ["node", "--input-type=module", "-e", GRAPH_PLUGIN_RUNNER],
            cwd=TEMPLATE_DIR,
            env={**os.environ, "VIBE_TEST_ROOT": str(self.root)},
            check=True,
            capture_output=True,
            text=True,
        )
        source_map = json.loads(
            (self.root / "vibe-source-map.json").read_text(encoding="utf-8")
        )
        call_sites = sorted(
            (
                item
                for item in source_map["callSites"].values()
                if item["component"] == "UserCard"
            ),
            key=lambda item: item["start"],
        )
        self.assertEqual([item["props"]["title"] for item in call_sites], ["A", "B"])
        self.assertFalse(call_sites[0]["conditional"])
        self.assertTrue(call_sites[1]["conditional"])
        self.assertFalse(call_sites[1]["safeForOrder"])
        definition = source_map["nodes"][call_sites[0]["definitionOid"]]
        self.assertEqual(
            [item["props"]["title"] for item in definition["callSites"]],
            ["A", "B"],
        )

    def test_call_site_slot_requires_children_forwarding(self) -> None:
        (self.root / "src" / "UserCard.jsx").write_text(
            """export default function UserCard({ children }) {
  return (
    <section>
      {children}
      <p>UserCard 内部</p>
    </section>
  )
}
""",
            encoding="utf-8",
        )
        (self.root / "src" / "App.jsx").write_text(
            """import UserCard from './UserCard'

export default function App() {
  return (
    <main>
      <UserCard><span>slot</span></UserCard>
    </main>
  )
}
""",
            encoding="utf-8",
        )
        subprocess.run(
            ["node", "--input-type=module", "-e", GRAPH_PLUGIN_RUNNER],
            cwd=TEMPLATE_DIR,
            env={**os.environ, "VIBE_TEST_ROOT": str(self.root)},
            check=True,
            capture_output=True,
            text=True,
        )
        source_map = json.loads(
            (self.root / "vibe-source-map.json").read_text(encoding="utf-8")
        )
        call_site = next(
            item
            for item in source_map["callSites"].values()
            if item["component"] == "UserCard"
        )
        self.assertTrue(call_site["hasChildren"])
        self.assertTrue(call_site["forwardsChildren"])
        self.assertTrue(call_site["slot"])
        definition = source_map["nodes"][call_site["definitionOid"]]
        self.assertEqual(definition["containerScope"], "call_site_slot")
        self.assertTrue(definition["callSites"][0]["slot"])

    def test_importer_graph_and_call_site_props_edit(self) -> None:
        source_map = self._run_graph_plugin()
        call_sites = list(source_map["callSites"].values())
        self.assertEqual(len(call_sites), 1)
        call_site = call_sites[0]
        self.assertEqual(call_site["file"], "src/App.jsx")
        self.assertEqual(call_site["definitionFile"], "src/UserCard.jsx")
        self.assertEqual(call_site["component"], "UserCard")
        self.assertEqual(call_site["props"]["title"], "原始标题")

        definition = source_map["nodes"][call_site["definitionOid"]]
        self.assertEqual(definition["containerScope"], "component_definition")
        app_root = next(
            item
            for item in source_map["nodes"].values()
            if item["file"] == "src/App.jsx" and item["tag"] == "main"
        )
        self.assertEqual(app_root["containerScope"], "local")
        self.assertFalse(app_root["deleteSafe"])
        self.assertFalse(definition["deleteSafe"])
        app_leaf = next(
            item
            for item in source_map["nodes"].values()
            if item["file"] == "src/App.jsx" and item["tag"] == "h1"
        )
        self.assertTrue(app_leaf["deleteSafe"])
        self.assertEqual(len(definition["callSites"]), 1)
        self.assertEqual(
            definition["callSites"][0]["callSiteOid"],
            call_site["oid"],
        )

        spec = self.root / "call-site-spec.json"
        spec.write_text(
            json.dumps(
                {
                    "workdir": str(self.root),
                    "parserRoot": str(TEMPLATE_DIR),
                    "anchors": [
                        {
                            "anchorId": "anc_props",
                            "annotationId": "ann_props",
                            "callSiteOid": call_site["oid"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        resolved = json.loads(
            subprocess.run(
                ["node", "resolve_target.mjs", str(spec)],
                cwd=TEMPLATE_DIR,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )["anchors"][0]
        self.assertEqual(resolved["status"], "RESOLVED")
        self.assertEqual(resolved["file"], "src/App.jsx")
        self.assertTrue(resolved["isCallSite"])
        self.assertIn('<UserCard title="原始标题" />', resolved["nodeSource"])

        edited = self._run_ast_edit(
            {
                "file": "src/App.jsx",
                "operation": "update_props",
                "anchorRange": resolved["range"],
                "props": {"title": "来自调用点的新标题"},
            }
        )
        self.assertEqual(edited["status"], "OK")
        self.assertIn('title="来自调用点的新标题"', edited["content"])
        self.assertIn(
            "UserCard 内部",
            (self.root / "src" / "UserCard.jsx").read_text(encoding="utf-8"),
        )
        self.assertNotIn(
            "来自调用点的新标题",
            (self.root / "src" / "UserCard.jsx").read_text(encoding="utf-8"),
        )

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
