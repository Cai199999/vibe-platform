#!/usr/bin/env python3
"""E2E：开发者友好的 HITL 澄清交互。

覆盖：
  1. Agent 发起澄清后，主输入栏仍然可见且可编辑；
  2. 点击预设选项不会关闭主输入栏，也不会清空已输入文本；
  3. 点击“自定义”后平滑展开 textarea，可输入多行补充说明；
  4. 提交时把主输入栏 / 自定义说明一起发给后端，Run 正常恢复并完成。

运行：
    /tmp/vibe-browser/bin/python scripts/clarification_ui_e2e_test.py
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
        # 只有核心业务决策才允许中断：密码校验放在哪里会影响接口契约。
        context.add_init_script(
            "window.localStorage.setItem('vibe.llmProvider', 'mock')"
        )
        page = context.new_page()
        sandbox_id: str | None = None

        try:
            print("== 1. 登录并进入演示项目 ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            expect(page.locator(".workspace-tab")).to_have_count(1)
            active = page.locator(".tab-window.is-active")
            expect(active.locator(".mock-card")).to_be_visible()

            print("\n== 2. 在卡片空白区域画圈并发送核心业务指令 ==")
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
            expect(popover).to_have_count(1)
            popover.locator(".popover-row input").fill(
                "加个登录框，密码校验放在前端还是对接已有认证服务"
            )
            popover.locator(".popover-send").click()

            print("\n== 3. 等待澄清，并验证主输入栏仍可用 ==")
            expect(
                active.locator(".runtime-overlay").get_by_text("等待你确认")
            ).to_be_visible(timeout=60_000)
            active.get_by_role("button", name="去画布回答").click()
            expect(popover.locator(".popover-clarification")).to_be_visible(
                timeout=10_000
            )

            main_input = popover.locator(".popover-row input")
            if not main_input.is_enabled():
                fail("澄清期间主输入栏被禁用")
            main_input.fill("补充：整体风格保持现有设计")
            if main_input.input_value() != "补充：整体风格保持现有设计":
                fail("主输入栏文本未保留")

            print("\n== 4. 验证一键决策按钮，再点击预设选项 ==")
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
            preset = popover.locator(".clarification-option").first
            preset.click()
            if not main_input.is_enabled():
                fail("点击预设选项后主输入栏被禁用")
            if main_input.input_value() != "补充：整体风格保持现有设计":
                fail("点击预设选项后主输入栏文本被清空")

            print("\n== 5. 展开自定义 textarea 并输入多行说明 ==")
            custom_option = popover.locator(".clarification-option.is-custom")
            if custom_option.count() == 0:
                fail("没有找到“自定义”选项")
            custom_option.click()
            # 单选自定义与自由文本题会同时展开，这里定位自定义所在的问题块。
            expand = popover.locator(".clarification-expand.is-open").first
            expect(expand).to_be_visible()
            textarea = expand.locator("textarea")
            textarea.fill("改成深蓝色圆角按钮\n文案用“立即开始”")
            if main_input.input_value() != "补充：整体风格保持现有设计":
                fail("进入自定义模式后主输入栏文本丢失")

            screenshot = SCREENSHOT_DIR / "clarification-ui.png"
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot))
            print(f"   screenshot -> {screenshot}")

            print("\n== 6. 发送并等待 Run 完成 ==")
            popover.locator(".clarification-submit").click()
            expect(
                active.locator(".runtime-result-overlay .status-pill")
            ).to_contain_text("已完成", timeout=240_000)
            sandbox_id = active.locator(".runtime-panel").get_attribute(
                "data-sandbox-id"
            )
            print(f"   状态: 已完成 / 沙盒: {sandbox_id}")

            print("\nCLARIFICATION UI E2E: PASS")
            return 0
        finally:
            cleanup_sandbox(sandbox_id)
            context.close()
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
