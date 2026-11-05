"""CodeEditor 抽象单元测试。

运行（无需 Docker / 模型密钥）：

    PYTHONPATH=services/orchestrator python3 -m unittest discover \
        -s services/orchestrator/tests -v
"""

from __future__ import annotations

import unittest

from ai_core.editor import (
    AnchorCodeEditor,
    CodeEdit,
    EditKind,
    EditLayout,
    EditorError,
    EditorNotAvailableError,
    EditorRegistry,
    FileCreator,
    TreeSitterEditor,
    default_editor_registry,
)

APP = """export default function App() {
  return (
    <main>
      <style>{"[data-vibe-target='cta']{background:red}"}</style>
      <section data-vibe-target="card">
        <button data-vibe-target="cta">Go</button>
      </section>
    </main>
  )
}
"""


class AnchorEditorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.editor = AnchorCodeEditor()

    def test_replace_anchor_first_and_last(self) -> None:
        source = "a X b X c"
        self.assertEqual(self.editor.replace_anchor(source, "X", "1"), "a 1 b X c")
        self.assertEqual(
            self.editor.replace_anchor(source, "X", "2", occurrence=-1),
            "a X b 2 c",
        )

    def test_missing_anchor_raises(self) -> None:
        with self.assertRaises(EditorError):
            self.editor.replace_anchor("abc", "zzz", "x", path="src/App.jsx")

    def test_expected_matches_guard(self) -> None:
        with self.assertRaises(EditorError):
            self.editor.replace_anchor(
                "X X", "X", "y", expected_matches=1, path="src/App.jsx"
            )

    def test_insert_inline_before_style_marker(self) -> None:
        edit = CodeEdit(
            kind=EditKind.INSERT_BEFORE,
            path="src/App.jsx",
            anchor='"}</style>',
            content=".cta{color:#fff}",
        )
        result = self.editor.apply(edit, {"src/App.jsx": APP})
        self.assertIn('.cta{color:#fff}"}</style>', result.files["src/App.jsx"])
        self.assertTrue(result.changed)

    def test_insert_block_before_closing_tag_keeps_indent(self) -> None:
        edit = CodeEdit(
            kind=EditKind.INSERT_BEFORE,
            path="src/App.jsx",
            anchor="</section>",
            content="<button>New</button>",
            layout=EditLayout.BLOCK,
            indent="        ",
        )
        updated = self.editor.apply(edit, {"src/App.jsx": APP}).files["src/App.jsx"]
        self.assertIn("        <button>New</button>\n      </section>", updated)

    def test_insert_after_leaf_element(self) -> None:
        edit = CodeEdit(
            kind=EditKind.INSERT_AFTER,
            path="src/App.jsx",
            anchor="</button>",
            content='<button data-vibe-target="added">New</button>',
        )
        updated = self.editor.apply(edit, {"src/App.jsx": APP}).files["src/App.jsx"]
        self.assertIn('</button><button data-vibe-target="added">New</button>', updated)

    def test_missing_target_file_raises(self) -> None:
        edit = CodeEdit(
            kind=EditKind.REPLACE_ANCHOR,
            path="src/Missing.jsx",
            anchor="X",
            content="Y",
        )
        with self.assertRaises(EditorError):
            self.editor.apply(edit, {"src/App.jsx": APP})


class FileCreatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.creator = FileCreator()

    def test_create_new_file(self) -> None:
        edit = CodeEdit(
            kind=EditKind.CREATE_FILE,
            path="services/api/src/Order.java",
            content="class Order {}\n",
        )
        result = self.creator.apply(edit, {"src/App.jsx": APP})
        self.assertTrue(result.changed)
        self.assertEqual(
            result.files["services/api/src/Order.java"], "class Order {}\n"
        )

    def test_create_existing_file_rejected(self) -> None:
        edit = CodeEdit(
            kind=EditKind.CREATE_FILE, path="src/App.jsx", content="overwrite"
        )
        with self.assertRaises(EditorError):
            self.creator.apply(edit, {"src/App.jsx": APP})

    def test_rewrite_missing_file_rejected(self) -> None:
        edit = CodeEdit(kind=EditKind.REWRITE_FILE, path="src/None.jsx", content="x")
        with self.assertRaises(EditorError):
            self.creator.apply(edit, {"src/App.jsx": APP})

    def test_path_traversal_rejected(self) -> None:
        for path in ("../secret.txt", "/etc/passwd", "a\\b.txt", "./a.txt"):
            with self.subTest(path=path), self.assertRaises(EditorError):
                self.creator.apply(
                    CodeEdit(kind=EditKind.CREATE_FILE, path=path, content="x"),
                    {},
                )


class RegistryTest(unittest.TestCase):
    def test_routes_by_edit_kind(self) -> None:
        registry = default_editor_registry()
        self.assertIsInstance(
            registry.resolve(EditKind.REPLACE_ANCHOR), AnchorCodeEditor
        )
        self.assertIsInstance(registry.resolve(EditKind.CREATE_FILE), FileCreator)
        self.assertIsInstance(registry.resolve(EditKind.STRUCTURED), TreeSitterEditor)

    def test_structured_editor_is_placeholder(self) -> None:
        registry = EditorRegistry([TreeSitterEditor()])
        with self.assertRaises(EditorNotAvailableError):
            registry.apply(
                CodeEdit(kind=EditKind.STRUCTURED, path="src/App.jsx", content=""),
                {"src/App.jsx": APP},
            )

    def test_apply_all_returns_net_changes_only(self) -> None:
        registry = default_editor_registry()
        edits = [
            CodeEdit(
                kind=EditKind.REPLACE_ANCHOR,
                path="src/App.jsx",
                anchor="Go",
                content="Submit",
            ),
            CodeEdit(
                kind=EditKind.CREATE_FILE,
                path="db/migrations/V1__init.sql",
                content="CREATE TABLE orders(id BIGINT);\n",
            ),
        ]
        result = registry.apply_all(edits, {"src/App.jsx": APP})
        self.assertEqual(
            set(result.files),
            {"src/App.jsx", "db/migrations/V1__init.sql"},
        )
        self.assertIn("Submit", result.files["src/App.jsx"])

    def test_apply_all_drops_unchanged_content(self) -> None:
        registry = default_editor_registry()
        result = registry.apply_all(
            [
                CodeEdit(
                    kind=EditKind.REWRITE_FILE,
                    path="src/App.jsx",
                    content=APP,
                )
            ],
            {"src/App.jsx": APP},
        )
        self.assertFalse(result.changed)
        self.assertEqual(dict(result.files), {})


if __name__ == "__main__":
    unittest.main()
