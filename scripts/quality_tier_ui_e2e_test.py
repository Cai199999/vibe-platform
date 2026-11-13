#!/usr/bin/env python3
"""浏览器级 E2E：风险自适应门禁（L1 / L2 / L3）三种质量分级。

验收要点：
  1. 契约确认前必须先选择质量分级，未选择时【确认契约，进入生成】保持禁用；
  2. L1_PROTOTYPE：生成成功并展示黄色告警徽标，跳过真实 MySQL 迁移验收；
  3. L2_STANDARD：生成成功，不展示任何质量徽标，也不执行真实 MySQL 验收；
  4. L3_PRODUCTION：生成成功，展示绿色通过徽标，并展示真实 MySQL
     迁移验收结果（information_schema 校验通过）。

前置：
    docker compose up -d
    /tmp/vibe-e2e/bin/python scripts/quality_tier_ui_e2e_test.py
"""

from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
WEB_URL = "http://127.0.0.1:5173/"
BFF_URL = "http://127.0.0.1:8080"

L1_WARNING_TEXT = (
    "⚠️ 当前为 L1 敏捷模式，代码未经过严格一致性校验，" "不建议直接用于生产环境。"
)
L3_PASSED_TEXT = "✅ L3 生产级校验通过，具备部署条件。"


def cleanup_sandbox(sandbox_id: str | None) -> None:
    """删除本轮生成的沙盒，避免占用资源。"""
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
            "点击提交后创建待办并刷新列表，支持团队协作与登录，"
            "数据保存到数据库；"
            "数据实体 Todo(id, tenantId, name, status, createdAt)"
        )

    form.get_by_role("button", name="提交回答并继续").click()


def drive_to_contract(page: Page, active) -> None:
    """推进多轮澄清，直到契约面板出现（仍处于等待确认状态）。"""
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


def confirm_contract_with_tier(page: Page, tier: str) -> None:
    """验证“质量分级不可跳过”，选择后确认契约。"""
    panel = page.locator(".architect-panel")
    tier_field = panel.locator("[data-quality-tier]")
    expect(tier_field).to_be_visible(timeout=30_000)

    confirm = panel.get_by_role("button", name="确认契约，进入生成")
    # 未选择分级前：确认按钮禁用 + 必选提示可见。
    expect(panel.locator("[data-quality-tier-required]")).to_be_visible()
    expect(confirm).to_be_disabled()

    radio = tier_field.locator(f'input[type="radio"][value="{tier}"]')
    expect(radio).to_have_count(1)
    radio.check()
    expect(radio).to_be_checked()
    expect(panel.locator("[data-quality-tier-required]")).to_have_count(0)
    expect(confirm).to_be_enabled()

    confirm.click()
    expect(panel.locator(".architect-status")).to_contain_text("已确认", timeout=60_000)
    expect(panel.locator(".architect-outcome.is-success")).to_be_visible(timeout=30_000)


def run_tier(page: Page, tier: str, screenshot: str) -> str | None:
    """在全新会话中跑完一轮：登录 -> 澄清 -> 选分级 -> 生成。"""
    panel = page.locator(".architect-panel")
    sandbox_id: str | None = None
    try:
        page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
        page.get_by_role("button", name="一键体验（推荐）").click()
        expect(page.locator(".workspace-tab")).to_have_count(1)
        active = page.locator(".tab-window.is-active")
        active.locator(".model-picker select").select_option("mock")

        active.get_by_role("button", name=re.compile("下一步：构建全栈方案")).click()
        drive_to_contract(page, active)
        confirm_contract_with_tier(page, tier)

        trigger = panel.locator("[data-fullstack-generate]")
        expect(trigger).to_be_visible(timeout=30_000)
        expect(trigger).to_be_enabled()
        trigger.click()

        expect(panel.locator("[data-fullstack-status]")).to_have_attribute(
            "data-fullstack-status", "SUCCEEDED", timeout=300_000
        )
        page.screenshot(path=str(SCREENSHOT_DIR / screenshot), full_page=True)
        sandbox_id = active.locator(".runtime-panel").get_attribute("data-sandbox-id")
        return sandbox_id
    finally:
        cleanup_sandbox(sandbox_id)


def main() -> int:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for tier, screenshot in (
                ("L1_PROTOTYPE", "quality-tier-l1-prototype.png"),
                ("L2_STANDARD", "quality-tier-l2-standard.png"),
                ("L3_PRODUCTION", "quality-tier-l3-production.png"),
            ):
                print(f"\n===== 质量分级 {tier} =====")
                context = browser.new_context(viewport={"width": 1600, "height": 1000})
                page = context.new_page()
                try:
                    run_tier(page, tier, screenshot)
                    panel = page.locator(".architect-panel")
                    mysql_hint = panel.locator("[data-quality-mysql]")

                    if tier == "L1_PROTOTYPE":
                        badge = panel.locator('[data-quality-badge="warning"]')
                        expect(badge).to_be_visible()
                        expect(badge).to_contain_text(L1_WARNING_TEXT)
                        expect(mysql_hint).to_have_count(0)
                        print("   OK：L1 黄色告警徽标，未执行真实 MySQL 验收")
                    elif tier == "L2_STANDARD":
                        expect(panel.locator("[data-quality-badge]")).to_have_count(0)
                        expect(mysql_hint).to_have_count(0)
                        print("   OK：L2 无徽标，未执行真实 MySQL 验收")
                    else:
                        badge = panel.locator('[data-quality-badge="success"]')
                        expect(badge).to_be_visible()
                        expect(badge).to_contain_text(L3_PASSED_TEXT)
                        expect(
                            panel.locator('[data-quality-mysql="passed"]')
                        ).to_be_visible(timeout=30_000)
                        print("   OK：L3 绿色通过徽标，真实 MySQL 迁移验收通过")
                except AssertionError as exc:  # noqa: PERF203
                    failures.append(f"{tier}: {exc}")
                    page.screenshot(
                        path=str(SCREENSHOT_DIR / f"{tier.lower()}-failed.png"),
                        full_page=True,
                    )
                    print(f"   FAIL：{exc}")
                finally:
                    context.close()
        finally:
            browser.close()

    if failures:
        print("\nQUALITY TIER UI E2E: FAIL")
        for failure in failures:
            print(f" - {failure}")
        return 1

    print("\nQUALITY TIER UI E2E: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
