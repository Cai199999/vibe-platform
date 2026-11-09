#!/usr/bin/env python3
"""M3.1 管理中台入口、看板渲染与前端路由守卫 E2E。

覆盖：
  1. 管理员登录后，右上角账户菜单出现「管理中台（Admin Console）」；
  2. 点击进入 /admin，4 个指标卡、Agent 占比图、最近执行表正常渲染；
  3. 非管理员账户看不到入口，直接输入 /admin 被整页 403 拦截，
     且不会向 /api/v1/admin/metrics 发起请求；
  4. 未登录直接访问 /admin 会跳转 /login。

前置：
    # 后端（允许使用演示指标）
    ADMIN_METRICS_DEMO_SEED=true SERVER_PORT=18080 ... mvn spring-boot:run
    # 前端
    VITE_BFF_PROXY_TARGET=http://127.0.0.1:18080 npm run dev -- --port 5174

运行：
    ADMIN_WEB_URL=http://127.0.0.1:5174/ \
      /tmp/vibe-browser/bin/python scripts/admin_console_e2e_test.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
WEB_URL = os.environ.get("ADMIN_WEB_URL", "http://127.0.0.1:5173/").rstrip("/")


def fail(message: str) -> None:
    print(f"\nFAIL: {message}")
    raise SystemExit(1)


def login(page, account: str, *, admin: bool) -> None:
    page.goto(f"{WEB_URL}/login", wait_until="networkidle")
    page.locator("#login-account").fill(account)
    page.locator("#login-password").fill("vibe-demo")
    checkbox = page.get_by_role("checkbox")
    if admin and not checkbox.is_checked():
        checkbox.check()
    page.get_by_role("button", name="登录并进入工作台").click()
    page.wait_for_url("**/workspace", timeout=30_000)
    print(f"   已登录: {account} (admin={admin})")


def main() -> int:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        # ----------------------------------------------------------
        # 1. 管理员入口 + 看板
        # ----------------------------------------------------------
        context = browser.new_context(
            viewport={"width": 1440, "height": 1080},
            device_scale_factor=2,
        )
        page = context.new_page()
        login(page, "admin@vibe.local", admin=True)

        print("\n== 1. 账户菜单展示管理中台入口 ==")
        page.locator(".avatar-button").click()
        admin_entry = page.get_by_role("menuitem", name="管理中台（Admin Console）")
        expect(admin_entry).to_be_visible(timeout=10_000)
        print("   管理中台入口可见")
        admin_entry.click()

        print("\n== 2. /admin 看板渲染 ==")
        page.wait_for_url("**/admin", timeout=15_000)
        expect(page.locator("#admin-title")).to_have_text("系统概览")
        kpis = page.locator(".admin-kpi")
        expect(kpis).to_have_count(4, timeout=20_000)
        expect(kpis.nth(0).locator(".admin-kpi-value")).to_have_text("3")
        expect(kpis.nth(1).locator(".admin-kpi-value")).to_have_text("18")
        expect(kpis.nth(2).locator(".admin-kpi-value")).to_have_text("93.3%")
        expect(kpis.nth(3).locator(".admin-kpi-value")).not_to_have_text("0 ms")

        chart_rows = page.locator(".admin-agent-row")
        expect(chart_rows).to_have_count(2, timeout=20_000)
        expect(chart_rows.nth(0).locator(".admin-agent-names strong")).to_have_text(
            "局部 UI 工程师"
        )
        expect(chart_rows.nth(1).locator(".admin-agent-names strong")).to_have_text(
            "全局架构师"
        )

        table_rows = page.locator(".admin-table tbody tr")
        expect(table_rows).to_have_count(10, timeout=20_000)
        expect(page.locator(".admin-status").first).to_be_visible()

        page.screenshot(
            path=str(SCREENSHOT_DIR / "m3-1-admin-console.png"),
            full_page=True,
        )
        print(f"   截图: {SCREENSHOT_DIR / 'm3-1-admin-console.png'}")

        # ----------------------------------------------------------
        # 2. 非管理员：无入口 + 直连 URL 被守卫拦截
        # ----------------------------------------------------------
        print("\n== 3. 非管理员访问控制 ==")
        member_context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            device_scale_factor=1,
        )
        member_page = member_context.new_page()
        admin_requests: list[str] = []
        member_page.on(
            "request",
            lambda request: (
                admin_requests.append(request.url)
                if "/api/v1/admin/metrics" in request.url
                else None
            ),
        )
        login(member_page, "member@vibe.local", admin=False)

        member_page.locator(".avatar-button").click()
        expect(
            member_page.get_by_role("menuitem", name="管理中台（Admin Console）")
        ).to_have_count(0)
        print("   非管理员菜单中没有管理中台入口")
        member_page.keyboard.press("Escape")

        member_page.goto(f"{WEB_URL}/admin", wait_until="networkidle")
        expect(member_page.locator(".admin-forbidden-card")).to_be_visible(
            timeout=10_000
        )
        expect(member_page.locator(".admin-kpi")).to_have_count(0)
        expect(member_page.get_by_text("没有管理中台权限")).to_be_visible()
        if admin_requests:
            fail(f"非管理员页面不应请求管理指标: {admin_requests}")
        print("   /admin 被前端守卫拦截，且未发起管理 API 请求")
        member_page.screenshot(
            path=str(SCREENSHOT_DIR / "m3-1-admin-forbidden.png"),
            full_page=True,
        )

        member_page.get_by_role("button", name="切换管理员账号").click()
        expect(member_page).to_have_url(re.compile(r"/login$"))
        print("   切换账号会先清理会话并回到登录页")
        member_context.close()

        # ----------------------------------------------------------
        # 3. 未登录直连 /admin -> /login
        # ----------------------------------------------------------
        print("\n== 4. 未登录直连 /admin ==")
        anon_context = browser.new_context()
        anon_page = anon_context.new_page()
        anon_page.goto(f"{WEB_URL}/admin", wait_until="networkidle")
        expect(anon_page).to_have_url(re.compile(r"/login$"))
        print("   未登录被重定向到 /login")
        anon_context.close()

        context.close()
        browser.close()

    print("\nADMIN CONSOLE E2E: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
