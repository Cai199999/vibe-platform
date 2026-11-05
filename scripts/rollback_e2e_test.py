#!/usr/bin/env python3
"""验证单标注多轮 Git checkpoint 与回滚。

覆盖：
  1. 每轮成功 Run 返回 commitHash / turnCount，并在沙盒 Git 历史中按顺序提交；
  2. Chatbox 每条成功助手消息旁存在“回滚到此步”；
  3. 回滚到第一轮后：chatHistory 截断、HEAD 回到第一轮 commit、预览变回红色；
  4. 回滚后继续对话：下一轮重新使用 turn 2，新 commit 接在被回滚的 HEAD 上；
  5. Python ThreadMemoryStore checkpoint 同步恢复（checkpointRestored=true）。

运行：
    /tmp/vibe-browser/bin/python scripts/rollback_e2e_test.py
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
WEB_URL = "http://127.0.0.1:5173/"


def fail(message: str) -> None:
    print(f"\nFAIL: {message}")
    raise SystemExit(1)


def draw_around(page, locator) -> None:
    box = locator.bounding_box()
    if not box:
        fail("找不到圈选目标")
    page.mouse.move(box["x"] - 26, box["y"] - 18)
    page.mouse.down()
    page.mouse.move(
        box["x"] + box["width"] / 2,
        box["y"] + box["height"] / 2,
        steps=8,
    )
    page.mouse.move(
        box["x"] + box["width"] + 26,
        box["y"] + box["height"] + 18,
        steps=8,
    )
    page.mouse.up()
    page.wait_for_timeout(260)


def send_in_popover(page, popover, text: str, label: str) -> dict:
    popover.locator("input").fill(text, force=True)
    with page.expect_response(
        lambda response: response.request.method == "POST"
        and response.url.endswith("/api/v1/runs"),
        timeout=180_000,
    ) as response_info:
        popover.locator(".popover-send").click(force=True)
    response = response_info.value
    if not response.ok:
        fail(f"{label} Run 请求失败: HTTP {response.status} {response.text()}")
    payload = response.json()
    commit = (payload.get("commits") or [{}])[0]
    print(
        f"   {label}: status={payload.get('status')} "
        f"turn={commit.get('turnCount')} commit={(commit.get('commitHash') or '')[:7]}"
    )
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

            print("== 1. 登录并圈选 CTA ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            expect(page.locator(".workspace-tab")).to_have_count(1)
            active = page.locator(".tab-window.is-active")
            active.locator(".model-picker select").select_option("mock")
            draw_around(page, active.locator(".mock-cta"))
            popover = active.locator(".canvas-popover")
            expect(popover).to_have_count(1)

            print("\n== 2. 第一轮：改成红色 ==")
            first = send_in_popover(page, popover, "改成红色", "第一轮")
            if first.get("status") not in ("SUCCEEDED", "AWAITING_APPROVAL"):
                fail(f"第一轮执行失败: {first}")
            sandbox_id = first.get("sandboxId") or ""
            first_commit = (first.get("commits") or [{}])[0]
            if not sandbox_id or len(first_commit.get("commitHash") or "") != 40:
                fail(f"第一轮未返回合法 commit: {first}")
            if first_commit.get("turnCount") != 1:
                fail(f"第一轮 turnCount 不是 1: {first_commit}")
            expect(popover.locator(".chat-rollback")).to_have_count(1)

            print("\n== 3. 第二轮：不，我要蓝色 ==")
            second = send_in_popover(page, popover, "不，我要蓝色", "第二轮")
            second_commit = (second.get("commits") or [{}])[0]
            if second.get("sandboxId") != sandbox_id:
                fail("第二轮没有复用同一沙盒")
            if second_commit.get("turnCount") != 2:
                fail(f"第二轮 turnCount 不是 2: {second_commit}")
            if second_commit.get("commitHash") == first_commit.get("commitHash"):
                fail("两轮 commit hash 相同")
            expect(popover.locator(".chat-bubble")).to_have_count(4)
            expect(popover.locator(".chat-rollback")).to_have_count(2)

            print("\n== 4. 校验沙盒 Git 提交顺序 ==")
            container = f"vibe-sandbox-{sandbox_id}"
            log = git(container, "log", "--reverse", "--format=%s", "-3").splitlines()
            print(f"   git log: {log}")
            expected = [
                "baseline: sandbox init",
                f"run: {first.get('runId')} - turn: 1",
                f"run: {second.get('runId')} - turn: 2",
            ]
            if log != expected:
                fail(f"Git 提交顺序异常:\nactual={log}\nexpected={expected}")
            head_before = git(container, "rev-parse", "HEAD")
            if head_before != second_commit.get("commitHash"):
                fail("HEAD 不是第二轮 commit")

            print("\n== 5. 回滚到第一轮 ==")
            first_rollback = popover.locator(".chat-bubble.is-assistant").first.locator(
                ".chat-rollback"
            )
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url.endswith("/api/v1/runs/rollback"),
                timeout=120_000,
            ) as rollback_info:
                first_rollback.click(force=True)
            rollback_response = rollback_info.value
            if not rollback_response.ok:
                fail(
                    "回滚接口失败: "
                    f"HTTP {rollback_response.status} {rollback_response.text()}"
                )
            rollback = rollback_response.json()
            print(
                f"   rollback: checkpointRestored={rollback.get('checkpointRestored')} "
                f"turn={rollback.get('turnCount')} preview={rollback.get('previewUrl')}"
            )
            if rollback.get("checkpointRestored") is not True:
                fail(f"后端未恢复 thread checkpoint: {rollback}")
            if rollback.get("commitHash") != first_commit.get("commitHash"):
                fail("回滚响应的 commit 不是第一轮")
            if "?v=" not in (rollback.get("previewUrl") or ""):
                fail("回滚 previewUrl 缺少 cache-busting 参数")

            expect(popover.locator(".chat-bubble")).to_have_count(2)
            expect(popover.locator(".chat-rollback")).to_have_count(1)
            head_after = git(container, "rev-parse", "HEAD")
            if head_after != first_commit.get("commitHash"):
                fail(f"HEAD 未回到第一轮: {head_after}")
            print(f"   HEAD restored -> {head_after[:7]}")
            page.screenshot(
                path=str(SCREENSHOT_DIR / "m1-rollback-to-turn1.png"),
                full_page=True,
            )

            print("\n== 6. 预览恢复为第一轮的红色 ==")
            active.get_by_role("button", name="预览", exact=True).click()
            frame = page.frame_locator(".tab-window.is-active .runtime-preview-frame")
            wait_cta_color(frame, "rgb(255, 0, 0)", "回滚后 CTA")

            print("\n== 7. 回滚后继续对话：改成绿色，应重新使用 turn 2 ==")
            active.get_by_role("button", name="画布", exact=True).click()
            expect(active.locator(".canvas-popover")).to_have_count(1)
            popover = active.locator(".canvas-popover")
            third = send_in_popover(page, popover, "改成绿色", "回滚后下一轮")
            third_commit = (third.get("commits") or [{}])[0]
            if third_commit.get("turnCount") != 2:
                fail(f"回滚后 turnCount 未重新使用 2: {third_commit}")
            if third_commit.get("commitHash") in {
                first_commit.get("commitHash"),
                second_commit.get("commitHash"),
            }:
                fail("回滚后的新 commit 与旧 commit 重复")
            expect(popover.locator(".chat-bubble")).to_have_count(4)
            expect(popover.locator(".chat-rollback")).to_have_count(2)

            log_after = git(
                container, "log", "--reverse", "--format=%s", "-3"
            ).splitlines()
            print(f"   git log after: {log_after}")
            if log_after[-1] != f"run: {third.get('runId')} - turn: 2":
                fail(f"回滚后的新提交顺序异常: {log_after}")
            if git(
                container, "rev-parse", f"{second_commit.get('commitHash')}^"
            ) != first_commit.get("commitHash"):
                fail("第二轮旧 commit 的父提交不是第一轮")

            active.get_by_role("button", name="预览", exact=True).click()
            frame = page.frame_locator(".tab-window.is-active .runtime-preview-frame")
            wait_cta_color(frame, "rgb(0, 128, 0)", "回滚后新一轮 CTA")
            page.screenshot(
                path=str(SCREENSHOT_DIR / "m1-rollback-then-new-turn.png"),
                full_page=True,
            )

            context.close()
            browser.close()
        print("\nROLLBACK E2E: PASS")
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
