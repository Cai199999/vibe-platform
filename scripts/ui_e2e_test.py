#!/usr/bin/env python3
"""Phase 2 真实浏览器 E2E：登录 → 多项目 Tab → 拖拽排序 → 持久化 → Agent → 预览 / Diff。

覆盖点：
    - 未登录跳转 / 一键登录 / 账号密码登录
    - 项目下拉栏：新建项目、导入项目（Phase 3 占位禁用）
    - 多 Tab 工作区：新建、切换、拖拽排序、关闭、重新打开
    - localStorage 工作区持久化（刷新后 Tab 与顺序不丢）
    - 每个 Tab 独立的画布标注、意图草稿与沙盒实时预览
    - 账户设置弹窗、退出登录

前置：
    pip install playwright && playwright install chromium
    docker compose up -d

运行：
    python3 scripts/ui_e2e_test.py

产出：
    docs/screenshots/phase2-login.png
    docs/screenshots/phase2-project-menu.png
    docs/screenshots/phase2-workspace.png
    docs/screenshots/phase2-canvas-popover.png
    docs/screenshots/phase2-loading.png
    docs/screenshots/phase2-result.png
    docs/screenshots/phase2-account.png
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
        page.screenshot(path=str(SCREENSHOT_DIR / "phase2-login.png"), full_page=True)
        print(f"   登录页截图: {SCREENSHOT_DIR / 'phase2-login.png'}")

        print("\n== 2. 一键登录并进入多 Tab 工作区 ==")
        page.get_by_role("button", name="一键体验（推荐）").click()
        expect(page).to_have_url(re.compile(r"/workspace$"))
        expect(page.locator(".workspace-tab")).to_have_count(1)
        expect(page.locator(".tab-window.is-active .canvas-stage")).to_be_visible()
        session = page.evaluate("window.localStorage.getItem('vibe.auth.session')")
        if not session:
            fail("Token 未写入 localStorage")
        workspace = page.evaluate("window.localStorage.getItem('vibe.workspace.v1')")
        if not workspace:
            fail("工作区未写入 localStorage")
        print("   登录成功，会话与工作区状态均已写入 localStorage")

        print("\n== 3. 项目下拉栏与新建项目 ==")
        page.locator(".project-switcher-button").click()
        menu = page.locator(".project-menu")
        expect(menu).to_be_visible()
        expect(
            menu.get_by_role("menuitem", name=re.compile("导入项目"))
        ).to_be_disabled()
        page.screenshot(
            path=str(SCREENSHOT_DIR / "phase2-project-menu.png"), full_page=True
        )
        print(f"   项目下拉栏截图: {SCREENSHOT_DIR / 'phase2-project-menu.png'}")

        menu.get_by_role("menuitem", name=re.compile("新建项目")).click()
        expect(page.locator(".workspace-tab")).to_have_count(2)
        expect(page.locator(".tab-window.is-active .tab-window-title h2")).to_have_text(
            "未命名项目 2"
        )
        tab_names = page.locator(".workspace-tab .tab-name").all_inner_texts()
        if tab_names != ["演示项目", "未命名项目 2"]:
            fail(f"新建项目后 Tab 顺序异常: {tab_names}")
        page.wait_for_timeout(300)
        page.screenshot(
            path=str(SCREENSHOT_DIR / "phase2-workspace.png"), full_page=True
        )
        print(f"   多 Tab 工作区截图: {SCREENSHOT_DIR / 'phase2-workspace.png'}")

        print("\n== 4. 拖拽 Tab 重新排序 ==")
        drag_tab(page, "演示项目", "未命名项目 2")
        expect(page.locator(".workspace-tab .tab-name")).to_have_text(
            ["未命名项目 2", "演示项目"]
        )
        print("   拖拽排序成功: 演示项目 → 未命名项目 2 之后")

        print("\n== 5. 刷新页面，验证 Tab 顺序持久化 ==")
        page.reload(wait_until="networkidle")
        expect(page).to_have_url(re.compile(r"/workspace$"))
        expect(page.locator(".workspace-tab .tab-name")).to_have_text(
            ["未命名项目 2", "演示项目"]
        )
        expect(page.locator(".workspace-tab")).to_have_count(2)
        print("   刷新后 Tab 数量与顺序保持不变")

        print("\n== 6. 切换 Tab，在激活窗口内圈选并发送意图 ==")
        page.locator(".workspace-tab", has_text="演示项目").click()
        expect(page.locator(".tab-window.is-active .tab-window-title h2")).to_have_text(
            "演示项目"
        )
        draw_around(page, page.locator(".tab-window.is-active .mock-cta"))
        popover = page.locator(".tab-window.is-active .canvas-popover")
        expect(popover).to_be_visible()
        expect(
            page.locator(".tab-window.is-active .runtime-placeholder")
        ).to_be_visible()
        page.wait_for_timeout(300)  # 等待浮层入场动画结束再截图
        page.screenshot(
            path=str(SCREENSHOT_DIR / "phase2-canvas-popover.png"), full_page=True
        )
        print(f"   浮层截图: {SCREENSHOT_DIR / 'phase2-canvas-popover.png'}")

        popover.locator("input").fill("改颜色")
        popover.locator("input").press("Enter")

        print("\n== 7. 激活 Tab 内展示 Agent 执行状态 ==")
        expect(
            page.locator(".tab-window.is-active").get_by_role(
                "heading", name="Agent 正在执行", exact=True
            )
        ).to_be_visible(timeout=10_000)
        page.wait_for_timeout(900)
        page.screenshot(path=str(SCREENSHOT_DIR / "phase2-loading.png"), full_page=True)
        print(f"   Loading 截图: {SCREENSHOT_DIR / 'phase2-loading.png'}")

        print("\n== 8. 等待闭环结果，预览出现在当前 Tab 内 ==")
        active = page.locator(".tab-window.is-active")
        expect(active.locator(".status-pill.is-success")).to_be_visible(timeout=120_000)
        expect(active.locator(".runtime-preview-frame")).to_be_visible()

        retry_text = active.locator(".result-head .elapsed").inner_text()
        if "自动修复 1 次" not in retry_text:
            fail(f"重试信息异常: {retry_text}")

        sandbox_id = active.locator(".runtime-meta code").nth(3).inner_text()
        preview_url = active.locator(".preview-url").inner_text()
        print(f"   状态: 等待审批 / {retry_text}")
        print(f"   沙盒: {sandbox_id}")
        print(f"   预览: {preview_url}")

        print("\n== 9. 校验 iframe 内按钮已变红，并展开 Diff ==")
        frame = page.frame_locator(".tab-window.is-active .runtime-preview-frame")
        cta = frame.locator("button")
        expect(cta).to_be_visible(timeout=30_000)
        background = cta.evaluate("el => getComputedStyle(el).backgroundColor")
        print(f"   iframe 按钮背景色: {background}")
        if background != "rgb(255, 0, 0)":
            fail(f"按钮不是红色: {background}")

        active.get_by_text("查看本次生成的代码 Diff").click()
        diff = active.locator(".diff-view")
        expect(diff).to_be_visible()
        expect(diff).to_contain_text("backgroundColor")
        expect(diff).to_contain_text("red")

        page.screenshot(path=str(SCREENSHOT_DIR / "phase2-result.png"), full_page=True)
        print(f"   结果截图: {SCREENSHOT_DIR / 'phase2-result.png'}")

        print("\n== 10. 关闭 Tab 并从项目下拉栏重新打开 ==")
        page.locator(".workspace-tab.is-active .tab-close").click()
        expect(page.locator(".workspace-tab")).to_have_count(1)
        expect(page.locator(".tab-window.is-active .tab-window-title h2")).to_have_text(
            "未命名项目 2"
        )
        page.locator(".project-switcher-button").click()
        page.get_by_role("menuitem", name=re.compile("演示项目")).click()
        expect(page.locator(".workspace-tab")).to_have_count(2)
        expect(page.locator(".tab-window.is-active .tab-window-title h2")).to_have_text(
            "演示项目"
        )
        expect(
            page.locator(".tab-window.is-active .runtime-preview-frame")
        ).to_be_visible()
        print("   Tab 关闭与项目重新打开均正常，会话内预览状态保留")

        print("\n== 11. 账户设置弹窗 ==")
        page.locator(".avatar-button").click()
        page.get_by_role("menuitem", name="账户设置").click()
        expect(page.locator(".account-dialog")).to_be_visible()
        expect(page.get_by_text("demo@vibe.local")).to_be_visible()
        page.screenshot(path=str(SCREENSHOT_DIR / "phase2-account.png"), full_page=True)
        print(f"   账户弹窗截图: {SCREENSHOT_DIR / 'phase2-account.png'}")
        page.get_by_role("button", name="完成").click()
        expect(page.locator(".account-dialog")).to_have_count(0)

        print("\n== 12. 退出登录，再用账号密码登录 ==")
        page.locator(".avatar-button").click()
        page.get_by_role("menuitem", name="退出登录").click()
        expect(page).to_have_url(re.compile(r"/login$"))
        page.locator("#login-account").fill("alice@example.com")
        page.locator("#login-password").fill("vibe-demo")
        page.get_by_role("button", name="登录并进入工作台").click()
        expect(page).to_have_url(re.compile(r"/workspace$"))
        expect(page.locator(".account-name")).to_have_text("alice")
        print("   账号密码登录与退出登录均正常")

        if console_errors:
            print("\n   浏览器控制台错误:")
            for item in console_errors:
                print(f"   - {item}")
            fail("页面存在控制台错误")

        cleanup_sandbox(sandbox_id)
        browser.close()

    print(
        "\nPASS: 多 Tab 工作区 → 拖拽排序 → 持久化 → 项目内预览 → 账户体系 全流程通过"
    )


if __name__ == "__main__":
    main()
