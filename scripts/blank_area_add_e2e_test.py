#!/usr/bin/env python3
"""E2E：在卡片内一片完全空白的区域画圈，要求“加个按钮”。

覆盖链路（任务二验收）：
  1. 圈选区域不命中任何 data-source 叶子节点；
  2. 前端几何判定必须自动得到 action=ADD（不能误判成 MODIFY 卡片）；
  3. 最近的 data-source 父容器（card）被选为 ADD 目标；
  4. AnnotationEnvelope 携带 relativeTarget（相对父容器的归一化坐标）；
  5. Mock Agent 在该父容器内部插入完整 JSX，构建通过；
  6. 预览中出现新按钮，原有 CTA 未被覆盖。

运行：
    /tmp/vibe-browser/bin/python scripts/blank_area_add_e2e_test.py
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
WEB_URL = "http://127.0.0.1:5173/"
PY_AI_URL = "http://127.0.0.1:8100"


def fail(message: str) -> None:
    print(f"\nFAIL: {message}")
    raise SystemExit(1)


def cleanup_sandbox(sandbox_id: str | None) -> None:
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


def annotation_payload(route_payload: dict) -> dict:
    annotations = route_payload.get("annotations") or []
    if not annotations:
        fail("Supervisor payload 中没有 annotations")
    return annotations[0]


def main() -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        # 几何契约测试必须使用 Mock 模型，避免真实 DeepSeek 因“按钮文案”
        # 触发 HITL 澄清，导致结果不确定。
        context.add_init_script(
            "window.localStorage.setItem('vibe.llmProvider', 'mock')"
        )
        page = context.new_page()
        sandbox_id: str | None = None
        route_payloads: list[dict] = []

        # 抓 Supervisor /route 的完整请求体：这是前端发给后端的最终契约。
        def capture_route(request) -> None:
            if request.method == "POST" and request.url.endswith(
                "/api/v1/supervisor/route"
            ):
                route_payloads.append(request.post_data_json or {})

        page.on("request", capture_route)

        try:
            print("== 1. 登录并进入演示项目 ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            expect(page.locator(".workspace-tab")).to_have_count(1)
            active = page.locator(".tab-window.is-active")
            expect(active.locator(".mock-card")).to_be_visible()

            print("\n== 2. 计算卡片右下方的纯空白区域 ==")
            card = active.locator(".mock-card").bounding_box()
            if not card:
                fail("找不到大卡片")
            # 该区域位于说明文字下方、CTA 按钮右侧，不覆盖任何
            # data-source 叶子节点，只落在 card 容器背景上。
            left = card["x"] + card["width"] * 0.62
            right = card["x"] + card["width"] * 0.95
            top = card["y"] + card["height"] * 0.72
            bottom = card["y"] + card["height"] * 0.94
            print(
                f"   selection=({left:.1f},{top:.1f})-({right:.1f},{bottom:.1f})"
                f" card={card}"
            )

            print("\n== 3. 画圈：几何判定必须自动为 ADD，父容器为 card ==")
            page.mouse.move(left, top)
            page.mouse.down()
            page.mouse.move(right, bottom, steps=12)
            page.mouse.up()

            popover = active.locator(".canvas-popover")
            expect(popover).to_have_count(1)
            expect(popover).to_have_attribute("data-action", "CREATE_NEW")
            print("   popover data-action = CREATE_NEW")

            print("\n== 4. 输入“加个按钮”并发送 ==")
            popover.locator(".popover-row input").fill("加个按钮")
            popover.locator(".popover-send").click()
            expect(active.locator(".seg-status-running")).to_be_visible(timeout=10_000)

            # 等 /route 请求真正发出后再断言契约。
            page.wait_for_timeout(800)
            if not route_payloads:
                fail("没有捕获到 /api/v1/supervisor/route 请求")
            annotation = annotation_payload(route_payloads[-1])

            if annotation.get("action") not in {"ADD", "CREATE_NEW"}:
                fail(f"action 不是 ADD: {annotation.get('action')}")
            target = annotation.get("target") or {}
            selector = target.get("selector")
            if selector != '[data-source="card"]':
                fail(f"ADD 目标不是 card 父容器: {selector}")
            relative = annotation.get("relativeTarget") or {}
            if relative.get("coordinateSpace") != "parent-relative":
                fail(f"relativeTarget 坐标系错误: {relative}")
            if relative.get("parentSelector") != selector:
                fail(f"relativeTarget.parentSelector 不匹配: {relative}")
            # 圈位于卡片右下角：归一化 x/y 应明显大于 0.5。
            if not (0.5 <= relative.get("x", -1) <= 0.75):
                fail(f"relativeTarget.x 不在右下区域: {relative}")
            if not (0.6 <= relative.get("y", -1) <= 0.85):
                fail(f"relativeTarget.y 不在底部区域: {relative}")
            if not (0 < relative.get("width", 0) <= 1):
                fail(f"relativeTarget.width 非法: {relative}")
            if not (0 < relative.get("height", 0) <= 1):
                fail(f"relativeTarget.height 非法: {relative}")

            # 新增开发者级像素坐标：必须齐全、非负，且归一化比例与像素
            # 比例一致（证明 CanvasStage 已正确除以 viewport.scale）。
            pixel_keys = (
                "relativeX",
                "relativeY",
                "widthPx",
                "heightPx",
                "parentWidth",
                "parentHeight",
            )
            for key in pixel_keys:
                value = relative.get(key)
                if not isinstance(value, (int, float)):
                    fail(f"relativeTarget.{key} 缺失或非数字: {relative}")
                if value < 0:
                    fail(f"relativeTarget.{key} 为负数: {relative}")
            if relative["parentWidth"] <= 0 or relative["parentHeight"] <= 0:
                fail(f"父容器尺寸非法: {relative}")
            ratio_x = relative["relativeX"] / relative["parentWidth"]
            ratio_y = relative["relativeY"] / relative["parentHeight"]
            if abs(ratio_x - relative.get("x", 0)) > 0.02:
                fail(f"像素 x 与归一化 x 不一致: {relative}")
            if abs(ratio_y - relative.get("y", 0)) > 0.02:
                fail(f"像素 y 与归一化 y 不一致: {relative}")
            if relative["relativeX"] + relative["widthPx"] > relative["parentWidth"] + 1:
                fail(f"圈选超出父容器右边界: {relative}")
            if relative["relativeY"] + relative["heightPx"] > relative["parentHeight"] + 1:
                fail(f"圈选超出父容器下边界: {relative}")
            print(
                "   契约校验通过: "
                f"action={annotation['action']} target={selector} "
                f"relative={relative}"
            )

            print("\n== 5. 等待 Agent 插入代码并通过构建 ==")
            expect(
                active.locator(".runtime-result-overlay .status-pill")
            ).to_contain_text("已完成", timeout=240_000)
            status_text = (
                active.locator(".runtime-result-overlay .status-pill").text_content()
                or ""
            )
            sandbox_id = active.locator(".runtime-panel").get_attribute(
                "data-sandbox-id"
            )
            print(f"   状态: {status_text.strip()} / 沙盒: {sandbox_id}")

            print("\n== 6. 预览：新按钮存在且原 CTA 未被覆盖 ==")
            view_result = active.locator(".view-result-cta")
            if view_result.is_visible():
                view_result.click()
            expect(active.locator(".preview-pane.is-visible")).to_be_visible()
            frame = page.frame_locator(".tab-window.is-active .runtime-preview-frame")
            added = frame.locator("button.vibe-added-button")
            expect(added).to_be_visible(timeout=30_000)
            expect(added).to_have_text("新增")
            original = frame.locator("button.vibe-cta")
            expect(original).to_be_visible()
            expect(original).to_have_text("开始体验")
            print("   新按钮: 新增；原按钮仍为“开始体验”")

            page.wait_for_timeout(300)
            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(SCREENSHOT_DIR / "blank-area-add-preview.png"),
                full_page=True,
            )
            print("   screenshot -> docs/screenshots/blank-area-add-preview.png")
        finally:
            cleanup_sandbox(sandbox_id)
            context.close()
            browser.close()

    print("\nBLANK AREA ADD E2E: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
