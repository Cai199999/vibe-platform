#!/usr/bin/env python3
"""验证 M1.5 全局标注删除的基线回滚。

场景：A、B 两个圈修改同一个 src/App.jsx。
  - A 把 CTA 改红；
  - B 在 A 之后创建，把 CTA 改绿；
  - 删除 B 时 git reset --hard 到 B 创建前（A1），A 的红色保留；
  - 再点“清除标注”时回到沙盒 baseline，预览恢复初始颜色。

运行：
    /tmp/vibe-browser/bin/python scripts/annotation_delete_rollback_e2e_test.py
"""

from __future__ import annotations

import subprocess
import time

from playwright.sync_api import expect, sync_playwright

WEB_URL = "http://127.0.0.1:5173/"


def fail(message: str) -> None:
    print(f"\nFAIL: {message}")
    raise SystemExit(1)


def draw_around(page, locator) -> None:
    box = locator.bounding_box()
    if not box:
        fail("找不到圈选目标")
    page.mouse.move(box["x"] - 24, box["y"] - 16)
    page.mouse.down()
    page.mouse.move(
        box["x"] + box["width"] / 2,
        box["y"] + box["height"] / 2,
        steps=8,
    )
    page.mouse.move(
        box["x"] + box["width"] + 24,
        box["y"] + box["height"] + 16,
        steps=8,
    )
    page.mouse.up()
    page.wait_for_timeout(260)


def send(page, popover, text: str, label: str) -> dict:
    """发送单个标注并返回最终 Run 结果。

    Supervisor 改造后，POST /runs 不再同步返回结果；真实执行由
    /supervisor/dispatch 启动，前端通过 SSE 把终态写入会话快照。
    因此这里先捕获 dispatch.runId，再等待界面完成并从
    sessionStorage 读取该 Run 的 result。
    """
    popover.locator("input").fill(text, force=True)
    with page.expect_response(
        lambda response: response.request.method == "POST"
        and response.url.endswith("/api/v1/supervisor/dispatch"),
        timeout=180_000,
    ) as info:
        popover.locator(".popover-send").click(force=True)
    response = info.value
    if not response.ok:
        fail(f"{label} 请求失败: HTTP {response.status} {response.text()}")
    dispatch = response.json()
    ui_dispatch = next(
        (
            item
            for item in dispatch.get("dispatches") or []
            if item.get("agentId") == "ui_graph"
        ),
        None,
    )
    if not ui_dispatch:
        fail(f"{label} 未派发 UI Run: {dispatch}")
    run_id = ui_dispatch.get("runId")

    active = page.locator(".tab-window.is-active")
    expect(active.locator(".runtime-result-overlay .status-pill")).to_contain_text(
        "已完成", timeout=180_000
    )
    payload = page.evaluate(
        """(runId) => {
            const saved = JSON.parse(
                window.sessionStorage.getItem('vibe.runs.v1') || '{}'
            );
            for (const value of Object.values(saved)) {
                const result = value && value.result;
                if (result && result.runId === runId) return result;
            }
            return null;
        }""",
        run_id,
    )
    if not payload:
        fail(f"{label} 会话快照中没有 Run 结果: {run_id}")
    commit = (payload.get("commits") or [{}])[0]
    print(
        f"   {label}: turn={commit.get('turnCount')} "
        f"commit={(commit.get('commitHash') or '')[:7]} "
        f"baseline={(payload.get('baselineCommit') or '')[:7]}"
    )
    if payload.get("status") not in ("SUCCEEDED", "AWAITING_APPROVAL"):
        fail(f"{label} 执行失败: {payload}")
    return payload


def git(container: str, *args: str) -> str:
    return subprocess.check_output(
        ["docker", "exec", container, "git", *args],
        text=True,
    ).strip()


def wait_cta_color(frame, expected: str, label: str) -> None:
    cta = frame.locator("[data-vibe-target='cta-button']")
    expect(cta).to_be_visible(timeout=30_000)
    deadline = time.time() + 30
    color = ""
    while time.time() < deadline:
        color = cta.evaluate("el => getComputedStyle(el).backgroundColor")
        if color == expected:
            print(f"   {label}: {color}")
            return
        time.sleep(0.4)
    fail(f"{label} 颜色不是 {expected}: {color}")


def main() -> int:
    sandbox_id = ""
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1600, "height": 1000})
            page = context.new_page()

            print("== 1. 登录、记录 CTA 初始颜色 ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            expect(page.locator(".workspace-tab")).to_have_count(1)
            active = page.locator(".tab-window.is-active")
            active.locator(".model-picker select").select_option("mock")
            initial_color = active.locator(".mock-cta").evaluate(
                "el => getComputedStyle(el).backgroundColor"
            )
            print(f"   初始 CTA: {initial_color}")

            print("\n== 2. 圈 A，改红色 ==")
            draw_around(page, active.locator(".mock-cta"))
            popover_a = active.locator(".canvas-popover").first
            annotation_a = popover_a.get_attribute("data-annotation-id")
            first = send(page, popover_a, "改成红色", "A 第一轮")
            sandbox_id = first.get("sandboxId") or ""
            commit_a = (first.get("commits") or [{}])[0].get("commitHash")
            baseline = first.get("baselineCommit") or ""
            if not sandbox_id or not commit_a or not baseline:
                fail(f"缺少 sandbox / commit / baseline: {first}")
            container = f"vibe-sandbox-{sandbox_id}"

            print("\n== 3. 在 A 之后新增圈 B，改绿色 ==")
            # Supervisor 派发后会自动切到预览，先回画布再画第二个圈。
            active.get_by_role("button", name="画布", exact=True).click()
            expect(active.locator(".canvas-pane.is-visible")).to_be_visible()
            draw_around(page, active.locator(".mock-cta"))
            popovers = active.locator(".canvas-popover")
            expect(popovers).to_have_count(2)
            popover_b = popovers.nth(1)
            annotation_b = popover_b.get_attribute("data-annotation-id")
            if not annotation_b or annotation_b == annotation_a:
                fail("圈 B 未创建独立的 annotationId")
            second = send(page, popover_b, "改成绿色", "B 第一轮")
            commit_b = (second.get("commits") or [{}])[0].get("commitHash")
            if commit_b == commit_a:
                fail("B 没有产生新 commit")
            log = git(container, "log", "--reverse", "--format=%s", "-3").splitlines()
            print(f"   git log: {log}")
            if log[-1] != f"run: {second.get('runId')} - turn: 1":
                fail(f"B 提交顺序异常: {log}")

            print("\n== 4. 删除圈 B：应 reset 到 B 创建前的 A1 基线 ==")
            active.get_by_role("button", name="画布", exact=True).click()
            expect(active.locator(".canvas-pane.is-visible")).to_be_visible()
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url.endswith("/api/v1/runs/rollback"),
                timeout=120_000,
            ) as rollback_info:
                popover_b.locator(".popover-remove").click(force=True)
            rollback_response = rollback_info.value
            if not rollback_response.ok:
                fail(
                    "删除 B 的回滚失败: "
                    f"HTTP {rollback_response.status} {rollback_response.text()}"
                )
            rollback = rollback_response.json()
            if rollback.get("commitHash") != commit_a:
                fail(f"删除 B 未回到 A1: {rollback}")
            expect(active.locator(".canvas-popover")).to_have_count(1)
            if (
                active.locator(".canvas-popover").first.get_attribute(
                    "data-annotation-id"
                )
                != annotation_a
            ):
                fail("删除 B 时误删了 A")
            expect(
                active.locator(".canvas-popover").first.locator(".chat-bubble")
            ).to_have_count(2)
            if git(container, "rev-parse", "HEAD") != commit_a:
                fail("Git HEAD 未回到 A1")
            print(f"   HEAD -> {commit_a[:7]}；A 的两条聊天记录保留")

            print("\n== 5. 预览恢复为 A 的红色 ==")
            active.get_by_role("button", name="预览", exact=True).click()
            frame = page.frame_locator(".tab-window.is-active .runtime-preview-frame")
            wait_cta_color(frame, "rgb(255, 0, 0)", "删除 B 后 CTA")

            print("\n== 6. 清除标注：应 reset 到 sandbox baseline ==")
            active.get_by_role("button", name="画布", exact=True).click()
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url.endswith("/api/v1/runs/rollback"),
                timeout=120_000,
            ) as clear_info:
                # 清除标注已折叠进工具栏「更多」菜单。
                active.locator("[data-toolbar-more]").click()
                active.get_by_role("menuitem", name="清除标注").click()
            clear_response = clear_info.value
            if not clear_response.ok:
                fail(
                    "清除标注失败: "
                    f"HTTP {clear_response.status} {clear_response.text()}"
                )
            clear = clear_response.json()
            if clear.get("commitHash") != baseline:
                fail(f"清除标注未回到 baseline: {clear}")
            expect(active.locator(".canvas-popover")).to_have_count(0)
            if git(container, "rev-parse", "HEAD") != baseline:
                fail("Git HEAD 未回到 baseline")
            print(f"   HEAD -> baseline {baseline[:7]}")

            active.get_by_role("button", name="预览", exact=True).click()
            frame = page.frame_locator(".tab-window.is-active .runtime-preview-frame")
            wait_cta_color(frame, initial_color, "清除后 CTA")

            context.close()
            browser.close()
        print("\nANNOTATION DELETE ROLLBACK E2E: PASS")
        return 0
    finally:
        if sandbox_id:
            import urllib.request

            request = urllib.request.Request(
                f"http://127.0.0.1:8100/api/v1/internal/sandbox/{sandbox_id}",
                method="DELETE",
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    print(f"   cleanup {sandbox_id} -> HTTP {response.status}")
            except Exception as exc:  # noqa: BLE001
                print(f"   cleanup {sandbox_id} -> {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
