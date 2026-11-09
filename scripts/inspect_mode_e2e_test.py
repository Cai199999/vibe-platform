#!/usr/bin/env python3
"""E2E：精准点击选择（Inspect Mode）与源码出处回传。

覆盖链路：
  1. 登录演示项目，先执行一次圈选修改，拿到可预览的沙盒；
  2. 工具栏「精准选择」进入 Inspect 模式，父页面自动切到预览；
  3. 鼠标悬停 React 按钮：SDK elementFromPoint -> closest([data-source])
     回传 rect + src/App.jsx:line，父页面绘制蓝色高亮；
  4. 左键点击：弹出标注输入框，写意图后生成 AnnotationEnvelope；
  5. 切换 iframe 到 Vue 示例页，重复悬停 / 点击，必须精确回传
     src/VueDemo.vue:17；
  6. 发送标注，捕获 Supervisor route 请求，校验 envelope 的
     type/source/snippet/measured 均为 inspect 精确数据。

运行：
    PLAYWRIGHT_CHROMIUM_EXECUTABLE=<chrome> \
      /tmp/vibe-browser/bin/python scripts/inspect_mode_e2e_test.py
"""

from __future__ import annotations

import json
import os
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
        f"{PY_AI_URL}/api/v1/internal/sandbox/{sandbox_id}",
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            print(f"   cleanup {sandbox_id} -> HTTP {response.status}")
    except Exception as exc:  # noqa: BLE001 - 清理失败不掩盖主流程
        print(f"   cleanup {sandbox_id} -> {exc}")


def launch_browser(playwright):
    """本地缓存浏览器与 pip Playwright 版本可能不一致，允许显式指定路径。"""
    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    options = {"headless": True, "args": ["--no-sandbox"]}
    if executable:
        options["executable_path"] = executable
    return playwright.chromium.launch(**options)


def draw_around(page, locator) -> None:
    box = locator.bounding_box()
    if not box:
        fail("找不到可圈选元素")
    page.mouse.move(box["x"] - 20, box["y"] - 14)
    page.mouse.down()
    page.mouse.move(
        box["x"] + box["width"] / 2,
        box["y"] + box["height"] / 2,
        steps=8,
    )
    page.mouse.move(
        box["x"] + box["width"] + 20,
        box["y"] + box["height"] + 14,
        steps=8,
    )
    page.mouse.up()
    page.wait_for_timeout(300)


def preview_frame(page, preview_url: str):
    for frame in page.frames:
        if frame.url.startswith(preview_url):
            return frame
    fail(f"找不到预览 iframe: {preview_url}")
    return None


def hover_center(page, frame, selector: str) -> dict:
    box = frame.locator(selector).bounding_box()
    if not box:
        fail(f"找不到元素: {selector}")
    page.mouse.move(
        box["x"] + box["width"] / 2,
        box["y"] + box["height"] / 2,
        steps=4,
    )
    return box


def main() -> int:
    captured: list[dict] = []

    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()
        # 强制离线 Mock，避免 E2E 依赖 DeepSeek 网络与额度。
        page.add_init_script("window.localStorage.setItem('vibe.llmProvider', 'mock')")

        def on_request(request) -> None:
            if request.method != "POST":
                return
            if not request.url.endswith("/api/v1/supervisor/route"):
                return
            try:
                body = json.loads(request.post_data or "{}")
            except json.JSONDecodeError:
                return
            if body.get("annotations"):
                captured.append(body)

        page.on("request", on_request)
        sandbox_id: str | None = None

        try:
            print("== 1. 登录并创建可预览沙盒 ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            demo = page.get_by_role("button", name="一键体验（推荐）")
            if demo.is_visible():
                demo.click()
            expect(page.locator(".workspace-tab")).to_have_count(1)
            active = page.locator(".tab-window.is-active")
            expect(active.locator(".mock-card")).to_be_visible()

            draw_around(page, active.locator(".mock-cta"))
            popover = active.locator(".canvas-popover")
            expect(popover).to_have_count(1)
            popover.locator("input").fill("把主按钮改成蓝色")
            popover.locator(".popover-send").click()
            expect(
                active.locator(".runtime-result-overlay .status-pill")
            ).to_contain_text("已完成", timeout=180_000)

            captured.clear()  # 只保留 Inspect 标注的请求，忽略首次圈选。
            view_result = active.locator(".view-result-cta")
            if view_result.is_visible():
                view_result.click()
            expect(active.locator(".preview-pane.is-visible")).to_be_visible()
            sandbox_id = active.locator(".runtime-panel").get_attribute(
                "data-sandbox-id"
            )
            preview_url = (
                active.locator(".runtime-preview-frame").get_attribute("src") or ""
            )
            print(f"   sandbox={sandbox_id} preview={preview_url}")

            print("\n== 2. 进入精准选择模式 ==")
            toggle = active.locator("[data-inspect-toggle]")
            expect(toggle).to_be_enabled(timeout=30_000)
            toggle.click()
            expect(toggle).to_have_attribute("aria-pressed", "true")
            expect(
                active.locator(".preview-gesture-layer.is-inspecting")
            ).to_be_visible()
            print("   Inspect 已开启，画布自动切换到预览")

            print("\n== 3. 悬停 React 按钮：蓝色高亮 + 源码行号 ==")
            frame = preview_frame(page, preview_url)
            hover_center(page, frame, "button.vibe-cta")
            highlight = active.locator("[data-inspect-highlight]")
            expect(highlight).to_be_visible(timeout=10_000)
            highlight_text = highlight.text_content() or ""
            print(f"   highlight: {highlight_text.strip()}")
            if "src/App.jsx:" not in highlight_text:
                fail(f"高亮未显示 React 源码路径: {highlight_text}")

            print("\n== 4. 点击生成精确标注草稿 ==")
            box = frame.locator("button.vibe-cta").bounding_box()
            if not box:
                fail("找不到 React CTA")
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            draft = active.locator(".inspect-draft")
            expect(draft).to_be_visible(timeout=10_000)
            draft_text = draft.text_content() or ""
            print(f"   draft: {' '.join(draft_text.split())[:240]}")
            if "src/App.jsx:" not in draft_text:
                fail(f"标注草稿未显示 React 源码路径: {draft_text}")
            modify_button = draft.get_by_role("button", name="修改此组件")
            if modify_button.get_attribute("aria-pressed") != "true":
                fail(f"命中元素应默认 MODIFY: {draft_text}")

            intent = "把主按钮文案改成“立即开始”"
            draft.locator("[data-inspect-intent]").fill(intent)
            draft.get_by_role("button", name="添加标注").click()
            expect(active.locator(".inspect-draft")).to_have_count(0)
            expect(active.locator(".selection-state")).to_contain_text("2 个标注")
            page.screenshot(
                path=str(SCREENSHOT_DIR / "m3-inspect-react.png"),
                full_page=True,
            )
            print("   React 标注草稿已生成")

            print("\n== 5. 切换 Vue 页面：src/VueDemo.vue:17 ==")
            frame.goto(
                preview_url.rstrip("/") + "/vue-demo.html",
                wait_until="networkidle",
            )
            hover_center(page, frame, ".vue-cta")
            expect(highlight).to_be_visible(timeout=10_000)
            vue_highlight = highlight.text_content() or ""
            print(f"   vue highlight: {vue_highlight.strip()}")
            if "src/VueDemo.vue:17" not in vue_highlight:
                fail(f"Vue 高亮未精确到 17 行: {vue_highlight}")

            vbox = frame.locator(".vue-cta").bounding_box()
            if not vbox:
                fail("找不到 Vue 按钮")
            page.mouse.click(
                vbox["x"] + vbox["width"] / 2,
                vbox["y"] + vbox["height"] / 2,
            )
            expect(draft).to_be_visible(timeout=10_000)
            vue_draft = draft.text_content() or ""
            print(f"   vue draft: {' '.join(vue_draft.split())[:240]}")
            if "src/VueDemo.vue:17" not in vue_draft:
                fail(f"Vue 草稿未精确到 17 行: {vue_draft}")
            page.screenshot(
                path=str(SCREENSHOT_DIR / "m3-inspect-vue.png"),
                full_page=True,
            )
            draft.get_by_role("button", name="取消").click()
            expect(active.locator(".inspect-draft")).to_have_count(0)

            print("\n== 6. 发送 React 标注，校验 AnnotationEnvelope ==")
            active.get_by_role("button", name="画布").click()
            canvas_popover = active.locator(".canvas-popover[data-annotation-id]").last
            expect(canvas_popover).to_be_visible()
            # 浮层可能被画布内容遮挡，直接强制点击发送；发送只依赖
            # annotation.intent，与浮层是否 active 无关。
            canvas_popover.locator("input").fill(intent)
            canvas_popover.locator(".popover-send").click(force=True)
            deadline_pages = 50
            for _ in range(deadline_pages):
                if captured:
                    break
                page.wait_for_timeout(100)
            if not captured:
                fail("未捕获到 Supervisor route 请求")
            annotation = captured[0]["annotations"][0]
            print(
                "   envelope:",
                json.dumps(
                    {
                        "type": annotation.get("type"),
                        "source": (annotation.get("target") or {}).get("source"),
                        "intent": annotation.get("intent", {})
                        .get("ui", {})
                        .get("summary"),
                        "measured": annotation.get("measured"),
                    },
                    ensure_ascii=False,
                )[:900],
            )
            target = annotation.get("target") or {}
            source = target.get("source") or {}
            if source.get("file") != "src/App.jsx":
                fail(f"envelope.source.file 错误: {source}")
            if annotation.get("type") != "visual-draw":
                fail(f"inspect 标注 type 应为 visual-draw: {annotation.get('type')}")
            if not annotation.get("measured", {}).get("source"):
                fail("envelope.measured.source 缺失")
            snippet = target.get("snippet") or {}
            content = snippet.get("content") if isinstance(snippet, dict) else None
            if not content:
                fail("envelope 缺少精确源码片段")
            lines = [line for line in content.splitlines() if line.strip()]
            if len(lines) > 5:
                fail(f"精确标注只应携带命中行附近片段，实际 {len(lines)} 行")
            line = int(source.get("line") or 0)
            print(f"   source={source.get('file')}:{line} snippet_lines={len(lines)}")
            if line <= 0:
                fail("source.line 必须为正整数")
        finally:
            cleanup_sandbox(sandbox_id)
            context.close()
            browser.close()

    print("\nINSPECT MODE E2E: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
