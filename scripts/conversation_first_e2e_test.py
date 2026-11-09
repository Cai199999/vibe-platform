#!/usr/bin/env python3
"""E2E：新建项目 -> 开始创建 -> 纯对话敲定契约 -> 自动生成初始前端。

覆盖对话前置完整链路：
  1. 新建项目后画布不再渲染 Vibe Studio Mock，只显示【✨ 开始创建】；
  2. 点击后打开 Architect 面板，用户用自然语言描述目标即可启动；
  3. 澄清表单不再出现“全栈 / 轻量 / 脚本”三选一卡片；
  4. 契约确认后自动触发 FrontendCoder（三节点），无需手动点击生成；
  5. 画布自动切换为沙盒实时页面，且页面内容来自契约路由与组件。

前置：
    docker compose up -d
    /tmp/vibe-browser/bin/python scripts/conversation_first_e2e_test.py
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
BFF_URL = "http://127.0.0.1:8080"

GOAL = (
    "我要做一个团队任务看板：成员可以登录，创建和指派任务，"
    "拖拽任务状态，数据需要长期保存到数据库。"
)


def cleanup_sandbox(sandbox_id: str | None) -> None:
    if not sandbox_id:
        return
    request = urllib.request.Request(
        f"{BFF_URL}/api/v1/sandboxes/{sandbox_id}",
        method="DELETE",
    )
    try:
        urllib.request.urlopen(request, timeout=20).close()
    except Exception:  # noqa: BLE001 - 清理失败不影响测试结论
        print(f"WARN: 清理沙盒失败 {sandbox_id}")


def middle_drag(page, x: float, y: float, dx: float = 48, dy: float = 24) -> None:
    """在指定坐标执行一次中键拖拽。"""
    page.mouse.move(x, y)
    page.mouse.down(button="middle")
    page.mouse.move(x + dx, y + dy, steps=6)
    page.mouse.up(button="middle")
    page.wait_for_timeout(250)


def assert_canvas_fill(page, label: str) -> None:
    """断言画布外壳、舞台和 live 页面严格铺满，无黑边 / 无裁切。"""
    metrics = page.evaluate("""() => {
            const box = (node) => {
                const rect = node.getBoundingClientRect();
                return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
            };
            const shell = document.querySelector('.app-shell-workspace');
            const stage = document.querySelector('.tab-window.is-active .canvas-stage');
            const live = document.querySelector('.tab-window.is-active .canvas-live-page');
            const viewport = document.querySelector('.tab-window.is-active .canvas-viewport');
            if (!shell || !stage || !live || !viewport) return null;
            const matrix = new DOMMatrix(getComputedStyle(viewport).transform);
            return {
                innerHeight: window.innerHeight,
                documentHeight: document.documentElement.scrollHeight,
                shell: box(shell),
                stage: box(stage),
                live: box(live),
                scaleX: matrix.a,
                scaleY: matrix.d,
                logicalWidth: viewport.offsetWidth,
                logicalHeight: viewport.offsetHeight,
            };
        }""")
    if not metrics:
        raise AssertionError(f"{label}: 画布节点缺失")

    def near(actual: float, expected: float, tolerance: float = 1.5) -> bool:
        return abs(actual - expected) <= tolerance

    if not near(metrics["shell"]["height"], metrics["innerHeight"], 1):
        raise AssertionError(
            f"{label}: 应用壳高度未铺满视口 "
            f"{metrics['shell']['height']} vs {metrics['innerHeight']}"
        )
    if metrics["documentHeight"] > metrics["innerHeight"] + 1:
        raise AssertionError(
            f"{label}: 页面出现纵向滚动 "
            f"{metrics['documentHeight']} > {metrics['innerHeight']}"
        )
    for key, label_text in (("width", "宽度"), ("height", "高度")):
        if not near(metrics["live"][key], metrics["stage"][key]):
            raise AssertionError(
                f"{label}: 页面{label_text}未铺满画布 "
                f"{metrics['live'][key]} vs {metrics['stage'][key]}"
            )
    for key, label_text in (("x", "左边距"), ("y", "顶边距")):
        if not near(metrics["live"][key], metrics["stage"][key]):
            raise AssertionError(
                f"{label}: 页面{label_text}存在偏移 "
                f"{metrics['live'][key]} vs {metrics['stage'][key]}"
            )
    # transform 后的逻辑视口必须精确等于舞台尺寸，证明没有 cover 裁切。
    if not near(metrics["scaleX"] * metrics["logicalWidth"], metrics["stage"]["width"]):
        raise AssertionError(f"{label}: 横向缩放未铺满 {metrics}")
    if not near(
        metrics["scaleY"] * metrics["logicalHeight"], metrics["stage"]["height"]
    ):
        raise AssertionError(f"{label}: 纵向缩放未铺满 {metrics}")
    print(
        f"   OK {label}: stage={metrics['stage']['width']:.0f}×"
        f"{metrics['stage']['height']:.0f} / "
        f"logical={metrics['logicalWidth']}×{metrics['logicalHeight']}"
    )


def assert_preview_fill(page, label: str) -> None:
    """断言预览 iframe 与预览屏幕严格重合，没有黑边或裁切。"""
    metrics = page.evaluate("""() => {
            const box = (node) => {
                const rect = node.getBoundingClientRect();
                return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
            };
            const screen = document.querySelector(
                '.tab-window.is-active .runtime-preview-screen');
            const frame = document.querySelector(
                '.tab-window.is-active .runtime-preview-frame');
            if (!screen || !frame) return null;
            const matrix = new DOMMatrix(getComputedStyle(frame).transform);
            return {
                screen: box(screen),
                frame: box(frame),
                scaleX: matrix.a,
                scaleY: matrix.d,
                logicalWidth: frame.offsetWidth,
                logicalHeight: frame.offsetHeight,
            };
        }""")
    if not metrics:
        raise AssertionError(f"{label}: 预览节点缺失")
    for key in ("x", "y", "width", "height"):
        if abs(metrics["frame"][key] - metrics["screen"][key]) > 1.5:
            raise AssertionError(f"{label}: 预览 iframe 未铺满 {key}: {metrics}")
    if (
        abs(metrics["scaleX"] * metrics["logicalWidth"] - metrics["screen"]["width"])
        > 1.5
    ):
        raise AssertionError(f"{label}: 预览横向缩放未铺满 {metrics}")
    if (
        abs(metrics["scaleY"] * metrics["logicalHeight"] - metrics["screen"]["height"])
        > 1.5
    ):
        raise AssertionError(f"{label}: 预览纵向缩放未铺满 {metrics}")
    print(
        f"   OK {label}: preview={metrics['screen']['width']:.0f}×"
        f"{metrics['screen']['height']:.0f} / "
        f"logical={metrics['logicalWidth']}×{metrics['logicalHeight']}"
    )


def fill_clarification(panel) -> None:
    """填写当前一轮澄清：选择项取第一项，自由文本按问题语义作答。"""
    form = panel.locator(".architect-clarification")
    expect(form).to_be_visible(timeout=60_000)
    # 形态不再有卡片；若回归成三选一，这里必须失败。
    assert form.locator(".shape-choice").count() == 0, "不应出现应用形态三选一卡片"

    groups = form.locator("fieldset.architect-question")
    for index in range(groups.count()):
        group = groups.nth(index)
        pills = group.locator(".choice-pills button")
        if pills.count() == 0:
            continue
        if group.locator(".choice-pills button.is-selected").count() == 0:
            pills.first.click()

    areas = form.locator("label.architect-question:not(.architect-comment) textarea")
    for index in range(areas.count()):
        area = areas.nth(index)
        label = area.locator("xpath=..").inner_text()
        if "业务动作" in label:
            area.fill("成员登录 -> 创建任务 -> 指派负责人 -> 拖拽任务状态")
        elif "数据实体" in label:
            area.fill(
                "Task(id, title, status, assigneeId, createdAt)；"
                "Member(id, name)；一个 Member 可以负责多个 Task"
            )
        else:
            area.fill(GOAL)

    form.get_by_role("button", name="提交回答并继续").click()


def drive_to_confirmed_contract(page, panel) -> None:
    contract = panel.locator(".contract-viewer")
    for round_index in range(10):
        if contract.count() > 0 and contract.first.is_visible():
            break
        print(f"   澄清第 {round_index + 1} 轮")
        fill_clarification(panel)
        page.wait_for_timeout(900)

    expect(contract.first).to_be_visible(timeout=60_000)
    expect(panel.locator(".architect-status")).to_contain_text("等待确认")
    panel.get_by_role("button", name="确认契约，进入生成").click()
    expect(panel.locator(".architect-status")).to_contain_text("已确认", timeout=60_000)


def main() -> int:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    sandbox_id = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()
        try:
            print("== 1. 登录并新建空白项目 ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            expect(page.locator(".workspace-tab")).to_have_count(1)
            page.locator(".tab-new").click()
            expect(page.locator(".workspace-tab")).to_have_count(2)

            active = page.locator(".tab-window.is-active")
            active.locator(".model-picker select").select_option("mock")
            # 新项目画布必须是空白引导，而不是 Vibe Studio Mock 卡片。
            expect(active.locator(".mock-page")).to_have_count(0)
            start_button = active.locator(".canvas-start-button")
            expect(start_button).to_be_visible(timeout=30_000)
            expect(start_button).to_contain_text("开始创建")
            page.screenshot(
                path=str(SCREENSHOT_DIR / "conversation-first-blank.png"),
                full_page=True,
            )
            print("   OK：空白画布只显示【✨ 开始创建】")

            print("\n== 2. 点击开始创建，用自然语言启动架构对话 ==")
            start_button.click()
            panel = page.locator(".architect-panel")
            expect(panel).to_be_visible(timeout=30_000)
            goal_input = panel.locator("[data-architect-goal]")
            expect(goal_input).to_be_visible(timeout=10_000)
            goal_input.fill(GOAL)
            panel.locator("[data-architect-start]").click()

            print("\n== 3. 纯对话补齐契约，确认后自动初始化前端 ==")
            drive_to_confirmed_contract(page, panel)
            bootstrap = panel.locator("[data-frontend-bootstrap-status]")
            expect(bootstrap).to_be_visible(timeout=30_000)
            # 契约确认后自动运行 FrontendCoder / BuildVerifier / SandboxRestart。
            expect(bootstrap).to_have_attribute(
                "data-frontend-bootstrap-status", "SUCCEEDED", timeout=240_000
            )
            expect(panel.locator("[data-fullstack-step]")).to_have_count(3)
            for node in ("FrontendCoder", "BuildVerifier", "SandboxRestart"):
                expect(
                    panel.locator(f'[data-fullstack-step="{node}"]')
                ).to_have_attribute("data-status", "SUCCEEDED")
            expect(panel.locator(".architect-outcome.is-success").last).to_be_visible()
            page.screenshot(
                path=str(SCREENSHOT_DIR / "conversation-first-bootstrap.png"),
                full_page=True,
            )
            print("   OK：三节点自动完成，无需手动点击生成")

            print("\n== 4. 关闭面板，画布切换为真实前端页面 ==")
            panel.get_by_role("button", name="关闭架构师面板").click()
            expect(panel).to_be_hidden()
            live_frame = active.locator(".canvas-live-frame")
            expect(live_frame).to_be_visible(timeout=30_000)
            frame = page.frame_locator(".tab-window.is-active .canvas-live-frame")
            expect(frame.locator("body")).to_contain_text("总览", timeout=60_000)
            expect(frame.locator("body")).to_contain_text("RecordList")
            expect(frame.locator("body")).to_contain_text("SubmitForm")
            expect(active.locator(".canvas-start-state")).to_have_count(0)
            page.screenshot(
                path=str(SCREENSHOT_DIR / "conversation-first-live-canvas.png"),
                full_page=True,
            )
            print("   OK：画布已替换为契约生成的真实 React 页面")

            print("\n== 4b. 窗口缩放后画布始终铺满且不裁切 ==")
            for width, height in (
                (1600, 1000),
                (1280, 800),
                (1920, 1080),
                (1600, 1000),
            ):
                page.set_viewport_size({"width": width, "height": height})
                page.wait_for_timeout(450)
                assert_canvas_fill(page, f"{width}×{height}")
            print("   小 / 中 / 大窗口均无黑边、无页面滚动")

            print("\n== 4c. 预览面板同样自适应铺满 ==")
            active.get_by_role("button", name="预览", exact=True).click()
            expect(active.locator(".runtime-preview-frame")).to_be_visible()
            page.wait_for_timeout(450)
            assert_preview_fill(page, "预览 1600×1000")
            page.set_viewport_size({"width": 1280, "height": 800})
            page.wait_for_timeout(450)
            assert_preview_fill(page, "预览 1280×800")
            page.set_viewport_size({"width": 1600, "height": 1000})
            page.wait_for_timeout(450)
            active.get_by_role("button", name="画布", exact=True).click()
            expect(active.locator(".canvas-pane.is-visible")).to_be_visible()
            print("   预览 iframe 与屏幕区域严格重合")

            print("\n== 5. 标注态 / 交互态自动切换 ==")
            stage = active.locator(".canvas-stage")
            expect(stage).to_have_attribute("data-canvas-interaction", "annotate")
            # 标注态：iframe 不吃指针，单击页面中心先“放行”指针。
            page_box = active.locator(".canvas-live-page").bounding_box()
            if not page_box:
                raise AssertionError("无法读取 live 页面位置")
            page.mouse.click(
                page_box["x"] + page_box["width"] / 2,
                page_box["y"] + page_box["height"] / 2,
            )
            expect(stage).to_have_attribute("data-canvas-interaction", "interact")
            expect(live_frame).to_have_css("pointer-events", "auto")
            # 交互态：iframe 内部按钮可以直接点击。
            frame.locator("body").click(position={"x": 40, "y": 40})

            print("\n== 6. 两种状态都保留中键平移 ==")
            canvas_viewport = active.locator(".canvas-viewport")
            center_x = page_box["x"] + page_box["width"] / 2
            center_y = page_box["y"] + page_box["height"] / 2
            # 交互态：中键事件由沙盒内的 inspect SDK 转发给父页面。
            before_interact = canvas_viewport.get_attribute("style")
            middle_drag(page, center_x, center_y)
            after_interact = canvas_viewport.get_attribute("style")
            if before_interact == after_interact:
                raise AssertionError("交互态中键平移未生效")
            # 点击工具栏开关回到标注态，覆盖层重新接管指针。
            active.locator("[data-canvas-interaction-toggle]").click()
            expect(stage).to_have_attribute("data-canvas-interaction", "annotate")
            expect(live_frame).to_have_css("pointer-events", "none")
            # 标注态：中键由父页面直接处理。
            before_annotate = canvas_viewport.get_attribute("style")
            middle_drag(page, center_x, center_y, dx=-36, dy=-18)
            after_annotate = canvas_viewport.get_attribute("style")
            if before_annotate == after_annotate:
                raise AssertionError("标注态中键平移未生效")
            print("   OK：单击页面进入交互，点开关回到标注，中键始终可平移")

            # 从浏览器恢复快照里取本次项目的 sandboxId，测试结束清理容器。
            sandbox_id = page.evaluate("""() => {
                  const workspace = JSON.parse(
                    window.localStorage.getItem('vibe.workspace.v1') || '{}')
                  const architect = JSON.parse(
                    window.sessionStorage.getItem('vibe.architect.v1') || '{}')
                  const project = workspace.projects?.[workspace.projects.length - 1]
                  return project ? architect[project.id]?.fullstack?.sandboxId : null
                }""")
            print(f"\nPASS：对话前置链路完成（sandbox={sandbox_id}）")
            return 0
        finally:
            context.close()
            browser.close()
            cleanup_sandbox(sandbox_id)


if __name__ == "__main__":
    sys.exit(main())
