#!/usr/bin/env python3
"""E2E：画布直接拖拽 → 静默 MOVE → Coder 写 CSS transform → 失败回滚。

覆盖用户要求的完整闭环：

  1. SDK 在 iframe 内接管左键手势，只改内联 ``translate``，不触发回流；
  2. 松手立即 Toast「位置已更新，Agent 正在同步代码...」；
  3. 500ms 防抖：连续拖拽只提交最后一次累计 dx/dy；
  4. 标准 AnnotationEnvelope（action=MOVE）直接派发给 ui_graph，
     后端确定性调用 ``move_component``，只追加
     ``transform: translate3d(...)``，禁止改宽高；
  5. Vite HMR 后位置生效，内联位移归零，画布焦点与拖拽模式保持不变；
  6. 第二次拖拽在已提交位移上继续累加；
  7. 调度失败时视觉回滚到上一次成功位置，并提示
     「代码同步失败，位置已还原」。

运行（需要本机 Playwright 与已启动的 5173 / 8080 / 8100）：

    playwright install chromium   # 首次；或先启动 9222 调试 Chromium
    python scripts/direct_drag_sync_e2e_test.py

说明：为了跳过 Architect 全栈生成，测试在有沙盒预览后把项目
``canvasMode`` 直接改为 ``live`` 并刷新。这是 E2E 测试夹具，不是产品逻辑。
"""

from __future__ import annotations

import json
import re
import urllib.request

from playwright.sync_api import Page, expect, sync_playwright

WEB_URL = "http://127.0.0.1:5173/"
PY_AI_URL = "http://127.0.0.1:8100"
CDP_URL = "http://127.0.0.1:9222"


def open_browser(playwright) -> tuple[object, bool]:
    """优先复用 9222 调试 Chromium，浏览器版本不必与 Playwright 匹配。

    返回 ``(browser, owned)``；只有自己 launch 的浏览器才需要在结束时关闭，
    避免顺手关掉开发者正在用的调试实例。
    """
    try:
        return playwright.chromium.connect_over_cdp(CDP_URL, timeout=5_000), False
    except Exception:  # noqa: BLE001 - 调试端口未开则回退到自带浏览器
        return playwright.chromium.launch(headless=True), True


def wait_run(active) -> None:
    """等待一次常规 UI Run 结束。"""
    trace = active.locator("[data-supervisor-trace]")
    expect(trace).to_be_visible(timeout=30_000)
    expect(trace).not_to_have_attribute(
        "data-supervisor-phase", "analyzing", timeout=240_000
    )
    expect(active.locator(".runtime-overlay")).to_have_count(0, timeout=240_000)
    expect(active.locator(".runtime-result-overlay .status-pill")).to_contain_text(
        "已完成", timeout=30_000
    )


def drag(page: Page, target, dx: float, dy: float, steps: int = 10) -> dict:
    """按屏幕坐标拖动 iframe 内元素；target 需支持 bounding_box()。"""
    box = target.bounding_box()
    if not box:
        raise AssertionError("目标组件不可见，无法拖拽")
    start_x = box["x"] + box["width"] / 2
    start_y = box["y"] + box["height"] / 2
    page.mouse.move(start_x, start_y)
    page.mouse.down()
    page.mouse.move(start_x + dx, start_y + dy, steps=steps)
    page.mouse.up()
    return box


def style_of(node) -> dict:
    """读取 iframe 内元素的 transform / 内联 translate / 视口矩形。"""
    return node.evaluate("""el => {
          const style = getComputedStyle(el);
          const rect = el.getBoundingClientRect();
          return {
            transform: style.transform,
            inlineTranslate: el.style.translate || '',
            x: rect.x, y: rect.y, width: rect.width, height: rect.height,
          };
        }""")


def matrix_offset(value: str) -> tuple[float, float]:
    """从 computed matrix / matrix3d 中取 translate 分量。"""
    match = re.match(r"matrix(?:3d)?\(([^)]+)\)", value or "")
    if not match:
        return (0.0, 0.0)
    parts = [float(item) for item in match.group(1).split(",")]
    if len(parts) == 16:
        return (parts[-4], parts[-3])
    return (parts[4], parts[5])


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


def main() -> int:
    with sync_playwright() as playwright:
        browser, owned = open_browser(playwright)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        # 无 DeepSeek Key 的确定性 Mock；MOVE 路径本身不调用 LLM。
        context.add_init_script(
            "window.localStorage.setItem('vibe.llmProvider', 'mock')"
        )
        page = context.new_page()
        page.set_default_timeout(30_000)
        dispatches: list[dict] = []
        page.on(
            "request",
            lambda request: (
                dispatches.append(request.post_data_json)
                if request.url.endswith("/api/v1/supervisor/dispatch")
                else None
            ),
        )
        sandbox_id: str | None = None
        try:
            print("== 1. 登录并生成一个真实沙盒页面 ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            active = page.locator(".tab-window.is-active")
            expect(active.locator(".mock-card")).to_be_visible()

            card = active.locator(".mock-card").bounding_box()
            if not card:
                raise AssertionError("找不到演示卡片")
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
            popover.locator("input").fill("添加一个提交按钮")
            popover.locator(".popover-send").click()
            wait_run(active)
            sandbox_id = active.locator(".runtime-panel").get_attribute(
                "data-sandbox-id"
            )
            print(f"   sandbox: {sandbox_id}")
            # 只统计拖拽产生的静默 dispatch，排除初始 CREATE_NEW。
            dispatches.clear()

            print("== 2. 切换 live 画布并进入拖拽模式 ==")
            page.evaluate("""() => {
                  const key = 'vibe.workspace.v1'
                  const state = JSON.parse(localStorage.getItem(key))
                  const project = state.projects.find(
                    item => item.id === state.activeTabId
                  )
                  project.canvasMode = 'live'
                  project.bootstrap = 'ready'
                  localStorage.setItem(key, JSON.stringify(state))
                }""")
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(2500)
            active = page.locator(".tab-window.is-active")
            toggle = active.locator("[data-canvas-drag-toggle]")
            expect(toggle).to_be_visible(timeout=30_000)
            toggle.click()
            stage = active.locator(".canvas-stage")
            expect(stage).to_have_attribute("data-canvas-drag", "on")

            frame = page.frame_locator(".tab-window.is-active iframe.canvas-live-frame")
            button = frame.locator('button[data-source="cta-button"]')
            expect(button).to_be_visible(timeout=30_000)
            expect(frame.locator("html")).to_have_class(re.compile("vibe-drag-enabled"))
            before = style_of(button)
            print(
                f"   进入拖拽模式，初始样式: {json.dumps(before, ensure_ascii=False)}"
            )

            print("== 3. 连续拖拽：500ms 防抖只提交最后一次 ==")
            drag(page, button, 40, 20, steps=8)
            page.wait_for_timeout(180)
            drag(page, button, 80, 40, steps=8)
            expect(page.locator(".workspace-toast.supervisor-toast")).to_contain_text(
                "位置已更新", timeout=10_000
            )
            expect(page.locator(".workspace-toast.supervisor-toast")).to_contain_text(
                "位置已同步到代码", timeout=180_000
            )
            if len(dispatches) != 1:
                raise AssertionError(f"防抖失败：dispatch 次数={len(dispatches)}")

            annotation = dispatches[0]["annotations"][0]
            move = annotation["payload"].get("move") or {}
            print(
                "   dispatch annotation: "
                f"{annotation['action']} {json.dumps(move, ensure_ascii=False)}"
            )
            if annotation["action"] != "MOVE":
                raise AssertionError("action 不是 MOVE")
            if abs(move.get("dx", 0) - 120) > 2 or abs(move.get("dy", 0) - 60) > 2:
                raise AssertionError(f"累计位移不正确: {move}")
            if not annotation.get("target", {}).get("selector"):
                raise AssertionError("MOVE 缺少 selector")

            page.wait_for_timeout(1200)
            after = style_of(button)
            tx, ty = matrix_offset(after["transform"])
            print(f"   HMR 后样式: {json.dumps(after, ensure_ascii=False)}")
            if abs(tx - 120) > 3 or abs(ty - 60) > 3:
                raise AssertionError(f"CSS transform 未生效: {after['transform']}")
            if after["inlineTranslate"] not in ("", "none"):
                raise AssertionError(
                    f"成功同步后内联位移未清理: {after['inlineTranslate']}"
                )
            expect(stage).to_have_attribute("data-canvas-drag", "on")
            expect(
                page.locator(".tab-window.is-active iframe.canvas-live-frame")
            ).to_be_visible()
            print("   拖拽模式与画布焦点在静默同步后保持")

            print("== 4. 第二次拖拽叠加累计位移 ==")
            expect(page.locator(".workspace-toast.supervisor-toast")).to_have_count(
                0, timeout=10_000
            )
            before_count = len(dispatches)
            drag(page, button, 30, 20, steps=8)
            page.wait_for_timeout(1200)
            if len(dispatches) != before_count + 1:
                raise AssertionError(
                    f"第二次拖拽未提交或重复提交: "
                    f"{before_count} -> {len(dispatches)}"
                )
            expect(page.locator(".workspace-toast.supervisor-toast")).to_contain_text(
                "位置已同步到代码", timeout=180_000
            )
            page.wait_for_timeout(1500)
            second = style_of(button)
            tx2, ty2 = matrix_offset(second["transform"])
            print(
                f"   第二次 HMR 后: {second['transform']} "
                f"inline={second['inlineTranslate']!r}"
            )
            if abs(tx2 - 150) > 3 or abs(ty2 - 80) > 3:
                raise AssertionError(f"累计 transform 不正确: {second['transform']}")
            move2 = dispatches[-1]["annotations"][0]["payload"].get("move") or {}
            if abs(move2.get("dx", 0) - 150) > 2 or abs(move2.get("dy", 0) - 80) > 2:
                raise AssertionError(f"第二次累计位移不正确: {move2}")

            print("== 5. 模拟调度失败：视觉回滚 + 错误提示 ==")
            expect(page.locator(".workspace-toast.supervisor-toast")).to_have_count(
                0, timeout=10_000
            )
            aborted = {"done": False}

            def abort_dispatch(route) -> None:
                if not aborted["done"]:
                    aborted["done"] = True
                    route.abort()
                    return
                route.continue_()

            page.route("**/api/v1/supervisor/dispatch", abort_dispatch)
            drag(page, button, 50, 50, steps=10)
            expect(page.locator(".workspace-toast.supervisor-toast")).to_contain_text(
                "代码同步失败，位置已还原", timeout=30_000
            )
            if not aborted["done"]:
                raise AssertionError("失败注入未生效")
            page.wait_for_timeout(800)
            rolled = style_of(button)
            tx3, ty3 = matrix_offset(rolled["transform"])
            print(
                f"   回滚后: {rolled['transform']} "
                f"inline={rolled['inlineTranslate']!r}"
            )
            if abs(tx3 - 150) > 3 or abs(ty3 - 80) > 3:
                raise AssertionError(
                    f"失败后未回到上一次成功位置: {rolled['transform']}"
                )
            if rolled["inlineTranslate"] not in ("", "none"):
                raise AssertionError(
                    f"失败后内联位移未还原: {rolled['inlineTranslate']}"
                )
            expect(stage).to_have_attribute("data-canvas-drag", "on")
            page.unroute("**/api/v1/supervisor/dispatch", abort_dispatch)

            print("\nALL DRAG E2E CHECKS PASSED")
        finally:
            cleanup_sandbox(sandbox_id)
            context.close()
            if owned:
                browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
