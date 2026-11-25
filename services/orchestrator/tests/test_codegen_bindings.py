"""契约交互 -> VisualDocument 绑定的通用测试（无行业关键词）。"""

from __future__ import annotations

import json
import unittest

from ai_core.codegen.bindings import apply_contract_interactions
from ai_core.architect.llm import ContractDraft, _with_stack_guard
from ai_core.codegen.stack_capabilities import get_stack_capabilities
from ai_core.codegen.stack_dependencies import resolve_stack_dependencies
from ai_core.codegen.visual import compile_visual_document
from ai_core.codegen.visual_extensions import (
    extension_dependencies,
    load_visual_extensions,
)
from ai_core.fullstack.graph import _merge_package_manifests
from ai_core.domain.visual_document import (
    VisualDocument,
    VisualNode,
    freeze_visual_document,
)


def _document() -> VisualDocument:
    root = VisualNode(
        id="nd_root",
        type="page",
        name="任务总览",
        children=[
            VisualNode(id="nd_list", type="component", name="任务列表"),
            VisualNode(id="nd_form", type="form", name="任务表单"),
            VisualNode(id="nd_title", type="text", name="标题"),
        ],
    )
    return VisualDocument(doc_id="vdoc_binding_test", title="任务总览", root=root)


INTERACTIONS = [
    {
        "component": "任务列表",
        "event": "change",
        "apiRoute": "/api/v1/tasks",
        "logic": "筛选变化时加载任务列表",
    },
    {
        "component": "任务表单",
        "event": "submit",
        "apiRoute": "/api/v1/tasks",
        "logic": "提交后创建任务",
    },
]


class ContractBindingTests(unittest.TestCase):
    def test_maps_interactions_to_nodes_and_methods(self) -> None:
        document = _document()
        enriched = apply_contract_interactions(document, INTERACTIONS)
        self.assertIsNotNone(enriched)
        self.assertEqual(len(enriched.bindings), 2)
        self.assertEqual(len(enriched.interactions), 2)
        by_node = {item.node_id: item for item in enriched.interactions}
        self.assertEqual(by_node["nd_list"].action, "call_api")
        self.assertEqual(by_node["nd_form"].action, "submit")
        bindings = {item.node_id: item for item in enriched.bindings}
        self.assertEqual(bindings["nd_list"].method, "GET")
        self.assertEqual(bindings["nd_form"].method, "POST")

    def test_is_idempotent(self) -> None:
        document = _document()
        once = apply_contract_interactions(document, INTERACTIONS)
        twice = apply_contract_interactions(once, INTERACTIONS)
        self.assertEqual(len(twice.bindings), 2)
        self.assertEqual(len(twice.interactions), 2)

    def test_unmatched_component_is_skipped(self) -> None:
        document = _document()
        enriched = apply_contract_interactions(
            document,
            [{"component": "不存在的组件", "event": "click", "apiRoute": "/api/x"}],
        )
        self.assertEqual(enriched.bindings, [])
        self.assertEqual(enriched.interactions, [])

    def test_compiler_emits_api_runtime_for_frozen_document(self) -> None:
        frozen = freeze_visual_document(_document())
        enriched = apply_contract_interactions(frozen, INTERACTIONS)
        compiled = compile_visual_document(enriched, include_interactions=True)
        source = compiled.files["src/App.jsx"]
        self.assertIn("VISUAL_ACTIONS", source)
        self.assertIn("runVisualAction", source)
        self.assertIn("fetch(action.endpoint", source)
        self.assertIn("/api/v1/tasks", source)

    def test_static_compile_omits_runtime_without_interactions(self) -> None:
        frozen = freeze_visual_document(_document())
        compiled = compile_visual_document(frozen, include_interactions=False)
        source = compiled.files["src/App.jsx"]
        self.assertNotIn("runVisualAction", source)

    def test_stack_dependencies_are_config_driven(self) -> None:
        stack = resolve_stack_dependencies(
            {"frontend": "React + Vite + Leaflet", "database": "mysql"}
        )
        self.assertIn("react", stack.dependencies)
        self.assertIn("leaflet", stack.dependencies)
        self.assertTrue(
            any("leaflet/dist/leaflet.css" in line for line in stack.imports)
        )
        empty = resolve_stack_dependencies({})
        self.assertEqual(empty.dependencies, {})
        self.assertEqual(empty.imports, ())

    def test_visual_extension_renders_config_component(self) -> None:
        page = VisualNode(
            id="nd_root",
            type="page",
            name="页面",
            children=[VisualNode(id="nd_map", type="component", name="地图画布")],
        )
        document = VisualDocument(doc_id="vdoc_map_test", title="地图", root=page)
        frozen = freeze_visual_document(document)
        extensions = load_visual_extensions()
        self.assertTrue(any(item.id == "leaflet_map" for item in extensions))
        compiled = compile_visual_document(
            frozen,
            include_interactions=False,
            extra_dependencies=extension_dependencies(extensions),
            visual_extensions=extensions,
        )
        source = compiled.files["src/App.jsx"]
        self.assertIn("react-leaflet", source)
        self.assertIn("MapContainer", source)
        self.assertIn("react-leaflet", compiled.files["package.json"])

    def test_visual_extension_preserves_child_node_ids(self) -> None:
        map_node = VisualNode(
            id="nd_map",
            type="component",
            name="地图画布",
            children=[
                VisualNode(
                    id="nd_map_title",
                    type="text",
                    name="标题",
                    props={"text": "地图画布"},
                )
            ],
        )
        page = VisualNode(
            id="nd_root", type="page", name="页面", children=[map_node]
        )
        document = VisualDocument(
            doc_id="vdoc_map_children", title="地图", root=page
        )
        frozen = freeze_visual_document(document)
        extensions = load_visual_extensions()
        compiled = compile_visual_document(
            frozen,
            include_interactions=False,
            extra_dependencies=extension_dependencies(extensions),
            visual_extensions=extensions,
        )
        self.assertFalse(compiled.repaired)
        self.assertIn("MapContainer", compiled.files["src/App.jsx"])
        self.assertIn("nd_map_title", compiled.files["src/App.jsx"])
        self.assertIn("hidden", compiled.files["src/App.jsx"])

    def test_generic_data_list_extension_renders_endpoint(self) -> None:
        page = VisualNode(
            id="nd_root",
            type="page",
            name="页面",
            children=[
                VisualNode(id="nd_list", type="component", name="设施列表")
            ],
        )
        document = VisualDocument(
            doc_id="vdoc_data_list", title="列表", root=page
        )
        frozen = freeze_visual_document(document)
        enriched = apply_contract_interactions(
            frozen,
            [
                {
                    "component": "设施列表",
                    "event": "change",
                    "apiRoute": "/api/v1/facilities",
                    "logic": "加载列表",
                }
            ],
        )
        extensions = load_visual_extensions()
        compiled = compile_visual_document(
            enriched,
            include_interactions=True,
            extra_dependencies=extension_dependencies(extensions),
            visual_extensions=extensions,
        )
        self.assertFalse(compiled.repaired)
        self.assertIn("src/DataList.jsx", compiled.files)
        self.assertIn(
            'endpoint="/api/v1/facilities"', compiled.files["src/App.jsx"]
        )
        self.assertIn("data-testid", compiled.files["src/DataList.jsx"])
        self.assertIn("retryToken", compiled.files["src/DataList.jsx"])
        self.assertIn("attempt < 2", compiled.files["src/DataList.jsx"])
        self.assertIn("重试", compiled.files["src/DataList.jsx"])

    def test_stack_capabilities_match_default_codegen_stack(self) -> None:
        capabilities = get_stack_capabilities()
        self.assertIn("Node", capabilities.default["backend"])
        self.assertIn("Sequelize", capabilities.default["orm"])
        block = capabilities.prompt_block()
        self.assertIn("平台可执行栈", block)
        self.assertIn("Node.js + Express", block)
        self.assertEqual(
            capabilities.conflicts(
                {"backend": "Spring Boot", "orm": "JPA"}
            ),
            ["backend: spring", "orm: jpa"],
        )
        self.assertEqual(
            capabilities.conflicts(
                {"backend": "Node.js + Express", "orm": "Sequelize"}
            ),
            [],
        )

    def test_stack_guard_records_conflict_as_assumption(self) -> None:
        draft = ContractDraft(
            contract_markdown="test",
            tech_stack={"backend": "Spring Boot", "orm": "JPA"},
        )
        guarded = _with_stack_guard(draft)
        self.assertTrue(
            any("超出平台可执行栈" in item for item in guarded.assumptions)
        )

    def test_fullstack_package_manifest_keeps_both_sides(self) -> None:
        base = json.dumps(
            {
                "name": "fullstack-app",
                "scripts": {"dev": "node scripts/dev.mjs"},
                "dependencies": {"react": "18.3.1", "express": "4.21.2"},
            }
        )
        override = json.dumps(
            {
                "name": "visual-app",
                "scripts": {"build": "vite build"},
                "dependencies": {"leaflet": "^1.9.4"},
                "devDependencies": {"tailwindcss": "^3.4.0"},
            }
        )
        merged = json.loads(_merge_package_manifests(base, override))
        self.assertEqual(merged["name"], "fullstack-app")
        self.assertEqual(merged["scripts"]["dev"], "node scripts/dev.mjs")
        self.assertEqual(merged["scripts"]["build"], "vite build")
        self.assertEqual(merged["dependencies"]["express"], "4.21.2")
        self.assertEqual(merged["dependencies"]["leaflet"], "^1.9.4")
        self.assertEqual(merged["devDependencies"]["tailwindcss"], "^3.4.0")

    def test_compiler_merges_stack_dependencies_and_imports(self) -> None:
        frozen = freeze_visual_document(_document())
        stack = resolve_stack_dependencies({"frontend": "React + Leaflet"})
        compiled = compile_visual_document(
            frozen,
            include_interactions=False,
            extra_dependencies=stack.dependencies,
            extra_dev_dependencies=stack.dev_dependencies,
            extra_imports=list(stack.imports),
        )
        self.assertIn('"leaflet"', compiled.files["package.json"])
        self.assertTrue(
            compiled.files["src/App.jsx"].startswith(
                "import 'leaflet/dist/leaflet.css'"
            )
        )


if __name__ == "__main__":
    unittest.main()
