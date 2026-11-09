#!/usr/bin/env python3
"""浏览器级 E2E：契约确认 -> 点击生成全栈代码 -> 沙盒真实联调。

覆盖：
  1. Architect 面板在 CONFIRMED 后展示【🚀 开始生成全栈代码】；
  2. 点击后 SSE 六节点（含 InfraCoder）依次完成；
  3. 沙盒重启后预览 iframe 中数据面板进入 ready；
  4. 面板通过 Vite /api 代理对 Node 后端执行 GET / POST，不再出现网络错误；
  5. 预览区【部署上线】按钮展示生产部署配置指引（Mock）。

前置：
    docker compose up -d
    /tmp/vibe-browser/bin/python scripts/fullstack_generation_ui_e2e_test.py
"""

from __future__ import annotations

import re
import sys
import urllib.request
import uuid
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
WEB_URL = "http://127.0.0.1:5173/"
BFF_URL = "http://127.0.0.1:8080"


def cleanup_sandbox(sandbox_id: str | None) -> None:
    if not sandbox_id:
        return
    request = urllib.request.Request(
        f"{BFF_URL}/api/v1/sandboxes/{sandbox_id}",
        method="DELETE",
    )
    try:
        urllib.request.urlopen(request, timeout=20).close()
    except Exception:  # noqa: BLE001 - 清理失败不影响测试结论
        print(f"WARN: 清理沙盒失败 {sandbox_id}")


def fill_clarification(panel) -> None:
    """填写当前一轮澄清表单：形态选全栈，其余选择/文本给默认值。"""
    form = panel.locator(".architect-clarification")
    expect(form).to_be_visible(timeout=60_000)

    shape_choices = form.locator(".shape-choice")
    if shape_choices.count() > 0:
        fullstack = form.get_by_role("button", name=re.compile("全栈系统"))
        expect(fullstack).to_be_visible()
        fullstack.click()

    groups = form.locator("fieldset.architect-question")
    for index in range(groups.count()):
        group = groups.nth(index)
        pills = group.locator(".choice-pills button")
        if pills.count() == 0:
            continue
        selected = group.locator(".choice-pills button.is-selected")
        if selected.count() == 0:
            pills.first.click()

    areas = form.locator("textarea")
    for index in range(areas.count()):
        area = areas.nth(index)
        # 最后一个 textarea 是“补充说明”，可以留空。
        is_comment = area.evaluate("el => !!el.closest('.architect-comment')")
        if is_comment:
            continue
        area.fill(
            "点击提交后创建待办并刷新列表；"
            "数据实体 Todo(id, tenantId, name, status, createdAt)"
        )

    form.get_by_role("button", name="提交回答并继续").click()


def drive_to_confirmed_contract(page, active) -> None:
    panel = page.locator(".architect-panel")
    expect(panel).to_be_visible(timeout=30_000)
    contract = panel.locator(".contract-viewer")

    for round_index in range(8):
        if contract.count() > 0 and contract.first.is_visible():
            break
        print(f"   澄清第 {round_index + 1} 轮")
        fill_clarification(panel)
        page.wait_for_timeout(1200)

    expect(contract.first).to_be_visible(timeout=60_000)
    expect(panel.locator(".architect-status")).to_contain_text("等待确认")
    panel.get_by_role("button", name="确认契约，进入生成").click()
    expect(panel.locator(".architect-status")).to_contain_text("已确认", timeout=60_000)
    expect(panel.locator(".architect-outcome.is-success")).to_be_visible(timeout=30_000)


def main() -> int:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    sandbox_id = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()
        try:
            print("== 1. 登录并选择 Mock 模型 ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            expect(page.locator(".workspace-tab")).to_have_count(1)
            active = page.locator(".tab-window.is-active")
            active.locator(".model-picker select").select_option("mock")

            print("\n== 2. 发起全局架构澄清并推进到 CONFIRMED ==")
            active.get_by_role(
                "button", name=re.compile("下一步：构建全栈方案")
            ).click()
            drive_to_confirmed_contract(page, active)

            panel = page.locator(".architect-panel")
            trigger = panel.locator("[data-fullstack-generate]")
            expect(trigger).to_be_visible(timeout=30_000)
            expect(trigger).to_be_enabled()
            expect(trigger).to_contain_text("开始生成全栈代码")

            print("\n== 3. 点击【开始生成全栈代码】 ==")
            trigger.click()
            progress = panel.locator("[data-fullstack-progress]")
            expect(progress).to_be_visible(timeout=30_000)
            expect(progress.locator("[data-fullstack-step]")).to_have_count(6)

            # 只断言终态；运行中节点状态交替太快，不适合逐帧断言。
            expect(panel.locator("[data-fullstack-status]")).to_have_attribute(
                "data-fullstack-status", "SUCCEEDED", timeout=240_000
            )
            for node in (
                "DBACoder",
                "BackendCoder",
                "FrontendCoder",
                "InfraCoder",
            ):
                expect(
                    panel.locator(f'[data-fullstack-step="{node}"]')
                ).to_have_attribute("data-status", "SUCCEEDED")
            for path in (
                "server/index.js",
                "server/db.js",
                "server/schema.sql",
                "Dockerfile",
                "render.yaml",
                ".env.example",
            ):
                expect(
                    panel.locator(f'[data-fullstack-output="{path}"]')
                ).to_have_count(1)
            expect(panel.locator("[data-fullstack-result]")).to_contain_text("接口前缀")
            page.screenshot(
                path=str(SCREENSHOT_DIR / "fullstack-generation-completed.png"),
                full_page=True,
            )
            print("   OK：六个节点完成，ORM 与部署配置日志可见")

            print("\n== 4. 关闭面板，检查预览区真实数据面板与部署指引 ==")
            panel.get_by_role("button", name="关闭架构师面板").click()
            expect(panel).to_be_hidden()
            expect(active.locator(".preview-pane.is-visible")).to_be_visible(
                timeout=30_000
            )

            deploy_button = active.locator("[data-deploy-button]")
            expect(deploy_button).to_be_visible()
            deploy_button.click()
            deploy_dialog = active.locator("[data-deploy-dialog]")
            expect(deploy_dialog).to_be_visible(timeout=10_000)
            for keyword in ("Dockerfile", "render.yaml", "Vercel", "Railway"):
                expect(deploy_dialog).to_contain_text(keyword)
            page.screenshot(
                path=str(SCREENSHOT_DIR / "fullstack-deploy-guide.png"),
                full_page=True,
            )
            deploy_dialog.get_by_role("button", name="关闭部署指引").click()
            expect(deploy_dialog).to_be_hidden()
            print(
                "   OK：部署上线 Mock 指引展示 Dockerfile / render.yaml / Vercel / Railway"
            )
            frame = page.frame_locator(".tab-window.is-active .runtime-preview-frame")
            panel_root = frame.locator("[data-fullstack-panel]")
            expect(panel_root).to_be_visible(timeout=90_000)
            expect(panel_root).to_have_attribute("data-fullstack-status", "ready")
            expect(panel_root.locator("[data-fullstack-error]")).to_have_count(0)
            expect(panel_root.locator("[data-fullstack-row]").first).to_be_visible()

            print("\n== 5. 通过预览 UI 调用真实 POST 接口 ==")
            name = f"UI E2E {uuid.uuid4().hex[:6]}"
            panel_root.locator("[data-fullstack-input]").fill(name)
            # 预览 iframe 被 ScaledPreview 缩放，Playwright 的 outer-page
            # 命中测试会误判“outside viewport”；用 DOM click 触发同样的
            # submit 事件，保持浏览器内验证语义不变。
            panel_root.locator("[data-fullstack-submit]").evaluate("el => el.click()")
            expect(
                panel_root.locator("[data-fullstack-row]").filter(has_text=name)
            ).to_be_visible(timeout=30_000)
            page.screenshot(
                path=str(SCREENSHOT_DIR / "fullstack-preview-live-api.png"),
                full_page=True,
            )
            print(f"   OK：POST 成功并出现在列表中：{name}")

            sandbox_id = active.locator(".runtime-panel").get_attribute(
                "data-sandbox-id"
            )
            print(f"   sandbox={sandbox_id}")
        finally:
            cleanup_sandbox(sandbox_id)
            context.close()
            browser.close()

    print("\nFULLSTACK UI E2E: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
