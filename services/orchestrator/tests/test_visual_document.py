"""VisualDocument 离线虚拟树契约与校验器测试。"""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from ai_core.domain.visual_document import (
    VisualDocument,
    compute_content_hash,
    freeze_visual_document,
    validate_visual_document,
)

_DEFAULT_ROOT = object()


def _document(root: dict | None | object = _DEFAULT_ROOT, **overrides):
    default_root = {
        "id": "nd_root",
        "type": "page",
        "children": [
            {"id": "nd_title", "type": "text", "props": {"text": "标题"}},
            {
                "id": "nd_button",
                "type": "button",
                "props": {"text": "导出"},
            },
        ],
    }
    payload = {
        "schemaVersion": "1.0",
        "docId": "vdoc_test",
        "projectId": "proj_test",
        "revision": 1,
        "title": "测试页面",
        "root": default_root if root is _DEFAULT_ROOT else root,
        "interactions": [
            {
                "id": "act_export",
                "nodeId": "nd_button",
                "action": "export",
                "intent": "导出订单",
                "bindingId": "bind_export",
                "status": "UNRESOLVED",
            }
        ],
        "bindings": [
            {
                "id": "bind_export",
                "nodeId": "nd_button",
                "kind": "api",
                "entity": "order",
                "field": "export",
                "required": True,
                "status": "UNRESOLVED",
            }
        ],
    }
    payload.update(overrides)
    return payload


class VisualDocumentSchemaTest(unittest.TestCase):
    def test_empty_root_is_valid_blank_canvas(self) -> None:
        document = VisualDocument.model_validate(_document(root=None))
        self.assertIsNone(document.root)
        report = validate_visual_document(document)
        self.assertTrue(report.ok)
        self.assertEqual(report.node_count, 0)

    def test_valid_tree_and_binding(self) -> None:
        document = VisualDocument.model_validate(_document())
        report = validate_visual_document(document)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.node_count, 3)
        self.assertEqual(report.max_depth, 2)

    def test_duplicate_node_id_is_rejected(self) -> None:
        report = validate_visual_document(
            _document(
                root={
                    "id": "nd_root",
                    "type": "page",
                    "children": [
                        {"id": "nd_same", "type": "text"},
                        {"id": "nd_same", "type": "text"},
                    ],
                }
            )
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("重复的 nodeId" in item for item in report.errors))

    def test_pixel_coordinates_are_rejected(self) -> None:
        report = validate_visual_document(
            _document(
                root={
                    "id": "nd_root",
                    "type": "page",
                    "x": 120,
                    "children": [],
                }
            )
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("像素坐标" in item for item in report.errors))

    def test_jsx_markup_is_rejected(self) -> None:
        report = validate_visual_document(
            _document(
                root={
                    "id": "nd_root",
                    "type": "page",
                    "children": [
                        {
                            "id": "nd_text",
                            "type": "text",
                            "props": {"text": "<Button>提交</Button>"},
                        }
                    ],
                }
            )
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("JSX/HTML" in item for item in report.errors))

    def test_non_container_cannot_have_children(self) -> None:
        report = validate_visual_document(
            _document(
                root={
                    "id": "nd_root",
                    "type": "page",
                    "children": [
                        {
                            "id": "nd_text",
                            "type": "text",
                            "children": [{"id": "nd_child", "type": "text"}],
                        }
                    ],
                }
            )
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("不能包含子节点" in item for item in report.errors))

    def test_interaction_must_reference_existing_node(self) -> None:
        report = validate_visual_document(
            _document(
                interactions=[
                    {
                        "id": "act_missing",
                        "nodeId": "nd_missing",
                        "action": "custom",
                    }
                ],
                bindings=[],
            )
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("不存在的节点" in item for item in report.errors))

    def test_repeat_requires_matching_binding(self) -> None:
        report = validate_visual_document(
            _document(
                root={
                    "id": "nd_root",
                    "type": "page",
                    "children": [
                        {
                            "id": "nd_list",
                            "type": "list",
                            "repeat": {"source": "orders"},
                            "children": [],
                        }
                    ],
                },
                interactions=[],
                bindings=[],
                status="FROZEN",
            )
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("repeat.source" in item for item in report.errors))

    def test_draft_repeat_without_binding_is_warning_only(self) -> None:
        report = validate_visual_document(
            _document(
                root={
                    "id": "nd_root",
                    "type": "page",
                    "children": [
                        {
                            "id": "nd_list",
                            "type": "list",
                            "repeat": {"source": "orders"},
                            "children": [],
                        }
                    ],
                },
                interactions=[],
                bindings=[],
            )
        )
        self.assertTrue(report.ok)
        self.assertTrue(any("业务审查" in item for item in report.warnings))

    def test_freeze_writes_stable_content_hash(self) -> None:
        document = VisualDocument.model_validate(_document())
        frozen = freeze_visual_document(document)
        self.assertEqual(frozen.status, "FROZEN")
        self.assertTrue(frozen.metadata.content_hash.startswith("sha256:"))
        self.assertIsNotNone(frozen.review.frozen_at)
        # 哈希覆盖冻结状态本身；同一份冻结文档重复计算必须稳定。
        self.assertEqual(
            frozen.metadata.content_hash,
            compute_content_hash(frozen),
        )
        self.assertEqual(
            frozen.metadata.content_hash,
            compute_content_hash(frozen.model_copy(deep=True)),
        )

    def test_invalid_root_type_raises(self) -> None:
        with self.assertRaises(ValidationError):
            VisualDocument.model_validate(
                _document(root={"id": "nd_root", "type": "nope"})
            )


if __name__ == "__main__":
    unittest.main()
