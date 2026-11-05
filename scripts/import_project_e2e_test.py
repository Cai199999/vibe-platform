#!/usr/bin/env python3
"""E2E：通过左上角「导入项目」上传 zip / 图片，验证新建 Tab 与预览。

覆盖三种输入：
  1. React zip：沙盒内 npm install + Vite dev server；
  2. HTML zip：镜像内置 Node 静态服务器；
  3. PNG 截图：视觉模型缺失时走确定性 Mock，生成可构建的 React 项目。

每个导入都必须：
  - 返回 201 与 sandboxId / previewUrl；
  - 新建一个 Tab 并自动聚焦预览；
  - iframe 中出现导入项目的真实内容。

运行：
    /tmp/vibe-browser/bin/python scripts/import_project_e2e_test.py
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path("/tmp/vibe-import-fixtures")
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
        with urllib.request.urlopen(request, timeout=120) as response:
            print(f"   cleanup {sandbox_id} -> HTTP {response.status}")
    except Exception as exc:  # noqa: BLE001 - 清理失败不掩盖主流程
        print(f"   cleanup {sandbox_id} -> {exc}")


def import_via_ui(page, path: Path, expected_text: str, expected_count: int) -> dict:
    """点击「导入项目」上传文件，等待接口返回并校验新 Tab / 预览。"""
    page.locator(".project-switcher-button").click()
    with page.expect_file_chooser() as chooser_info:
        page.get_by_role("menuitem", name="导入项目").click()
    chooser = chooser_info.value

    with page.expect_response(
        lambda response: response.request.method == "POST"
        and response.url.endswith("/api/v1/projects/import"),
        timeout=360_000,
    ) as response_info:
        chooser.set_files(str(path))
    response = response_info.value
    if response.status != 201:
        fail(f"{path.name} 导入失败 HTTP {response.status}: {response.text()}")
    payload = response.json()
    print(
        f"   {path.name} -> sandbox={payload.get('sandboxId')} "
        f"kind={payload.get('kind')} entry={payload.get('entryFile')}"
    )
    if not payload.get("sandboxId") or not payload.get("previewUrl"):
        fail(f"{path.name} 响应缺少 sandboxId / previewUrl: {payload}")

    expect(page.locator(".workspace-tab")).to_have_count(expected_count)
    active = page.locator(".tab-window.is-active")
    expect(active.locator(".preview-pane.is-visible")).to_be_visible(timeout=30_000)
    frame = page.frame_locator(".tab-window.is-active .runtime-preview-frame")
    expect(frame.locator("body")).to_contain_text(expected_text, timeout=60_000)
    return payload


def main() -> int:
    for name in ("react.zip", "html.zip", "landing.png"):
        if not (FIXTURES / name).is_file():
            fail(f"缺少测试文件 {FIXTURES / name}")

    sandbox_ids: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()
        try:
            print("== 1. 登录并进入演示项目 ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            expect(page.locator(".workspace-tab")).to_have_count(1)
            expect(page.locator(".tab-window.is-active .mock-card")).to_be_visible()

            print("\n== 2. 导入 React zip ==")
            payload = import_via_ui(
                page, FIXTURES / "react.zip", "导入的 React 项目", 2
            )
            sandbox_ids.append(payload["sandboxId"])
            if (
                payload.get("kind") != "react"
                or payload.get("entryFile") != "src/App.jsx"
            ):
                fail(f"React 导入元数据错误: {payload}")
            expect(
                page.locator(".workspace-tab").filter(has_text="react")
            ).to_be_visible()
            print("   React 项目 Tab 与预览正常")

            print("\n== 3. 导入 HTML zip ==")
            payload = import_via_ui(page, FIXTURES / "html.zip", "导入的 HTML 项目", 3)
            sandbox_ids.append(payload["sandboxId"])
            if (
                payload.get("kind") != "html"
                or payload.get("entryFile") != "index.html"
            ):
                fail(f"HTML 导入元数据错误: {payload}")
            print("   HTML 静态项目 Tab 与预览正常")

            print("\n== 4. 导入 PNG 截图（无视觉 Key 时走确定性生成） ==")
            # 未配置 VISION_API_KEY 时，后端生成结构化占位页并把原图作为主视觉。
            payload = import_via_ui(page, FIXTURES / "landing.png", "imported image", 4)
            sandbox_ids.append(payload["sandboxId"])
            if payload.get("kind") != "react":
                fail(f"图片导入应生成 React 项目: {payload}")

            print("\n== 5. 三个导入项目均保持独立 Tab ==")
            expect(page.locator(".workspace-tab")).to_have_count(4)
            print("   4 个 Tab（演示 + React + HTML + 图片）")

            page.screenshot(
                path=str(ROOT / "docs" / "screenshots" / "import-project-e2e.png"),
                full_page=True,
            )
            print("   screenshot -> docs/screenshots/import-project-e2e.png")
        finally:
            for sandbox_id in sandbox_ids:
                cleanup_sandbox(sandbox_id)
            context.close()
            browser.close()

    print("\nIMPORT PROJECT E2E: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
