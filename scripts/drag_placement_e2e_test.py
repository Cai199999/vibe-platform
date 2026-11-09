#!/usr/bin/env python3
"""E2E：CREATE_NEW → 拖拽定位 → 保存门控 → 全栈澄清。

覆盖用户要求的主链路：
  1. 画圈新增组件，Planner / 前端动作统一为 CREATE_NEW；
  2. Mock Coder 只生成组件主体与 data-vibe-created 标记；
  3. 预览层通过 inspect SDK measure 找到新组件，出现可拖拽定位框；
  4. 拖动并确认后，以 MODIFY_EXISTING + payload.position 触发 Coder
     回写 position:absolute; left/top；
  5. 未保存时【下一步：构建全栈方案】置灰，点击仅 Toast“请先保存当前修改”；
  6. 保存快照后门控解除，成功进入 Architect 全栈澄清。

运行：
    /tmp/vibe-browser/bin/python scripts/drag_placement_e2e_test.py
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


def wait_run_completed(active) -> None:
    """等待一次 UI Run 完成。

    预览面板可能处于隐藏状态（对比模式仍停留在画布），不能用
    ``to_be_visible`` 判断 overlay；改为等待 Supervisor 结束思考、
    loading overlay 从 DOM 移除，并确认结果状态为“已完成”。
    """
    trace = active.locator("[data-supervisor-trace]")
    expect(trace).to_be_visible(timeout=30_000)
    expect(trace).not_to_have_attribute(
        "data-supervisor-phase", "analyzing", timeout=240_000
    )
    expect(active.locator(".runtime-overlay")).to_have_count(0, timeout=240_000)
    expect(active.locator(".runtime-result-overlay .status-pill")).to_contain_text(
        "已完成", timeout=30_000
    )


def main() -> int:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        # 无 DeepSeek Key 的确定性 Mock，让新增组件带 data-vibe-created。
        context.add_init_script(
            "window.localStorage.setItem('vibe.llmProvider', 'mock')"
        )
        page = context.new_page()
        sandbox_id: str | None = None
        # 记录进入 Architect 时的 dispatch 请求，验证全栈上下文确实带了
        # 项目摘要与本次快照改动，而不只是前端状态里有。
        architect_dispatches: list[dict] = []

        def capture_dispatch(request) -> None:
            if not request.url.endswith("/api/v1/supervisor/dispatch"):
                return
            try:
                body = request.post_data_json
            except Exception:  # noqa: BLE001 - 非 JSON 请求与用例无关
                return
            if body and "下一步" in str(body.get("message") or ""):
                architect_dispatches.append(body)

        page.on("request", capture_dispatch)

        try:
            print("== 1. 登录并进入演示项目 ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            expect(page.locator(".workspace-tab")).to_have_count(1)
            active = page.locator(".tab-window.is-active")
            expect(active.locator(".mock-card")).to_be_visible()

            print("\n== 2. 圈选空白区域并生成 CREATE_NEW 组件 ==")
            card = active.locator(".mock-card").bounding_box()
            if not card:
                fail("找不到大卡片")
            page.mouse.move(
                card["x"] + card["width"] * 0.08,
                card["y"] + card["height"] * 0.08,
            )
            page.mouse.down()
            page.mouse.move(
                card["x"] + card["width"] * 0.45,
                card["y"] + card["height"] * 0.35,
                steps=12,
            )
            page.mouse.up()

            popover = active.locator(".canvas-popover")
            expect(popover).to_have_count(1)
            popover.locator("input").fill("添加一个提交按钮")
            expect(popover).to_have_attribute("data-action", "CREATE_NEW")
            popover.locator(".popover-send").click()
            wait_run_completed(active)
            sandbox_id = active.locator(".runtime-panel").get_attribute(
                "data-sandbox-id"
            )
            print(f"   首次生成完成 sandbox={sandbox_id}")

            print("\n== 3. 预览中出现可拖拽定位框 ==")
            view_result = active.locator(".view-result-cta")
            if view_result.is_visible():
                view_result.click()
            expect(active.locator(".preview-pane.is-visible")).to_be_visible()
            frame = page.frame_locator(".tab-window.is-active .runtime-preview-frame")
            added = frame.locator("button.vibe-added-button")
            expect(added).to_be_visible(timeout=60_000)
            # CREATE_NEW 阶段：只生成主体，尚未写死 absolute 定位。
            initial_position = added.evaluate("el => getComputedStyle(el).position")
            if initial_position == "absolute":
                fail("CREATE_NEW 首次生成不应写死 absolute 定位")

            placement = active.locator("[data-placement-frame]")
            expect(placement).to_be_visible(timeout=60_000)
            box = placement.bounding_box()
            if not box:
                fail("定位框没有可拖拽区域")
            print(
                f"   定位框可见: x={box['x']:.1f} y={box['y']:.1f} "
                f"w={box['width']:.1f} h={box['height']:.1f}"
            )

            print("\n== 4. 拖动定位框并确认 ==")
            start_x = box["x"] + box["width"] / 2
            start_y = box["y"] + box["height"] / 2
            page.mouse.move(start_x, start_y)
            page.mouse.down()
            page.mouse.move(start_x + 180, start_y + 90, steps=18)
            page.mouse.up()
            page.wait_for_timeout(200)
            moved = placement.bounding_box()
            if not moved or abs(moved["x"] - box["x"]) < 20:
                fail("拖拽后定位框位置没有变化")
            placement.locator("[data-placement-confirm]").click()

            print("   已确认位置，等待 Coder 回写绝对定位")
            wait_run_completed(active)
            expect(active.locator("[data-placement-frame]")).to_have_count(
                0, timeout=30_000
            )
            added = frame.locator("button.vibe-added-button")
            expect(added).to_be_visible(timeout=60_000)
            # HMR 后等待 CSS Patch 生效。
            page.wait_for_timeout(800)
            patched = added.evaluate("""el => {
                    const style = getComputedStyle(el);
                    return {
                      position: style.position,
                      left: parseFloat(style.left || '0'),
                      top: parseFloat(style.top || '0'),
                    };
                }""")
            print(f"   回写后样式: {patched}")
            if patched["position"] != "absolute":
                fail(f"拖拽坐标没有回写 absolute: {patched}")
            if patched["left"] <= 0 or patched["top"] <= 0:
                fail(f"拖拽坐标 left/top 异常: {patched}")

            print("\n== 5. 未保存时下一步被门控 ==")
            next_button = active.get_by_role(
                "button", name=re.compile("下一步：构建全栈方案")
            )
            expect(next_button).to_have_attribute("aria-disabled", "true")
            # aria-disabled 会被 Playwright 视为 disabled；这里强制点击，
            # 验证产品保留点击后的 Toast 解释，而不是静默无响应。
            next_button.click(force=True)
            expect(page.locator(".workspace-toast.supervisor-toast")).to_contain_text(
                "请先保存当前修改", timeout=10_000
            )
            expect(page.locator(".architect-panel")).to_have_count(0)
            print("   门控生效：按钮置灰 + Toast 提示")

            page.screenshot(
                path=str(SCREENSHOT_DIR / "drag-placement-before-save.png"),
                full_page=True,
            )

            print("\n== 6. 保存快照后解除门控 ==")
            active.locator("[data-snapshot-save]").click()
            modal = page.locator(".snapshot-modal")
            expect(modal).to_be_visible()
            modal.locator("#snapshot-note").fill("CREATE_NEW 拖拽定位已完成")
            modal.locator('button[type="submit"]').click()
            expect(page.locator(".workspace-toast.supervisor-toast")).to_contain_text(
                "快照已保存", timeout=60_000
            )
            expect(modal).to_have_count(0)
            expect(next_button).to_have_attribute("aria-disabled", "false")

            print("\n== 7. 进入全栈澄清 ==")
            next_button.click()
            panel = page.locator(".architect-panel")
            expect(panel).to_be_visible(timeout=30_000)
            expect(panel.locator(".architect-clarification")).to_be_visible(
                timeout=60_000
            )
            if not architect_dispatches:
                fail("没有捕获到进入 Architect 的 dispatch 请求")
            context_payload = architect_dispatches[-1]
            summary = str(context_payload.get("projectSummary") or "")
            changes = str(context_payload.get("latestChanges") or "")
            if not summary:
                fail("Architect 请求缺少 projectSummary")
            if "拖拽定位" not in changes:
                fail(f"Architect 请求 latestChanges 未包含本次快照改动: {changes!r}")
            print(
                "   已成功进入 Architect 全栈澄清；"
                f"projectSummary={len(summary)} 字，latestChanges={changes!r}"
            )

            page.screenshot(
                path=str(SCREENSHOT_DIR / "drag-placement-architect.png"),
                full_page=True,
            )
            print("   screenshots -> docs/screenshots/drag-placement-*.png")
        finally:
            cleanup_sandbox(sandbox_id)
            context.close()
            browser.close()

    print("\nDRAG PLACEMENT + SAVE GATE E2E: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
