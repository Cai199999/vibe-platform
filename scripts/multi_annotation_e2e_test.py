#!/usr/bin/env python3
"""M1 批量标注 E2E：UI 画 3 个圈 → AnnotationEnvelope[] → Agent 一次性修改。

覆盖：
  1. 画布最多 3 个标注，每个标注有独立输入框；
  2. 达到上限后再圈选会被拦截并提示；
  3. 顶部「提交标注」一次提交 3 条不同意图（颜色 / 文案 / 尺寸）；
  4. 请求体确实是 annotations 数组，长度为 3；
  5. 预览中 3 处修改同时生效；
  6. 导出包源码与 VIBE_CONTEXT.md 同时包含 3 条标注；
  7. 「清除标注」清空全部圈与输入框。

前置：
    docker compose up -d
    /tmp/vibe-browser/bin/playwright install chromium

运行：
    /tmp/vibe-browser/bin/python scripts/multi_annotation_e2e_test.py
"""

from __future__ import annotations

import json
import re
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
    raise SystemExit(1)


def cleanup_sandbox(sandbox_id: str) -> None:
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


def draw_around(page, locator) -> None:
    box = locator.bounding_box()
    if not box:
        fail("找不到圈选目标")
    page.mouse.move(box["x"] - 30, box["y"] - 20)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=8)
    page.mouse.move(
        box["x"] + box["width"] + 30, box["y"] + box["height"] + 20, steps=8
    )
    page.mouse.up()
    page.wait_for_timeout(280)


def fill_annotation(active, index: int, text: str) -> None:
    inputs = active.locator(".canvas-popover input")
    # 三个浮层可能互相遮挡；fill(force=True) 只验证输入框本身可编辑。
    inputs.nth(index).fill(text, force=True)


def read_download_source(download) -> tuple[str, str]:
    if download.suggested_filename != "project-export.tar.gz":
        fail(f"导出文件名异常: {download.suggested_filename}")
    path = download.path()
    if not path:
        fail("未拿到导出文件")
    with tarfile.open(path, "r:gz") as tar:
        app = tar.extractfile("./src/App.jsx") or tar.extractfile("src/App.jsx")
        context = tar.extractfile("./VIBE_CONTEXT.md") or tar.extractfile(
            "VIBE_CONTEXT.md"
        )
        if app is None or context is None:
            fail("导出包缺少 src/App.jsx 或 VIBE_CONTEXT.md")
        return (
            app.read().decode("utf-8", errors="replace"),
            context.read().decode("utf-8", errors="replace"),
        )


def main() -> int:
    captured: dict[str, dict] = {}
    sandbox_id = ""
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1600, "height": 1000})
            page = context.new_page()

            def on_request(request) -> None:
                if request.method == "POST" and request.url.endswith(
                    "/api/v1/runs/stream"
                ):
                    try:
                        captured["body"] = json.loads(request.post_data or "{}")
                    except json.JSONDecodeError:
                        captured["body"] = {}

            page.on("request", on_request)

            print("== 1. 登录并显式选择 Mock 模型（无 Key 的确定性 E2E）==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            expect(page.locator(".workspace-tab")).to_have_count(1)
            active = page.locator(".tab-window.is-active")
            active.locator(".model-picker select").select_option("mock")
            print("   model=mock")

            print("\n== 2. 连续圈选 3 个区域，每个区域独立输入 ==")
            draw_around(page, active.locator(".mock-cta"))
            expect(active.locator(".canvas-popover")).to_have_count(1)
            fill_annotation(active, 0, "改成蓝色")

            # 隐藏浮层，避免输入框挡住下一笔圈选。
            active.get_by_role("button", name="隐藏标注输入").click(force=True)
            draw_around(page, active.locator(".mock-title"))
            expect(active.locator(".canvas-popover")).to_have_count(2)
            fill_annotation(active, 1, "文字改为全新标题")

            active.get_by_role("button", name="隐藏标注输入").click(force=True)
            draw_around(page, active.locator(".mock-copy"))
            expect(active.locator(".canvas-popover")).to_have_count(3)
            fill_annotation(active, 2, "放大一倍")
            expect(active.locator(".annotation-ellipse")).to_have_count(3)
            expect(active.locator(".submit-annotations")).to_be_enabled()
            page.wait_for_timeout(250)
            page.screenshot(
                path=str(SCREENSHOT_DIR / "m1-three-annotations.png"), full_page=True
            )

            print("\n== 3. 达到上限后继续圈选被拦截 ==")
            active.get_by_role("button", name="隐藏标注输入").click(force=True)
            draw_around(page, active.locator(".mock-kicker"))
            expect(active.locator("[data-annotation-limit]")).to_be_visible()
            expect(active.locator(".annotation-ellipse")).to_have_count(3)
            print("   提示: " + active.locator("[data-annotation-limit]").inner_text())

            print("\n== 4. 一键提交 AnnotationEnvelope[] ==")
            active.locator(".submit-annotations").click()
            # 产品前端默认 approvalMode=required：构建通过后先审批再落盘。
            pause_cta = active.locator(".view-result-cta.is-pending")
            expect(pause_cta).to_be_visible(timeout=180_000)
            pause_cta.click()
            approval = active.locator(".loading-state.is-approval")
            expect(approval).to_be_visible(timeout=30_000)
            approval.get_by_role("button", name="批准并应用").click()
            expect(
                active.locator(".runtime-result-overlay .status-pill")
            ).to_contain_text("已完成", timeout=180_000)
            body = captured.get("body") or {}
            annotations = body.get("annotations") or []
            if len(annotations) != 3:
                fail(f"请求体 annotations 数量不是 3: {body}")
            texts = [item.get("payload", {}).get("text") for item in annotations]
            if texts != ["改成蓝色", "文字改为全新标题", "放大一倍"]:
                fail(f"标注顺序 / 指令异常: {texts}")
            if body.get("annotation") is not None:
                fail("新协议不应再发送单 annotation 字段")
            sandbox_id = (
                active.locator(".runtime-panel").get_attribute("data-sandbox-id") or ""
            )
            print(f"   提交 3 条指令: {texts}")
            print(f"   沙盒: {sandbox_id}")

            print("\n== 5. 预览中 3 处修改同时生效 ==")
            # 审批暂停会自动切到预览；兼容仍停留在画布的情况。
            view_result = active.locator(".view-result-cta")
            if view_result.is_visible():
                view_result.click()
            expect(active.locator(".runtime-preview-frame")).to_be_visible()
            frame = page.frame_locator(".tab-window.is-active .runtime-preview-frame")
            cta = frame.locator("[data-vibe-target='cta-button']")
            expect(cta).to_be_visible(timeout=30_000)
            background = cta.evaluate("el => getComputedStyle(el).backgroundColor")
            if background != "rgb(0, 0, 255)":
                fail(f"CTA 未变蓝: {background}")
            title = frame.locator("[data-vibe-target='title']")
            if title.inner_text().strip() != "全新标题":
                fail(f"标题文案未修改: {title.inner_text()}")
            copy_transform = frame.locator("[data-vibe-target='copy']").evaluate(
                "el => getComputedStyle(el).transform"
            )
            if not copy_transform.startswith("matrix(2"):
                fail(f"说明文字未放大一倍: {copy_transform}")
            print(
                f"   CTA={background} / title=全新标题 / copy transform={copy_transform}"
            )
            page.screenshot(
                path=str(SCREENSHOT_DIR / "m1-multi-annotation-result.png"),
                full_page=True,
            )

            print("\n== 6. 导出源码与 VIBE_CONTEXT.md 同时包含 3 条修改 ==")
            with page.expect_download(timeout=30_000) as download_info:
                active.locator(".export-button").click()
            source, context_doc = read_download_source(download_info.value)
            for token in ("background-color:blue", "全新标题", "transform:scale(2)"):
                if token not in source:
                    fail(f"导出源码缺少: {token}")
            for token in (
                "标注数量：3",
                "改成蓝色",
                "文字改为全新标题",
                "放大一倍",
                "AnnotationEnvelope",
            ):
                if token not in context_doc:
                    fail(f"VIBE_CONTEXT.md 缺少: {token}")
            print("   导出源码 / VIBE_CONTEXT.md 均包含 3 条标注")

            print("\n== 7. Diff 展示 3 处修改 ==")
            active.get_by_role("button", name="详情 / 代码 Diff").click()
            expect(active.locator(".runtime-drawer")).to_be_visible()
            diff = active.locator(".diff-view").inner_text()
            for token in ("background-color:blue", "全新标题", "transform:scale(2)"):
                if token not in diff:
                    fail(f"Diff 缺少: {token}")
            active.locator(".runtime-drawer").get_by_role("button", name="关闭").click()
            print("   Diff 同时包含颜色 / 文案 / 尺寸修改")

            print("\n== 8. 清除标注：圈与输入框全部清空 ==")
            active.get_by_role("button", name="画布", exact=True).click()
            active.locator(".ghost-button", has_text="清除标注").click()
            expect(active.locator(".annotation-ellipse")).to_have_count(0)
            expect(active.locator(".canvas-popover")).to_have_count(0)
            expect(active.locator(".submit-annotations")).to_be_disabled()
            print("   已清空 3 个标注")

            context.close()
            browser.close()
        print("\nMULTI ANNOTATION E2E: PASS")
        return 0
    finally:
        cleanup_sandbox(sandbox_id)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"\nMULTI ANNOTATION E2E: FAIL - {exc}", file=__import__("sys").stderr)
        raise
