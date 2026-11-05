#!/usr/bin/env python3
"""设置面板与「代码生成后需人工审批」开关 E2E。

覆盖：
  1. 右上角账户菜单包含「账户」与「设置」；
  2. 设置弹窗开关默认关闭，并写入 localStorage；
  3. 关闭时 `/supervisor/route` 的 options 携带 requireManualApproval=false，
     构建验证通过后直接 SUCCEEDED，不出现审批暂停；
  4. 打开开关后同一字段变为 true，构建通过后暂停在审批面板；
  5. 批准后正常完成。

运行：
    docker compose up -d
    /tmp/vibe-browser/bin/python scripts/settings_manual_approval_e2e_test.py
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
WEB_URL = "http://127.0.0.1:5173/"
PY_AI_URL = "http://127.0.0.1:8100"


def fail(message: str) -> None:
    print(f"\nFAIL: {message}")
    raise SystemExit(1)


def cleanup_sandbox(sandbox_id: str) -> None:
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


def draw_around(page, locator) -> None:
    box = locator.bounding_box()
    if not box:
        fail("找不到圈选目标")
    page.mouse.move(box["x"] - 30, box["y"] - 20)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=8)
    page.mouse.move(
        box["x"] + box["width"] + 30, box["y"] + box["height"] + 20, steps=8
    )
    page.mouse.up()
    page.wait_for_timeout(280)


def open_settings(page):
    """通过头像菜单打开设置弹窗，同时校验菜单项名称。"""
    page.locator(".avatar-button").click()
    expect(page.get_by_role("menuitem", name="账户", exact=True)).to_be_visible()
    page.get_by_role("menuitem", name="设置", exact=True).click()
    dialog = page.locator("[data-settings-dialog]")
    expect(dialog).to_be_visible()
    return dialog


def main() -> int:
    sandbox_id = ""
    requests: list[dict] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1600, "height": 1000})
            page = context.new_page()

            def on_request(request) -> None:
                if request.method == "POST" and request.url.endswith(
                    "/api/v1/supervisor/route"
                ):
                    try:
                        requests.append(json.loads(request.post_data or "{}"))
                    except json.JSONDecodeError:
                        requests.append({})

            page.on("request", on_request)

            print("== 1. 登录并选择 Mock 模型 ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            expect(page.locator(".workspace-tab")).to_have_count(1)
            active = page.locator(".tab-window.is-active")
            active.locator(".model-picker select").select_option("mock")

            print("\n== 2. 账户菜单：账户 + 设置 ==")
            page.locator(".avatar-button").click()
            expect(
                page.get_by_role("menuitem", name="账户", exact=True)
            ).to_be_visible()
            expect(
                page.get_by_role("menuitem", name="设置", exact=True)
            ).to_be_visible()
            page.get_by_role("menuitem", name="设置", exact=True).click()
            dialog = page.locator("[data-settings-dialog]")
            expect(dialog).to_be_visible()

            switch = dialog.get_by_role("switch", name="代码生成后需人工审批")
            expect(switch).to_have_attribute("aria-checked", "false")
            expect(dialog).to_contain_text("已关闭")
            dialog.get_by_role("button", name="完成").click()
            expect(dialog).to_be_hidden()
            print("   默认关闭")

            print("\n== 3. 关闭开关：验证通过后直接成功 ==")
            draw_around(page, active.locator(".mock-cta"))
            popover = active.locator(".canvas-popover")
            expect(popover).to_have_count(1)
            popover.locator("input").fill("改成蓝色", force=True)
            popover.locator(".popover-send").click(force=True)

            expect(
                active.locator(".runtime-result-overlay .status-pill")
            ).to_contain_text("已完成", timeout=180_000)
            expect(active.locator(".loading-state.is-approval")).to_have_count(0)
            expect(active.locator(".view-result-cta.is-pending")).to_have_count(0)
            sandbox_id = (
                active.locator(".runtime-panel").get_attribute("data-sandbox-id") or ""
            )
            if not requests:
                fail("未捕获 /runs/stream 请求")
            first_options = requests[-1].get("options") or {}
            first_ui = first_options.get("ui") or {}
            if first_ui.get("requireManualApproval") is not False:
                fail(f"关闭时请求未携带 false: {first_options}")
            if "approvalMode" in first_ui:
                fail(f"新协议不应再发送 approvalMode: {first_options}")
            print(f"   requireManualApproval=false / sandbox={sandbox_id}")

            print("\n== 4. 设置面板打开审批并持久化 ==")
            dialog = open_settings(page)
            switch = dialog.get_by_role("switch", name="代码生成后需人工审批")
            switch.click()
            expect(switch).to_have_attribute("aria-checked", "true")
            expect(dialog).to_contain_text("已开启人工审批")
            saved = page.evaluate(
                "JSON.parse(localStorage.getItem('vibe.settings.v1') || '{}')"
            )
            if saved.get("requireManualApproval") is not True:
                fail(f"设置未写入 localStorage: {saved}")
            dialog.get_by_role("button", name="完成").click()
            print("   localStorage 已同步为 true")

            print("\n== 5. 打开开关：构建通过后暂停审批 ==")
            # 第一轮完成后已自动切到预览；重新发送需要先回到画布。
            active.get_by_role("button", name="画布", exact=True).click()
            expect(active.locator(".canvas-pane.is-visible")).to_be_visible()
            expect(popover.locator("input")).to_be_enabled(timeout=30_000)
            popover.locator("input").fill("改成绿色", force=True)
            popover.locator(".popover-send").click(force=True)

            # 派发后自动聚焦预览，审批面板直接在预览侧展示。
            failure = active.locator(".loading-state.is-approval")
            expect(failure).to_be_visible(timeout=180_000)
            second_options = requests[-1].get("options") or {}
            second_ui = second_options.get("ui") or {}
            if second_ui.get("requireManualApproval") is not True:
                fail(f"开启时请求未携带 true: {second_options}")
            failure.get_by_role("button", name="批准并应用").click()
            expect(
                active.locator(".runtime-result-overlay .status-pill")
            ).to_contain_text("已完成", timeout=180_000)
            print("   审批暂停 -> 批准 -> 已完成")

            context.close()
            browser.close()
        print("\nSETTINGS MANUAL APPROVAL E2E: PASS")
        return 0
    finally:
        cleanup_sandbox(sandbox_id)


if __name__ == "__main__":
    raise SystemExit(main())
