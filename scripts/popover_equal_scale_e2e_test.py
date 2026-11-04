#!/usr/bin/env python3
"""验证券商浮层与画布严格等比缩放（50% / 100% / 150%）。

约束：
  1. 浮层是 .canvas-viewport 的子元素，不携带任何 1/scale 反向 transform；
  2. 浮层宽度固定 320 世界单位，屏幕宽度 = 320 × 画布缩放；
  3. 圆角、字号、输入框随画布一起等比缩放，浮层内部宽高比恒定；
  4. 浮层与圈线的间距（世界坐标）不随缩放变化；
  5. 浮层层级高于所有圈线。

运行：
    /tmp/vibe-browser/bin/python scripts/popover_equal_scale_e2e_test.py
"""

from __future__ import annotations

import math
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
WEB_URL = "http://127.0.0.1:5173/"

POPOVER_WIDTH = 320
POPOVER_GAP = 14
VIEWPORT_BASE_WIDTH = 1280


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


def set_zoom(page, stage, target_percent: int) -> None:
    """用原生 wheel 事件精确缩放到目标百分比（deltaY 由当前 zoom 反推）。"""
    stage.scroll_into_view_if_needed()
    box = stage.bounding_box()
    if not box:
        fail("找不到画布 Stage")
    client_x = box["x"] + box["width"] / 2
    client_y = box["y"] + box["height"] / 2
    for _ in range(8):
        current = int(
            page.locator(".tab-window.is-active [data-viewport-zoom]")
            .inner_text()
            .strip()
            .rstrip("%")
        )
        if current == target_percent:
            page.wait_for_timeout(120)
            return
        # usePanZoom: factor = exp(-deltaY * 0.0015)，反解 deltaY。
        delta = -math.log(target_percent / current) / 0.0015
        page.evaluate(
            """([x, y, deltaY]) => {
                const stage = document.querySelector(
                    '.tab-window.is-active .canvas-stage'
                )
                stage.dispatchEvent(
                    new WheelEvent('wheel', {
                        deltaY,
                        clientX: x,
                        clientY: y,
                        bubbles: true,
                        cancelable: true,
                    })
                )
            }""",
            [client_x, client_y, delta],
        )
        page.wait_for_timeout(120)
    final = page.locator(".tab-window.is-active [data-viewport-zoom]").inner_text()
    fail(f"无法缩放到 {target_percent}%，当前 {final}")


def metrics(page) -> dict:
    return page.evaluate("""() => {
            const active = document.querySelector('.tab-window.is-active')
            const viewport = active.querySelector('.canvas-viewport')
            const popover = active.querySelector('.canvas-popover')
            const ellipse = active.querySelector('.annotation-ellipse')
            const input = popover.querySelector('.popover-row input')
            const head = popover.querySelector('.popover-head')
            const vp = viewport.getBoundingClientRect()
            const pp = popover.getBoundingClientRect()
            const ip = input.getBoundingClientRect()
            const hp = head.getBoundingClientRect()
            const ep = ellipse.getBoundingClientRect()
            const cs = getComputedStyle(popover)
            const inputCs = getComputedStyle(input)
            return {
                viewportWidth: vp.width,
                viewportHeight: vp.height,
                viewportLeft: vp.left,
                viewportTop: vp.top,
                viewportScale: vp.width / 1280,
                popoverWidth: pp.width,
                popoverHeight: pp.height,
                popoverLeft: pp.left - vp.left,
                popoverTop: pp.top - vp.top,
                ellipseRight: ep.right - vp.left,
                ellipseTop: ep.top - vp.top,
                ellipseHeight: ep.height,
                inputWidth: ip.width,
                inputHeight: ip.height,
                headHeight: hp.height,
                popoverTransform: cs.transform,
                popoverRadius: cs.borderRadius,
                popoverZ: parseInt(cs.zIndex, 10),
                ellipseZ: parseInt(getComputedStyle(ellipse).zIndex, 10),
                inputFontSize: inputCs.fontSize,
                zoomLabel: active.querySelector('[data-viewport-zoom]').textContent,
            }
        }""")


def main() -> int:
    sandbox_id = ""
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1600, "height": 1000})
            page = context.new_page()

            print("== 1. 登录并圈选 CTA ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            expect(page.locator(".workspace-tab")).to_have_count(1)
            active = page.locator(".tab-window.is-active")
            active.locator(".model-picker select").select_option("mock")
            draw_around(page, active.locator(".mock-cta"))
            popover = active.locator(".canvas-popover")
            expect(popover).to_have_count(1)
            stage = active.locator(".canvas-stage")

            samples = {}
            for percent in (100, 50, 150, 100):
                print(f"\n== 缩放到 {percent}% ==")
                set_zoom(page, stage, percent)
                page.wait_for_timeout(200)
                data = metrics(page)
                print(
                    f"   zoom={data['zoomLabel']} scale={data['viewportScale']:.4f} "
                    f"popover={data['popoverWidth']:.2f}×{data['popoverHeight']:.2f} "
                    f"input={data['inputWidth']:.2f}×{data['inputHeight']:.2f} "
                    f"transform={data['popoverTransform']} radius={data['popoverRadius']}"
                )
                if data["zoomLabel"] != f"{percent}%":
                    fail(f"缩放标签不是 {percent}%: {data['zoomLabel']}")
                if data["popoverTransform"] != "none":
                    fail(
                        f"浮层仍有独立 transform（反向缩放未移除）: {data['popoverTransform']}"
                    )

                # 1) 浮层宽度 = 320 世界单位 × 画布缩放。
                expected_width = POPOVER_WIDTH * data["viewportScale"]
                if abs(data["popoverWidth"] - expected_width) > 0.5:
                    fail(
                        f"浮层宽度未跟随缩放: {data['popoverWidth']:.2f} "
                        f"!= {expected_width:.2f}"
                    )
                # 2) 浮层与圈线间距 = 14 世界单位 × 画布缩放。
                gap = data["popoverLeft"] - data["ellipseRight"]
                expected_gap = POPOVER_GAP * data["viewportScale"]
                if data["viewportScale"] > 0.1 and abs(gap - expected_gap) > 1.0:
                    fail(f"浮层与圈线间距异常: {gap:.2f} != {expected_gap:.2f}")
                # 3) 浮层内部宽高比恒定（无拉扯）。
                samples[percent] = data
                if data["popoverZ"] <= data["ellipseZ"]:
                    fail(
                        f"浮层层级低于圈线: popover={data['popoverZ']} "
                        f"ellipse={data['ellipseZ']}"
                    )
                if data["popoverRadius"] != "14px":
                    fail(f"浮层圆角世界值被改写: {data['popoverRadius']}")

                page.screenshot(
                    path=str(SCREENSHOT_DIR / f"m1-popover-scale-{percent}.png"),
                    full_page=True,
                )

            # 4) 对比不同缩放下的内部比例：任意两档之间应完全一致。
            print("\n== 校验等比缩放比例 ==")
            normalized = {}
            for percent, data in samples.items():
                normalized[percent] = {
                    "input_h_over_popover_w": data["inputHeight"]
                    / data["popoverWidth"],
                    "popover_h_over_w": data["popoverHeight"] / data["popoverWidth"],
                    "head_h_over_w": data["headHeight"] / data["popoverWidth"],
                }
                print(
                    f"   {percent}%: input/popover={normalized[percent]['input_h_over_popover_w']:.5f} "
                    f"popover h/w={normalized[percent]['popover_h_over_w']:.5f} "
                    f"head/popover={normalized[percent]['head_h_over_w']:.5f}"
                )
            baseline = normalized[100]
            for percent in (50, 150):
                for key, value in normalized[percent].items():
                    if abs(value - baseline[key]) > 0.002:
                        fail(
                            f"{percent}% 下 {key} 发生畸变: {value:.5f} "
                            f"vs {baseline[key]:.5f}"
                        )

            # 5) 平移后浮层锚点随画布同步（世界坐标偏移不变）。
            before = metrics(page)
            # 中键平移不依赖焦点，避免 Space 被输入框吞掉。
            page.mouse.move(800, 500)
            page.mouse.down(button="middle")
            page.mouse.move(700, 420, steps=6)
            page.mouse.up(button="middle")
            page.wait_for_timeout(180)
            after = metrics(page)
            viewport_dx = after["viewportLeft"] - before["viewportLeft"]
            viewport_dy = after["viewportTop"] - before["viewportTop"]
            if abs(viewport_dx) < 20 or abs(viewport_dy) < 20:
                fail(f"平移手势未生效: dx={viewport_dx:.2f} dy={viewport_dy:.2f}")
            # popoverLeft/Top 是相对 .canvas-viewport 的世界偏移；
            # 平移后保持不变，说明浮层完整继承了画布 transform。
            rel_dx = after["popoverLeft"] - before["popoverLeft"]
            rel_dy = after["popoverTop"] - before["popoverTop"]
            if abs(rel_dx) > 0.5 or abs(rel_dy) > 0.5:
                fail(
                    "浮层未跟随画布平移: "
                    f"相对画布偏移变化=({rel_dx:.2f},{rel_dy:.2f})"
                )
            print(
                f"   画布平移 {viewport_dx:.0f}×{viewport_dy:.0f}px，"
                "浮层相对画布偏移保持 0"
            )

            context.close()
            browser.close()
        print("\nPOPOVER EQUAL-SCALE E2E: PASS")
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
            except Exception as exc:  # noqa: BLE001
                print(f"   cleanup {sandbox_id} -> {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
