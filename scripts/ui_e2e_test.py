#!/usr/bin/env python3
"""Phase 3 真实浏览器 E2E：单视口切换 → Agent → 沙盒销毁 → 5 Tab 上限 → 命名复用。

覆盖点：
    - 未登录跳转 / 一键登录 / 账号密码登录
    - 多 Tab 新建、拖拽排序、localStorage 持久化
    - 单视口「画布编辑 / 实时预览」切换：尺寸不变、画布状态与 iframe 不丢失
    - Agent 完成后「查看效果」快捷切换
    - 关闭 Tab = 删除项目 + 销毁 Docker 沙盒（通过 BFF 调用 Python internal API）
    - 最多 5 个 Tab；关闭项目后按最小空闲编号复用项目名

前置：
    pip install playwright && playwright install chromium
    docker compose up -d

运行：
    python3 scripts/ui_e2e_test.py

产出：
    docs/screenshots/phase3-workspace.png
    docs/screenshots/phase3-canvas-mode.png
    docs/screenshots/phase3-loading.png
    docs/screenshots/phase3-preview-mode.png
    docs/screenshots/phase3-tab-limit.png
    docs/screenshots/phase3-account.png
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


def main() -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    sandbox_id = ""

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1680, "height": 1050})

        console_errors: list[str] = []
        page.on(
            "console",
            lambda message: (
                console_errors.append(message.text) if message.type == "error" else None
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
            path=str(SCREENSHOT_DIR / "phase3-workspace.png"), full_page=True
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
            path=str(SCREENSHOT_DIR / "phase3-canvas-mode.png"), full_page=True
        )

        popover.locator("input").fill("改颜色")
        popover.locator("input").press("Enter")

        print("\n== 5. Agent 执行完成后仍停留在画布模式 ==")
        # 执行中进度面板位于隐藏的预览视口，画布模式下通过状态点与顶栏提示反馈。
        expect(active.locator(".seg-status-running")).to_be_visible(timeout=10_000)
        expect(page.locator(".chip-live")).to_be_visible()
        page.wait_for_timeout(900)
        page.screenshot(path=str(SCREENSHOT_DIR / "phase3-loading.png"), full_page=True)

        expect(active.locator(".view-result-cta")).to_be_visible(timeout=120_000)
        expect(active.locator(".canvas-pane.is-visible")).to_be_visible()
        expect(active.locator(".runtime-preview-frame")).to_be_hidden()

        retry_text = active.locator(".result-head .elapsed").inner_text()
        if "自动修复 1 次" not in retry_text:
            fail(f"重试信息异常: {retry_text}")
        sandbox_id = active.locator(".runtime-meta code").nth(3).inner_text()
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

        page.screenshot(
            path=str(SCREENSHOT_DIR / "phase3-preview-mode.png"), full_page=True
        )
        active.get_by_text("查看本次生成的代码 Diff").click()
        expect(active.locator(".diff-view")).to_contain_text("backgroundColor")

        print("\n== 7. 往返切换：画布标注与 iframe 状态均不丢失 ==")
        active.get_by_role("button", name="画布编辑").click()
        expect(active.locator(".canvas-pane.is-visible")).to_be_visible()
        expect(active.locator(".annotation-ellipse")).to_be_visible()
        canvas_box_again = active.locator(".project-viewport").bounding_box()
        assert_same_box(canvas_box, canvas_box_again, "预览 → 画布")

        active.get_by_role("button", name="实时预览").click()
        expect(active.locator(".runtime-preview-frame")).to_be_visible()
        assert_same_box(
            canvas_box, active.locator(".project-viewport").bounding_box(), "再次切换"
        )
        frame = page.frame_locator(".tab-window.is-active .runtime-preview-frame")
        expect(frame.locator("button")).to_have_css(
            "background-color", "rgb(255, 0, 0)"
        )
        print("   切换过程外层视口尺寸稳定，圈选与 iframe 均保留")

        print("\n== 8. 刷新页面：恢复预览结果，默认回到画布模式 ==")
        page.reload(wait_until="networkidle")
        active = page.locator(".tab-window.is-active")
        expect(active.locator(".tab-window-title h2")).to_have_text("演示项目")
        expect(active.locator(".canvas-pane.is-visible")).to_be_visible()
        expect(active.locator(".view-result-cta")).to_be_visible()
        expect(active.locator(".runtime-meta code").nth(3)).to_have_text(sandbox_id)
        active.locator(".view-result-cta").click()
        frame = page.frame_locator(".tab-window.is-active .runtime-preview-frame")
        expect(frame.locator("button")).to_have_css(
            "background-color", "rgb(255, 0, 0)"
        )
        print("   刷新后预览与沙盒信息均恢复，关闭项目仍可追踪沙盒")

        print("\n== 9. 关闭项目：删除状态并销毁沙盒容器 ==")
        expect(active.locator(".runtime-meta code").nth(3)).to_have_text(sandbox_id)
        page.locator(".workspace-tab.is-active .tab-close").click()
        expect(page.locator(".workspace-tab")).to_have_count(1)
        page.locator(".project-switcher-button").click()
        expect(page.get_by_role("menuitem", name=re.compile("演示项目"))).to_have_count(
            0
        )
        page.keyboard.press("Escape")
        wait_sandbox_removed(sandbox_id)
        print(f"   项目已删除，容器 vibe-sandbox-{sandbox_id} 已销毁")

        print("\n== 10. 同时打开 5 个项目，触发上限 ==")
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
            path=str(SCREENSHOT_DIR / "phase3-tab-limit.png"), full_page=True
        )
        page.keyboard.press("Escape")
        print("   5 个 Tab 时新建入口已禁用并展示上限提示")

        print("\n== 11. 关闭项目 2 后新建，名称复用最小空闲编号 ==")
        page.locator(".workspace-tab", has_text="未命名项目 2").locator(
            ".tab-close"
        ).click()
        expect(page.locator(".workspace-tab")).to_have_count(4)
        open_new_project(page)
        expect(page.locator(".workspace-tab")).to_have_count(5)
        expect(page.locator(".tab-window.is-active .tab-window-title h2")).to_have_text(
            "未命名项目 2"
        )
        print("   关闭项目 2 后，新项目重新使用名称「未命名项目 2」")

        print("\n== 12. 账户设置、退出与账号密码登录 ==")
        page.locator(".avatar-button").click()
        page.get_by_role("menuitem", name="账户设置").click()
        expect(page.locator(".account-dialog")).to_be_visible()
        expect(page.get_by_text("demo@vibe.local")).to_be_visible()
        page.screenshot(path=str(SCREENSHOT_DIR / "phase3-account.png"), full_page=True)
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
        "\nPASS: 单视口切换 → 沙盒销毁 → 5 Tab 上限 → 项目名复用 → 账户体系 全流程通过"
    )


if __name__ == "__main__":
    main()
