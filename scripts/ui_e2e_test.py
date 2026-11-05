#!/usr/bin/env python3
"""Phase 4 真实浏览器 E2E：对比/展示模式 → 缩放预览 → 沙盒销毁 → 5 Tab 上限。

覆盖点：
    - 未登录跳转 / 一键登录 / 账号密码登录
    - 多 Tab 新建、拖拽排序、localStorage 持久化
    - 对比模式：画布/预览单屏切换；展示模式：左右双屏同显
    - 两种模式往返时组件零卸载，画布标注与 iframe / HMR 状态不丢失
    - 预览 iframe 等比缩放居中、外层无滚动条、iframe body 隐藏滚动条
    - Agent 完成后「查看效果」快捷切换
    - 工具栏与项目下拉菜单导出 tar.gz，并校验这是可独立运行的 Vite 项目
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
    docs/screenshots/phase4-export.png
    docs/screenshots/phase4-dual-mode.png
    docs/screenshots/phase4-preview-offline.png
    docs/screenshots/phase4-tab-limit.png
    docs/screenshots/phase4-account.png
"""

from __future__ import annotations

import re
import subprocess
import sys
import tarfile
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


def assert_export_archive(download, label: str) -> None:
    """校验浏览器下载到的 tar.gz 是完整、可独立运行的 Vite 项目。"""
    if download.suggested_filename != "project-export.tar.gz":
        fail(f"{label} 文件名不正确: {download.suggested_filename}")
    path = download.path()
    if not path:
        fail(f"{label} 未拿到下载文件")

    with tarfile.open(path, "r:gz") as tar:
        names = set()
        for member in tar.getmembers():
            name = member.name[2:] if member.name.startswith("./") else member.name
            names.add(name)

        required = {
            "package.json",
            "package-lock.json",
            "vite.config.js",
            "index.html",
            "src/App.jsx",
            "src/main.jsx",
            "VIBE_CONTEXT.md",
        }
        missing = required - names
        if missing:
            fail(f"{label} 缺少文件: {sorted(missing)}")

        excluded = {"node_modules", ".git", "dist"}
        leaked = [name for name in names if name.split("/")[0] in excluded]
        if leaked:
            fail(f"{label} 包含应排除的目录: {leaked[:5]}")

        member = tar.extractfile("./src/App.jsx") or tar.extractfile("src/App.jsx")
        if member is None:
            fail(f"{label} 无法读取 src/App.jsx")
        source = member.read().decode("utf-8", errors="replace")
        # Agent 已把 CTA 改成红色，导出包必须是修改后的真实沙盒内容。
        if "background-color:red" not in source:
            fail(f"{label} 的 src/App.jsx 不是 Agent 修改后的版本")

        context_file = tar.extractfile("./VIBE_CONTEXT.md") or tar.extractfile(
            "VIBE_CONTEXT.md"
        )
        if context_file is None:
            fail(f"{label} 缺少 VIBE_CONTEXT.md")
        context = context_file.read().decode("utf-8", errors="replace")
        # 本地 Cursor 需要从 Markdown 中读到画布意图与机器可读的信封。
        for token in ("VIBE_CONTEXT", "改成红色", "UI_ONLY", "AnnotationEnvelope"):
            if token not in context:
                fail(f"{label} 的 VIBE_CONTEXT.md 缺少内容: {token}")

    size_kb = Path(path).stat().st_size / 1024
    print(f"   {label}: project-export.tar.gz / {size_kb:.1f} KB / {len(names)} 个文件")


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


def viewport_snapshot(pane, label: str):
    """读取单个面板的视口状态：缩放百分比、transform 与内容包围盒。"""
    snapshot = pane.evaluate("""root => {
            const layer = root.querySelector(
                '.canvas-viewport, .runtime-preview-frame'
            );
            const zoomNode = root.querySelector('[data-viewport-zoom]');
            if (!layer) return null;
            const style = getComputedStyle(layer);
            const box = layer.getBoundingClientRect();
            return {
                zoom: zoomNode
                    ? parseInt(zoomNode.textContent.replace('%', ''), 10)
                    : null,
                transform: style.transform,
                x: box.x,
                y: box.y,
                width: box.width,
                height: box.height,
            };
        }""")
    if not snapshot:
        fail(f"{label} 未读取到视口元素")
    return snapshot


def pointer_world_point(pane, x: float, y: float, label: str):
    """指针在“设计视口坐标系”中的世界坐标，用于验证缩放锚点。

    参数 x / y 必须是相对容器（preview-screen 或 canvas-stage）的坐标。
    """
    point = pane.evaluate(
        """(root, args) => {
            const layer = root.querySelector(
                '.canvas-viewport, .runtime-preview-frame'
            );
            if (!layer) return null;
            const matrix = new DOMMatrix(getComputedStyle(layer).transform);
            return {
                x: (args.x - matrix.e) / matrix.a,
                y: (args.y - matrix.f) / matrix.d,
            };
        }""",
        {"x": x, "y": y},
    )
    if not point:
        fail(f"{label} 未读取到视口变换矩阵")
    return point


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
        expect(page.locator(".workspace-tab.is-active .tab-name")).to_have_text(
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

        popover.locator("input").fill("改成红色")
        # M1 升级后不再由单个浮层直接发送：顶部「提交标注」统一打包
        # AnnotationEnvelope[]，支持一次最多 3 个标注。
        active.locator(".submit-annotations").click()

        print("\n== 5. Agent 执行完成后仍停留在画布模式 ==")
        # 执行中进度面板位于隐藏的预览视口，画布模式下通过状态点与顶栏提示反馈。
        expect(active.locator(".seg-status-running")).to_be_visible(timeout=10_000)
        expect(page.locator(".chip-live")).to_be_visible()
        page.wait_for_timeout(900)
        page.screenshot(path=str(SCREENSHOT_DIR / "phase4-loading.png"), full_page=True)

        # 默认审批：构建通过后画布侧出现“查看审批差异”，批准后回到画布。
        pause_cta = active.locator(".view-result-cta.is-pending")
        expect(pause_cta).to_be_visible(timeout=180_000)
        pause_cta.click()
        approval = active.locator(".loading-state.is-approval")
        expect(approval).to_be_visible(timeout=30_000)
        approval.get_by_role("button", name="批准并应用").click()
        expect(active.locator(".runtime-result-overlay .status-pill")).to_contain_text(
            "已完成", timeout=180_000
        )
        active.get_by_role("button", name="画布", exact=True).click()
        expect(active.locator(".canvas-pane.is-visible")).to_be_visible()

        expect(active.locator(".view-result-cta")).to_be_visible(timeout=120_000)
        expect(active.locator(".canvas-pane.is-visible")).to_be_visible()
        expect(active.locator(".runtime-preview-frame")).to_be_hidden()

        # 预览面板在对比模式画布下是 visibility:hidden，inner_text 会返回空；
        # 这里只需要校验 DOM 内容，使用 text_content 不受可见性影响。
        status_text = (
            active.locator(".runtime-result-overlay .status-pill").text_content() or ""
        )
        if "已完成" not in status_text:
            fail(f"运行状态异常: {status_text}")
        sandbox_id = active.locator(".runtime-panel").get_attribute("data-sandbox-id")
        if not sandbox_id:
            fail("未读取到 sandboxId")
        print(f"   沙盒: {sandbox_id} / 状态: {status_text.strip()}")

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

        print("\n== 7. 导出项目：工具栏与项目下拉菜单均下载 tar.gz ==")
        export_button = active.locator(".export-button")
        expect(export_button).to_be_enabled()
        with page.expect_download(timeout=30_000) as download_info:
            export_button.click()
        assert_export_archive(download_info.value, "工具栏导出")
        expect(page.locator("[data-export-notice='success']")).to_be_visible()

        page.locator(".project-switcher-button").click()
        menu_export = page.get_by_role("menuitem", name=re.compile("导出当前项目"))
        expect(menu_export).to_be_enabled()
        page.screenshot(path=str(SCREENSHOT_DIR / "phase4-export.png"), full_page=True)
        with page.expect_download(timeout=30_000) as menu_download_info:
            menu_export.click()
        assert_export_archive(menu_download_info.value, "下拉菜单导出")
        expect(page.locator(".project-menu")).to_have_count(0)

        print("\n== 8. 对比/展示模式往返：组件不卸载、尺寸严格对齐 ==")
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
        expect(active.locator(".diff-view")).to_contain_text("background-color")
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

        print("\n== 9. 视口交互：滚轮缩放、中键平移、状态保持与视口联动 ==")
        preview_pane = active.locator(".preview-pane")
        canvas_pane = active.locator(".canvas-pane")

        # 8.1 预览区：滚轮放大 + 中键平移（当前处于对比模式预览）。
        preview_initial = viewport_snapshot(preview_pane, "预览初始")
        if preview_initial["zoom"] != 100:
            fail(f"预览初始视口不是 100%: {preview_initial}")
        preview_screen = active.locator(
            ".preview-pane .runtime-preview-screen"
        ).bounding_box()
        if not preview_screen:
            fail("未读取到预览屏幕尺寸")
        px = preview_screen["x"] + preview_screen["width"] / 2
        py = preview_screen["y"] + preview_screen["height"] / 2

        # Cover 基准在 100% 时总有一个方向恰好填满容器。旧实现会在该方向
        # 强制居中，表现为「对比模式只能上下拖、展示模式只能左右拖」。
        # 先在 100% 下验证中键二维平移，再重置进入缩放测试。
        page.mouse.move(px, py)
        page.mouse.down(button="middle")
        page.mouse.move(px + 90, py + 60, steps=6)
        page.mouse.up(button="middle")
        page.wait_for_timeout(200)
        preview_pan0 = viewport_snapshot(preview_pane, "预览 100% 平移")
        dx0 = preview_pan0["x"] - preview_initial["x"]
        dy0 = preview_pan0["y"] - preview_initial["y"]
        if abs(dx0) < 40 or abs(dy0) < 40:
            fail(f"预览 100% 中键平移未同时作用于 X/Y: ({dx0:.1f}, {dy0:.1f})")
        print(f"   预览 100% 中键二维平移: ({dx0:.0f}, {dy0:.0f})")
        active.locator(".preview-pane .viewport-reset[title^='恢复']").click()
        page.wait_for_timeout(200)

        anchor_before = pointer_world_point(
            preview_pane,
            preview_screen["width"] / 2,
            preview_screen["height"] / 2,
            "预览缩放前",
        )
        page.mouse.move(px, py)
        page.mouse.wheel(0, -600)
        page.wait_for_timeout(250)
        preview_zoomed = viewport_snapshot(preview_pane, "预览滚轮放大")
        if preview_zoomed["zoom"] <= 130:
            fail(f"预览滚轮未放大: {preview_initial} -> {preview_zoomed}")
        if preview_zoomed["transform"] == preview_initial["transform"]:
            fail("预览滚轮改变了比例但没有更新 transform")
        anchor_after = pointer_world_point(
            preview_pane,
            preview_screen["width"] / 2,
            preview_screen["height"] / 2,
            "预览缩放后",
        )
        if (
            abs(anchor_before["x"] - anchor_after["x"]) > 1.5
            or abs(anchor_before["y"] - anchor_after["y"]) > 1.5
        ):
            fail(
                "预览滚轮缩放没有以鼠标指针为中心: "
                f"{anchor_before} -> {anchor_after}"
            )

        page.mouse.move(px, py)
        page.mouse.down(button="middle")
        page.mouse.move(px + 90, py + 60, steps=6)
        page.mouse.up(button="middle")
        page.wait_for_timeout(200)
        preview_panned = viewport_snapshot(preview_pane, "预览中键平移")
        if (
            abs(preview_panned["x"] - preview_zoomed["x"]) < 20
            or abs(preview_panned["y"] - preview_zoomed["y"]) < 20
        ):
            fail(f"预览中键平移未生效: {preview_zoomed} -> {preview_panned}")
        print(
            f"   预览视口: 100% -> {preview_zoomed['zoom']}%，"
            f"中键平移 ({preview_panned['x'] - preview_zoomed['x']:.0f}, "
            f"{preview_panned['y'] - preview_zoomed['y']:.0f})"
        )

        # 8.2 画布区独立缩放；在画布/预览间往返，两个视口都不得重置。
        active.get_by_role("button", name="画布", exact=True).click()
        expect(active.locator(".canvas-pane.is-visible")).to_be_visible()
        canvas_initial = viewport_snapshot(canvas_pane, "画布初始")
        if canvas_initial["zoom"] != 100:
            fail(f"画布初始视口不是 100%: {canvas_initial}")
        canvas_stage = active.locator(".canvas-pane .canvas-stage").bounding_box()
        if not canvas_stage:
            fail("未读取到画布舞台尺寸")

        # 对比模式 100% 同样必须二维自由平移（此前 Y 轴之外的方向会被锁住）。
        center_x = canvas_stage["x"] + canvas_stage["width"] / 2
        center_y = canvas_stage["y"] + canvas_stage["height"] / 2
        page.mouse.move(center_x, center_y)
        page.mouse.down(button="middle")
        page.mouse.move(center_x - 80, center_y + 60, steps=6)
        page.mouse.up(button="middle")
        page.wait_for_timeout(200)
        canvas_pan0 = viewport_snapshot(canvas_pane, "画布 100% 平移")
        dx0 = canvas_pan0["x"] - canvas_initial["x"]
        dy0 = canvas_pan0["y"] - canvas_initial["y"]
        if abs(dx0) < 40 or abs(dy0) < 40:
            fail(f"画布 100% 中键平移未同时作用于 X/Y: ({dx0:.1f}, {dy0:.1f})")
        print(f"   画布 100% 中键二维平移: ({dx0:.0f}, {dy0:.0f})")
        active.locator(".canvas-pane .viewport-reset[title^='恢复']").click()
        page.wait_for_timeout(200)

        cx = canvas_stage["x"] + canvas_stage["width"] * 0.15
        cy = canvas_stage["y"] + canvas_stage["height"] * 0.75
        page.mouse.move(cx, cy)
        page.mouse.wheel(0, -600)
        page.wait_for_timeout(250)
        canvas_zoomed = viewport_snapshot(canvas_pane, "画布滚轮放大")
        if canvas_zoomed["zoom"] <= 130:
            fail(f"画布滚轮未放大: {canvas_initial} -> {canvas_zoomed}")

        page.mouse.move(cx, cy)
        page.mouse.down(button="middle")
        page.mouse.move(cx - 70, cy - 50, steps=6)
        page.mouse.up(button="middle")
        page.wait_for_timeout(200)
        canvas_panned = viewport_snapshot(canvas_pane, "画布中键平移")

        active.get_by_role("button", name="预览", exact=True).click()
        expect(active.locator(".preview-pane.is-visible")).to_be_visible()
        preview_restored = viewport_snapshot(preview_pane, "预览恢复")
        if preview_restored["transform"] != preview_panned["transform"]:
            fail(
                "切换到预览后视口被重置: "
                f"{preview_panned['transform']} -> {preview_restored['transform']}"
            )

        active.get_by_role("button", name="画布", exact=True).click()
        canvas_restored = viewport_snapshot(canvas_pane, "画布恢复")
        if canvas_restored["transform"] != canvas_panned["transform"]:
            fail(
                "切回画布后视口被重置: "
                f"{canvas_panned['transform']} -> {canvas_restored['transform']}"
            )
        print(
            f"   画布视口: 100% -> {canvas_zoomed['zoom']}%，"
            "画布/预览往返后两边 transform 均保持"
        )

        # 8.3 展示模式视口联动：同一组 zoom/pan 同步到两侧。
        active.get_by_role("button", name="展示模式").click()
        link_toggle = active.get_by_role("button", name="视口联动")
        expect(link_toggle).to_be_visible()
        link_toggle.click()
        expect(link_toggle).to_have_attribute("aria-pressed", "true")

        stage_box = active.locator(".canvas-pane .canvas-stage").bounding_box()
        # 先重置到 100%，再以画面中心为锚点放大，截图能同时看到两侧同一内容。
        active.locator(".canvas-pane .viewport-reset[title^='恢复']").click()
        page.wait_for_timeout(200)
        lx = stage_box["x"] + stage_box["width"] / 2
        ly = stage_box["y"] + stage_box["height"] / 2
        page.mouse.move(lx, ly)
        page.mouse.wheel(0, -300)
        page.wait_for_timeout(250)
        linked_canvas = viewport_snapshot(canvas_pane, "联动画布")
        linked_preview = viewport_snapshot(preview_pane, "联动预览")
        if linked_canvas["zoom"] != linked_preview["zoom"]:
            fail(f"视口联动缩放不一致: {linked_canvas} / {linked_preview}")
        if linked_canvas["transform"] != linked_preview["transform"]:
            fail(f"视口联动 transform 不一致: {linked_canvas} / {linked_preview}")
        page.screenshot(
            path=str(SCREENSHOT_DIR / "phase4-viewport-link.png"), full_page=True
        )
        print(f"   视口联动: 两侧同步为 {linked_canvas['zoom']}%，transform 完全一致")

        # 8.4 展示模式 100%：中键 / 空格 + 左键都必须二维自由平移。
        active.locator(".canvas-pane .viewport-reset[title^='恢复']").click()
        page.wait_for_timeout(250)
        dual_initial = viewport_snapshot(canvas_pane, "展示模式 100%")
        if dual_initial["zoom"] != 100:
            fail(f"展示模式重置后不是 100%: {dual_initial}")
        dual_stage = active.locator(".canvas-pane .canvas-stage").bounding_box()
        if not dual_stage:
            fail("未读取到展示模式画布尺寸")
        dx = dual_stage["x"] + dual_stage["width"] / 2
        dy = dual_stage["y"] + dual_stage["height"] / 2

        page.mouse.move(dx, dy)
        page.mouse.down(button="middle")
        page.mouse.move(dx + 80, dy + 55, steps=6)
        page.mouse.up(button="middle")
        page.wait_for_timeout(200)
        dual_panned = viewport_snapshot(canvas_pane, "展示模式中键平移")
        delta_x = dual_panned["x"] - dual_initial["x"]
        delta_y = dual_panned["y"] - dual_initial["y"]
        if abs(delta_x) < 40 or abs(delta_y) < 40:
            fail("展示模式中键平移未同时作用于 X/Y: " f"({delta_x:.1f}, {delta_y:.1f})")
        linked_panned = viewport_snapshot(preview_pane, "展示模式联动平移")
        if linked_panned["transform"] != dual_panned["transform"]:
            fail("展示模式平移后画布/预览 transform 不一致")
        print(f"   展示模式 100% 中键二维平移: ({delta_x:.0f}, {delta_y:.0f})")
        active.locator(".canvas-pane .viewport-reset[title^='恢复']").click()
        page.wait_for_timeout(200)

        # 空格 + 左键：按住时 grab，拖拽中 grabbing，并且同样二维平移。
        page.keyboard.down("Space")
        page.wait_for_timeout(120)
        cursor_ready = active.locator(".canvas-pane .canvas-stage").evaluate(
            "el => getComputedStyle(el).cursor"
        )
        if cursor_ready != "grab":
            fail(f"空格按下后光标不是 grab: {cursor_ready}")
        page.mouse.move(dx, dy)
        page.mouse.down()
        page.mouse.move(dx - 70, dy - 45, steps=6)
        page.wait_for_timeout(120)
        cursor_panning = active.locator(".canvas-pane .canvas-stage").evaluate(
            "el => getComputedStyle(el).cursor"
        )
        page.mouse.up()
        page.keyboard.up("Space")
        page.wait_for_timeout(200)
        if cursor_panning != "grabbing":
            fail(f"拖拽中光标不是 grabbing: {cursor_panning}")
        dual_space = viewport_snapshot(canvas_pane, "展示模式空格平移")
        delta_x = dual_space["x"] - dual_initial["x"]
        delta_y = dual_space["y"] - dual_initial["y"]
        if abs(delta_x) < 40 or abs(delta_y) < 40:
            fail("空格 + 左键平移未同时作用于 X/Y: " f"({delta_x:.1f}, {delta_y:.1f})")
        print(
            f"   空格+左键二维平移: ({delta_x:.0f}, {delta_y:.0f})，"
            "光标 grab -> grabbing"
        )

        # 8.5 重置恢复 100%，画布与预览重新严格对齐。
        active.locator(".canvas-pane .viewport-reset[title^='恢复']").click()
        active.locator(".preview-pane .viewport-reset[title^='恢复']").click()
        page.wait_for_timeout(250)
        reset_canvas = viewport_snapshot(canvas_pane, "重置画布")
        reset_preview = viewport_snapshot(preview_pane, "重置预览")
        if reset_canvas["zoom"] != 100 or reset_preview["zoom"] != 100:
            fail(f"重置视图未回到 100%: {reset_canvas} / {reset_preview}")
        assert_canvas_preview_parity(page, active, "重置视口后展示模式")
        print("   重置视图后回到 100%，画布/预览卡片再次严格对齐")

        print("\n== 10. 刷新页面：恢复预览结果，默认回到画布模式 ==")
        page.reload(wait_until="networkidle")
        active = page.locator(".tab-window.is-active")
        expect(page.locator(".workspace-tab.is-active .tab-name")).to_have_text(
            "演示项目"
        )
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

        print("\n== 11. 沙盒被外部销毁：预览显示连接失败并可重新执行 ==")
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
        expect(offline).to_contain_text("预览已失效")
        retry = offline.get_by_role("button", name="重新执行 Agent")
        expect(retry).to_be_visible()
        page.screenshot(
            path=str(SCREENSHOT_DIR / "phase4-preview-offline.png"), full_page=True
        )

        retry.click()
        # retry 从预览侧发起，审批面板直接出现在当前视口。
        approval = active.locator(".loading-state.is-approval")
        expect(approval).to_be_visible(timeout=180_000)
        approval.get_by_role("button", name="批准并应用").click()
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

        print("\n== 12. 关闭项目：删除状态并销毁沙盒容器 ==")
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

        print("\n== 13. 同时打开 5 个项目，触发上限 ==")
        for _ in range(4):
            open_new_project(page)
        expect(page.locator(".workspace-tab")).to_have_count(5)
        expect(page.locator(".workspace-tab.is-active .tab-name")).to_have_text(
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

        print("\n== 14. 关闭项目 2 后新建，按存活数量递增且不重名 ==")
        page.locator(".workspace-tab", has_text="未命名项目 2").locator(
            ".tab-close"
        ).click()
        expect(page.locator(".workspace-tab")).to_have_count(4)
        open_new_project(page)
        expect(page.locator(".workspace-tab")).to_have_count(5)
        # 存活 4 个 Tab → 从 5 起编号；5、6 已被占用，顺延到 7，绝不重名。
        expect(page.locator(".workspace-tab.is-active .tab-name")).to_have_text(
            "未命名项目 7"
        )
        names = page.locator(".workspace-tab .tab-name").all_inner_texts()
        if len(names) != len(set(names)):
            fail(f"项目名称出现重复: {names}")
        print("   关闭项目 2 后，新项目按存活数递增为「未命名项目 7」")

        print("\n== 15. 账户设置、退出与账号密码登录 ==")
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
