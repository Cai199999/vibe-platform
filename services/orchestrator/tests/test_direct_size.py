"""Direct Ops 尺寸契约：SET_SIZE / ADD_COMPONENT 的类名映射与合并。"""

from __future__ import annotations

import unittest

from ai_core.direct_ops.sizing import (
    add_component_size_classes,
    apply_add_size_context,
    clamp_px,
    merge_size_classes,
    normalize_axis,
    parse_class_name,
    size_classes,
)


class SizeMappingTest(unittest.TestCase):
    def test_fill_width_in_flex_row_uses_flex_grow(self) -> None:
        self.assertEqual(
            size_classes(
                {"mode": "fill"},
                None,
                {"display": "flex", "flexDirection": "row"},
            ),
            ["flex-1", "min-w-0"],
        )

    def test_fill_height_in_flex_row_uses_self_stretch(self) -> None:
        self.assertEqual(
            size_classes(
                None,
                {"mode": "fill"},
                {"display": "flex", "flexDirection": "row"},
            ),
            ["self-stretch"],
        )

    def test_fill_height_in_flex_column_uses_flex_grow(self) -> None:
        self.assertEqual(
            size_classes(
                None,
                {"mode": "fill"},
                {"display": "flex", "flexDirection": "column"},
            ),
            ["flex-1", "min-h-0"],
        )

    def test_fill_height_in_block_uses_min_height(self) -> None:
        # block 父容器可能是 auto 高度；h-full 会退化为 auto，min-h-full
        # 在有确定高度时填满、auto 时不影响，不会强行溢出。
        self.assertEqual(
            size_classes(None, {"mode": "fill"}, {"display": "block"}),
            ["min-h-full"],
        )

    def test_fill_height_in_grid_uses_full_height(self) -> None:
        self.assertEqual(
            size_classes(None, {"mode": "fill"}, {"display": "grid"}),
            ["h-full"],
        )

    def test_hug_and_fixed(self) -> None:
        self.assertEqual(size_classes({"mode": "hug"}, {"mode": "hug"}), ["w-fit", "h-fit"])
        self.assertEqual(
            size_classes({"mode": "fixed", "px": 240}, {"mode": "fixed", "px": 60}),
            ["w-[240px]", "h-[60px]"],
        )

    def test_normalize_axis_requires_fixed_value(self) -> None:
        self.assertEqual(normalize_axis({"mode": "fill"}), {"mode": "fill"})
        self.assertEqual(
            normalize_axis({"mode": "fixed", "value": 120}),
            {"mode": "fixed", "px": 120},
        )
        self.assertIsNone(normalize_axis({"mode": "fixed"}))
        self.assertIsNone(normalize_axis({"mode": "bogus"}))
        self.assertEqual(clamp_px(0), 1)
        self.assertEqual(clamp_px(99999), 4000)


class ClassMergeTest(unittest.TestCase):
    def test_merge_removes_conflicting_size_classes(self) -> None:
        merged = merge_size_classes(
            "w-full h-10 flex-1 text-sm",
            {"mode": "fixed", "px": 200},
            {"mode": "hug"},
            {"display": "block"},
        )
        self.assertIn("w-[200px]", merged)
        self.assertIn("h-fit", merged)
        self.assertIn("text-sm", merged)
        for removed in ("w-full", "h-10", "flex-1"):
            self.assertNotIn(removed, merged)

    def test_merge_keeps_unrelated_classes(self) -> None:
        merged = merge_size_classes(
            "rounded-lg p-4 self-center",
            {"mode": "fill"},
            None,
            {"display": "flex", "flexDirection": "column"},
        )
        self.assertIn("rounded-lg", merged)
        self.assertIn("p-4", merged)
        self.assertIn("w-full", merged)
        # width fill 不需要 self-stretch，对齐类保持不变。
        self.assertIn("self-center", merged)

    def test_parse_static_class_name(self) -> None:
        self.assertEqual(
            parse_class_name('<div className="w-full text-sm">x</div>'),
            (True, True, "w-full text-sm"),
        )
        self.assertEqual(
            parse_class_name("<div className={'w-full'}>x</div>"),
            (True, True, "w-full"),
        )
        self.assertEqual(
            parse_class_name("<div className={`w-full`}>x</div>"),
            (True, True, "w-full"),
        )
        self.assertEqual(parse_class_name("<div>x</div>"), (False, True, ""))

    def test_parse_dynamic_class_name(self) -> None:
        self.assertEqual(
            parse_class_name("<div className={clsx('w-full')}>x</div>"),
            (True, False, ""),
        )
        self.assertEqual(
            parse_class_name("<div className={`w-${size}`}>x</div>"),
            (True, False, ""),
        )


class AddComponentSizeTest(unittest.TestCase):
    def test_explicit_size_overrides_inherited_classes(self) -> None:
        jsx = '<div className="vibe-direct-container w-full h-10 p-3"></div>'
        merged = apply_add_size_context(jsx, "Container", 200, 50)
        self.assertIn("w-[200px]", merged)
        self.assertIn("h-[50px]", merged)
        self.assertIn("min-h-[30px]", merged)
        self.assertIn("p-3", merged)
        self.assertNotIn("w-full", merged)
        self.assertNotIn("h-10", merged)

    def test_type_default_min_height(self) -> None:
        self.assertIn(
            "min-h-[120px]",
            add_component_size_classes("Container", None, None),
        )
        self.assertIn(
            "min-h-[96px]",
            add_component_size_classes("Card", None, None),
        )
        self.assertIn(
            "min-h-[30px]",
            add_component_size_classes("Button", None, None),
        )

    def test_missing_class_name_is_added(self) -> None:
        merged = apply_add_size_context("<button>按钮</button>", "Button", 120, 40)
        self.assertIn('className="', merged)
        self.assertIn("w-[120px]", merged)


if __name__ == "__main__":
    unittest.main()
