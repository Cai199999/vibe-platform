#!/usr/bin/env python3
"""E2E：项目快照与版本历史。

覆盖：
  1. 工具栏 💾 保存快照 → Modal 输入备注 → Git commit；
  2. 顶部“版本历史”入口 → 右侧抽屉按时间倒序列出快照；
  3. 点击“查看” → 新建只读虚拟标签并展示该版本代码 Diff；
  4. 点击“恢复” → 二次确认（未保存修改将丢失）→ reset --hard；
  5. 恢复后抽屉刷新、预览 iframe 重建并加载回滚版本。

运行：
    /tmp/vibe-browser/bin/python scripts/snapshot_management_e2e_test.py
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
WEB_URL = "http://127.0.0.1:5173/"
PY_AI_URL = "http://127.0.0.1:8100"

NOTE_ONE = "第一个快照：完成新增按钮"
NOTE_TWO = "第二个快照：准备回滚"


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


def save_snapshot(page, active, note: str) -> None:
    """通过工具栏「更多」菜单保存一个快照并等待成功提示。"""
    active.locator("[data-toolbar-more]").click()
    active.locator("[data-snapshot-save]").click()
    modal = page.locator(".snapshot-modal")
    expect(modal).to_be_visible()
    modal.locator("#snapshot-note").fill(note)
    modal.locator('button[type="submit"]').click()
    expect(page.locator(".workspace-toast.supervisor-toast")).to_contain_text(
        "快照已保存", timeout=60_000
    )
    expect(modal).to_have_count(0)


def main() -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        context.add_init_script(
            "window.localStorage.setItem('vibe.llmProvider', 'mock')"
        )
        page = context.new_page()
        sandbox_id: str | None = None
        preview_requests: list[str] = []
        preview_prefix = ""

        def capture_preview(request) -> None:
            if preview_prefix and request.url.startswith(preview_prefix):
                preview_requests.append(request.url)

        page.on("request", capture_preview)

        try:
            print("== 1. 登录并执行一次成功的 ADD Run ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            active = page.locator(".tab-window.is-active")
            expect(active.locator(".mock-card")).to_be_visible()

            card = active.locator(".mock-card").bounding_box()
            if not card:
                fail("找不到大卡片")
            page.mouse.move(
                card["x"] + card["width"] * 0.62,
                card["y"] + card["height"] * 0.72,
            )
            page.mouse.down()
            page.mouse.move(
                card["x"] + card["width"] * 0.95,
                card["y"] + card["height"] * 0.94,
                steps=12,
            )
            page.mouse.up()

            popover = active.locator(".canvas-popover")
            popover.locator(".popover-row input").fill("加个按钮")
            popover.locator(".popover-send").click()
            expect(
                active.locator(".runtime-result-overlay .status-pill")
            ).to_contain_text("已完成", timeout=240_000)
            sandbox_id = active.locator(".runtime-panel").get_attribute(
                "data-sandbox-id"
            )
            print(f"   sandbox={sandbox_id}")

            preview_src = active.locator(".runtime-preview-frame").get_attribute("src")
            if not preview_src:
                fail("找不到预览 iframe")
            preview_prefix = preview_src.rstrip("/")

            print("\n== 2. 保存两个快照 ==")
            save_snapshot(page, active, NOTE_ONE)
            page.wait_for_timeout(500)
            save_snapshot(page, active, NOTE_TWO)

            print("\n== 3. 打开版本历史抽屉 ==")
            page.locator("[data-snapshot-history-toggle]").click()
            drawer = page.locator("[data-snapshot-drawer]")
            expect(drawer).to_be_visible()
            expect(drawer.locator(".snapshot-item", has_text=NOTE_ONE)).to_be_visible()
            expect(drawer.locator(".snapshot-item", has_text=NOTE_TWO)).to_be_visible()
            order = drawer.locator(".snapshot-item-message").all_inner_texts()
            print(f"   history={order}")
            if order.index(NOTE_TWO) > order.index(NOTE_ONE):
                fail("版本历史没有按时间倒序排列")

            screenshot = SCREENSHOT_DIR / "snapshot-history-drawer.png"
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot))
            print(f"   screenshot -> {screenshot}")

            print("\n== 4. 查看快照：新建只读虚拟标签并展示 Diff ==")
            first_item = drawer.locator(".snapshot-item", has_text=NOTE_ONE)
            first_item.get_by_role("button", name="查看").click()
            expect(drawer).to_have_count(0)
            snapshot_tab = page.locator(
                ".workspace-tab.snapshot-tab", has_text=NOTE_ONE
            )
            expect(snapshot_tab).to_be_visible()
            snapshot_window = page.locator(".snapshot-window.is-active")
            expect(snapshot_window).to_be_visible()
            expect(snapshot_window.locator(".snapshot-badge")).to_have_text("只读快照")
            diff_view = snapshot_window.locator(".diff-view")
            expect(diff_view).to_be_visible(timeout=30_000)
            diff_text = diff_view.inner_text()
            if "vibe-added-button" not in diff_text:
                fail("快照 Diff 没有包含 Agent 新增按钮的改动")
            print("   只读快照标签与代码 Diff 正常")

            print("\n== 5. 关闭快照标签，回到项目工作区 ==")
            snapshot_tab.locator(".tab-close").click()
            expect(snapshot_window).to_have_count(0)
            # Run 完成后工作区会自动聚焦预览面板，因此断言项目 Tab 的
            # 预览 iframe 可见，而不是回去找画布里的卡片。
            expect(
                page.locator(".tab-window.is-active .runtime-preview-frame")
            ).to_be_visible()

            print("\n== 6. 恢复旧快照：确认 → reset --hard → 预览重载 ==")
            page.locator("[data-snapshot-history-toggle]").click()
            drawer = page.locator("[data-snapshot-drawer]")
            expect(drawer).to_be_visible()
            first_item = drawer.locator(".snapshot-item", has_text=NOTE_ONE)
            first_item.get_by_role("button", name="恢复", exact=True).click()
            confirm = drawer.locator(".snapshot-confirm")
            expect(confirm).to_be_visible()
            expect(confirm).to_contain_text("当前未保存的修改将丢失，是否继续？")
            before_requests = len(preview_requests)
            confirm.get_by_role("button", name="继续恢复").click()
            expect(page.locator(".workspace-toast.supervisor-toast")).to_contain_text(
                "已恢复到快照", timeout=60_000
            )

            # 恢复后后一个 commit 被 reset 丢弃，历史里只应剩第一个快照及其祖先。
            expect(drawer.locator(".snapshot-item", has_text=NOTE_TWO)).to_have_count(
                0, timeout=20_000
            )
            head_item = drawer.locator(".snapshot-item", has_text=NOTE_ONE)
            expect(head_item.locator(".snapshot-head-tag")).to_have_text("当前版本")
            print("   历史已刷新，第二个快照被回滚")

            # iframe 以新 key 重建，必须重新请求预览地址。
            for _ in range(40):
                if len(preview_requests) > before_requests:
                    break
                page.wait_for_timeout(500)
            if len(preview_requests) <= before_requests:
                fail("恢复后预览 iframe 没有重新加载")
            print(
                f"   预览请求数 {before_requests} -> {len(preview_requests)}（iframe 已重建）"
            )

            page.locator("[data-snapshot-drawer] .snapshot-drawer-close").click()
            active.locator(".pane-switch button", has_text="预览").click()
            frame = page.frame_locator(".tab-window.is-active .runtime-preview-frame")
            expect(frame.locator("button.vibe-added-button")).to_be_visible(
                timeout=30_000
            )

            screenshot = SCREENSHOT_DIR / "snapshot-restored.png"
            page.screenshot(path=str(screenshot))
            print(f"   screenshot -> {screenshot}")

            print("\nSNAPSHOT MANAGEMENT E2E: PASS")
            return 0
        finally:
            cleanup_sandbox(sandbox_id)
            context.close()
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
