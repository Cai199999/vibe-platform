#!/usr/bin/env python3
"""验证工作区 UI 文案极简清洗。

断言：
  1. 页面不再出现同步链路、Planner / Coder / Verifier、Mock / fail_first 等
     开发者视角文案；
  2. 文件路径、浏览器 URL、页脚说明、页面副标题全部移除；
  3. 模型选择只显示 DeepSeek V3，输入框占位符为“描述修改意图...”；
  4. 浮层标题只剩颜色圆点与关闭按钮，无“标注 1 / 1”；
  5. 空闲时不显示底部提示，画布提示为极弱样式；
  6. 工作区只保留导航、功能按钮、画布与悬浮输入框。

运行：
    /tmp/vibe-browser/bin/python scripts/ui_copy_clean_e2e_test.py
"""

from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
WEB_URL = "http://127.0.0.1:5173/"

FORBIDDEN_TEXT = [
    "同步链路",
    "Planner",
    "Coder",
    "Verifier",
    "Mock",
    "fail_first",
    "src/App.jsx",
    "vibe://",
    "发送后立即执行",
    "沿画布圈选组件",
]


def fail(message: str) -> None:
    print(f"\nFAIL: {message}")
    raise SystemExit(1)


def draw_around(page, locator) -> None:
    box = locator.bounding_box()
    if not box:
        fail("找不到圈选目标")
    page.mouse.move(box["x"] - 26, box["y"] - 18)
    page.mouse.down()
    page.mouse.move(
        box["x"] + box["width"] / 2,
        box["y"] + box["height"] / 2,
        steps=8,
    )
    page.mouse.move(
        box["x"] + box["width"] + 26,
        box["y"] + box["height"] + 18,
        steps=8,
    )
    page.mouse.up()
    page.wait_for_timeout(260)


def main() -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()

        print("== 1. 登录并检查工作区静态文案 ==")
        page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
        page.get_by_role("button", name="一键体验（推荐）").click()
        expect(page.locator(".workspace-tab")).to_have_count(1)
        active = page.locator(".tab-window.is-active")
        workspace_text = page.locator(".workspace").inner_text()
        for phrase in FORBIDDEN_TEXT:
            if phrase in workspace_text:
                fail(f"仍存在开发者文案: {phrase!r}")
        if active.locator(".tab-window-head").count():
            fail("页面副标题区域仍然存在")
        if active.locator(".footnote").count():
            fail("页脚说明仍然存在")
        if active.locator(".source-path").count():
            fail("文件路径标签仍然存在")
        print("   同步链路 / 技术术语 / 文件路径 / 页脚均已移除")

        model = active.locator(".model-picker select")
        if model.input_value() != "auto":
            fail(f"默认模型不是 auto: {model.input_value()}")
        selected_label = model.locator("option:checked").inner_text()
        if selected_label != "DeepSeek V3":
            fail(f"模型展示文案异常: {selected_label}")
        options = model.locator("option").all_inner_texts()
        if any("Mock" in text for text in options):
            fail(f"模型下拉仍包含 Mock: {options}")
        print(f"   模型: {selected_label} / 选项: {options}")

        toolbar_buttons = active.locator(
            ".project-toolbar-right button"
        ).all_inner_texts()
        print(f"   功能按钮: {toolbar_buttons}")

        print("\n== 2. 圈选后检查悬浮框文案 ==")
        draw_around(page, active.locator(".mock-cta"))
        popover = active.locator(".canvas-popover")
        expect(popover).to_have_count(1)

        placeholder = popover.locator("input").get_attribute("placeholder")
        if placeholder != "描述修改意图...":
            fail(f"占位符未精简: {placeholder!r}")
        # 标题区只保留颜色圆点、操作类型切换与关闭按钮；
        # 不应再有“标注 1/1”或组件类型标签。
        mode_buttons = popover.locator(".popover-action button")
        mode_labels = mode_buttons.all_inner_texts()
        if mode_labels != ["修改", "新增"]:
            fail(f"操作类型切换异常: {mode_labels}")
        head_text = (
            (popover.locator(".popover-head").text_content() or "")
            .replace("×", "")
            .replace("修改", "")
            .replace("新增", "")
            .strip()
        )
        if head_text:
            fail(f"浮层标题仍有文字: {head_text!r}")
        if popover.locator(".popover-target").count():
            fail("浮层仍显示组件类型标签")
        if popover.locator(".popover-hint").count():
            fail("空闲状态仍显示底部说明")
        print("   标题仅保留颜色圆点、修改/新增切换；占位符：描述修改意图...")

        hint = active.locator(".canvas-hint")
        expect(hint).to_be_visible()
        style = hint.evaluate("""el => {
                const cs = getComputedStyle(el);
                return {fontSize: cs.fontSize, opacity: cs.opacity, color: cs.color};
            }""")
        if float(style["fontSize"].replace("px", "")) > 10:
            fail(f"画布提示字号仍偏大: {style}")
        if float(style["opacity"]) > 0.5:
            fail(f"画布提示仍过于醒目: {style}")
        print(f"   画布提示: {style}")

        chrome_text = (
            active.locator(".canvas-chrome").first.text_content() or ""
        ).strip()
        if chrome_text:
            fail(f"画布顶栏仍有 URL / 文件路径: {chrome_text!r}")
        print("   画布顶栏仅保留窗口圆点")

        page.screenshot(
            path=str(SCREENSHOT_DIR / "m1-ui-polished-workspace.png"),
            full_page=True,
        )
        context.close()
        browser.close()
    print("\nUI COPY CLEAN E2E: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
