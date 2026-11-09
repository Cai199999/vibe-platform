#!/usr/bin/env python3
"""E2E：分级决策机制。

验证两条产品规则：
  1. 实现细节（“加个登录框，做得漂亮一点”）不打断用户，Agent 自主完成；
  2. 核心业务（“密码提交时调用后端密码校验接口”）暂停并给出一键决策，
     点击推荐项后自动恢复，无需再点“发送并继续”。

运行：
    /tmp/vibe-browser/bin/python scripts/tiered_decision_e2e_test.py
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

DETAIL_PROMPT = "加个登录框，做得漂亮一点"
CORE_PROMPT = "加个登录框，密码校验放在前端还是对接已有认证服务"


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


def draw_on_card(page, active) -> None:
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
    expect(active.locator(".canvas-popover")).to_have_count(1)


def open_demo(context):
    page = context.new_page()
    page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
    page.get_by_role("button", name="一键体验（推荐）").click()
    expect(page.locator(".workspace-tab")).to_have_count(1)
    active = page.locator(".tab-window.is-active")
    active.locator(".model-picker select").select_option("mock")
    return page, active


def sandbox_id(active) -> str | None:
    return active.locator(".runtime-panel").get_attribute("data-sandbox-id")


def scenario_detail(context) -> str | None:
    page, active = open_demo(context)
    try:
        print("== 场景 A：实现细节不打断 ==")
        draw_on_card(page, active)
        popover = active.locator(".canvas-popover")
        popover.locator(".popover-row input").fill(DETAIL_PROMPT)
        popover.locator(".popover-send").click()

        # 若错误地触发澄清，这里会先出现“等待你确认”，而不会直接完成。
        expect(active.locator(".runtime-result-overlay .status-pill")).to_contain_text(
            "已完成", timeout=180_000
        )
        if popover.locator(".popover-clarification").count() != 0:
            fail("实现细节不应触发澄清")
        print("   OK：未出现澄清，直接生成完成")
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(
            path=str(SCREENSHOT_DIR / "tiered-decision-detail.png"),
            full_page=True,
        )
        return sandbox_id(active)
    finally:
        page.close()


def scenario_core(context) -> str | None:
    page, active = open_demo(context)
    try:
        print("\n== 场景 B：核心业务暂停并一键决策 ==")
        draw_on_card(page, active)
        popover = active.locator(".canvas-popover")
        popover.locator(".popover-row input").fill(CORE_PROMPT)
        popover.locator(".popover-send").click()

        expect(
            active.locator(".runtime-overlay").get_by_text("等待你确认")
        ).to_be_visible(timeout=60_000)
        active.get_by_role("button", name="去画布回答").click()
        expect(popover.locator(".popover-clarification")).to_be_visible(timeout=10_000)
        quick = popover.locator("[data-clarification-quick-option]")
        if quick.count() != 3:
            fail(f"核心决策应提供 3 个一键选项，实际 {quick.count()}")
        recommended = popover.locator(
            '[data-clarification-quick-option][data-recommended="true"]'
        )
        if recommended.count() != 1:
            fail("核心决策必须且只能有一个推荐项")
        if "后端密码校验接口" not in recommended.first.inner_text():
            fail("密码校验场景应推荐补齐后端接口")

        page.screenshot(
            path=str(SCREENSHOT_DIR / "tiered-decision-core.png"),
            full_page=True,
        )
        # 只点一次推荐项，不点“发送并继续”；前端应自动提交并恢复 Run。
        recommended.first.click()
        expect(active.locator(".runtime-result-overlay .status-pill")).to_contain_text(
            "已完成", timeout=240_000
        )
        print("   OK：点击推荐项后自动恢复并完成")
        return sandbox_id(active)
    finally:
        page.close()


def main() -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            # 两个场景各自使用全新 context，避免第一个场景保存的工作区状态
            # 让第二个场景跳过“一键体验”入口。
            for scenario in (scenario_detail, scenario_core):
                context = browser.new_context(viewport={"width": 1600, "height": 1000})
                context.add_init_script(
                    "window.localStorage.setItem('vibe.llmProvider', 'mock')"
                )
                try:
                    value = scenario(context)
                finally:
                    context.close()
                cleanup_sandbox(value)
            print("\nTIERED DECISION E2E: PASS")
            return 0
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
