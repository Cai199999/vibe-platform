#!/usr/bin/env python3
"""生成 UI 极简清洗后的工作区截图。

流程：一键体验 → 圈选 CTA → 输入一句意图 → 截取整页。
不执行 Agent，避免截图里出现运行详情等内部信息。

运行：
    /tmp/vibe-browser/bin/python scripts/ui_polish_screenshot.py
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
WEB_URL = "http://127.0.0.1:5173/"


def main() -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()

        page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
        page.get_by_role("button", name="一键体验（推荐）").click()
        expect(page.locator(".workspace-tab")).to_have_count(1)
        active = page.locator(".tab-window.is-active")

        box = active.locator(".mock-cta").bounding_box()
        if not box:
            raise SystemExit("找不到 CTA")
        page.mouse.move(box["x"] - 26, box["y"] - 18)
        page.mouse.down()
        page.mouse.move(
            box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=8
        )
        page.mouse.move(
            box["x"] + box["width"] + 26,
            box["y"] + box["height"] + 18,
            steps=8,
        )
        page.mouse.up()
        page.wait_for_timeout(300)

        popover = active.locator(".canvas-popover")
        expect(popover).to_have_count(1)
        page.wait_for_timeout(200)

        page.screenshot(
            path=str(SCREENSHOT_DIR / "m1-ui-polished-workspace.png"),
            full_page=True,
        )
        print("screenshot -> docs/screenshots/m1-ui-polished-workspace.png")

        context.close()
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
