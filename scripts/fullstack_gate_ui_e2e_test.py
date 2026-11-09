#!/usr/bin/env python3
"""浏览器级 E2E：真实产品主链路 → 契约确认 → 点击生成全栈代码。

这条用例覆盖用户实际操作的路径，而不是“一键体验直达澄清”：

  1. 圈选空白区域生成组件 → 拖拽定位 → 保存快照（保存门控）；
  2. 点击【下一步：构建全栈方案】进入 Architect；
  3. 多轮澄清 → 契约 ready → 确认契约；
  4. 断言【开始生成全栈代码】按钮**可点击**，并打印门控诊断信息；
  5. 点击按钮前先确认 both 门控（若存在）已释放；
  6. 等待 FullstackGraph 六节点完成；
  7. 直接进入沙盒容器断言：
       /app/server/index.js、db.js、schema.sql 存在；
       /app/data/fullstack.sqlite 是真实 SQLite 文件（文件头校验）。

前置：
    docker compose up -d
    /tmp/vibe-browser/bin/python scripts/fullstack_gate_ui_e2e_test.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
WEB_URL = "http://127.0.0.1:5173/"
BFF_URL = "http://127.0.0.1:8080"

# 沙盒容器命名规则：vibe-sandbox-<sandboxId>。
CONTAINER_PREFIX = "vibe-sandbox-"


def fail(message: str) -> None:
    print(f"\nFAIL: {message}")
    raise SystemExit(1)


def cleanup_sandbox(sandbox_id: str | None) -> None:
    if not sandbox_id:
        return
    request = urllib.request.Request(
        f"{BFF_URL}/api/v1/sandboxes/{sandbox_id}", method="DELETE"
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            print(f"   cleanup {sandbox_id} -> HTTP {response.status}")
    except Exception as exc:  # noqa: BLE001 - 清理失败不掩盖主流程
        print(f"   cleanup {sandbox_id} -> {exc}")


def docker_exec(
    container: str, *args: str, timeout: float = 30
) -> subprocess.CompletedProcess:
    """在沙盒容器内执行命令；返回 CompletedProcess 供断言使用。"""
    return subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def assert_sandbox_files(sandbox_id: str) -> None:
    container = f"{CONTAINER_PREFIX}{sandbox_id}"
    listing = docker_exec(container, "sh", "-c", "ls -la /app /app/server /app/data")
    if listing.returncode != 0:
        fail(f"无法进入沙盒容器 {container}: {listing.stderr.strip()}")
    print("   /app 目录：")
    for line in listing.stdout.splitlines():
        print(f"     {line}")

    for name in ("index.js", "db.js", "schema.sql", "migrate.mjs"):
        check = docker_exec(container, "test", "-f", f"/app/server/{name}")
        if check.returncode != 0:
            fail(f"沙盒缺少 /app/server/{name}")
    for name in ("Dockerfile", "render.yaml", ".env.example", "DEPLOY.md"):
        check = docker_exec(container, "test", "-f", f"/app/{name}")
        if check.returncode != 0:
            fail(f"沙盒缺少 /app/{name}")

    # SQLite 文件必须真实落盘；只检查文件存在还不够，校验文件头。
    header = docker_exec(
        container,
        "sh",
        "-c",
        "head -c 16 /app/data/fullstack.sqlite | od -An -tc",
    )
    # od -tc 会把每个字符用空格隔开（S   Q   L ...），先压缩空白再比对。
    compact_header = "".join(header.stdout.split())
    if header.returncode != 0 or "SQLiteformat3" not in compact_header:
        fail(
            "沙盒数据文件不是真实 SQLite："
            f"rc={header.returncode} out={compact_header!r} err={header.stderr.strip()}"
        )
    print(
        "   OK：server/index.js、db.js、schema.sql、migrate.mjs、"
        "Dockerfile、render.yaml、.env.example 与 data/fullstack.sqlite 均存在"
    )


def wait_run_completed(active) -> None:
    """等待一次 UI Run 完成（与 drag_placement E2E 相同的判定）。"""
    trace = active.locator("[data-supervisor-trace]")
    expect(trace).to_be_visible(timeout=30_000)
    expect(trace).not_to_have_attribute(
        "data-supervisor-phase", "analyzing", timeout=240_000
    )
    expect(active.locator(".runtime-overlay")).to_have_count(0, timeout=240_000)
    expect(active.locator(".runtime-result-overlay .status-pill")).to_contain_text(
        "已完成", timeout=30_000
    )


def fill_clarification(panel) -> None:
    """填写当前澄清轮次：应用形态选全栈，其余给默认值。"""
    form = panel.locator(".architect-clarification")
    expect(form).to_be_visible(timeout=60_000)

    shape_choices = form.locator(".shape-choice")
    if shape_choices.count() > 0:
        fullstack = form.get_by_role("button", name=re.compile("全栈系统"))
        expect(fullstack).to_be_visible()
        fullstack.click()

    groups = form.locator("fieldset.architect-question")
    for index in range(groups.count()):
        group = groups.nth(index)
        pills = group.locator(".choice-pills button")
        if pills.count() == 0:
            continue
        if group.locator(".choice-pills button.is-selected").count() == 0:
            pills.first.click()

    areas = form.locator("textarea")
    for index in range(areas.count()):
        area = areas.nth(index)
        if area.evaluate("el => !!el.closest('.architect-comment')"):
            continue
        # 对话前置：形态由自然语言识别，明确要求数据库 / 团队协作。
        area.fill(
            "点击提交后创建待办并刷新列表，支持团队协作与登录，"
            "数据保存到数据库；"
            "数据实体 Todo(id, tenantId, name, status, createdAt)"
        )

    form.get_by_role("button", name="提交回答并继续").click()


def drive_to_confirmed_contract(panel) -> None:
    contract = panel.locator(".contract-viewer")
    for round_index in range(8):
        if contract.count() > 0 and contract.first.is_visible():
            break
        print(f"   澄清第 {round_index + 1} 轮")
        fill_clarification(panel)
        panel.page.wait_for_timeout(1200)
    expect(contract.first).to_be_visible(timeout=60_000)
    expect(panel.locator(".architect-status")).to_contain_text("等待确认")
    panel.get_by_role("button", name="确认契约，进入生成").click()
    expect(panel.locator(".architect-status")).to_contain_text("已确认", timeout=60_000)
    expect(panel.locator(".architect-outcome.is-success")).to_be_visible(timeout=30_000)


def read_gate_state(panel) -> str | None:
    gate = panel.locator("[data-supervisor-gate]")
    if gate.count() == 0:
        return None
    return gate.first.get_attribute("data-supervisor-gate")


def main() -> int:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        context.add_init_script(
            "window.localStorage.setItem('vibe.llmProvider', 'mock')"
        )
        page = context.new_page()
        sandbox_id: str | None = None
        console_logs: list[str] = []
        fullstack_requests: list[str] = []

        page.on(
            "console",
            lambda message: console_logs.append(f"{message.type}: {message.text}"),
        )

        def capture_request(request) -> None:
            if "/api/v1/runs/fullstack" in request.url:
                fullstack_requests.append(f"{request.method} {request.url}")
            if "/api/v1/supervisor/gates/" in request.url and request.url.endswith(
                "/release"
            ):
                fullstack_requests.append(f"{request.method} {request.url}")

        page.on("request", capture_request)

        try:
            print("== 1. 进入演示项目，生成并拖拽新组件 ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            expect(page.locator(".workspace-tab")).to_have_count(1)
            active = page.locator(".tab-window.is-active")
            expect(active.locator(".mock-card")).to_be_visible()

            card = active.locator(".mock-card").bounding_box()
            if not card:
                fail("找不到大卡片")
            page.mouse.move(
                card["x"] + card["width"] * 0.08,
                card["y"] + card["height"] * 0.08,
            )
            page.mouse.down()
            page.mouse.move(
                card["x"] + card["width"] * 0.45,
                card["y"] + card["height"] * 0.35,
                steps=12,
            )
            page.mouse.up()

            popover = active.locator(".canvas-popover")
            expect(popover).to_have_count(1)
            popover.locator("input").fill("添加一个提交按钮")
            popover.locator(".popover-send").click()
            wait_run_completed(active)
            sandbox_id = active.locator(".runtime-panel").get_attribute(
                "data-sandbox-id"
            )
            print(f"   首次生成完成 sandbox={sandbox_id}")

            view_result = active.locator(".view-result-cta")
            if view_result.is_visible():
                view_result.click()
            expect(active.locator(".preview-pane.is-visible")).to_be_visible()
            frame = page.frame_locator(".tab-window.is-active .runtime-preview-frame")
            added = frame.locator("button.vibe-added-button")
            expect(added).to_be_visible(timeout=60_000)

            placement = active.locator("[data-placement-frame]")
            expect(placement).to_be_visible(timeout=60_000)
            box = placement.bounding_box()
            if not box:
                fail("定位框没有可拖拽区域")
            start_x = box["x"] + box["width"] / 2
            start_y = box["y"] + box["height"] / 2
            page.mouse.move(start_x, start_y)
            page.mouse.down()
            page.mouse.move(start_x + 180, start_y + 90, steps=18)
            page.mouse.up()
            placement.locator("[data-placement-confirm]").click()
            wait_run_completed(active)

            print("\n== 2. 保存快照，进入全栈澄清 ==")
            # 保存快照已折叠进工具栏「更多」菜单。
            active.locator("[data-toolbar-more]").click()
            active.locator("[data-snapshot-save]").click()
            modal = page.locator(".snapshot-modal")
            expect(modal).to_be_visible()
            modal.locator("#snapshot-note").fill("全栈门控 E2E")
            modal.locator('button[type="submit"]').click()
            expect(page.locator(".workspace-toast.supervisor-toast")).to_contain_text(
                "快照已保存", timeout=60_000
            )
            expect(modal).to_have_count(0)

            print("\n== 2b. 新增一个未发送标注，触发 Supervisor both 门控 ==")
            # 回到画布，再圈一块区域并输入 UI 指令但不点发送；该标注会随
            # “下一步”一起提交，使路由同时命中 UI + 架构关键词 → handoff_both。
            active.get_by_role("button", name="画布", exact=True).click()
            expect(active.locator(".mock-card")).to_be_visible()
            card = active.locator(".mock-card").bounding_box()
            if not card:
                fail("找不到大卡片（第二轮标注）")
            page.mouse.move(
                card["x"] + card["width"] * 0.55,
                card["y"] + card["height"] * 0.12,
            )
            page.mouse.down()
            page.mouse.move(
                card["x"] + card["width"] * 0.78,
                card["y"] + card["height"] * 0.28,
                steps=12,
            )
            page.mouse.up()
            popover = active.locator(".canvas-popover")
            expect(popover).to_have_count(1)
            popover.locator("input").fill("把提交按钮改成蓝色")
            # 关键：不点击 popover-send，标注保持“未发送”状态。

            active.get_by_role(
                "button", name=re.compile("下一步：构建全栈方案")
            ).click()
            panel = page.locator(".architect-panel")
            expect(panel).to_be_visible(timeout=30_000)
            gate = panel.locator("[data-supervisor-gate]")
            expect(gate).to_be_visible(timeout=60_000)
            print(f"   门控已创建：{gate.get_attribute('data-supervisor-gate')}")

            print("\n== 3. 澄清到契约并确认 ==")
            drive_to_confirmed_contract(panel)

            # 契约确认后，both 门控必须自动释放；页面无需手动点“释放门控”。
            expect(gate).to_have_attribute(
                "data-supervisor-gate", "RELEASED", timeout=60_000
            )
            print("   门控已自动释放：RELEASED")

            print("\n== 4. 检查【开始生成全栈代码】按钮门控 ==")
            trigger = panel.locator("[data-fullstack-generate]")
            expect(trigger).to_be_visible(timeout=30_000)
            disabled = trigger.is_disabled()
            gate_state = read_gate_state(panel)
            wrapper = panel.locator(".architect-fullstack-trigger-wrap")
            hint = panel.locator("[data-fullstack-disabled-reason]")
            print(f"   gate={gate_state} disabled={disabled}")
            print(f"   tooltip={wrapper.get_attribute('data-tooltip')!r}")
            print(f"   可见禁用提示={hint.inner_text() if hint.count() else '无'}")
            print("   Console 最近 20 行：")
            for line in console_logs[-20:]:
                print(f"     {line}")
            expect(panel.locator("[data-fullstack-ready]")).to_have_attribute(
                "data-fullstack-ready", "true"
            )
            if disabled:
                fail(
                    "【开始生成全栈代码】仍被禁用："
                    f"gate={gate_state} "
                    f"hint={hint.inner_text() if hint.count() else '无'}"
                )

            # both 门控：生成按钮出现时，门控必须已经 RELEASED（或本来就没有）。
            if gate_state is not None and gate_state not in {"RELEASED"}:
                print(f"   等待门控释放，当前 {gate_state}")
                expect(panel.locator("[data-supervisor-gate]")).to_have_attribute(
                    "data-supervisor-gate", "RELEASED", timeout=60_000
                )

            print("\n== 5. 点击生成并等待六节点完成 ==")
            trigger.click()
            expect(panel.locator("[data-fullstack-progress]")).to_be_visible(
                timeout=30_000
            )
            expect(panel.locator("[data-fullstack-status]")).to_have_attribute(
                "data-fullstack-status", "SUCCEEDED", timeout=240_000
            )
            for node in ("DBACoder", "BackendCoder", "FrontendCoder"):
                expect(
                    panel.locator(f'[data-fullstack-step="{node}"]')
                ).to_have_attribute("data-status", "SUCCEEDED")
            print("   OK：DBACoder / BackendCoder / FrontendCoder 均 SUCCEEDED")

            page.screenshot(
                path=str(SCREENSHOT_DIR / "fullstack-gate-ui-e2e-completed.png"),
                full_page=True,
            )

            print("\n== 6. 沙盒容器内断言 server/ 与 SQLite 数据文件 ==")
            container = f"{CONTAINER_PREFIX}{sandbox_id}"
            running = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", container],
                capture_output=True,
                text=True,
                check=False,
            )
            if running.returncode != 0 or running.stdout.strip() != "true":
                fail(f"沙盒容器未运行: {container} -> {running.stderr.strip()}")
            assert_sandbox_files(sandbox_id)

            if fullstack_requests:
                print("   请求轨迹：")
                for item in fullstack_requests:
                    print(f"     {item}")
            post_index = next(
                (
                    index
                    for index, item in enumerate(fullstack_requests)
                    if "/api/v1/runs/fullstack" in item
                ),
                None,
            )
            if post_index is None:
                fail("浏览器没有真正发出 POST /api/v1/runs/fullstack")

            release_index = next(
                (
                    index
                    for index, item in enumerate(fullstack_requests)
                    if "/api/v1/supervisor/gates/" in item and item.endswith("/release")
                ),
                None,
            )
            if release_index is None:
                fail("both 门控没有发出 release 请求")
            if release_index > post_index:
                fail(
                    "门控 release 晚于全栈生成请求，UI 锁未先释放："
                    f"release=#{release_index} fullstack=#{post_index}"
                )
            print(
                f"   OK：release 请求先于全栈生成（release=#{release_index} "
                f"fullstack=#{post_index}）"
            )

            print("\nFULLSTACK GATE UI E2E: PASS")
            return 0
        finally:
            cleanup_sandbox(sandbox_id)
            context.close()
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
