#!/usr/bin/env python3
"""E2E：在画布上圈选大卡片内部，要求“添加一个红色的提交按钮”。

覆盖链路：
  1. 圈选区域未命中具体控件 / 命中的是卡片容器时，前端自动把 action 推断为 ADD；
  2. 浮层支持手动切换 修改 / 新增；
  3. AnnotationEnvelope.action=ADD 传到 python-ai，Mock Agent 在卡片内部
     插入完整 <button> JSX，而不是覆盖已有元素；
  4. Applier 全量写回沙盒，npm run build 通过；
  5. 预览 iframe 中出现红色“提交”按钮，原有 CTA 仍然存在。

运行：
    /tmp/vibe-browser/bin/python scripts/add_component_e2e_test.py
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
        page = context.new_page()
        sandbox_id: str | None = None

        try:
            print("== 1. 登录并进入演示项目 ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            expect(page.locator(".workspace-tab")).to_have_count(1)
            active = page.locator(".tab-window.is-active")
            expect(active.locator(".mock-card")).to_be_visible()

            print("\n== 2. 圈选大卡片内部（不刻意命中按钮 / 标题） ==")
            card = active.locator(".mock-card").bounding_box()
            if not card:
                fail("找不到大卡片")
            page.mouse.move(
                card["x"] + card["width"] * 0.08,
                card["y"] + card["height"] * 0.08,
            )
            page.mouse.down()
            page.mouse.move(
                card["x"] + card["width"] * 0.48,
                card["y"] + card["height"] * 0.36,
                steps=10,
            )
            page.mouse.up()
            page.wait_for_timeout(300)

            popover = active.locator(".canvas-popover")
            expect(popover).to_have_count(1)

            print("\n== 3. 输入新增意图，action 自动切换为 ADD ==")
            popover.locator("input").fill("添加一个红色的提交按钮")
            expect(popover).to_have_attribute("data-action", "ADD")

            # 手动切换：修改 -> 新增，验证用户可覆盖自动推断。
            mode_group = popover.locator(".popover-action button")
            mode_group.filter(has_text="修改").click()
            expect(popover).to_have_attribute("data-action", "MODIFY")
            mode_group.filter(has_text="新增").click()
            expect(popover).to_have_attribute("data-action", "ADD")
            print("   action: 自动 ADD，手动 修改/MODIFY ↔ 新增/ADD 正常")

            print("\n== 4. 发送并等待沙盒构建完成 ==")
            popover.locator(".popover-send").click()
            expect(active.locator(".seg-status-running")).to_be_visible(timeout=10_000)
            # 产品前端默认 approvalMode=required：构建通过后先审批再落盘。
            # 画布侧出现“查看审批差异”入口，点击后切到预览侧审批面板。
            pause_cta = active.locator(".view-result-cta.is-pending")
            expect(pause_cta).to_be_visible(timeout=180_000)
            pause_cta.click()
            approval = active.locator(".loading-state.is-approval")
            expect(approval).to_be_visible(timeout=30_000)
            approval.get_by_role("button", name="批准并应用").click()
            expect(
                active.locator(".runtime-result-overlay .status-pill")
            ).to_contain_text("已完成", timeout=180_000)
            status_text = (
                active.locator(".runtime-result-overlay .status-pill").text_content()
                or ""
            )
            sandbox_id = active.locator(".runtime-panel").get_attribute(
                "data-sandbox-id"
            )
            print(f"   状态: {status_text.strip()} / 沙盒: {sandbox_id}")

            print("\n== 5. 预览中出现红色提交按钮，原有 CTA 未被覆盖 ==")
            # 审批暂停会自动切到预览；若仍停在画布（例如以后关闭自动切换），
            # 则点击“查看效果”进入预览。
            view_result = active.locator(".view-result-cta")
            if view_result.is_visible():
                view_result.click()
            expect(active.locator(".preview-pane.is-visible")).to_be_visible()
            frame = page.frame_locator(".tab-window.is-active .runtime-preview-frame")
            added = frame.locator("button.vibe-added-button")
            expect(added).to_be_visible(timeout=30_000)
            expect(added).to_have_text("提交")
            background = added.evaluate("el => getComputedStyle(el).backgroundColor")
            if background != "rgb(255, 0, 0)":
                fail(f"新增按钮不是红色: {background}")
            original = frame.locator("button.vibe-cta")
            expect(original).to_be_visible()
            expect(original).to_have_text("开始体验")
            print("   新增按钮: 提交 / 背景 " f"{background}; 原按钮仍为“开始体验”")

            page.wait_for_timeout(300)
            page.screenshot(
                path=str(SCREENSHOT_DIR / "m1-add-component-preview.png"),
                full_page=True,
            )
            print("   screenshot -> docs/screenshots/m1-add-component-preview.png")
        finally:
            cleanup_sandbox(sandbox_id)
            context.close()
            browser.close()

    print("\nADD COMPONENT E2E: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
