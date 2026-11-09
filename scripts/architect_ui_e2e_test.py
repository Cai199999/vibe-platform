#!/usr/bin/env python3
"""M2.5 Architect 面板浏览器级 E2E（Mock LLM，无需 DeepSeek Key）。

覆盖：
  1. 工具栏主 CTA 可直接发起全局架构澄清；
  2. 首轮不再提供三选一卡片，自然语言目标直接推断轻量形态；
  3. 轻量工具不出现后端 / 数据库问题，只追问 UI 主流程；
  4. 轻量契约不包含接口、DDL、脚本规格；
  5. 确认契约后状态收敛，关闭再打开仍能恢复已确认结果。

前置：
    docker compose up -d
    /tmp/vibe-browser/bin/python scripts/architect_ui_e2e_test.py
"""

from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
WEB_URL = "http://127.0.0.1:5173/"


def main() -> int:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()

        print("== 1. 登录并选择 Mock 模型 ==")
        page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
        page.get_by_role("button", name="一键体验（推荐）").click()
        expect(page.locator(".workspace-tab")).to_have_count(1)
        active = page.locator(".tab-window.is-active")
        active.locator(".model-picker select").select_option("mock")

        print("\n== 2. 点击「下一步：构建全栈方案」 ==")
        active.get_by_role("button", name=re.compile("下一步：构建全栈方案")).click()
        panel = page.locator(".architect-panel")
        expect(panel).to_be_visible(timeout=30_000)
        expect(panel).to_contain_text("全栈 Path 架构师")

        print("\n== 3. 首轮：自然语言描述目标，不再三选一 ==")
        form = panel.locator(".architect-clarification")
        expect(form).to_be_visible(timeout=60_000)
        expect(form).to_contain_text("补齐实施所需信息")
        submit = form.get_by_role("button", name="提交回答并继续")
        expect(submit).to_be_disabled()
        expect(form.locator(".shape-choice")).to_have_count(0)

        goal = form.locator("textarea").first
        goal.fill("做一个个人待办清单，纯前端保存到 localStorage，不需要登录。")
        expect(submit).to_be_enabled()
        submit.click()
        print("   已用自然语言描述 lightweight_web 并提交")

        print("\n== 4. 第二轮：只追问 UI 主流程 ==")
        form = panel.locator(".architect-clarification")
        expect(form).to_contain_text("补齐实施所需信息", timeout=60_000)
        expect(form).to_contain_text("核心操作流程")
        # 轻量形态不应出现后端 / 数据库槽位。
        expect(form).not_to_contain_text("后端希望使用哪种技术栈")
        expect(form).not_to_contain_text("数据库使用哪一种")
        flow = form.locator("textarea").first
        flow.fill("输入待办 -> 标记完成 -> 保存到 localStorage")
        form.get_by_role("button", name="提交回答并继续").click()
        print("   已提交 UI 主流程")

        print("\n== 5. 契约按轻量形态分级生成 ==")
        contract = panel.locator(".contract-viewer")
        expect(contract).to_be_visible(timeout=60_000)
        expect(contract).to_contain_text("实施契约")
        expect(contract.locator(".contract-tabs")).to_contain_text("前端设计")
        expect(contract.locator(".contract-tabs")).not_to_contain_text("接口 1")
        expect(contract.locator(".contract-tabs")).not_to_contain_text("数据库 DDL")
        expect(contract.locator(".contract-tabs")).not_to_contain_text("脚本规格")
        expect(panel.locator(".shape-badge").first).to_contain_text("轻量网页工具")
        expect(panel.locator(".architect-status")).to_contain_text("等待确认")
        # 置信度必须达到 85% 才会出契约。
        confidence = panel.locator("[role=progressbar][aria-label=架构置信度]")
        expect(confidence).to_have_attribute(
            "aria-valuenow", re.compile(r"^(8[5-9]|9[0-9]|100)$")
        )
        page.screenshot(
            path=str(SCREENSHOT_DIR / "m2-5-architect-contract.png"),
            full_page=True,
        )
        print("   契约已生成：无 API / DDL / 脚本规格")

        print("\n== 6. 确认契约 ==")
        panel.get_by_role("button", name="确认契约，进入生成").click()
        expect(panel.locator(".architect-status")).to_contain_text(
            "已确认", timeout=60_000
        )
        expect(panel.locator(".architect-outcome.is-success")).to_be_visible(
            timeout=30_000
        )
        page.screenshot(
            path=str(SCREENSHOT_DIR / "m2-5-architect-confirmed.png"),
            full_page=True,
        )
        print("   契约已确认")

        print("\n== 7. 关闭面板再打开，恢复已确认契约 ==")
        panel.get_by_role("button", name="关闭架构师面板").click()
        expect(panel).to_be_hidden()
        active.get_by_role("button", name=re.compile("下一步：构建全栈方案")).click()
        expect(panel).to_be_visible(timeout=10_000)
        expect(panel.locator(".architect-status")).to_contain_text("已确认")
        expect(panel.locator(".contract-viewer")).to_be_visible()
        print("   已恢复同一份契约")

        browser.close()
    print("\nARCHITECT UI E2E: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
