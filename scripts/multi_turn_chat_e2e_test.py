#!/usr/bin/env python3
"""验证画布标注的三大交互升级。

覆盖：
  1. 圈选后平移 + 滚轮缩放，标注圈与页面 DOM 相对位置不变（世界坐标绑定）；
  2. 同一圈迷你 Chatbox 连续发送两轮指令，前端 threadId / chatHistory 持续增长；
  3. 第二轮复用同一沙盒（reusedSandbox=true），基于上一轮源码增量修改；
  4. 第一轮“改成红色”、第二轮“不，我要蓝色”，最终预览只能是蓝色；
  5. 激活的浮层提升 z-index，失焦后恢复；
  6. 浮层跟随画布等比缩放（无反向补偿），缩放后内部比例不变。

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
                scale: viewport.getBoundingClientRect().width / 1280,
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


def send_in_popover(page, active, popover, text: str, label: str) -> dict:
    """发送一条指令，等待 SSE 成功终态后返回摘要。

    设置默认关闭人工审批：验证通过后直接 SUCCEEDED，不再停留于审批。
    """
    popover.locator("input").fill(text, force=True)
    popover.locator(".popover-send").click(force=True)

    expect(active.locator(".runtime-result-overlay .status-pill")).to_contain_text(
        "已完成", timeout=180_000
    )
    # sandboxId 在 run.succeeded 后才写入 RuntimePanel。
    sandbox_id = active.locator(".runtime-panel").get_attribute("data-sandbox-id") or ""
    # 免审批路径不切换视口；若测试中曾手动切到预览，则回到画布继续下一轮。
    active.get_by_role("button", name="画布", exact=True).click()
    expect(active.locator(".canvas-pane.is-visible")).to_be_visible()

    payload = {"status": "SUCCEEDED", "sandboxId": sandbox_id}
    print(f"   {label}: status={payload['status']} sandbox={sandbox_id}")
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
                if request.method == "POST" and request.url.endswith(
                    "/api/v1/supervisor/route"
                ):
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
            # 新语义：浮层不反向补偿，屏幕宽度 = 320 世界单位 × 画布缩放。
            world_width_before = popover_before["width"] / before["scale"]
            world_width_after = popover_after["width"] / zoomed["scale"]
            if abs(world_width_before - 320) > 1 or abs(world_width_after - 320) > 1:
                fail(
                    "Chatbox 未按固定世界宽度等比例缩放: "
                    f"{world_width_before:.1f} -> {world_width_after:.1f}"
                )
            if zoomed["scale"] <= before["scale"] + 0.05:
                fail(f"画布未实际放大: {before['scale']} -> {zoomed['scale']}")
            popover_transform = popover.evaluate("el => getComputedStyle(el).transform")
            if popover_transform != "none":
                fail(f"浮层仍带独立 transform: {popover_transform}")
            print(
                f"   Chatbox 世界宽度恒定 320: 屏幕 "
                f"{popover_before['width']:.0f} -> {popover_after['width']:.0f}px "
                f"(scale {before['scale']:.2f} -> {zoomed['scale']:.2f})"
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
            first_payload = send_in_popover(page, active, popover, "改成红色", "第一轮")
            expect(popover.locator(".chat-bubble.is-user")).to_contain_text("改成红色")
            expect(popover.locator(".chat-bubble.is-assistant")).to_be_visible(
                timeout=120_000
            )
            if first_payload.get("status") != "SUCCEEDED":
                fail(f"第一轮执行失败: {first_payload}")
            sandbox_id = first_payload.get("sandboxId") or ""
            if not sandbox_id:
                fail("第一轮未返回 sandboxId")
            if requests and requests[0].get("sandboxId"):
                fail(f"第一轮不应复用沙盒: {requests[0].get('sandboxId')}")

            print("\n== 6. 第二轮：不，我要蓝色（同一个圈 / 同一个沙盒）==")
            expect(popover.locator(".chat-bubble")).to_have_count(2)
            second_payload = send_in_popover(
                page, active, popover, "不，我要蓝色", "第二轮"
            )
            expect(popover.locator(".chat-bubble.is-user")).to_have_count(2)
            expect(popover.locator(".chat-bubble.is-assistant")).to_have_count(
                2, timeout=120_000
            )
            expect(popover.locator(".chat-bubble.is-user").last).to_contain_text(
                "不，我要蓝色"
            )
            if second_payload.get("status") != "SUCCEEDED":
                fail(f"第二轮执行失败: {second_payload}")
            if second_payload.get("sandboxId") != sandbox_id:
                fail("第二轮没有复用同一沙盒")

            print("\n== 7. 校验两轮请求的 threadId / chatHistory / sandboxId ==")
            if len(requests) != 2:
                fail(f"应捕获 2 次单圈请求，实际 {len(requests)}")
            first_ann = requests[0]["annotations"][0]
            second_ann = requests[1]["annotations"][0]
            if first_ann.get("threadId") != second_ann.get("threadId"):
                fail("两轮 threadId 不一致")
            first_history = first_ann.get("chatHistory") or []
            second_history = second_ann.get("chatHistory") or []
            # 当前指令放在 payload.text；chatHistory 只包含此前已完成的轮次。
            if first_history:
                fail(f"第一轮历史应为空: {first_history}")
            if [m.get("role") for m in second_history] != ["user", "assistant"]:
                fail(f"第二轮历史应包含 user/assistant 2 条: {second_history}")
            if second_history[0].get("content") != "改成红色":
                fail(f"第二轮未携带第一轮用户指令: {second_history}")
            if second_history[1].get("role") != "assistant":
                fail(f"第二轮未携带第一轮助手回复: {second_history}")
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
