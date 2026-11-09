#!/usr/bin/env python3
"""E2E：开发者详情面板与状态栏。

覆盖：
  1. 预览顶部状态栏展示模型与沙盒实时状态；
  2. 底部常驻状态栏可拉起开发者详情抽屉；
  3. 抽屉展示 AST 路径（file:line:column）、LangGraph 时间轴与原始 Diff；
  4. 再次点击可收起抽屉。

运行：
    /tmp/vibe-browser/bin/python scripts/developer_panel_e2e_test.py
"""

from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
WEB_URL = "http://127.0.0.1:5173/"
PY_AI_URL = "http://127.0.0.1:8100"


def fail(message: str) -> None:
    print(f"\nFAIL: {message}")
    raise SystemExit(1)


def cleanup_sandbox(sandbox_id: str | None) -> None:
    if not sandbox_id:
        return
    request = urllib.request.Request(
        f"{PY_AI_URL}/api/v1/internal/sandbox/{sandbox_id}", method="DELETE"
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            print(f"   cleanup {sandbox_id} -> HTTP {response.status}")
    except Exception as exc:  # noqa: BLE001 - 清理失败不掩盖主流程
        print(f"   cleanup {sandbox_id} -> {exc}")


def main() -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        context.add_init_script(
            "window.localStorage.setItem('vibe.llmProvider', 'mock')"
        )
        page = context.new_page()
        sandbox_id: str | None = None

        try:
            print("== 1. 登录并创建一次成功的 ADD Run ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            active = page.locator(".tab-window.is-active")
            expect(active.locator(".mock-card")).to_be_visible()

            card = active.locator(".mock-card").bounding_box()
            if not card:
                fail("找不到大卡片")
            left = card["x"] + card["width"] * 0.62
            right = card["x"] + card["width"] * 0.95
            top = card["y"] + card["height"] * 0.72
            bottom = card["y"] + card["height"] * 0.94
            page.mouse.move(left, top)
            page.mouse.down()
            page.mouse.move(right, bottom, steps=12)
            page.mouse.up()

            popover = active.locator(".canvas-popover")
            popover.locator(".popover-row input").fill("加个红色按钮")
            popover.locator(".popover-send").click()
            expect(
                active.locator(".runtime-result-overlay .status-pill")
            ).to_contain_text("已完成", timeout=240_000)
            sandbox_id = active.locator(".runtime-panel").get_attribute(
                "data-sandbox-id"
            )

            print("\n== 2. 顶部状态栏展示模型与沙盒 ==")
            chrome = active.locator(".runtime-chrome")
            expect(chrome).to_contain_text("Mock 引擎")
            expect(chrome).to_contain_text("沙盒")
            print(f"   chrome={chrome.inner_text()!r}")

            print("\n== 3. 底部状态栏拉起开发者详情 ==")
            statusbar = active.locator(".runtime-statusbar")
            expect(statusbar).to_be_visible()
            expect(statusbar).to_contain_text("Mock 引擎")
            statusbar.locator(".runtime-statusbar-toggle").click()

            drawer = active.locator(".runtime-drawer")
            expect(drawer).to_be_visible()
            expect(drawer).to_contain_text("目标元素 AST 路径")
            expect(drawer).to_contain_text("LangGraph 节点流转")
            expect(drawer).to_contain_text("原始代码 Diff")

            ast_paths = drawer.locator(".ast-path")
            if ast_paths.count() == 0:
                fail("详情抽屉没有 AST 路径")
            ast_text = ast_paths.first.inner_text()
            if not re.match(r"^src/App\.jsx:\d+:\d+$", ast_text):
                fail(f"AST 路径格式不正确: {ast_text}")
            print(f"   ast={ast_text}")
            if drawer.locator(".run-timeline .timeline-item").count() == 0:
                fail("详情抽屉没有 LangGraph 时间轴节点")

            screenshot = SCREENSHOT_DIR / "developer-panel.png"
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot))
            print(f"   screenshot -> {screenshot}")

            print("\n== 4. 再次点击收起抽屉 ==")
            statusbar.locator(".runtime-statusbar-toggle").click()
            expect(drawer).to_have_count(0)

            print("\nDEVELOPER PANEL E2E: PASS")
            return 0
        finally:
            cleanup_sandbox(sandbox_id)
            context.close()
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
