#!/usr/bin/env python3
"""Phase 4 真实浏览器 E2E：对比/展示模式 → 缩放预览 → 沙盒销毁 → 5 Tab 上限。

覆盖点：
    - 未登录跳转 / 一键登录 / 账号密码登录
    - 多 Tab 新建、拖拽排序、localStorage 持久化
    - 对比模式：画布/预览单屏切换；展示模式：左右双屏同显
    - 两种模式往返时组件零卸载，画布标注与 iframe / HMR 状态不丢失
    - 预览 iframe 等比缩放居中、外层无滚动条、iframe body 隐藏滚动条
    - Agent 完成后「查看效果」快捷切换
    - 沙盒被外部销毁时预览显示连接失败，并可一键重新执行
    - 关闭 Tab = 删除项目 + 销毁 Docker 沙盒（通过 BFF 调用 Python internal API）
    - 最多 5 个 Tab；新建项目按存活数量递增命名且不重名

前置：
    pip install playwright && playwright install chromium
    docker compose up -d

运行：
    python3 scripts/ui_e2e_test.py

产出：
    docs/screenshots/phase4-workspace.png
    docs/screenshots/phase4-canvas-mode.png
    docs/screenshots/phase4-loading.png
    docs/screenshots/phase4-preview-mode.png
    docs/screenshots/phase4-dual-mode.png
    docs/screenshots/phase4-preview-offline.png
    docs/screenshots/phase4-tab-limit.png
    docs/screenshots/phase4-account.png
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
WEB_URL = "http://127.0.0.1:5173/"
PY_AI_URL = "http://127.0.0.1:8100"


def fail(message: str) -> None:
    print(f"\nFAIL: {message}")
    sys.exit(1)


def cleanup_sandbox(sandbox_id: str) -> None:
    if not sandbox_id:
        return
    request = urllib.request.Request(
        f"{PY_AI_URL}/api/v1/internal/sandbox/{sandbox_id}", method="DELETE"
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            print(f"   cleanup {sandbox_id} -> HTTP {response.status}")
    except Exception as exc:  # noqa: BLE001 - 清理失败不应掩盖主流程结果
        print(f"   cleanup {sandbox_id} -> {exc}")


def sandbox_container_exists(sandbox_id: str) -> bool:
    """通过 Docker CLI 确认容器是否仍存在。"""
    result = subprocess.run(
        ["docker", "inspect", f"vibe-sandbox-{sandbox_id}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def wait_sandbox_removed(sandbox_id: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not sandbox_container_exists(sandbox_id):
            return
        time.sleep(0.5)
    fail(f"沙盒容器未被销毁: vibe-sandbox-{sandbox_id}")


def draw_around(page, locator) -> None:
    box = locator.bounding_box()
    if not box:
        fail("找不到按钮位置")
    page.mouse.move(box["x"] - 26, box["y"] - 18)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=8)
    page.mouse.move(
        box["x"] + box["width"] + 26, box["y"] + box["height"] + 18, steps=8
    )
    page.mouse.up()


def drag_tab(page, source_name: str, target_name: str) -> None:
    """用真实鼠标事件拖拽 Tab。超过 6px 后 @dnd-kit 才会激活排序。"""
    source = page.locator(".workspace-tab", has_text=source_name).bounding_box()
    target = page.locator(".workspace-tab", has_text=target_name).bounding_box()
    if not source or not target:
        fail(f"找不到可拖拽的 Tab: {source_name} / {target_name}")

    start_x = source["x"] + source["width"] / 2
    start_y = source["y"] + source["height"] / 2
    end_x = target["x"] + target["width"] / 2
    end_y = target["y"] + target["height"] / 2

    page.mouse.move(start_x, start_y)
    page.mouse.down()
    page.mouse.move(start_x + 12, start_y, steps=4)
    page.mouse.move(end_x, end_y, steps=18)
    page.mouse.up()
    page.wait_for_timeout(450)


def open_new_project(page) -> None:
    page.locator(".project-switcher-button").click()
    page.get_by_role("menuitem", name=re.compile("新建项目")).click()
    page.wait_for_timeout(250)


def assert_same_box(before: dict, after: dict, label: str) -> None:
    for key in ("width", "height"):
        if abs(before[key] - after[key]) > 1:
            fail(f"{label} 视图尺寸发生变化: {before} -> {after}")


def assert_no_outer_scroll(active, label: str) -> None:
    """外层预览容器必须 overflow:hidden，且自身不能产生滚动距离。"""
    for selector in (".project-viewport", ".runtime-stage", ".canvas-stage"):
        metrics = active.locator(selector).evaluate("""el => {
                const style = getComputedStyle(el);
                return {
                    overflowX: style.overflowX,
                    overflowY: style.overflowY,
                    scrollWidth: el.scrollWidth,
                    clientWidth: el.clientWidth,
                    scrollHeight: el.scrollHeight,
                    clientHeight: el.clientHeight,
                };
            }""")
        if metrics["overflowX"] != "hidden" or metrics["overflowY"] != "hidden":
            fail(f"{label} {selector} 未设置 overflow:hidden: {metrics}")
        # cover 缩放会故意让画布 viewport 超出舞台，超出部分由 overflow:hidden
        # 裁切，因此 canvas-stage 的 scrollWidth 可以大于 clientWidth，但仍然
        # 不可滚动；其余外层容器必须完全没有滚动距离。
        if selector == ".canvas-stage":
            continue
        if metrics["scrollWidth"] > metrics["clientWidth"] + 1:
            fail(f"{label} {selector} 出现横向滚动: {metrics}")
        if metrics["scrollHeight"] > metrics["clientHeight"] + 1:
            fail(f"{label} {selector} 出现纵向滚动: {metrics}")


def assert_scaled_preview(active, label: str) -> None:
    """iframe 必须以 1280×720 为设计视口，cover 缩放后完全覆盖预览屏幕。

    cover 允许画面在某一方向超出容器（多余部分由外层 overflow:hidden 裁切），
    但必须满足：宽高都不小于屏幕、居中、且至少一个方向严丝合缝，不能留黑边。
    """
    metrics = active.locator(".runtime-preview-screen").evaluate("""el => {
            const frame = el.querySelector('.runtime-preview-frame');
            const screen = el.getBoundingClientRect();
            const box = frame.getBoundingClientRect();
            const style = getComputedStyle(frame);
            return {
                screen: {x: screen.x, y: screen.y, width: screen.width, height: screen.height},
                frame: {x: box.x, y: box.y, width: box.width, height: box.height},
                transform: style.transform,
                transformOrigin: style.transformOrigin,
                frameConnected: frame?.isConnected ?? false,
            };
        }""")
    if metrics["transform"] in ("none", ""):
        fail(f"{label} 未应用 transform scale: {metrics}")
    if not metrics["frameConnected"]:
        fail(f"{label} iframe 已被卸载")
    if metrics["transformOrigin"] not in ("0px 0px", "left top", "0 0"):
        fail(f"{label} transform-origin 不是 top left: {metrics}")

    screen = metrics["screen"]
    frame = metrics["frame"]
    if frame["width"] + 1 < screen["width"]:
        fail(f"{label} iframe 缩放后未覆盖宽度: {metrics}")
    if frame["height"] + 1 < screen["height"]:
        fail(f"{label} iframe 缩放后未覆盖高度: {metrics}")
    # 至少一个方向必须刚好等于屏幕尺寸，否则说明缩放比例算错、出现留白。
    if (
        abs(frame["width"] - screen["width"]) > 2
        and abs(frame["height"] - screen["height"]) > 2
    ):
        fail(f"{label} iframe 两个方向都未对齐屏幕: {metrics}")

    frame_center_x = frame["x"] + frame["width"] / 2
    screen_center_x = screen["x"] + screen["width"] / 2
    frame_center_y = frame["y"] + frame["height"] / 2
    screen_center_y = screen["y"] + screen["height"] / 2
    if abs(frame_center_x - screen_center_x) > 1:
        fail(f"{label} iframe 未在预览屏幕内水平居中: {metrics}")
    if abs(frame_center_y - screen_center_y) > 1:
        fail(f"{label} iframe 未在预览屏幕内垂直居中: {metrics}")


def assert_pane_stack(active, visible: str, hidden: str, label: str) -> None:
    """隐藏面板必须 visibility:hidden 且不接收指针事件，防止 iframe 挡住画布。"""
    visible_style = active.locator(visible).evaluate("""el => {
            const style = getComputedStyle(el);
            return {visibility: style.visibility, pointerEvents: style.pointerEvents, zIndex: style.zIndex};
        }""")
    hidden_style = active.locator(hidden).evaluate("""el => {
            const style = getComputedStyle(el);
            return {visibility: style.visibility, pointerEvents: style.pointerEvents, zIndex: style.zIndex};
        }""")
    if (
        visible_style["visibility"] != "visible"
        or visible_style["pointerEvents"] == "none"
    ):
        fail(f"{label} 当前面板不可交互: {visible_style}")
    if (
        hidden_style["visibility"] != "hidden"
        or hidden_style["pointerEvents"] != "none"
    ):
        fail(f"{label} 隐藏面板仍可能截获鼠标事件: {hidden_style}")
    if int(hidden_style["zIndex"]) >= int(visible_style["zIndex"]):
        fail(
            f"{label} 隐藏面板 z-index 未低于当前面板: {hidden_style} / {visible_style}"
        )
    print(
        f"   显隐层级: {label} visible={visible_style['visibility']} / hidden={hidden_style['visibility']}"
    )


def assert_canvas_preview_parity(page, active, label: str) -> None:
    """画布 Mock 页面与 iframe 内真实页面必须处于同一视觉尺寸与位置。"""
    canvas_card = active.locator(".mock-card").bounding_box()
    vibe_card = (
        page.frame_locator(".tab-window.is-active .runtime-preview-frame")
        .locator(".vibe-card")
        .bounding_box()
    )
    if not canvas_card or not vibe_card:
        fail(f"{label} 未读取到画布/预览卡片尺寸")

    for key in ("width", "height"):
        if abs(canvas_card[key] - vibe_card[key]) > 2:
            fail(
                f"{label} 画布与预览卡片 {key} 不一致: "
                f"{canvas_card[key]} vs {vibe_card[key]}"
            )

    canvas_stage = active.locator(".canvas-stage").bounding_box()
    preview_screen = active.locator(".runtime-preview-screen").bounding_box()
    if not canvas_stage or not preview_screen:
        fail(f"{label} 未读取到画布/预览屏幕尺寸")

    # 舞台必须同宽同高，否则卡片即使等尺寸也会整体偏移。
    for key in ("width", "height"):
        if abs(canvas_stage[key] - preview_screen[key]) > 2:
            fail(
                f"{label} 画布与预览屏幕 {key} 不一致: "
                f"{canvas_stage[key]:.1f} vs {preview_screen[key]:.1f}"
            )

    # 卡片相对各自屏幕的左上角偏移和垂直中心都必须一致，而不只是宽高一致。
    canvas_left = canvas_card["x"] - canvas_stage["x"]
    preview_left = vibe_card["x"] - preview_screen["x"]
    canvas_top = canvas_card["y"] - canvas_stage["y"]
    preview_top = vibe_card["y"] - preview_screen["y"]
    canvas_center = canvas_top + canvas_card["height"] / 2
    preview_center = preview_top + vibe_card["height"] / 2

    for name, left_a, left_b in (
        ("水平偏移", canvas_left, preview_left),
        ("垂直偏移", canvas_top, preview_top),
        ("垂直中心", canvas_center, preview_center),
    ):
        if abs(left_a - left_b) > 2:
            fail(
                f"{label} 画布与预览卡片{name}不一致: " f"{left_a:.1f} vs {left_b:.1f}"
            )

    print(
        f"   卡片对齐: {canvas_card['width']:.1f}×{canvas_card['height']:.1f} / "
        f"offset ({canvas_left:.1f},{canvas_top:.1f}) vs "
        f"({preview_left:.1f},{preview_top:.1f}) / "
        f"centerY {canvas_center:.1f} vs {preview_center:.1f}"
    )


def main() -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    sandbox_id = ""

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1680, "height": 1050})

        console_errors: list[str] = []
        # 失联演练会故意制造 ERR_CONNECTION_REFUSED / 旧沙盒 404，这段时间不收集。
        console_state = {"suppress": False}
        page.on(
            "console",
            lambda message: (
                console_errors.append(message.text)
                if message.type == "error" and not console_state["suppress"]
                else None
            ),
        )

        print("== 1. 未登录访问工作台应跳转登录页 ==")
        page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
        expect(page).to_have_url(re.compile(r"/login$"))
        expect(page.locator(".auth-card")).to_be_visible()

        print("\n== 2. 一键登录并进入工作台 ==")
        page.get_by_role("button", name="一键体验（推荐）").click()
        expect(page).to_have_url(re.compile(r"/workspace$"))
        expect(page.locator(".workspace-tab")).to_have_count(1)
        expect(page.locator(".tab-window.is-active .canvas-stage")).to_be_visible()
        session = page.evaluate("window.localStorage.getItem('vibe.auth.session')")
        if not session:
            fail("Token 未写入 localStorage")
        print("   登录成功，会话与工作区状态已写入 localStorage")

        print("\n== 3. 新建项目、拖拽排序、刷新持久化 ==")
        open_new_project(page)
        expect(page.locator(".workspace-tab")).to_have_count(2)
        expect(page.locator(".tab-window.is-active .tab-window-title h2")).to_have_text(
            "未命名项目 2"
        )
        page.screenshot(
            path=str(SCREENSHOT_DIR / "phase4-workspace.png"), full_page=True
        )

        drag_tab(page, "演示项目", "未命名项目 2")
        expect(page.locator(".workspace-tab .tab-name")).to_have_text(
            ["未命名项目 2", "演示项目"]
        )
        page.reload(wait_until="networkidle")
        expect(page.locator(".workspace-tab .tab-name")).to_have_text(
            ["未命名项目 2", "演示项目"]
        )
        print("   拖拽排序与刷新持久化正常")

        print("\n== 4. 单视口：画布模式圈选并发送意图 ==")
        page.locator(".workspace-tab", has_text="演示项目").click()
        active = page.locator(".tab-window.is-active")
        expect(active.locator(".canvas-pane.is-visible")).to_be_visible()
        expect(active.locator(".preview-pane.is-visible")).to_have_count(0)
        canvas_box = active.locator(".project-viewport").bounding_box()

        draw_around(page, active.locator(".mock-cta"))
        popover = active.locator(".canvas-popover")
        expect(popover).to_be_visible()
        page.wait_for_timeout(300)
        page.screenshot(
            path=str(SCREENSHOT_DIR / "phase4-canvas-mode.png"), full_page=True
        )

        popover.locator("input").fill("改颜色")
        popover.locator("input").press("Enter")

        print("\n== 5. Agent 执行完成后仍停留在画布模式 ==")
        # 执行中进度面板位于隐藏的预览视口，画布模式下通过状态点与顶栏提示反馈。
        expect(active.locator(".seg-status-running")).to_be_visible(timeout=10_000)
        expect(page.locator(".chip-live")).to_be_visible()
        page.wait_for_timeout(900)
        page.screenshot(path=str(SCREENSHOT_DIR / "phase4-loading.png"), full_page=True)

        expect(active.locator(".view-result-cta")).to_be_visible(timeout=120_000)
        expect(active.locator(".canvas-pane.is-visible")).to_be_visible()
        expect(active.locator(".runtime-preview-frame")).to_be_hidden()

        # 预览面板在对比模式画布下是 visibility:hidden，inner_text 会返回空；
        # 这里只需要校验 DOM 内容，使用 text_content 不受可见性影响。
        retry_text = (
            active.locator(".runtime-result-overlay .elapsed").text_content() or ""
        )
        if "自动修复 1 次" not in retry_text:
            fail(f"重试信息异常: {retry_text}")
        sandbox_id = active.locator(".runtime-panel").get_attribute("data-sandbox-id")
        if not sandbox_id:
            fail("未读取到 sandboxId")
        print(f"   沙盒: {sandbox_id} / {retry_text}")

        print("\n== 6. 点击「查看效果」切换到全屏预览 ==")
        active.locator(".view-result-cta").click()
        expect(active.locator(".preview-pane.is-visible")).to_be_visible()
        expect(active.locator(".runtime-preview-frame")).to_be_visible()
        expect(active.locator(".status-pill.is-success")).to_be_visible()
        preview_box = active.locator(".project-viewport").bounding_box()
        assert_same_box(canvas_box, preview_box, "画布 → 预览")

        frame = page.frame_locator(".tab-window.is-active .runtime-preview-frame")
        cta = frame.locator("button")
        expect(cta).to_be_visible(timeout=30_000)
        background = cta.evaluate("el => getComputedStyle(el).backgroundColor")
        if background != "rgb(255, 0, 0)":
            fail(f"按钮不是红色: {background}")

        page.wait_for_timeout(300)
        assert_scaled_preview(active, "对比模式预览")
        assert_canvas_preview_parity(page, active, "对比模式预览")
        page.screenshot(
            path=str(SCREENSHOT_DIR / "phase4-preview-mode.png"), full_page=True
        )

        print("\n== 7. 对比/展示模式往返：组件不卸载、尺寸严格对齐 ==")
        # 在 DOM 节点上打标记，任何一次切换若导致 React 重建节点都会被发现。
        page.evaluate("""() => {
                const windowNode = document.querySelector('.tab-window.is-active');
                window.__vibeNodes = {
                    canvas: windowNode.querySelector('.canvas-stage'),
                    frame: windowNode.querySelector('.runtime-preview-frame'),
                };
            }""")

        active.get_by_role("button", name="展示模式").click()
        expect(active.locator(".canvas-pane.is-visible")).to_be_visible()
        expect(active.locator(".preview-pane.is-visible")).to_be_visible()
        expect(active.locator(".pane-switch")).to_have_count(0)

        canvas_frame_box = active.locator(".canvas-frame").bounding_box()
        preview_panel_box = active.locator(".runtime-panel").bounding_box()
        assert_same_box(canvas_frame_box, preview_panel_box, "展示模式画布/预览")
        page.wait_for_timeout(300)
        assert_no_outer_scroll(active, "展示模式")
        assert_scaled_preview(active, "展示模式")

        identity = page.evaluate("""() => {
                const windowNode = document.querySelector('.tab-window.is-active');
                return {
                    canvas: windowNode.querySelector('.canvas-stage') === window.__vibeNodes.canvas,
                    frame: windowNode.querySelector('.runtime-preview-frame') === window.__vibeNodes.frame,
                };
            }""")
        if not identity["canvas"] or not identity["frame"]:
            fail(f"视图模式切换导致组件被重新挂载: {identity}")

        frame = page.frame_locator(".tab-window.is-active .runtime-preview-frame")
        expect(frame.locator("button")).to_have_css(
            "background-color", "rgb(255, 0, 0)"
        )
        body_style = frame.locator("body").evaluate("""el => {
                const style = getComputedStyle(el);
                return {overflowX: style.overflowX, overflowY: style.overflowY};
            }""")
        if body_style["overflowX"] != "hidden" or body_style["overflowY"] != "hidden":
            fail(f"iframe body 仍可能出现原生滚动条: {body_style}")
        assert_canvas_preview_parity(page, active, "展示模式")
        page.screenshot(
            path=str(SCREENSHOT_DIR / "phase4-dual-mode.png"), full_page=True
        )

        # Diff / Run 详情以抽屉覆盖，不挤压预览画面。
        active.get_by_role("button", name="详情 / 代码 Diff").click()
        expect(active.locator(".runtime-drawer")).to_be_visible()
        expect(active.locator(".runtime-meta code").nth(3)).to_have_text(sandbox_id)
        expect(active.locator(".diff-view")).to_contain_text("backgroundColor")
        active.locator(".runtime-drawer").get_by_role("button", name="关闭").click()
        expect(active.locator(".runtime-drawer")).to_have_count(0)

        active.get_by_role("button", name="对比模式").click()
        expect(active.locator(".pane-switch")).to_be_visible()
        expect(active.locator(".preview-pane.is-visible")).to_be_visible()
        expect(active.locator(".canvas-pane.is-visible")).to_have_count(0)
        assert_same_box(
            canvas_box,
            active.locator(".project-viewport").bounding_box(),
            "展示 → 对比",
        )

        active.get_by_role("button", name="画布", exact=True).click()
        expect(active.locator(".canvas-pane.is-visible")).to_be_visible()
        expect(active.locator(".annotation-ellipse")).to_be_visible()
        assert_pane_stack(active, ".canvas-pane", ".preview-pane", "画布模式")
        canvas_box_again = active.locator(".project-viewport").bounding_box()
        assert_same_box(canvas_box, canvas_box_again, "预览 → 画布")

        active.get_by_role("button", name="预览", exact=True).click()
        expect(active.locator(".runtime-preview-frame")).to_be_visible()
        assert_pane_stack(active, ".preview-pane", ".canvas-pane", "预览模式")
        assert_same_box(
            canvas_box, active.locator(".project-viewport").bounding_box(), "再次切换"
        )
        page.wait_for_timeout(300)
        assert_no_outer_scroll(active, "对比模式")
        assert_scaled_preview(active, "对比模式")
        frame = page.frame_locator(".tab-window.is-active .runtime-preview-frame")
        expect(frame.locator("button")).to_have_css(
            "background-color", "rgb(255, 0, 0)"
        )
        print("   两种模式/画布预览往返均无重挂载，圈选与 iframe 状态保留")

        print("\n== 8. 刷新页面：恢复预览结果，默认回到画布模式 ==")
        page.reload(wait_until="networkidle")
        active = page.locator(".tab-window.is-active")
        expect(active.locator(".tab-window-title h2")).to_have_text("演示项目")
        expect(active.locator(".canvas-pane.is-visible")).to_be_visible()
        expect(active.locator(".pane-switch")).to_be_visible()
        expect(active.locator(".view-result-cta")).to_be_visible()
        expect(active.locator(".runtime-panel")).to_have_attribute(
            "data-sandbox-id", sandbox_id
        )
        active.locator(".view-result-cta").click()
        frame = page.frame_locator(".tab-window.is-active .runtime-preview-frame")
        expect(frame.locator("button")).to_have_css(
            "background-color", "rgb(255, 0, 0)"
        )
        print("   刷新后预览与沙盒信息均恢复，关闭项目仍可追踪沙盒")

        print("\n== 9. 沙盒被外部销毁：预览显示连接失败并可重新执行 ==")
        console_state["suppress"] = True
        removed = subprocess.run(
            ["docker", "rm", "-f", f"vibe-sandbox-{sandbox_id}"],
            capture_output=True,
            text=True,
        )
        if removed.returncode != 0:
            fail(f"无法模拟沙盒失联: {removed.stderr.strip()}")

        active.get_by_role("button", name="画布", exact=True).click()
        expect(active.locator(".canvas-pane.is-visible")).to_be_visible()
        active.get_by_role("button", name="预览", exact=True).click()
        offline = active.locator(".runtime-error")
        expect(offline).to_be_visible(timeout=15_000)
        expect(offline).to_contain_text("预览连接失败")
        retry = offline.get_by_role("button", name="重新执行 Agent")
        expect(retry).to_be_visible()
        page.screenshot(
            path=str(SCREENSHOT_DIR / "phase4-preview-offline.png"), full_page=True
        )

        retry.click()
        expect(active.locator(".runtime-result-overlay")).to_be_visible(timeout=120_000)
        recreated_sandbox_id = active.locator(".runtime-panel").get_attribute(
            "data-sandbox-id"
        )
        if not recreated_sandbox_id or recreated_sandbox_id == sandbox_id:
            fail(f"重新执行后未创建新沙盒: {recreated_sandbox_id}")
        sandbox_id = recreated_sandbox_id
        frame = page.frame_locator(".tab-window.is-active .runtime-preview-frame")
        expect(frame.locator("button")).to_have_css(
            "background-color", "rgb(255, 0, 0)"
        )
        print(f"   失联提示正确，重新执行后新沙盒: {sandbox_id}")

        print("\n== 10. 关闭项目：删除状态并销毁沙盒容器 ==")
        page.locator(".workspace-tab.is-active .tab-close").click()
        expect(page.locator(".workspace-tab")).to_have_count(1)
        page.locator(".project-switcher-button").click()
        expect(page.get_by_role("menuitem", name=re.compile("演示项目"))).to_have_count(
            0
        )
        page.keyboard.press("Escape")
        wait_sandbox_removed(sandbox_id)
        print(f"   项目已删除，容器 vibe-sandbox-{sandbox_id} 已销毁")
        console_state["suppress"] = False

        print("\n== 11. 同时打开 5 个项目，触发上限 ==")
        for _ in range(4):
            open_new_project(page)
        expect(page.locator(".workspace-tab")).to_have_count(5)
        expect(page.locator(".tab-window.is-active .tab-window-title h2")).to_have_text(
            "未命名项目 6"
        )
        expect(page.locator(".tab-new")).to_be_disabled()
        expect(page.locator(".workspace-limit")).to_contain_text(
            "最多只能同时打开 5 个项目"
        )

        page.locator(".project-switcher-button").click()
        expect(
            page.get_by_role("menuitem", name=re.compile("新建项目"))
        ).to_be_disabled()
        expect(page.locator(".project-menu-hint")).to_contain_text("请关闭其他项目")
        page.wait_for_timeout(250)
        page.screenshot(
            path=str(SCREENSHOT_DIR / "phase4-tab-limit.png"), full_page=True
        )
        page.keyboard.press("Escape")
        print("   5 个 Tab 时新建入口已禁用并展示上限提示")

        print("\n== 12. 关闭项目 2 后新建，按存活数量递增且不重名 ==")
        page.locator(".workspace-tab", has_text="未命名项目 2").locator(
            ".tab-close"
        ).click()
        expect(page.locator(".workspace-tab")).to_have_count(4)
        open_new_project(page)
        expect(page.locator(".workspace-tab")).to_have_count(5)
        # 存活 4 个 Tab → 从 5 起编号；5、6 已被占用，顺延到 7，绝不重名。
        expect(page.locator(".tab-window.is-active .tab-window-title h2")).to_have_text(
            "未命名项目 7"
        )
        names = page.locator(".workspace-tab .tab-name").all_inner_texts()
        if len(names) != len(set(names)):
            fail(f"项目名称出现重复: {names}")
        print("   关闭项目 2 后，新项目按存活数递增为「未命名项目 7」")

        print("\n== 13. 账户设置、退出与账号密码登录 ==")
        page.locator(".avatar-button").click()
        page.get_by_role("menuitem", name="账户设置").click()
        expect(page.locator(".account-dialog")).to_be_visible()
        expect(page.get_by_text("demo@vibe.local")).to_be_visible()
        page.screenshot(path=str(SCREENSHOT_DIR / "phase4-account.png"), full_page=True)
        page.get_by_role("button", name="完成").click()

        page.locator(".avatar-button").click()
        page.get_by_role("menuitem", name="退出登录").click()
        expect(page).to_have_url(re.compile(r"/login$"))
        page.locator("#login-account").fill("alice@example.com")
        page.locator("#login-password").fill("vibe-demo")
        page.get_by_role("button", name="登录并进入工作台").click()
        expect(page).to_have_url(re.compile(r"/workspace$"))
        expect(page.locator(".account-name")).to_have_text("alice")
        expect(page.locator(".workspace-tab")).to_have_count(5)
        print("   登录体系正常，工作区在重新登录后保持")

        if console_errors:
            print("\n   浏览器控制台错误:")
            for item in console_errors:
                print(f"   - {item}")
            fail("页面存在控制台错误")

        cleanup_sandbox(sandbox_id)
        browser.close()

    print(
        "\nPASS: 对比/展示模式 → 缩放预览 → 失联恢复 → 沙盒销毁 → 5 Tab 上限 → 账户体系 全流程通过"
    )


if __name__ == "__main__":
    main()
