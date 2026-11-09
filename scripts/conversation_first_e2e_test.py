#!/usr/bin/env python3
"""E2E：新建项目 -> 开始创建 -> 纯对话敲定契约 -> 自动生成初始前端。

覆盖对话前置完整链路：
  1. 新建项目后画布不再渲染 Vibe Studio Mock，只显示【✨ 开始创建】；
  2. 点击后打开 Architect 面板，用户用自然语言描述目标即可启动；
  3. 澄清表单不再出现“全栈 / 轻量 / 脚本”三选一卡片；
  4. 契约确认后自动触发 FrontendCoder（三节点），无需手动点击生成；
  5. 画布自动切换为沙盒实时页面，且页面内容来自契约路由与组件。

前置：
    docker compose up -d
    /tmp/vibe-browser/bin/python scripts/conversation_first_e2e_test.py
"""

from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
WEB_URL = "http://127.0.0.1:5173/"
BFF_URL = "http://127.0.0.1:8080"

GOAL = (
    "我要做一个团队任务看板：成员可以登录，创建和指派任务，"
    "拖拽任务状态，数据需要长期保存到数据库。"
)


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
    """填写当前一轮澄清：选择项取第一项，自由文本按问题语义作答。"""
    form = panel.locator(".architect-clarification")
    expect(form).to_be_visible(timeout=60_000)
    # 形态不再有卡片；若回归成三选一，这里必须失败。
    assert form.locator(".shape-choice").count() == 0, "不应出现应用形态三选一卡片"

    groups = form.locator("fieldset.architect-question")
    for index in range(groups.count()):
        group = groups.nth(index)
        pills = group.locator(".choice-pills button")
        if pills.count() == 0:
            continue
        if group.locator(".choice-pills button.is-selected").count() == 0:
            pills.first.click()

    areas = form.locator("label.architect-question:not(.architect-comment) textarea")
    for index in range(areas.count()):
        area = areas.nth(index)
        label = area.locator("xpath=..").inner_text()
        if "业务动作" in label:
            area.fill("成员登录 -> 创建任务 -> 指派负责人 -> 拖拽任务状态")
        elif "数据实体" in label:
            area.fill(
                "Task(id, title, status, assigneeId, createdAt)；"
                "Member(id, name)；一个 Member 可以负责多个 Task"
            )
        else:
            area.fill(GOAL)

    form.get_by_role("button", name="提交回答并继续").click()


def drive_to_confirmed_contract(page, panel) -> None:
    contract = panel.locator(".contract-viewer")
    for round_index in range(10):
        if contract.count() > 0 and contract.first.is_visible():
            break
        print(f"   澄清第 {round_index + 1} 轮")
        fill_clarification(panel)
        page.wait_for_timeout(900)

    expect(contract.first).to_be_visible(timeout=60_000)
    expect(panel.locator(".architect-status")).to_contain_text("等待确认")
    panel.get_by_role("button", name="确认契约，进入生成").click()
    expect(panel.locator(".architect-status")).to_contain_text("已确认", timeout=60_000)


def main() -> int:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    sandbox_id = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()
        try:
            print("== 1. 登录并新建空白项目 ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            expect(page.locator(".workspace-tab")).to_have_count(1)
            page.locator(".tab-new").click()
            expect(page.locator(".workspace-tab")).to_have_count(2)

            active = page.locator(".tab-window.is-active")
            active.locator(".model-picker select").select_option("mock")
            # 新项目画布必须是空白引导，而不是 Vibe Studio Mock 卡片。
            expect(active.locator(".mock-page")).to_have_count(0)
            start_button = active.locator(".canvas-start-button")
            expect(start_button).to_be_visible(timeout=30_000)
            expect(start_button).to_contain_text("开始创建")
            page.screenshot(
                path=str(SCREENSHOT_DIR / "conversation-first-blank.png"),
                full_page=True,
            )
            print("   OK：空白画布只显示【✨ 开始创建】")

            print("\n== 2. 点击开始创建，用自然语言启动架构对话 ==")
            start_button.click()
            panel = page.locator(".architect-panel")
            expect(panel).to_be_visible(timeout=30_000)
            goal_input = panel.locator("[data-architect-goal]")
            expect(goal_input).to_be_visible(timeout=10_000)
            goal_input.fill(GOAL)
            panel.locator("[data-architect-start]").click()

            print("\n== 3. 纯对话补齐契约，确认后自动初始化前端 ==")
            drive_to_confirmed_contract(page, panel)
            bootstrap = panel.locator("[data-frontend-bootstrap-status]")
            expect(bootstrap).to_be_visible(timeout=30_000)
            # 契约确认后自动运行 FrontendCoder / BuildVerifier / SandboxRestart。
            expect(bootstrap).to_have_attribute(
                "data-frontend-bootstrap-status", "SUCCEEDED", timeout=240_000
            )
            expect(panel.locator("[data-fullstack-step]")).to_have_count(3)
            for node in ("FrontendCoder", "BuildVerifier", "SandboxRestart"):
                expect(
                    panel.locator(f'[data-fullstack-step="{node}"]')
                ).to_have_attribute("data-status", "SUCCEEDED")
            expect(panel.locator(".architect-outcome.is-success").last).to_be_visible()
            page.screenshot(
                path=str(SCREENSHOT_DIR / "conversation-first-bootstrap.png"),
                full_page=True,
            )
            print("   OK：三节点自动完成，无需手动点击生成")

            print("\n== 4. 关闭面板，画布切换为真实前端页面 ==")
            panel.get_by_role("button", name="关闭架构师面板").click()
            expect(panel).to_be_hidden()
            live_frame = active.locator(".canvas-live-frame")
            expect(live_frame).to_be_visible(timeout=30_000)
            frame = page.frame_locator(".tab-window.is-active .canvas-live-frame")
            expect(frame.locator("body")).to_contain_text("总览", timeout=60_000)
            expect(frame.locator("body")).to_contain_text("RecordList")
            expect(frame.locator("body")).to_contain_text("SubmitForm")
            expect(active.locator(".canvas-start-state")).to_have_count(0)
            page.screenshot(
                path=str(SCREENSHOT_DIR / "conversation-first-live-canvas.png"),
                full_page=True,
            )
            print("   OK：画布已替换为契约生成的真实 React 页面")

            # 从浏览器恢复快照里取本次项目的 sandboxId，测试结束清理容器。
            sandbox_id = page.evaluate("""() => {
                  const workspace = JSON.parse(
                    window.localStorage.getItem('vibe.workspace.v1') || '{}')
                  const architect = JSON.parse(
                    window.sessionStorage.getItem('vibe.architect.v1') || '{}')
                  const project = workspace.projects?.[workspace.projects.length - 1]
                  return project ? architect[project.id]?.fullstack?.sandboxId : null
                }""")
            print(f"\nPASS：对话前置链路完成（sandbox={sandbox_id}）")
            return 0
        finally:
            context.close()
            browser.close()
            cleanup_sandbox(sandbox_id)


if __name__ == "__main__":
    sys.exit(main())
