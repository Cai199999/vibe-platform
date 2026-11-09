#!/usr/bin/env python3
"""浏览器级 CustomAgent 秒级接入 E2E。

验证用户视角的完整演示链路：

  1. 工作台出现“我的专属 Agent”入口，数量来自 AgentRegistry；
  2. 展开后能看到 config/custom_agents/excel_report_agent.json 声明的
     “Excel 报表专家”、能力与关键词；
  3. 在画布输入框键入“帮我导出一份本月的销售数据”并发送；
  4. Supervisor 只调用 /route，返回 handoff_custom 并命中
     excel_report_agent；前端展示专属 Agent 卡片；
  5. 不调用 /dispatch、不创建沙盒，证明 M2.5 只做路由不做执行。

前置：
    docker compose up -d
    /tmp/vibe-browser/bin/playwright install chromium
    /tmp/vibe-browser/bin/python scripts/custom_agent_e2e_test.py
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
WEB_URL = "http://127.0.0.1:5173/"
AI_URL = "http://127.0.0.1:8100"
CUSTOM_AGENT_ID = "excel_report_agent"
CUSTOM_MESSAGE = "帮我导出一份本月的销售数据"


def draw_around(page, locator) -> None:
    box = locator.bounding_box()
    if not box:
        raise AssertionError("找不到圈选目标")
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


def destroy_sandbox(sandbox_id: str) -> None:
    request = urllib.request.Request(
        f"{AI_URL}/api/v1/internal/sandbox/{sandbox_id}",
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            print(f"   cleanup {sandbox_id} -> HTTP {response.status}")
    except Exception as exc:  # noqa: BLE001 - 清理失败不掩盖主流程
        print(f"   cleanup {sandbox_id} -> {exc}")


def main() -> int:
    sandbox_id = ""
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1600, "height": 1000})
            page = context.new_page()
            posts: list[str] = []

            def on_request(request) -> None:
                if request.method == "POST" and "/api/v1/supervisor/" in request.url:
                    posts.append(request.url)

            page.on("request", on_request)

            print("== 1. 登录并确认“我的专属 Agent”目录 ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            expect(page.locator(".workspace-tab")).to_have_count(1)
            active = page.locator(".tab-window.is-active")

            # Agent 目录已折叠进工具栏「更多」菜单。
            active.locator("[data-toolbar-more]").click()
            trigger = active.locator("[data-agent-catalog] > button")
            expect(trigger).to_be_visible(timeout=30_000)
            expect(active.locator("[data-custom-agent-count]")).to_have_text("1")
            trigger.click()

            card = active.locator(f'[data-custom-agent-id="{CUSTOM_AGENT_ID}"]')
            expect(card).to_be_visible()
            expect(card).to_contain_text("Excel 报表专家")
            expect(card).to_contain_text("excel_report_generation")
            expect(card).to_contain_text("销售数据")
            print("   Manifest Agent 已加载：Excel 报表专家 / excel_report_generation")

            page.keyboard.press("Escape")

            print("\n== 2. 画布输入“本月销售数据”并发送 ==")
            draw_around(page, active.locator(".mock-cta"))
            popover = active.locator(".canvas-popover")
            expect(popover).to_have_count(1)
            popover.locator("input").fill(CUSTOM_MESSAGE, force=True)
            expect(active.locator(".selection-state")).to_contain_text("1 个标注")
            popover.get_by_role("button", name="发送").click()

            # 先看到“正在分析意图”，再看到判定结果：证明不是只显示 Loading。
            expect(
                active.locator('[data-supervisor-phase="analyzing"]')
            ).to_be_visible()

            print("\n== 3. Supervisor 命中 Manifest 关键词并路由 ==")
            trace = active.locator("[data-supervisor-trace]")
            expect(trace).to_be_visible(timeout=30_000)
            expect(trace).to_have_attribute("data-supervisor-handoff", "handoff_custom")
            expect(trace).to_contain_text("判定为我的专属 Agent")
            expect(trace.locator("[data-custom-route-card]")).to_contain_text(
                "Excel 报表专家"
            )
            expect(trace.locator("[data-custom-route-card]")).to_contain_text(
                "已成功完成专属 Agent 路由"
            )
            expect(trace).to_contain_text("销售数据")

            # 只路由不执行：不能调用 dispatch，不能创建沙盒。
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not any(
                url.endswith("/api/v1/supervisor/route") for url in posts
            ):
                page.wait_for_timeout(150)
            assert any(
                url.endswith("/api/v1/supervisor/route") for url in posts
            ), f"未调用 Supervisor route: {posts}"
            assert not any(
                url.endswith("/api/v1/supervisor/dispatch") for url in posts
            ), f"CustomAgent 不应被 dispatch: {posts}"
            expect(active.locator(".runtime-panel")).to_have_attribute(
                "data-sandbox-id", ""
            )

            page.screenshot(
                path=str(SCREENSHOT_DIR / "custom-agent-routed.png"),
                full_page=True,
            )
            print("   route=handoff_custom / dispatch=0 / sandbox=0")

            context.close()
            browser.close()
        print("\nCUSTOM AGENT E2E: PASS")
        return 0
    finally:
        if sandbox_id:
            destroy_sandbox(sandbox_id)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"\nCUSTOM AGENT E2E: FAIL - {exc}")
        raise SystemExit(1) from exc
