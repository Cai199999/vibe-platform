#!/usr/bin/env python3
"""浏览器级 E2E：非全栈契约下【开始生成全栈代码】必须禁用且提示可见。

覆盖用户反馈的“按钮点不动却不知道为什么”：

  1. 走真实产品路径进入 Architect；
  2. 澄清时选择“轻量网页工具”，契约形态不是 full_stack_system；
  3. 契约确认后断言按钮 disabled；
  4. 断言按钮旁出现可见中文提示（data-fullstack-disabled-reason），
     而不是只在 disabled 按钮上挂一个浏览器不展示的 title；
  5. 断言控制台输出了 [fullstack] gate state 诊断。

运行：
    /tmp/vibe-browser/bin/python scripts/fullstack_disabled_hint_ui_e2e_test.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
WEB_URL = "http://127.0.0.1:5173/"


def fail(message: str) -> None:
    print(f"\nFAIL: {message}")
    raise SystemExit(1)


def fill_clarification(panel) -> None:
    """本轮统一选择轻量网页形态，其余问题取默认选项。"""
    form = panel.locator(".architect-clarification")
    expect(form).to_be_visible(timeout=60_000)

    if form.locator(".shape-choice").count() > 0:
        lightweight = form.get_by_role("button", name=re.compile("轻量网页"))
        expect(lightweight).to_be_visible()
        lightweight.click()

    groups = form.locator("fieldset.architect-question")
    for index in range(groups.count()):
        group = groups.nth(index)
        pills = group.locator(".choice-pills button")
        if pills.count() == 0:
            continue
        if group.locator(".choice-pills button.is-selected").count() == 0:
            pills.first.click()

    areas = form.locator("textarea")
    for index in range(areas.count()):
        area = areas.nth(index)
        if area.evaluate("el => !!el.closest('.architect-comment')"):
            continue
        area.fill("纯前端表单，数据只保存在浏览器 LocalStorage，不需要后端与数据库")

    form.get_by_role("button", name="提交回答并继续").click()


def main() -> int:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        context.add_init_script(
            "window.localStorage.setItem('vibe.llmProvider', 'mock')"
        )
        page = context.new_page()
        console_logs: list[str] = []
        page.on(
            "console",
            lambda message: console_logs.append(f"{message.type}: {message.text}"),
        )
        try:
            print("== 1. 进入演示项目并打开 Architect ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            active = page.locator(".tab-window.is-active")
            expect(active.locator(".mock-card")).to_be_visible()
            active.get_by_role(
                "button", name=re.compile("下一步：构建全栈方案")
            ).click()

            panel = page.locator(".architect-panel")
            expect(panel).to_be_visible(timeout=30_000)
            contract = panel.locator(".contract-viewer")
            for round_index in range(8):
                if contract.count() > 0 and contract.first.is_visible():
                    break
                print(f"   澄清第 {round_index + 1} 轮（选择轻量网页）")
                fill_clarification(panel)
                page.wait_for_timeout(1200)

            print("\n== 2. 确认轻量网页契约 ==")
            expect(contract.first).to_be_visible(timeout=60_000)
            expect(panel.locator(".architect-status")).to_contain_text("等待确认")
            panel.get_by_role("button", name="确认契约，进入生成").click()
            expect(panel.locator(".architect-status")).to_contain_text(
                "已确认", timeout=60_000
            )

            print("\n== 3. 断言按钮禁用 + 可见提示 + 控制台诊断 ==")
            trigger = panel.locator("[data-fullstack-generate]")
            expect(trigger).to_be_visible(timeout=30_000)
            expect(trigger).to_be_disabled()
            hint = panel.locator("[data-fullstack-disabled-reason]")
            expect(hint).to_be_visible(timeout=10_000)
            hint_text = hint.inner_text()
            wrapper = panel.locator(".architect-fullstack-trigger-wrap")
            tooltip = wrapper.get_attribute("data-tooltip")
            print(f"   disabled=true")
            print(f"   可见提示={hint_text!r}")
            print(f"   tooltip={tooltip!r}")
            if "无需生成后端代码" not in hint_text:
                fail(f"禁用提示未解释契约形态原因：{hint_text!r}")
            if not tooltip:
                fail("包裹层缺少 data-tooltip")
            if not any("[fullstack] gate state" in line for line in console_logs):
                fail("控制台没有 [fullstack] gate state 诊断日志")
            gate_logs = [
                line for line in console_logs if "[fullstack] gate state" in line
            ]
            # Playwright 的 console.text() 只保留对象的前若干个字段，
            # 因此只断言 appShape 的值出现，而不是字段名。
            if not any("lightweight_web" in line for line in gate_logs):
                fail(f"gate state 日志缺少 appShape 值：{gate_logs[-3:]}")

            page.screenshot(
                path=str(SCREENSHOT_DIR / "fullstack-disabled-hint.png"),
                full_page=True,
            )
            print("\nFULLSTACK DISABLED HINT UI E2E: PASS")
            return 0
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
