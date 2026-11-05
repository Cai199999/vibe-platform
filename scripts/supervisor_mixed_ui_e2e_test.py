#!/usr/bin/env python3
"""浏览器级 Supervisor 混合场景 E2E：UI 修改 + 架构澄清。

覆盖用户最终要求的前端链路：

  1. 画布圈选 CTA 并填写 UI 修改指令；
  2. 点击“下一步：构建全栈方案”，前端只调用
     ``/api/v1/supervisor/route`` + ``/dispatch``，不再直连
     ``/runs/stream`` 或 ``/architect/runs/stream``；
  3. Supervisor 返回 ``handoff_both`` 后：Architect 面板自动打开，
     右侧显示“正在进行全局架构设计，请先确认契约”，UI 时间轴不启动；
  4. 完成全栈澄清、生成契约并确认；
  5. 契约确认自动释放门控，前端 attach 到 Supervisor 预启动的 UI Run；
  6. UI 沙盒真实执行成功，预览时间轴显示“已完成”，且没有重复创建 Run；
  7. 清理沙盒。

前置：
    docker compose up -d
    /tmp/vibe-browser/bin/playwright install chromium
    /tmp/vibe-browser/bin/python scripts/supervisor_mixed_ui_e2e_test.py
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
WEB_URL = "http://127.0.0.1:5173/"
AI_URL = "http://127.0.0.1:8100"

UI_INSTRUCTION = "把提交按钮改成蓝色"
ARCHITECT_MESSAGE = "下一步：构建全栈方案"


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


def question_value(label: str) -> str:
    """为全栈澄清的文本槽位提供可执行答案。"""
    if any(term in label for term in ("业务", "流程", "场景")):
        return "用户提交订单 -> 校验库存 -> 创建订单 -> 扣减库存 -> 返回结果"
    if any(term in label for term in ("实体", "字段", "数据模型", "表结构")):
        return "Order(id, userId, total, status)；User(id, name, email)"
    if any(term in label for term in ("约束", "性能", "安全", "非功能")):
        return "单机日活 1 万，接口 P95 < 300ms，仅登录用户可下单"
    return "订单管理：创建、查询、更新状态；本期聚焦核心交易闭环"


def fill_clarification_form(form) -> None:
    """按问题类型填写当前轮澄清表单。"""
    shape = form.locator(".shape-choice", has_text="全栈系统")
    if shape.count():
        shape.first.click()

    # 单选项 / 多选项：未选择时取第一个候选，满足 required 校验。
    for fieldset in form.locator("fieldset.architect-question").all():
        pills = fieldset.locator(".choice-pills button")
        if not pills.count():
            continue
        selected = fieldset.locator(".choice-pills button.is-selected")
        if not selected.count():
            pills.first.click()

    for textarea in form.locator("label.architect-question textarea").all():
        if textarea.locator(
            "xpath=ancestor::label[contains(@class,'architect-comment')]"
        ).count():
            continue
        label = textarea.evaluate("el => el.closest('label')?.innerText || ''")
        textarea.fill(question_value(label))


def wait_for_form_round_or_contract(panel, previous_round: str) -> str:
    """等待澄清进入下一轮或直接出现契约。

    不能用整张表单文本判断“新的一轮”：提交中按钮文案变化也会让文本不同。
    只看轮次 eyebrow，避免把 submitting 误判成下一轮。
    """
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        contract = panel.locator(".contract-viewer")
        if contract.count() and contract.is_visible():
            return "contract"
        form = panel.locator(".architect-clarification")
        if form.count() and form.is_visible():
            eyebrow = form.locator(".architect-form-eyebrow")
            if eyebrow.count():
                current = eyebrow.inner_text()
                if current != previous_round:
                    return "form"
        panel.page.wait_for_timeout(400)
    raise AssertionError("等待澄清下一轮 / 契约超时")


def drive_to_contract(page, panel) -> None:
    """循环回答澄清，直到生成《全栈实施契约》。"""
    for round_index in range(1, 7):
        form = panel.locator(".architect-clarification")
        expect(form).to_be_visible(timeout=90_000)
        round_label = form.locator(".architect-form-eyebrow").inner_text()
        print(f"   第 {round_index} 轮澄清：{round_label}")
        fill_clarification_form(form)
        submit = form.get_by_role("button", name="提交回答并继续")
        expect(submit).to_be_enabled(timeout=10_000)
        submit.click()
        result = wait_for_form_round_or_contract(panel, round_label)
        if result == "contract":
            return
    raise AssertionError("超过 6 轮仍未生成契约")


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
            posts: list[tuple[str, dict]] = []

            def on_request(request) -> None:
                if request.method != "POST":
                    return
                if "/api/v1/supervisor/" not in request.url:
                    return
                try:
                    payload = json.loads(request.post_data or "{}")
                except json.JSONDecodeError:
                    payload = {}
                posts.append((request.url, payload))

            page.on("request", on_request)

            print("== 1. 登录、选择 Mock 模型并圈选 CTA ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            expect(page.locator(".workspace-tab")).to_have_count(1)
            active = page.locator(".tab-window.is-active")
            active.locator(".model-picker select").select_option("mock")

            draw_around(page, active.locator(".mock-cta"))
            popover = active.locator(".canvas-popover")
            expect(popover).to_have_count(1)
            popover.locator("input").fill(UI_INSTRUCTION, force=True)
            expect(active.locator(".selection-state")).to_contain_text("1 个标注")
            print(f"   annotation intent={UI_INSTRUCTION}")

            print("\n== 2. 点击“下一步”：只走 Supervisor route + dispatch ==")
            active.get_by_role(
                "button", name=re.compile(re.escape(ARCHITECT_MESSAGE))
            ).click()

            panel = page.locator(".architect-panel")
            expect(panel).to_be_visible(timeout=30_000)

            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not any(
                url.endswith("/api/v1/supervisor/dispatch") for url, _ in posts
            ):
                page.wait_for_timeout(200)
            urls = [url for url, _ in posts]
            assert any(
                url.endswith("/api/v1/supervisor/route") for url in urls
            ), f"未调用 Supervisor route: {urls}"
            assert any(
                url.endswith("/api/v1/supervisor/dispatch") for url in urls
            ), f"未调用 Supervisor dispatch: {urls}"
            direct_starts = [
                url
                for url in urls
                if url.endswith("/runs/stream")
                or url.endswith("/architect/runs/stream")
            ]
            assert not direct_starts, f"前端仍在直连子图启动接口: {direct_starts}"
            dispatch_payload = next(
                payload
                for url, payload in posts
                if url.endswith("/api/v1/supervisor/dispatch")
            )
            assert dispatch_payload.get("message") == (
                f"{ARCHITECT_MESSAGE}。{UI_INSTRUCTION}"
            ), f"下一步消息未携带标注意图: {dispatch_payload.get('message')}"
            assert len(dispatch_payload.get("annotations") or []) == 1
            print("   route + dispatch 均命中，无直连启动请求")

            print("\n== 3. both 强门控：Architect 打开，UI 冻结 ==")
            gate = panel.locator("[data-supervisor-gate]")
            expect(gate).to_be_visible(timeout=30_000)
            expect(gate).to_have_attribute("data-supervisor-gate", "AWAITING_CONTRACT")
            expect(gate).to_contain_text("正在进行全局架构设计，请先确认契约")
            expect(active.locator(".supervisor-gate-chip")).to_contain_text(
                "正在进行全局架构设计，请先确认契约"
            )
            expect(active.locator(".submit-annotations")).to_be_disabled()

            # Supervisor 决策可视化：判定 + both 双阶段时间轴。
            trace = active.locator("[data-supervisor-trace]")
            expect(trace).to_be_visible()
            expect(trace).to_have_attribute("data-supervisor-handoff", "handoff_both")
            expect(trace).to_contain_text("判定为全栈构建")
            expect(trace.locator("[data-both-timeline]")).to_be_visible()
            expect(trace.locator('[data-both-stage="architect"]')).to_have_attribute(
                "data-stage-state", "active"
            )
            expect(trace.locator('[data-both-stage="ui"]')).to_have_attribute(
                "data-stage-state", "waiting"
            )

            # 门控未释放前 UI 时间轴不能拿到沙盒。
            expect(active.locator(".runtime-panel")).to_have_attribute(
                "data-sandbox-id", ""
            )
            page.screenshot(
                path=str(SCREENSHOT_DIR / "supervisor-mixed-gate-locked.png"),
                full_page=True,
            )
            print("   AWAITING_CONTRACT：提交冻结、画布冻结、UI 时间轴未启动")

            print("\n== 4. 完成全栈澄清并确认契约 ==")
            drive_to_contract(page, panel)
            contract = panel.locator(".contract-viewer")
            expect(contract).to_be_visible(timeout=30_000)
            expect(panel.locator(".shape-badge").first).to_contain_text("全栈系统")
            expect(gate).to_have_attribute("data-supervisor-gate", "AWAITING_CONTRACT")
            panel.get_by_role("button", name="确认契约，进入生成").click()
            expect(panel.locator(".architect-status")).to_contain_text(
                "已确认", timeout=60_000
            )
            print("   contract CONFIRMED")

            print("\n== 5. 自动释放门控并 attach 预启动的 UI Run ==")
            expect(gate).to_have_attribute(
                "data-supervisor-gate", "RELEASED", timeout=60_000
            )
            expect(gate).to_contain_text("契约已确认，界面修改已启动")
            expect(
                active.locator('[data-supervisor-trace] [data-both-stage="architect"]')
            ).to_have_attribute("data-stage-state", "done")
            expect(
                active.locator('[data-supervisor-trace] [data-both-stage="ui"]')
            ).to_have_attribute("data-stage-state", "active")
            # 前端放行时调用 release，而不是自行 POST 启动 UI。
            assert any(
                url.endswith("/release") for url, _ in posts
            ), f"未调用门控 release: {[url for url, _ in posts]}"
            assert not any(
                url.endswith("/runs/stream") for url, _ in posts
            ), "前端仍自行启动 UI Graph"

            print("\n== 6. UI 沙盒真实执行成功 ==")
            expect(
                active.locator(".runtime-result-overlay .status-pill")
            ).to_contain_text("已完成", timeout=240_000)
            sandbox_id = (
                active.locator(".runtime-panel").get_attribute("data-sandbox-id") or ""
            )
            assert sandbox_id, "UI Run 未返回 sandboxId"
            page.screenshot(
                path=str(SCREENSHOT_DIR / "supervisor-mixed-gate-released.png"),
                full_page=True,
            )
            expect(
                active.locator('[data-supervisor-trace] [data-both-stage="ui"]')
            ).to_have_attribute("data-stage-state", "done")
            print(f"   run.succeeded sandbox={sandbox_id}")

            print("\n== 7. 校验没有重复 UI Run / 沙盒 ==")
            dispatch_payloads = [
                payload
                for url, payload in posts
                if url.endswith("/api/v1/supervisor/dispatch")
            ]
            release_calls = [
                payload for url, payload in posts if url.endswith("/release")
            ]
            assert (
                len(dispatch_payloads) == 1
            ), f"应只有一次 Supervisor dispatch，实际 {len(dispatch_payloads)}"
            # release 由 Architect 确认 effect 自动触发；失败重试时才可能多于一次。
            assert len(release_calls) >= 1, "缺少 release 调用"
            print(
                f"   dispatch={len(dispatch_payloads)} / release={len(release_calls)}"
            )

            context.close()
            browser.close()
        print("\nSUPERVISOR MIXED UI E2E: PASS")
        return 0
    finally:
        if sandbox_id:
            destroy_sandbox(sandbox_id)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"\nSUPERVISOR MIXED UI E2E: FAIL - {exc}")
        raise SystemExit(1) from exc
