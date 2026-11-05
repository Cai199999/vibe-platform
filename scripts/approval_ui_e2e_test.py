#!/usr/bin/env python3
"""M3-A Diff 审批 / 取消 Run 的浏览器级 E2E。

覆盖产品前端（approvalMode=required）的三条主路径：
  1. 提交修改 -> 预览面板出现逐文件 Diff -> 批准并应用 -> 预览变蓝；
  2. 审批暂停时画布输入框冻结；
  3. 再次提交 -> 取消执行 -> 状态「已取消」，预览保持上一轮结果；
  4. 再次提交 -> 拒绝并撤销 -> 状态「已拒绝」，预览仍保持上一轮结果。

前置：
    docker compose up -d
    /tmp/vibe-browser/bin/python scripts/approval_ui_e2e_test.py
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
WEB_URL = "http://127.0.0.1:5173/"
BLUE = "rgb(0, 0, 255)"


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


def send_instruction(page, active, popover, text: str, label: str) -> None:
    popover.locator("input").fill(text, force=True)
    popover.locator(".popover-send").click(force=True)
    print(f"   {label}: 已发送「{text}」")


def wait_approval(active, label: str, timeout: int = 120_000):
    """等待审批面板出现，返回它所在的 locator。"""
    panel = active.locator(".loading-state.is-approval")
    expect(panel).to_be_visible(timeout=timeout)
    expect(panel.locator(".approval-file").first).to_be_visible(timeout=timeout)
    print(f"   {label}: 审批面板已出现")
    return panel


def wait_cta_color(active, expected: str, label: str) -> None:
    frame = active.frame_locator(".runtime-preview-frame")
    cta = frame.locator("[data-vibe-target='cta-button']")
    expect(cta).to_be_visible(timeout=30_000)
    deadline = time.time() + 30
    color = ""
    while time.time() < deadline:
        color = cta.evaluate("el => getComputedStyle(el).backgroundColor")
        if color == expected:
            print(f"   {label}: {color}")
            return
        time.sleep(0.4)
    fail(f"{label} 颜色不是 {expected}: {color}")


def main() -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()

        print("== 1. 登录、选择 Mock、切到展示模式 ==")
        page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
        page.get_by_role("button", name="一键体验（推荐）").click()
        expect(page.locator(".workspace-tab")).to_have_count(1)
        active = page.locator(".tab-window.is-active")
        active.locator(".model-picker select").select_option("mock")
        active.get_by_role("button", name="展示模式").click()

        print("\n== 2. 圈选 CTA，发送「改成蓝色」 ==")
        draw_around(page, active.locator(".mock-cta"))
        popover = active.locator(".canvas-popover")
        expect(popover).to_have_count(1)
        send_instruction(page, active, popover, "改成蓝色", "第一轮")

        panel = wait_approval(active, "第一轮")
        expect(panel).to_contain_text("等待审批")
        expect(panel.locator(".approval-summary")).to_contain_text("构建 PASSED")
        # 暂停期间画布输入必须冻结，避免旧沙盒改动被新 Run 覆盖。
        expect(popover.locator("input")).to_be_disabled()

        print("\n== 3. 批准并应用 -> 预览变蓝 ==")
        panel.get_by_role("button", name="批准并应用").click()
        expect(active.locator(".status-pill")).to_contain_text(
            "已完成", timeout=180_000
        )
        expect(popover.locator(".chat-bubble.is-assistant")).to_have_count(1)
        wait_cta_color(active, BLUE, "批准后预览")
        page.screenshot(
            path=str(SCREENSHOT_DIR / "m3-approval-approved.png"),
            full_page=True,
        )

        print("\n== 4. 第二轮：发送「改成红色」-> 取消执行 ==")
        # 等审批面板消失、输入框解冻后再提交。
        expect(popover.locator("input")).to_be_enabled(timeout=30_000)
        send_instruction(page, active, popover, "改成红色", "第二轮")
        panel = wait_approval(active, "第二轮")
        panel.get_by_role("button", name="取消执行").click()
        expect(active.locator(".status-pill")).to_contain_text(
            "已取消", timeout=120_000
        )
        expect(popover.locator(".chat-bubble.is-assistant")).to_have_count(2)
        wait_cta_color(active, BLUE, "取消后预览仍为上一轮结果")

        print("\n== 5. 第三轮：发送「改成绿色」-> 拒绝并撤销 ==")
        expect(popover.locator("input")).to_be_enabled(timeout=30_000)
        send_instruction(page, active, popover, "改成绿色", "第三轮")
        panel = wait_approval(active, "第三轮")
        panel.get_by_role("button", name="拒绝并撤销").click()
        expect(active.locator(".status-pill")).to_contain_text(
            "已拒绝", timeout=120_000
        )
        expect(popover.locator(".chat-bubble.is-assistant")).to_have_count(3)
        wait_cta_color(active, BLUE, "拒绝后预览仍为上一轮结果")
        page.screenshot(
            path=str(SCREENSHOT_DIR / "m3-approval-rejected.png"),
            full_page=True,
        )

        print("\n== 6. 关闭 Tab：销毁本次会话创建的全部沙盒 ==")
        page.get_by_role("button", name=re.compile(r"^关闭 ")).click()
        expect(page.locator(".workspace-tab")).to_have_count(0, timeout=30_000)
        page.wait_for_timeout(1500)

        browser.close()
    print("\nAPPROVAL UI E2E: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
