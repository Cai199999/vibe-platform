#!/usr/bin/env python3
"""验证画布标注的三大交互升级。

覆盖：
  1. 圈选后平移 + 滚轮缩放，标注圈与页面 DOM 相对位置不变（世界坐标绑定）；
  2. 同一圈迷你 Chatbox 连续发送两轮指令，前端 threadId / chatHistory 持续增长；
  3. 第二轮复用同一沙盒（reusedSandbox=true），基于上一轮源码增量修改；
  4. 第一轮“改成红色”、第二轮“不，我要蓝色”，最终预览只能是蓝色；
  5. 激活的浮层提升 z-index，失焦后恢复。

前置：
    docker compose up -d
    /tmp/vibe-browser/bin/playwright install chromium

运行：
    /tmp/vibe-browser/bin/python scripts/multi_turn_chat_e2e_test.py
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
WEB_URL = "http://127.0.0.1:5173/"


def fail(message: str) -> None:
    print(f"\nFAIL: {message}")
    raise SystemExit(1)


def draw_around(page, locator) -> None:
    box = locator.bounding_box()
    if not box:
        fail("找不到圈选目标")
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


def geometry(active) -> dict:
    """读取圈与目标按钮相对几何；不会受 transform 像素值影响。"""
    return active.locator(".annotation-layer").evaluate("""layer => {
            const ellipse = layer.querySelector('.annotation-ellipse:not(.is-draft)');
            const cta = layer.closest('.canvas-viewport')
                .querySelector('[data-vibe-target="cta-button"]');
            const viewport = layer.closest('.canvas-viewport');
            const stage = viewport.closest('.canvas-stage');
            if (!ellipse || !cta) return null;
            const e = ellipse.getBoundingClientRect();
            const c = cta.getBoundingClientRect();
            const s = stage.getBoundingClientRect();
            return {
                ellipse: {x: e.x, y: e.y, width: e.width, height: e.height},
                cta: {x: c.x, y: c.y, width: c.width, height: c.height},
                relativeX: (e.x + e.width / 2 - (c.x + c.width / 2)) / c.width,
                relativeY: (e.y + e.height / 2 - (c.y + c.height / 2)) / c.height,
                stage: {x: s.x, y: s.y, width: s.width, height: s.height},
                transform: getComputedStyle(viewport).transform,
            };
        }""")


def assert_no_drift(before: dict, after: dict, label: str) -> None:
    if before is None or after is None:
        fail(f"{label} 无法读取圈 / 按钮几何")
    dx = abs(after["relativeX"] - before["relativeX"])
    dy = abs(after["relativeY"] - before["relativeY"])
    if dx > 0.015 or dy > 0.015:
        fail(
            f"{label} 标注发生漂移: relativeX {before['relativeX']} -> "
            f"{after['relativeX']} (Δ{dx:.4f}), relativeY "
            f"{before['relativeY']} -> {after['relativeY']} (Δ{dy:.4f})\n"
            f"before={before}\nafter={after}"
        )
    print(
        f"   {label} 无漂移: ΔX={dx:.5f}, ΔY={dy:.5f}, "
        f"transform={after['transform']}"
    )


def send_in_popover(page, popover, text: str, label: str) -> dict:
    """在同一个标注浮层内发送一条指令，返回 BFF 的 Run 响应 JSON。"""
    popover.locator("input").fill(text, force=True)
    with page.expect_response(
        lambda response: response.request.method == "POST"
        and response.url.endswith("/api/v1/runs"),
        timeout=180_000,
    ) as response_info:
        popover.locator(".popover-send").click(force=True)
    response = response_info.value
    if not response.ok:
        fail(f"{label} Run 请求失败: HTTP {response.status} {response.text()}")
    payload = response.json()
    print(
        f"   {label}: status={payload.get('status')} "
        f"sandbox={payload.get('sandboxId')} reused={payload.get('reusedSandbox')}"
    )
    return payload


def main() -> int:
    sandbox_id = ""
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1600, "height": 1000})
            page = context.new_page()
            requests: list[dict] = []

            def on_request(request) -> None:
                if request.method == "POST" and request.url.endswith("/api/v1/runs"):
                    requests.append(json.loads(request.post_data or "{}"))

            page.on("request", on_request)

            print("== 1. 登录并选择 Mock 模型 ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            expect(page.locator(".workspace-tab")).to_have_count(1)
            active = page.locator(".tab-window.is-active")
            active.locator(".model-picker select").select_option("mock")

            print("\n== 2. 圈选按钮，记录圈与按钮的相对几何 ==")
            draw_around(page, active.locator(".mock-cta"))
            popover = active.locator(".canvas-popover")
            expect(popover).to_have_count(1)
            annotation_id = popover.get_attribute("data-annotation-id")
            before = geometry(active)
            popover_before = popover.bounding_box()
            if not annotation_id or before is None or not popover_before:
                fail("未建立标注或读取几何失败")

            print("\n== 3. 平移 + 滚轮放大画布，检查圈不漂移 ==")
            cta_box = active.locator(".mock-cta").bounding_box()
            if not cta_box:
                fail("按钮不可见")
            # 以按钮中心为缩放中心，滚轮放大到约 150%~180%。
            page.mouse.move(
                cta_box["x"] + cta_box["width"] / 2,
                cta_box["y"] + cta_box["height"] / 2,
            )
            page.mouse.wheel(0, -280)
            page.wait_for_timeout(350)
            zoom_text = active.locator("[data-viewport-zoom]").inner_text()
            if int(zoom_text.replace("%", "")) <= 110:
                fail(f"画布未放大: {zoom_text}")

            # 中键自由平移，确保 X / Y 同时变化。
            stage = active.locator(".canvas-stage").bounding_box()
            if not stage:
                fail("画布不可见")
            start_x = stage["x"] + stage["width"] * 0.52
            start_y = stage["y"] + stage["height"] * 0.56
            page.mouse.move(start_x, start_y)
            page.mouse.down(button="middle")
            page.mouse.move(start_x - 64, start_y - 42, steps=12)
            page.mouse.up(button="middle")
            page.wait_for_timeout(300)

            zoomed = geometry(active)
            assert_no_drift(before, zoomed, "平移 + 缩放后")
            popover_after = popover.bounding_box()
            if not popover_after:
                fail("缩放后浮层不可见")
            if abs(popover_after["width"] - popover_before["width"]) > 2:
                fail(
                    "缩放后 Chatbox 宽度发生变化（反向缩放失效）: "
                    f"{popover_before['width']} -> {popover_after['width']}"
                )
            print(
                f"   Chatbox 屏幕宽度保持: {popover_before['width']:.0f} -> "
                f"{popover_after['width']:.0f}px"
            )
            page.screenshot(
                path=str(SCREENSHOT_DIR / "m1-multiturn-zoom-no-drift.png"),
                full_page=True,
            )

            print("\n== 4. 激活浮层 z-index，失焦后恢复 ==")
            expect(popover).to_have_class(re.compile(r"\bis-active\b"))
            active_z = int(popover.evaluate("el => getComputedStyle(el).zIndex"))
            # 点击画布空白处（左上角）失焦，不命中任何标注 / 浮层。
            active.locator(".canvas-stage").click(position={"x": 8, "y": 8})
            page.wait_for_timeout(150)
            idle_z = int(popover.evaluate("el => getComputedStyle(el).zIndex"))
            if not active_z > idle_z:
                fail(f"激活 z-index 未提升: active={active_z}, idle={idle_z}")
            print(f"   z-index: active={active_z} -> idle={idle_z}")
            # 重新聚焦当前浮层，继续对话。
            popover.locator("input").focus()

            print("\n== 5. 第一轮：改成红色 ==")
            first_payload = send_in_popover(page, popover, "改成红色", "第一轮")
            expect(popover.locator(".chat-bubble.is-user")).to_contain_text("改成红色")
            expect(popover.locator(".chat-bubble.is-assistant")).to_be_visible(
                timeout=120_000
            )
            if first_payload.get("status") != "AWAITING_APPROVAL":
                fail(f"第一轮执行失败: {first_payload}")
            sandbox_id = first_payload.get("sandboxId") or ""
            if not sandbox_id:
                fail("第一轮未返回 sandboxId")
            if first_payload.get("reusedSandbox") is not False:
                fail(f"第一轮不应复用沙盒: {first_payload.get('reusedSandbox')}")

            print("\n== 6. 第二轮：不，我要蓝色（同一个圈 / 同一个沙盒）==")
            expect(popover.locator(".chat-bubble")).to_have_count(2)
            second_payload = send_in_popover(page, popover, "不，我要蓝色", "第二轮")
            expect(popover.locator(".chat-bubble.is-user")).to_have_count(2)
            expect(popover.locator(".chat-bubble.is-assistant")).to_have_count(
                2, timeout=120_000
            )
            expect(popover.locator(".chat-bubble.is-user").last).to_contain_text(
                "不，我要蓝色"
            )
            if second_payload.get("status") != "AWAITING_APPROVAL":
                fail(f"第二轮执行失败: {second_payload}")
            if second_payload.get("sandboxId") != sandbox_id:
                fail("第二轮没有复用同一沙盒")
            if second_payload.get("reusedSandbox") is not True:
                fail(
                    "第二轮 reusedSandbox 不是 true: "
                    f"{second_payload.get('reusedSandbox')}"
                )

            print("\n== 7. 校验两轮请求的 threadId / chatHistory / sandboxId ==")
            if len(requests) != 2:
                fail(f"应捕获 2 次单圈请求，实际 {len(requests)}")
            first_ann = requests[0]["annotations"][0]
            second_ann = requests[1]["annotations"][0]
            if first_ann.get("threadId") != second_ann.get("threadId"):
                fail("两轮 threadId 不一致")
            first_history = first_ann.get("chatHistory") or []
            second_history = second_ann.get("chatHistory") or []
            if [m.get("role") for m in first_history] != ["user"]:
                fail(f"第一轮历史异常: {first_history}")
            if len(second_history) != 3:
                fail(f"第二轮历史应包含 user/assistant/user 3 条: {second_history}")
            if second_history[0].get("content") != "改成红色":
                fail(f"第二轮未携带第一轮用户指令: {second_history}")
            if second_history[1].get("role") != "assistant":
                fail(f"第二轮未携带第一轮助手回复: {second_history}")
            if second_history[2].get("content") != "不，我要蓝色":
                fail(f"第二轮最新指令异常: {second_history}")
            if requests[1].get("sandboxId") != sandbox_id:
                fail("第二轮请求体未携带第一轮 sandboxId")
            if first_ann.get("payload", {}).get("text") != "改成红色":
                fail("第一轮 payload.text 异常")
            if second_ann.get("payload", {}).get("text") != "不，我要蓝色":
                fail("第二轮 payload.text 异常")
            print(
                f"   threadId={second_ann.get('threadId')} / "
                f"chatHistory={[m.get('role') for m in second_history]} / "
                f"sandboxId={sandbox_id}"
            )

            print("\n== 8. 预览最终为蓝色（第二次指令覆盖第一次）==")
            active.get_by_role("button", name="预览", exact=True).click()
            expect(active.locator(".runtime-preview-frame")).to_be_visible()
            frame = page.frame_locator(".tab-window.is-active .runtime-preview-frame")
            cta = frame.locator("[data-vibe-target='cta-button']")
            expect(cta).to_be_visible(timeout=30_000)
            # Vite HMR / 重新构建后 CSS 规则需要一点时间生效。
            deadline = time.time() + 30
            background = ""
            while time.time() < deadline:
                background = cta.evaluate("el => getComputedStyle(el).backgroundColor")
                if background == "rgb(0, 0, 255)":
                    break
                page.wait_for_timeout(500)
            if background != "rgb(0, 0, 255)":
                fail(f"最终按钮颜色不是蓝色: {background}")
            print(f"   最终 CTA background={background}")

            print("\n== 9. 回到画布，圈仍与按钮绑定 ==")
            active.get_by_role("button", name="画布", exact=True).click()
            expect(active.locator(".canvas-pane.is-visible")).to_be_visible()
            final_geometry = geometry(active)
            assert_no_drift(zoomed, final_geometry, "两轮对话 + 面板切换后")
            page.screenshot(
                path=str(SCREENSHOT_DIR / "m1-multiturn-chat-result.png"),
                full_page=True,
            )

            print("\n== 10. 刷新后 thread / chatHistory 持久化 ==")
            page.reload(wait_until="networkidle")
            active = page.locator(".tab-window.is-active")
            expect(active.locator(".canvas-popover")).to_have_count(1)
            restored = active.locator(".canvas-popover")
            expect(restored.locator(".chat-bubble.is-user")).to_have_count(2)
            expect(restored.locator(".chat-bubble.is-assistant")).to_have_count(2)
            if restored.get_attribute("data-annotation-id") != annotation_id:
                fail("刷新后标注 id 发生变化")
            print("   刷新后两轮对话仍完整保留")

            context.close()
            browser.close()
        print("\nMULTI-TURN CHAT E2E: PASS")
        return 0
    finally:
        if sandbox_id:
            import urllib.request

            request = urllib.request.Request(
                f"http://127.0.0.1:8100/api/v1/internal/sandbox/{sandbox_id}",
                method="DELETE",
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    print(f"   cleanup {sandbox_id} -> HTTP {response.status}")
            except Exception as exc:  # noqa: BLE001 - 清理失败不掩盖主流程
                print(f"   cleanup {sandbox_id} -> {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
