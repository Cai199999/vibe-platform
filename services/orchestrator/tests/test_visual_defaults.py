"""Architect VisualDocument → 组件默认样式抽取。"""

from __future__ import annotations

import unittest

from ai_core.codegen.visual_defaults import component_style_defaults
from ai_core.domain.visual_document import VisualDocument


def _document() -> VisualDocument:
    return VisualDocument.model_validate(
        {
            "schemaVersion": "1.0",
            "docId": "vdoc_defaults",
            "projectId": "proj_defaults",
            "revision": 1,
            "title": "默认样式",
            "root": {
                "id": "nd_root",
                "type": "page",
                "children": [
                    {
                        "id": "nd_primary",
                        "type": "button",
                        "name": "主按钮",
                        "props": {"text": "保存"},
                        "semantics": {"role": "primary"},
                        "style": {"tokenRefs": ["color.primary"]},
                    },
                    {
                        "id": "nd_secondary",
                        "type": "button",
                        "name": "次要按钮",
                        "props": {"text": "取消"},
                        "semantics": {"role": "secondary"},
                    },
                    {
                        "id": "nd_login",
                        "type": "button",
                        "name": "登录",
                        "props": {"text": "登录"},
                    },
                    {
                        "id": "nd_input",
                        "type": "input",
                        "props": {"placeholder": "请输入"},
                    },
                ],
            },
            "interactions": [],
            "bindings": [],
        }
    )


class VisualDefaultsTest(unittest.TestCase):
    def test_component_defaults_use_codegen_classes(self) -> None:
        defaults = component_style_defaults(_document())

        primary = defaults["primary_button"]
        self.assertEqual(primary["label"], "主按钮")
        self.assertEqual(primary["tag"], "button")
        # CodeGen 默认类 + 主色令牌必须同时存在，保证“默认 = 显示”。
        self.assertIn("px-4", primary["classes"])
        self.assertIn("py-2", primary["classes"])
        self.assertIn("bg-[var(--color-primary)]", primary["classes"])
        self.assertEqual(
            primary["styles"].get("backgroundColor"),
            "var(--color-primary)",
        )
        self.assertIn("secondary_button", defaults)
        self.assertIn("login_button", defaults)
        self.assertIn("input", defaults)

    def test_missing_document_returns_empty(self) -> None:
        self.assertEqual(component_style_defaults(None), {})


if __name__ == "__main__":
    unittest.main()
