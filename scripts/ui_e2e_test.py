#!/usr/bin/env python3
"""M1 真实浏览器 E2E：在画布上圈选按钮、提交标注、观察 Loading、预览与 Diff。

前置：
    pip install playwright && playwright install chromium
    docker compose up -d

运行：
    python3 scripts/ui_e2e_test.py

产出：
    docs/screenshots/m1-loading.png
    docs/screenshots/m1-result.png
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


def main() -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

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

        print("== 1. 打开画布 ==")
        page.goto(WEB_URL, wait_until="networkidle")
        expect(page.locator(".canvas-stage")).to_be_visible()
        expect(page.get_by_text("尚未圈选组件")).to_be_visible()
        print("   画布与空闲状态已渲染")

        print("\n== 2. 圈选按钮并提交标注 ==")
        draw_around(page, page.locator(".mock-cta"))
        expect(page.get_by_text("标注已就绪")).to_be_visible()
        expect(page.get_by_text("已圈选", exact=False)).to_be_visible()

        page.locator("#instruction").fill("改颜色")
        page.get_by_role("button", name="提交标注").click()

        print("\n== 3. 观察 Agent 执行状态面板 ==")
        expect(page.get_by_text("Agent 正在执行")).to_be_visible(timeout=10_000)
        page.wait_for_timeout(900)
        page.screenshot(path=str(SCREENSHOT_DIR / "m1-loading.png"), full_page=True)
        print(f"   Loading 截图: {SCREENSHOT_DIR / 'm1-loading.png'}")

        print("\n== 4. 等待闭环结果 ==")
        expect(page.locator(".status-pill.is-success")).to_be_visible(timeout=120_000)
        expect(page.locator(".preview-frame")).to_be_visible()
        expect(page.locator(".diff-view")).to_contain_text("backgroundColor")
        expect(page.locator(".diff-view")).to_contain_text("red")

        retry_text = page.locator(".result-head .elapsed").inner_text()
        if "自动修复 1 次" not in retry_text:
            fail(f"重试信息异常: {retry_text}")

        sandbox_id = (
            page.locator(".meta-grid > div").nth(3).locator("code").inner_text()
        )
        preview_url = page.locator(".preview-url").inner_text()
        print(f"   状态: 等待审批 / {retry_text}")
        print(f"   沙盒: {sandbox_id}")
        print(f"   预览: {preview_url}")

        print("\n== 5. 验证 iframe 内的红色按钮 ==")
        frame = page.frame_locator(".preview-frame")
        cta = frame.locator("button")
        expect(cta).to_be_visible(timeout=30_000)
        background = cta.evaluate("el => getComputedStyle(el).backgroundColor")
        print(f"   iframe 按钮背景色: {background}")
        if background != "rgb(255, 0, 0)":
            fail(f"按钮不是红色: {background}")

        page.screenshot(path=str(SCREENSHOT_DIR / "m1-result.png"), full_page=True)
        print(f"   结果截图: {SCREENSHOT_DIR / 'm1-result.png'}")

        if console_errors:
            print("\n   浏览器控制台错误:")
            for item in console_errors:
                print(f"   - {item}")
            fail("页面存在控制台错误")

        cleanup_sandbox(sandbox_id)
        browser.close()

    print("\nPASS: 浏览器端到端验证通过（圈选 → Loading → 预览 → Diff → 清理）")


if __name__ == "__main__":
    main()
