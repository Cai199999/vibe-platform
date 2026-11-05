#!/usr/bin/env python3
"""回归：发送后立即关闭浮层不得卡死 / 不得污染预览区。

覆盖：
  1. 输入意图并点击发送；
  2. 立即点击浮层右上角“×”，执行中弹出确认后选择“仍然关闭”；
  3. 标注与浮层立即消失，页面仍可交互；
  4. 打开新的标注，输入框可正常使用；
  5. 切到展示模式，等待迟到的 Agent 结果，预览区不得出现“执行失败”；
  6. 全程不得出现未捕获的页面异常。

运行：
    /tmp/vibe-browser/bin/python scripts/popover_close_race_e2e_test.py
"""

from __future__ import annotations

import json
import urllib.request

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect, sync_playwright

WEB_URL = "http://127.0.0.1:5173/"
PY_AI_URL = "http://127.0.0.1:8100"


def fail(message: str) -> None:
    print(f"\nFAIL: {message}")
    raise SystemExit(1)


def draw_around(page, locator) -> None:
    box = locator.bounding_box()
    if not box:
        fail("找不到圈选目标")
    page.mouse.move(box["x"] - 28, box["y"] - 18)
    page.mouse.down()
    page.mouse.move(
        box["x"] + box["width"] / 2,
        box["y"] + box["height"] / 2,
        steps=8,
    )
    page.mouse.move(
        box["x"] + box["width"] + 28,
        box["y"] + box["height"] + 18,
        steps=8,
    )
    page.mouse.up()
    page.wait_for_timeout(260)


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


def main() -> int:
    sandbox_id = ""
    page_errors: list[str] = []
    route_requests: list[dict] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1600, "height": 1000})
            page = context.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))

            def on_request(request) -> None:
                if request.method == "POST" and request.url.endswith(
                    "/api/v1/supervisor/route"
                ):
                    try:
                        route_requests.append(json.loads(request.post_data or "{}"))
                    except json.JSONDecodeError:
                        route_requests.append({})

            page.on("request", on_request)

            print("== 1. 登录并选择 Mock 模型 ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            expect(page.locator(".workspace-tab")).to_have_count(1)
            active = page.locator(".tab-window.is-active")
            active.locator(".model-picker select").select_option("mock")

            print("\n== 2. 输入意图并点击发送 ==")
            draw_around(page, active.locator(".mock-cta"))
            popover = active.locator(".canvas-popover")
            expect(popover).to_have_count(1)
            popover.locator("input").fill("改成红色", force=True)
            popover.locator(".popover-send").click()

            print("\n== 3. 立即点击 X 关闭 ==")
            remove = popover.locator(".popover-remove")
            expect(remove).to_be_enabled(timeout=5_000)
            remove.click()
            confirm = popover.locator(".popover-close-confirm")
            try:
                expect(confirm).to_be_visible(timeout=1_500)
                confirm.get_by_role("button", name="仍然关闭").click()
                print("   执行中关闭：确认弹层 -> 仍然关闭")
            except PlaywrightTimeoutError:
                print("   关闭发生在 submitting 渲染前：直接移除（同样安全）")

            expect(active.locator(".canvas-popover")).to_have_count(0, timeout=10_000)
            expect(active.locator(".annotation-ellipse")).to_have_count(
                0, timeout=10_000
            )
            # 页面仍然可执行 JS / 可点击，说明没有主线程卡死。
            if page.evaluate("() => 1 + 1") != 2:
                fail("页面主线程无响应")

            print("\n== 4. 打开新的标注，确认输入可用 ==")
            active.get_by_role("button", name="画布", exact=True).click()
            expect(active.locator(".canvas-pane.is-visible")).to_be_visible()
            draw_around(page, active.locator(".mock-title"))
            new_popover = active.locator(".canvas-popover")
            expect(new_popover).to_have_count(1)
            new_input = new_popover.locator("input")
            expect(new_input).to_be_enabled()
            new_input.fill("把标题改成你好")
            if new_input.input_value() != "把标题改成你好":
                fail("新标注输入框无法更新")
            print("   新标注输入正常")

            print("\n== 5. 展示模式等待迟到结果：预览区不得报错 ==")
            active.get_by_role("button", name="展示模式", exact=True).click()
            expect(active.locator(".preview-pane.is-visible")).to_be_visible()
            # 给后端 route / dispatch / cancel 与迟到 SSE 充分时间落地。
            page.wait_for_timeout(8_000)
            runtime_errors = active.locator(".runtime-error, .error-state")
            if runtime_errors.count() > 0:
                fail(f"展示模式出现错误面板: {runtime_errors.first.inner_text()}")
            body_text = active.inner_text()
            if "执行失败" in body_text:
                fail("展示模式出现“执行失败”")
            if active.locator(".canvas-popover").count() != 1:
                fail("关闭的标注被迟到结果重新创建")
            if page.evaluate("() => document.body.innerText.length") <= 0:
                fail("页面内容为空（白屏）")
            print("   预览区无执行失败，页面无白屏")

            sandbox_id = (
                active.locator(".runtime-panel").get_attribute("data-sandbox-id") or ""
            )

            print("\n== 6. 已派发（SSE 执行中）再关闭：验证迟到终态被丢弃 ==")
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url.endswith("/api/v1/supervisor/dispatch"),
                timeout=30_000,
            ):
                new_popover.locator(".popover-send").click()
            # dispatch 返回后前端进入 attach SSE；此时关闭才是真正“结果返回前”
            # 的竞态窗口。确认弹层若出现，选择强行关闭。
            late_remove = new_popover.locator(".popover-remove")
            expect(late_remove).to_be_enabled(timeout=5_000)
            late_remove.click()
            late_confirm = new_popover.locator(".popover-close-confirm")
            try:
                expect(late_confirm).to_be_visible(timeout=1_500)
                late_confirm.get_by_role("button", name="仍然关闭").click()
                print("   SSE 执行中关闭：确认弹层 -> 仍然关闭")
            except PlaywrightTimeoutError:
                print("   Run 已先完成：按普通删除处理")
            expect(active.locator(".canvas-popover")).to_have_count(0, timeout=10_000)
            page.wait_for_timeout(8_000)
            if active.locator(".runtime-error, .error-state").count() > 0:
                fail(
                    "SSE 关闭后预览区报错: "
                    f"{active.locator('.runtime-error, .error-state').first.inner_text()}"
                )
            print("   迟到终态未污染预览区")

            print("\n== 7. 未捕获异常检查 ==")
            if page_errors:
                fail(f"出现未捕获页面异常: {page_errors}")
            print(f"   pageerror=0 / route 请求={len(route_requests)} 次")

            context.close()
            browser.close()
        print("\nPOPOVER CLOSE RACE E2E: PASS")
        return 0
    finally:
        cleanup_sandbox(sandbox_id)


if __name__ == "__main__":
    raise SystemExit(main())
