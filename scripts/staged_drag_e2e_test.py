#!/usr/bin/env python3
"""真实浏览器 E2E：暂存组件拖拽 → 同一父容器连续插入 → 保存布局。

验收目标（对应本轮三个边界任务）：

  1. 沙盒镜像必须包含最新 ``inspect_sdk.js``，新容器能响应
     ``stage-hit`` / ``stage-hit-result`` 协议；
  2. 圈选新增 → 视觉暂存（故意不调用 LLM）→ 拖到同一个卡片容器，
     拖动过程中父容器高亮框 / 插入线必须出现；
  3. 同容器连续插入两个按钮后点【保存布局】，后端 BATCH_LAYOUT 一次
     写盘，最终源码必须按拖入顺序落在同一个父容器内，且没有任何
     ``position:absolute`` / ``left`` / ``top`` 绝对定位；
  4. 拖动中按 Esc、以及拖动中触发 iframe 路由切换，都不能残留
     “幽灵组件”。

运行（依赖本机已启动 5173 / 8080 / 8100）：

    /tmp/vibe-browser/bin/python scripts/staged_drag_e2e_test.py

脚本会把真实截图与 Diff 写到 ``docs/screenshots/``。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

WEB_URL = "http://127.0.0.1:5173/"
PY_AI_URL = "http://127.0.0.1:8100"
SANDBOX_IMAGE = "vibe-platform/sandbox-react:dev"
SHOTS = Path("docs/screenshots")
DESIGN_WIDTH = 1440
DESIGN_HEIGHT = 900
# 诊断用：仅记录与 Run / dispatch 有关的 HTTP 结果。
NETWORK: list[str] = []


def log(message: str) -> None:
    print(message, flush=True)


def shot(page: Page, name: str) -> Path:
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / name
    page.screenshot(path=str(path))
    log(f"   screenshot -> {path}")
    return path


def docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["docker", *args],
        check=False,
        text=True,
        capture_output=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def sandbox_container(sandbox_id: str) -> str:
    """按 label 精确找到本次 Run 的容器，避免误读旧沙盒。"""
    output = docker(
        "ps",
        "--filter",
        f"label=vibe.sandbox.id={sandbox_id}",
        "--format",
        "{{.Names}}",
    ).strip()
    if not output:
        raise AssertionError(f"找不到运行中的沙盒容器: {sandbox_id}")
    return output.splitlines()[0]


def sandbox_exec(sandbox_id: str, *args: str) -> str:
    return docker("exec", sandbox_container(sandbox_id), *args)


def cleanup_sandbox(sandbox_id: str | None) -> None:
    if not sandbox_id:
        return
    request = urllib.request.Request(
        f"{PY_AI_URL}/api/v1/internal/sandbox/{sandbox_id}",
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            log(f"   cleanup {sandbox_id} -> HTTP {response.status}")
    except Exception as exc:  # noqa: BLE001 - 清理失败不掩盖主流程
        log(f"   cleanup {sandbox_id} -> {exc}")


def wait_run(active) -> None:
    """等待一次常规 UI Run 结束（与 direct_drag_sync_e2e_test 一致）。"""
    trace = active.locator("[data-supervisor-trace]")
    expect(trace).to_be_visible(timeout=30_000)
    expect(trace).not_to_have_attribute(
        "data-supervisor-phase", "analyzing", timeout=240_000
    )
    expect(active.locator(".runtime-overlay")).to_have_count(0, timeout=240_000)
    expect(active.locator(".runtime-result-overlay .status-pill")).to_contain_text(
        "已完成", timeout=30_000
    )


def force_live_canvas(page: Page) -> None:
    """测试夹具：跳过全栈架构流程，直接把当前项目切到 live 画布。"""
    page.evaluate("""() => {
          const key = 'vibe.workspace.v1'
          const state = JSON.parse(localStorage.getItem(key))
          const project = state.projects.find(
            item => item.id === state.activeTabId
          )
          project.canvasMode = 'live'
          project.bootstrap = 'ready'
          localStorage.setItem(key, JSON.stringify(state))
        }""")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(2500)


def probe_stage_hit(page: Page, x: float, y: float, timeout: int = 3000) -> dict:
    """从父页面直接发 stage-hit，验证新沙盒确实运行了最新 SDK。"""
    return page.evaluate(
        """async ({x, y, timeout}) => {
          const frame = document.querySelector(
            '.tab-window.is-active iframe.canvas-live-frame'
          )
          if (!frame?.contentWindow) return {ok: false, reason: 'NO_FRAME'}
          const requestId = 'e2e_probe_' + Math.random().toString(36).slice(2)
          return await new Promise((resolve) => {
            const timer = setTimeout(() => {
              window.removeEventListener('message', onMessage)
              resolve({ok: false, reason: 'TIMEOUT'})
            }, timeout)
            function onMessage(event) {
              const data = event.data
              if (!data || data.channel !== 'vibe-inspect') return
              if (data.type !== 'stage-hit-result') return
              if (data.payload?.requestId !== requestId) return
              clearTimeout(timer)
              window.removeEventListener('message', onMessage)
              resolve(data.payload)
            }
            window.addEventListener('message', onMessage)
            frame.contentWindow.postMessage(
              {
                channel: 'vibe-inspect',
                type: 'stage-hit',
                payload: {requestId, x, y},
              },
              '*',
            )
          })
        }""",
        {"x": x, "y": y, "timeout": timeout},
    )


def draw_circle(page: Page, start: tuple[float, float], end: tuple[float, float]):
    """在画布上画一个矩形圈选（屏幕坐标）。"""
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(*end, steps=16)
    page.mouse.up()


def _intersects(a: dict, b: dict, pad: int = 12) -> bool:
    return not (
        a["x"] + a["width"] + pad <= b["x"]
        or b["x"] + b["width"] + pad <= a["x"]
        or a["y"] + a["height"] + pad <= b["y"]
        or b["y"] + b["height"] + pad <= a["y"]
    )


def choose_draw_rect(page: Page, active) -> dict:
    """在画布可见区域内选一块未被浮层/幽灵遮挡的空白画圈区。

    标注浮层会拦截 pointerdown（stopPropagation），如果下一笔从浮层上
    起笔，画布就收不到手势，画圈会静默失败。这里主动避开所有已知浮层。
    """
    stage = active.locator(".canvas-stage").bounding_box()
    frame_box = active.locator("iframe.canvas-live-frame").bounding_box()
    if not stage or not frame_box:
        raise AssertionError("画布 / iframe 尚未就绪")
    left = max(stage["x"], frame_box["x"]) + 24
    top = max(stage["y"], frame_box["y"]) + 24
    right = min(stage["x"] + stage["width"], frame_box["x"] + frame_box["width"]) - 24
    bottom = (
        min(stage["y"] + stage["height"], frame_box["y"] + frame_box["height"]) - 24
    )
    obstacles = active.locator(
        ".canvas-popover, .pending-addition-ghost, .viewport-controls, "
        ".canvas-chrome, [data-annotation-limit]"
    ).evaluate_all(
        """els => els.map((el) => {
          const rect = el.getBoundingClientRect()
          return {
            x: rect.x, y: rect.y,
            width: rect.width, height: rect.height,
          }
        })"""
    )
    width, height = 120, 70
    y = top
    while y + height < bottom:
        x = left
        while x + width < right:
            candidate = {"x": x, "y": y, "width": width, "height": height}
            if not any(_intersects(candidate, item) for item in obstacles):
                blocked = page.evaluate(
                    """({x, y}) => {
                      const el = document.elementFromPoint(x, y)
                      if (!el) return true
                      return Boolean(el.closest(
                        '.canvas-popover, .pending-addition-ghost, '
                        + '.viewport-controls'
                      ))
                    }""",
                    {"x": x, "y": y},
                )
                if not blocked:
                    return candidate
            x += 140
        y += 80
    raise AssertionError("找不到未被浮层遮挡的可画圈区域")


def popover_ids(active) -> set[str]:
    return set(
        active.locator(".canvas-popover").evaluate_all(
            "els => els.map(el => el.dataset.annotationId).filter(Boolean)"
        )
    )


def stage_addition(page: Page, active, start, end, intent: str) -> str:
    """画圈 → 强制 CREATE_NEW → 写意图 → 发送（只暂存，不触发 Run）。"""
    before_ids = popover_ids(active)
    draw_circle(page, start, end)
    # 不能依赖 DOM 顺序取 .last：标注浮层可能因激活 / 重渲染换序，
    # 误删上一条已执行标注会触发 Git 回滚，直接污染本轮验收。
    deadline = time.time() + 15
    new_id = None
    while time.time() < deadline and not new_id:
        new_ids = popover_ids(active) - before_ids
        if new_ids:
            new_id = sorted(new_ids)[0]
            break
        page.wait_for_timeout(200)
    if not new_id:
        info = page.evaluate(
            """({x, y}) => {
              const rectOf = (node) => {
                const rect = node.getBoundingClientRect()
                return {
                  x: rect.x, y: rect.y,
                  width: rect.width, height: rect.height,
                }
              }
              const stage = document.querySelector(
                '.tab-window.is-active .canvas-stage'
              )
              const frame = document.querySelector(
                '.tab-window.is-active iframe.canvas-live-frame'
              )
              const hit = document.elementFromPoint(x, y)
              return {
                hit: hit ? `${hit.tagName}.${hit.className}` : null,
                interaction: stage?.dataset.canvasInteraction ?? null,
                stage: stage ? rectOf(stage) : null,
                frame: frame ? rectOf(frame) : null,
                popovers: [...document.querySelectorAll(
                  '.tab-window.is-active .canvas-popover'
                )].map((item) => ({
                  id: item.dataset.annotationId,
                  hidden: item.classList.contains('is-hidden'),
                  ...rectOf(item),
                })),
                ghosts: [...document.querySelectorAll(
                  '.tab-window.is-active .pending-addition-ghost'
                )].map(rectOf),
              }
            }""",
            {"x": start[0], "y": start[1]},
        )
        log("   draw diagnostics: " + json.dumps(info, ensure_ascii=False))
        shot(page, "staged-drag-no-popover.png")
        raise AssertionError("画圈后没有生成新的标注浮层（可能已达标注上限）")
    popover = active.locator(f'.canvas-popover[data-annotation-id="{new_id}"]')
    expect(popover).to_be_visible(timeout=15_000)
    expect(popover.locator("input")).to_be_enabled(timeout=15_000)
    # 默认动作可能因圈中元素被推断成 MODIFY；视觉暂存必须显式切到“新增”。
    if popover.get_attribute("data-action") != "CREATE_NEW":
        popover.locator(".popover-action button", has_text="新增").click()
    expect(popover).to_have_attribute("data-action", "CREATE_NEW")
    annotation_id = popover.get_attribute("data-annotation-id")
    popover.locator("input").fill(intent)
    popover.locator(".popover-send").click()
    expect(page.locator(".workspace-toast.supervisor-toast")).to_contain_text(
        "已加入未保存变更", timeout=10_000
    )
    ghost = active.locator(
        f'.pending-addition-ghost[data-pending-addition-id="{annotation_id}"]'
    )
    expect(ghost).to_be_visible(timeout=10_000)
    log(f"   staged {annotation_id}: {intent}")
    return annotation_id


def drag_ghost_to_card(page: Page, active, annotation_id: str, target) -> None:
    """拖动暂存卡片的把手到目标点，途中断言父容器高亮。"""
    ghost = active.locator(
        f'.pending-addition-ghost[data-pending-addition-id="{annotation_id}"]'
    )
    handle = ghost.locator(".pending-addition-drag-handle")
    expect(handle).to_be_visible(timeout=10_000)
    handle_box = handle.bounding_box()
    if not handle_box:
        raise AssertionError("拖拽把手不可见")
    target_x, target_y = target
    page.mouse.move(
        handle_box["x"] + handle_box["width"] / 2,
        handle_box["y"] + handle_box["height"] / 2,
    )
    page.mouse.down()
    page.mouse.move(target_x, target_y, steps=24)
    page.wait_for_timeout(900)
    drop_target = active.locator(".stage-drop-target")
    expect(drop_target).to_be_visible(timeout=8_000)
    tag = active.locator(".stage-drop-tag").inner_text()
    log(f"   drag {annotation_id} -> highlight: {tag}")
    # 高亮阶段截图：真实的 iframe 父容器命中框 + 插入线。
    shot(page, f"staged-drag-highlight-{annotation_id}.png")
    page.mouse.up()
    expect(ghost.locator(".pending-addition-placement-chip")).to_be_visible(
        timeout=10_000
    )
    chip = ghost.locator(".pending-addition-placement-chip").inner_text()
    log(f"   placed {annotation_id}: {chip}")


def answer_visible_clarification(page: Page, answered: dict[str, float]) -> bool:
    """回答保存布局过程中弹出的结构化澄清（视觉样式）。

    新增组件的 Planner 会先暂停 Run 询问样式；这不是失败，前端也不会
    弹 Toast（草稿必须保留）。E2E 必须像真实用户一样点选“推荐项”继续，
    否则恢复后的 Run 永远不会执行，也就拿不到最终 Diff。
    """
    panels = page.locator("[data-clarification-quick]")
    count = panels.count()
    for index in range(count):
        panel = panels.nth(index)
        try:
            if not panel.is_visible():
                continue
            annotation = panel.locator("xpath=ancestor::*[@data-annotation-id][1]")
            annotation_id = annotation.get_attribute("data-annotation-id") or "?"
            recommended = panel.locator(
                '[data-clarification-quick-option][data-recommended="true"]'
            )
            options = panel.locator("[data-clarification-quick-option]")
            option = recommended.first if recommended.count() else options.first
            if options.count() == 0:
                continue
            option_id = option.get_attribute("data-clarification-quick-option") or "?"
            key = f"{annotation_id}:{option_id}"
            now = time.time()
            # 点击后 Run 会立即进入 submitting，面板短暂保留；12 秒内不重复点击。
            if now - answered.get(key, 0) < 12:
                continue
            # 取色模式 / 提交中按钮是 disabled 的，不能阻塞 30 秒等它变可点。
            if not option.is_enabled():
                continue
            answered[key] = now
            label = (option.inner_text() or "").strip().replace("\n", " ")[:80]
            log(f"   clarification {annotation_id}: 选择推荐项「{label}」")
            option.click(timeout=3_000)
            return True
        except Exception as exc:  # noqa: BLE001 - 面板可能在轮询间隙重渲染
            log(f"   clarification probe skipped: {exc}")
    return False


def wait_layout_save(page: Page, expected: int, timeout: float = 150.0) -> None:
    """等待保存布局成功；期间自动回答视觉样式澄清。

    超时 / 失败时保留 Draft 并允许上层重试。
    """
    deadline = time.time() + timeout
    last = ""
    answered: dict[str, float] = {}
    while time.time() < deadline:
        toasts = [
            item.inner_text()
            for item in page.locator(".workspace-toast.supervisor-toast").all()
        ]
        last = " | ".join(toasts) if toasts else last
        if any(f"已同步 {expected} 个新增组件" in text for text in toasts):
            return
        if any(("同步超时" in text) or ("同步失败" in text) for text in toasts):
            raise AssertionError(f"保存布局失败: {last}")
        # 样式澄清是暂停而不是失败：点推荐项后同一 Run 恢复执行。
        if answer_visible_clarification(page, answered):
            page.wait_for_timeout(1500)
            continue
        page.wait_for_timeout(500)
    # 超时诊断：按钮 / 草稿条 / Toast / 运行面板的实时状态全部打印，
    # 区分“点击没触发”“请求挂起”“前端计时器未触发”三种情况。
    state = page.evaluate("""() => {
          const text = (selector) => {
            const node = document.querySelector(selector)
            return node ? (node.textContent || '').trim().slice(0, 300) : null
          }
          const save = document.querySelector('.layout-draft-save')
          return {
            draftBar: text('[data-layout-draft-bar]'),
            saveButton: save
              ? {disabled: save.disabled, text: save.textContent.trim()}
              : null,
            toasts: [...document.querySelectorAll('.workspace-toast')].map(
              (node) => node.textContent.trim()
            ),
            runtime: text('.runtime-panel'),
            result: text('.runtime-result-overlay'),
            bodyBusy: document.body.className,
          }
        }""")
    state["network"] = NETWORK[-12:]
    log("   save diagnostics: " + json.dumps(state, ensure_ascii=False))
    shot(page, "staged-drag-save-timeout.png")
    raise AssertionError(f"保存布局等待超时: {last}; state={state}")


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    image_id = docker("image", "inspect", SANDBOX_IMAGE, "--format", "{{.Id}}").strip()
    log(f"== 0. 当前沙盒镜像: {image_id} ==")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                f"--unsafely-treat-insecure-origin-as-secure={WEB_URL}",
            ],
        )
        context = browser.new_context(viewport={"width": 1680, "height": 1050})
        page = context.new_page()
        page.set_default_timeout(30_000)
        dispatches: list[dict] = []
        page.on(
            "request",
            lambda request: (
                dispatches.append(request.post_data_json)
                if request.url.endswith("/api/v1/supervisor/dispatch")
                else None
            ),
        )
        page.on(
            "response",
            lambda response: (
                NETWORK.append(
                    f"{response.status} {response.request.method} {response.url}"
                )
                if ("/supervisor/dispatch" in response.url or "/runs" in response.url)
                else None
            ),
        )
        page.on(
            "console",
            lambda message: (
                log(f"   [console.{message.type}] {message.text}")
                if message.type in {"error", "warning"}
                else None
            ),
        )
        sandbox_id: str | None = None
        try:
            log("== 1. 登录 / 一键体验 / 生成真实沙盒 ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            active = page.locator(".tab-window.is-active")
            expect(active.locator(".mock-card")).to_be_visible(timeout=30_000)
            page.wait_for_timeout(600)

            card = active.locator(".mock-card").bounding_box()
            if not card:
                raise AssertionError("找不到演示卡片")
            draw_circle(
                page,
                (card["x"] + card["width"] * 0.06, card["y"] + card["height"] * 0.08),
                (card["x"] + card["width"] * 0.92, card["y"] + card["height"] * 0.30),
            )
            popover = active.locator(".canvas-popover").last
            expect(popover).to_be_visible(timeout=15_000)
            if popover.get_attribute("data-action") != "MODIFY_EXISTING":
                popover.locator(".popover-action button", has_text="修改").click()
            popover.locator("input").fill("把标题文案改成「Vibe 工作台」")
            popover.locator(".popover-send").click()
            wait_run(active)
            sandbox_id = active.locator(".runtime-panel").get_attribute(
                "data-sandbox-id"
            )
            if not sandbox_id:
                raise AssertionError("Run 完成但没有拿到 sandboxId")
            log(f"   sandbox: {sandbox_id}")

            container = sandbox_container(sandbox_id)
            container_image = docker(
                "inspect", container, "--format", "{{.Image}}"
            ).strip()
            log(f"   container: {container} image: {container_image}")
            if container_image != image_id:
                raise AssertionError(
                    "新沙盒没有使用刚重建的镜像：" f"{container_image} != {image_id}"
                )

            log("== 2. 切 live 画布，确认最新 SDK 的 stage-hit 协议 ==")
            force_live_canvas(page)
            active = page.locator(".tab-window.is-active")
            frame = page.frame_locator(".tab-window.is-active iframe.canvas-live-frame")
            expect(active.locator("iframe.canvas-live-frame")).to_be_visible(
                timeout=30_000
            )
            # 先确认 iframe 里真的是渲染完成的 React 页面，再用真实
            # postMessage 探针验证 stage-hit 协议（SDK 收到消息才会注入样式，
            # 因此不能用 #vibe-inspect-style 判断 SDK 是否就绪）。
            expect(frame.locator(".vibe-card")).to_be_visible(timeout=30_000)
            card_point = frame.locator(".vibe-card").evaluate("""el => {
                  const rect = el.getBoundingClientRect()
                  return {
                    x: rect.left + rect.width / 2,
                    y: rect.bottom - 8,
                  }
                }""")
            probe: dict = {}
            deadline = time.time() + 20
            while time.time() < deadline:
                probe = probe_stage_hit(page, card_point["x"], card_point["y"])
                if probe.get("ok"):
                    break
                page.wait_for_timeout(500)
            log(f"   stage-hit probe: {json.dumps(probe, ensure_ascii=False)}")
            if not probe.get("ok"):
                raise AssertionError(f"stage-hit 协议失败: {probe}")
            if probe.get("parent", {}).get("marker") != "card":
                raise AssertionError(f"命中的父容器不是 card: {probe.get('parent')}")

            # 注意：.vibe-card 在跨域 iframe 内，必须通过 frame_locator 取
            # 屏幕坐标；frame_locator 返回的 bounding_box 已换算到父页面。
            card = frame.locator(".vibe-card").bounding_box()
            if not card:
                raise AssertionError("live 画布中找不到 .vibe-card")
            drop_point = (
                card["x"] + card["width"] / 2,
                card["y"] + card["height"] - 18,
            )

            log("== 3. 拖动中 Esc 取消：不得留下幽灵 / 落点 ==")
            # 先做 Esc 用例，完成后删除该标注，给后续两个正式新增留出
            # 3 个标注的上限（初始 MODIFY 标注仍占 1 个名额）。
            draw_rect = choose_draw_rect(page, active)
            esc_id = stage_addition(
                page,
                active,
                (draw_rect["x"], draw_rect["y"]),
                (
                    draw_rect["x"] + draw_rect["width"],
                    draw_rect["y"] + draw_rect["height"],
                ),
                "添加一个取消测试按钮",
            )
            ghost = active.locator(
                f'.pending-addition-ghost[data-pending-addition-id="{esc_id}"]'
            )
            handle_box = ghost.locator(".pending-addition-drag-handle").bounding_box()
            page.mouse.move(
                handle_box["x"] + handle_box["width"] / 2,
                handle_box["y"] + handle_box["height"] / 2,
            )
            page.mouse.down()
            page.mouse.move(drop_point[0], drop_point[1], steps=20)
            page.wait_for_timeout(900)
            expect(active.locator(".stage-drop-target")).to_be_visible(timeout=8_000)
            page.keyboard.press("Escape")
            expect(active.locator(".stage-drop-target")).to_have_count(0, timeout=5_000)
            expect(active.locator(".pending-addition-ghost.is-dragging")).to_have_count(
                0, timeout=5_000
            )
            # 取消提示是画布内的 stage-drag-notice，不是全局 Toast。
            expect(active.locator(".stage-drag-notice")).to_contain_text(
                "已取消拖动", timeout=8_000
            )
            page.mouse.up()
            # Esc 后该暂存项没有被落位，仍停留在原地。
            expect(ghost.locator(".pending-addition-placement-chip")).to_have_count(0)
            shot(page, "staged-drag-esc-cancelled.png")
            # 通过浮层按 ID 删除这条标注：视觉暂存未写码，只清本地草稿。
            # 切勿误删初始 MODIFY 标注，否则会触发一次 Git 回滚 Run。
            active.locator(
                f'.canvas-popover[data-annotation-id="{esc_id}"] .popover-remove'
            ).click()
            expect(ghost).to_have_count(0, timeout=8_000)
            expect(active.locator(".pending-addition-ghost")).to_have_count(0)
            expect(active.locator(".canvas-popover")).to_have_count(1)
            expect(
                active.locator(f'.canvas-popover[data-annotation-id="{esc_id}"]')
            ).to_have_count(0)

            log("== 4. 圈选两个新增组件（只暂存，不调用 LLM） ==")
            draw_rect = choose_draw_rect(page, active)
            first_id = stage_addition(
                page,
                active,
                (draw_rect["x"], draw_rect["y"]),
                (
                    draw_rect["x"] + draw_rect["width"],
                    draw_rect["y"] + draw_rect["height"],
                ),
                "添加一个提交按钮",
            )
            draw_rect = choose_draw_rect(page, active)
            second_id = stage_addition(
                page,
                active,
                (draw_rect["x"], draw_rect["y"]),
                (
                    draw_rect["x"] + draw_rect["width"],
                    draw_rect["y"] + draw_rect["height"],
                ),
                "添加一个重置按钮",
            )
            expect(active.locator(".pending-addition-ghost")).to_have_count(
                2, timeout=10_000
            )
            expect(active.locator("[data-layout-draft-bar]")).to_be_visible(
                timeout=10_000
            )
            shot(page, "staged-drag-two-ghosts.png")

            log("== 5. 连续拖入同一个父容器（卡片底部） ==")
            drag_ghost_to_card(page, active, first_id, drop_point)
            drag_ghost_to_card(page, active, second_id, drop_point)
            expect(active.locator(".pending-addition-placement-chip")).to_have_count(
                2, timeout=10_000
            )
            shot(page, "staged-drag-placed.png")

            log("== 6. 保存布局：同一父容器连续插入两个按钮 ==")
            before_log = sandbox_exec(
                sandbox_id, "git", "-C", "/app", "log", "--oneline", "-3"
            ).strip()
            log(f"   保存前 git log:\n{before_log}")
            active.locator(".layout-draft-save").click()
            wait_layout_save(page, 2)
            expect(active.locator(".pending-addition-ghost")).to_have_count(
                0, timeout=20_000
            )
            expect(active.locator("[data-layout-draft-bar]")).to_have_count(
                0, timeout=20_000
            )
            shot(page, "staged-drag-saved.png")

            if not dispatches:
                raise AssertionError("没有捕获到 supervisor dispatch")
            payload = dispatches[-1]
            log(
                "   dispatch payload:\n"
                + json.dumps(payload, ensure_ascii=False, indent=2)[:4000]
            )
            annotation = payload["annotations"][0]
            if annotation["action"] != "BATCH_LAYOUT":
                raise AssertionError(
                    f"action 不是 BATCH_LAYOUT: {annotation['action']}"
                )
            # buildBatchLayoutEntry 内部字段叫 additionBatch，但写进
            # AnnotationEnvelope.payload 时统一命名为 additions（Graph 读取
            # layout["additions"]）。
            additions = annotation["payload"].get("additions") or []
            if [item.get("annotationId") for item in additions] != [
                first_id,
                second_id,
            ]:
                raise AssertionError(f"additions 顺序不正确: {additions}")
            # placement 在 AnnotationEnvelope.payload 里（与 reference 同级），
            # 不在 annotation 顶层：Graph 的 _resolution_requests 同样读
            # payload.placement。
            placements = [
                (item.get("annotation") or {}).get("payload", {}).get("placement")
                for item in additions
            ]
            log(
                "   placements:\n"
                + json.dumps(placements, ensure_ascii=False, indent=2)[:3000]
            )
            if not all(isinstance(item, dict) for item in placements):
                raise AssertionError(f"新增组件缺少 placement: {placements}")
            if placements[0].get("targetParentId") != placements[1].get(
                "targetParentId"
            ):
                raise AssertionError("两次拖拽没有落到同一个父容器")
            if placements[0].get("insertPosition") not in {
                "after",
                "inside_index",
                "inside_end",
            }:
                raise AssertionError(f"落位语义异常: {placements[0]}")

            log("== 7. 校验最终源码 Diff（顺序 / 父容器 / 无绝对定位） ==")
            after_log = sandbox_exec(
                sandbox_id, "git", "-C", "/app", "log", "--oneline", "-3"
            ).strip()
            log(f"   保存后 git log:\n{after_log}")
            # 不要假设 HEAD 就是保存 Run 的提交：沙盒在 run.succeeded 后
            # 还可能追加 draft 提升 / 后续 Run。用 -S 精确找到“引入第二个
            # data-vibe-created 的提交”，Diff 才是本次保存的真实写盘证据。
            commit = sandbox_exec(
                sandbox_id,
                "git",
                "-C",
                "/app",
                "log",
                "-1",
                "--format=%H",
                "-S",
                f'data-vibe-created="{second_id}"',
                "--",
                "src/App.jsx",
            ).strip()
            if not commit:
                raise AssertionError("找不到包含第二个新增组件的提交")
            commit_subject = sandbox_exec(
                sandbox_id,
                "git",
                "-C",
                "/app",
                "log",
                "-1",
                "--format=%h %s",
                commit,
            ).strip()
            log(f"   本次写盘提交: {commit_subject}")
            diff = sandbox_exec(
                sandbox_id,
                "git",
                "-C",
                "/app",
                "show",
                "--format=",
                "--unified=8",
                commit,
                "--",
                "src/App.jsx",
            )
            if not diff.strip():
                raise AssertionError(f"提交 {commit_subject} 没有 src/App.jsx 的 Diff")
            source = sandbox_exec(sandbox_id, "cat", "/app/src/App.jsx")
            diff_path = SHOTS / "staged-drag-diff.log"
            diff_path.write_text(diff, encoding="utf-8")
            log(f"   diff -> {diff_path}\n{diff}")

            for marker in (first_id, second_id):
                if f'data-vibe-created="{marker}"' not in source:
                    raise AssertionError(f"源码缺少 data-vibe-created={marker}")
            first_pos = source.find(f'data-vibe-created="{first_id}"')
            second_pos = source.find(f'data-vibe-created="{second_id}"')
            if not 0 <= first_pos < second_pos:
                raise AssertionError("新增组件顺序与拖入顺序不一致")
            # 两个按钮都必须落在卡片容器内部：最后一个 </section> 之前。
            section_close = source.rfind("</section>")
            if second_pos > section_close:
                raise AssertionError("新增组件落在卡片容器外部")
            forbidden = re.findall(
                r"position\s*:\s*['\"]absolute['\"]|(?:^|\s)(?:left|top)\s*:\s*\d+",
                diff,
            )
            if forbidden:
                raise AssertionError(f"Diff 中出现绝对定位: {forbidden}")
            log("   源码顺序 / 父容器 / 无绝对定位 校验通过")

            log("== 8. 拖动中 iframe 路由切换：不得残留幽灵 ==")
            # 保存成功后等 HMR 重载的 iframe 重新就绪，再画最后一条测试标注。
            expect(frame.locator(".vibe-card")).to_be_visible(timeout=30_000)
            draw_rect = choose_draw_rect(page, active)
            fourth_id = stage_addition(
                page,
                active,
                (draw_rect["x"], draw_rect["y"]),
                (
                    draw_rect["x"] + draw_rect["width"],
                    draw_rect["y"] + draw_rect["height"],
                ),
                "添加一个路由切换测试按钮",
            )
            ghost = active.locator(
                f'.pending-addition-ghost[data-pending-addition-id="{fourth_id}"]'
            )
            handle_box = ghost.locator(".pending-addition-drag-handle").bounding_box()
            page.mouse.move(
                handle_box["x"] + handle_box["width"] / 2,
                handle_box["y"] + handle_box["height"] / 2,
            )
            page.mouse.down()
            page.mouse.move(drop_point[0], drop_point[1], steps=20)
            page.wait_for_timeout(900)
            expect(active.locator(".stage-drop-target")).to_be_visible(timeout=8_000)
            # FrameLocator 没有 evaluate：先拿 iframe 的 ElementHandle，
            # 再取真实 Frame，确保 pushState 执行在沙盒文档上下文里。
            iframe_handle = active.locator("iframe.canvas-live-frame").element_handle()
            if iframe_handle is None:
                raise AssertionError("找不到画布 live iframe")
            live_frame = iframe_handle.content_frame()
            if live_frame is None:
                raise AssertionError("live iframe 尚未加载出 contentFrame")
            # 父页面旁听 postMessage，区分“SDK 没发”与“发了但没处理”。
            page.evaluate("""() => {
                  window.__vibeRouteEvents = [];
                  window.addEventListener('message', (event) => {
                    const data = event.data;
                    if (!data || data.channel !== 'vibe-inspect') return;
                    if (data.type !== 'route-change') return;
                    window.__vibeRouteEvents.push({
                      path: data.payload && data.payload.path,
                      reason: data.payload && data.payload.reason,
                    });
                  });
                }""")
            before_path = live_frame.evaluate("() => location.pathname")
            live_frame.evaluate("() => history.pushState({}, '', '/e2e-route-switch')")
            after_path = live_frame.evaluate("() => location.pathname")
            page.wait_for_timeout(500)
            route_events = page.evaluate("() => window.__vibeRouteEvents")
            # 诊断：区分“父页面没收到消息”/“收到了但没清草稿”/“清了但拖拽
            # 会话仍在”。幽灵类名里的 is-capture-anchor 表示拖拽态。
            ghost_state = active.locator(".pending-addition-ghost").evaluate_all(
                """els => els.map((el) => ({
                      id: el.dataset.pendingAdditionId,
                      cls: el.className,
                      opacity: getComputedStyle(el).opacity,
                      ariaHidden: el.getAttribute('aria-hidden'),
                    }))"""
            )
            toast_texts = page.locator(
                ".workspace-toast.supervisor-toast"
            ).all_inner_texts()
            popover_count = active.locator(".canvas-popover").count()
            log(
                f"   iframe route: {before_path} -> {after_path}; "
                f"route-change messages={route_events}"
            )
            log(f"   ghosts={ghost_state}")
            log(f"   toasts={toast_texts} popovers={popover_count}")
            if not route_events:
                raise AssertionError(
                    "iframe 已换路由，但父页面没有收到 route-change 消息"
                )
            expect(active.locator(".pending-addition-ghost")).to_have_count(
                0, timeout=10_000
            )
            expect(active.locator(".stage-drop-target")).to_have_count(0, timeout=5_000)
            page.mouse.up()
            expect(page.locator(".workspace-toast.supervisor-toast")).to_contain_text(
                "页面已切换", timeout=10_000
            )
            shot(page, "staged-drag-route-cleared.png")
            log("   iframe 路由切换后标注与幽灵已清空")

            log("\nALL STAGED DRAG E2E CHECKS PASSED")
        finally:
            cleanup_sandbox(sandbox_id)
            context.close()
            browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
