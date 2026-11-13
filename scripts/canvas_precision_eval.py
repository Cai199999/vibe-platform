#!/usr/bin/env python3
"""圈选精度评测：真实浏览器画圈 -> AnnotationEnvelope -> 指标报告。

覆盖三类用例：
  - HIT_TEST：点击固定坐标，命中预期源码出处；
  - ELEMENTS_IN_BOX：圈住一片区域，召回框内全部预期组件；
  - LAYER_PRIORITY：点在按钮内部文字 / 图标上，应提升到最近的可交互
    父组件，而不是停在 span / svg。

指标：
  - Hit Rate：源码出处命中率（box 用例按召回集合计算）；
  - IoU：画布回传世界坐标框与预期组件框的交并比。

运行：
    docker compose up -d
    /tmp/vibe-e2e/bin/python scripts/canvas_precision_eval.py --label baseline
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "config" / "eval_suite" / "canvas_precision_cases.yaml"
REPORT_DIR = ROOT / "reports" / "eval"
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
WEB_URL = "http://127.0.0.1:5173/"
PY_AI_URL = "http://127.0.0.1:8100"


def cleanup_sandbox(sandbox_id: str | None) -> None:
    if not sandbox_id:
        return
    request = urllib.request.Request(
        f"{PY_AI_URL}/api/v1/internal/sandbox/{sandbox_id}",
        method="DELETE",
    )
    try:
        urllib.request.urlopen(request, timeout=60).close()
    except Exception as exc:  # noqa: BLE001 - 清理失败不影响评测结论
        print(f"WARN: 清理沙盒失败 {sandbox_id}: {exc}")


def load_cases() -> dict[str, Any]:
    with CASES_PATH.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict) or not config.get("cases"):
        raise SystemExit(f"评测集为空或格式错误: {CASES_PATH}")
    return config


def source_key(source: dict[str, Any] | None) -> str:
    """把 target.source 归一化为 file:line，便于与 YAML 比较。"""
    if not source:
        return ""
    file = str(source.get("file") or "").strip()
    line = int(source.get("line") or 0)
    return f"{file}:{line}" if file and line > 0 else ""


def iou(a: dict[str, float], b: dict[str, float]) -> float:
    left = max(a["x"], b["x"])
    top = max(a["y"], b["y"])
    right = min(a["x"] + a["width"], b["x"] + b["width"])
    bottom = min(a["y"] + a["height"], b["y"] + b["height"])
    if right <= left or bottom <= top:
        return 0.0
    intersection = (right - left) * (bottom - top)
    union = a["width"] * a["height"] + b["width"] * b["height"] - intersection
    return intersection / union if union > 0 else 0.0


def world_to_pixels(rect: dict[str, Any], viewport: dict[str, int]) -> dict[str, float]:
    return {
        "x": float(rect.get("x") or 0) * viewport["width"],
        "y": float(rect.get("y") or 0) * viewport["height"],
        "width": float(rect.get("width") or 0) * viewport["width"],
        "height": float(rect.get("height") or 0) * viewport["height"],
    }


def frame_geometry(active, frame) -> tuple[dict[str, float], int, int]:
    """返回 iframe 在父页面的屏幕框 + iframe 内部视口尺寸。"""
    box = active.locator(".canvas-live-frame").bounding_box()
    if not box:
        raise AssertionError("找不到 live 画布 iframe")
    width, height = frame.evaluate("[window.innerWidth, window.innerHeight]")
    return box, int(width), int(height)


def point_to_page(
    point: dict[str, float],
    box: dict[str, float],
    inner_width: int,
    inner_height: int,
) -> tuple[float, float]:
    """iframe CSS 坐标 -> 父页面屏幕坐标（考虑画布 scale / 滚动）。"""
    return (
        box["x"] + point["x"] * box["width"] / inner_width,
        box["y"] + point["y"] * box["height"] / inner_height,
    )


def force_live_canvas(page: Page) -> None:
    """把演示项目的画布从 mock 切到 live。

    「一键体验」生成的演示项目 canvasMode 固定为 mock，只有真实生成完成
    才会切 live；评测只需要沙盒，因此直接改写 localStorage 后刷新。
    fullstack.previewUrl 持久化在 sessionStorage，刷新不会丢失。
    """
    changed = page.evaluate("""() => {
          const key = 'vibe.workspace.v1'
          const state = JSON.parse(localStorage.getItem(key))
          const project = state.projects.find(
            (item) => item.id === state.activeTabId
          )
          if (!project) return false
          project.canvasMode = 'live'
          project.bootstrap = 'ready'
          localStorage.setItem(key, JSON.stringify(state))
          return true
        }""")
    if not changed:
        raise AssertionError("localStorage 中找不到当前演示项目")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1500)


def prepare_fixture(page: Page, active, fixture_url: str):
    """把 live 画布 iframe 导航到固定夹具并返回 frame 对象。"""
    # 确保处于画布视图（生成完成后默认就在画布）。
    if not active.locator(".canvas-live-frame").is_visible():
        canvas_tab = active.get_by_role("button", name="画布", exact=True)
        if canvas_tab.count() > 0:
            canvas_tab.first.click()
    expect(active.locator(".canvas-live-frame")).to_be_visible(timeout=60_000)

    frame = None
    for _ in range(20):
        handle = active.locator(".canvas-live-frame").element_handle()
        frame = handle.content_frame() if handle else None
        if frame:
            break
        page.wait_for_timeout(100)
    if frame is None:
        raise AssertionError("live 画布 iframe 未就绪")

    print(f"   iframe -> {fixture_url}")
    frame.goto(fixture_url, wait_until="domcontentloaded")
    frame.wait_for_selector("#deep-export", timeout=30_000)
    print("   夹具已加载")
    frame.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)
    return frame


def hide_popovers(active) -> None:
    """折叠所有还展开着的标注输入浮层，避免遮挡下一次圈选。"""
    for _ in range(5):
        hide = active.get_by_role("button", name="隐藏标注输入")
        visible = [item for item in hide.all() if item.is_visible()]
        if not visible:
            return
        visible[0].click(force=True)


def inspect_hit(page: Page, active, case: dict[str, Any], frame) -> None:
    """用 Inspect 模式在固定坐标点击并生成一个标注。"""
    toggle = active.locator("[data-inspect-toggle]")
    if toggle.get_attribute("aria-pressed") != "true":
        toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "true")

    box, inner_w, inner_h = frame_geometry(active, frame)
    x, y = point_to_page(case["point"], box, inner_w, inner_h)
    print(
        f"   iframe box={ {k: round(v, 1) for k, v in box.items()} } "
        f"inner={inner_w}x{inner_h} page=({x:.1f},{y:.1f})"
    )
    hit = frame.evaluate(
        """([px, py]) => {
          const node = document.elementFromPoint(px, py)
          if (!node) return null
          return {
            tag: node.tagName,
            id: node.id,
            marker: node.getAttribute('data-source') || '',
            inspectEnabled: document.documentElement.classList.contains(
              'vibe-inspect-enabled'
            ),
          }
        }""",
        [case["point"]["x"], case["point"]["y"]],
    )
    print(f"   iframe elementFromPoint={hit}")
    page.mouse.click(x, y)

    # RuntimePanel 与画布层各渲染一份 InspectDraftCard（共享同一份状态），
    # 这里固定操作第一份，避免 strict mode 冲突。
    draft = active.locator(".inspect-draft").first
    if draft.count() == 0:
        print(
            "   WARN: 未出现草稿，annotations="
            f"{active.locator('.annotation-ellipse').count()} "
            f"limit={active.locator('[data-annotation-limit]').count()}"
        )
    expect(draft).to_be_visible(timeout=10_000)
    print(f"   命中草稿: {' '.join((draft.text_content() or '').split())[:150]}")
    draft.locator("[data-inspect-intent]").fill(f"精度评测 {case['id']}")
    draft.get_by_role("button", name="添加标注").click()
    expect(draft).to_have_count(0, timeout=10_000)
    page.wait_for_timeout(250)
    hide_popovers(active)


def draw_box(page: Page, active, case: dict[str, Any], frame) -> None:
    """按 YAML 固定矩形完成一次真实按下 / 拖拽 / 松开。"""
    box, inner_w, inner_h = frame_geometry(active, frame)
    rect = case["box"]
    start = point_to_page({"x": rect["x"], "y": rect["y"]}, box, inner_w, inner_h)
    center = point_to_page(
        {
            "x": rect["x"] + rect["width"] / 2,
            "y": rect["y"] + rect["height"] / 2,
        },
        box,
        inner_w,
        inner_h,
    )
    end = point_to_page(
        {"x": rect["x"] + rect["width"], "y": rect["y"] + rect["height"]},
        box,
        inner_w,
        inner_h,
    )
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(*center, steps=6)
    page.mouse.move(*end, steps=6)
    page.mouse.up()
    page.wait_for_timeout(450)
    print(
        "   框选后: annotations="
        f"{active.locator('.annotation-ellipse').count()} "
        f"popovers={active.locator('.canvas-popover').count()} "
        f"interaction={active.locator('.canvas-stage').get_attribute('data-canvas-interaction')}"
    )


def wait_run_settled(active, timeout: int = 180_000) -> None:
    """等待一次提交结束（成功或失败都算结束），避免清理操作被冻结。"""
    try:
        expect(active.locator(".runtime-overlay")).to_have_count(0, timeout=timeout)
    except AssertionError:
        print("WARN: Run 仍未结束，继续执行清理")


def submit_batch(page: Page, active, captured: list[dict], expected: int) -> dict:
    before = len(captured)
    active.locator(".submit-annotations").click()
    for _ in range(300):
        if len(captured) > before:
            break
        page.wait_for_timeout(100)
    if len(captured) <= before:
        raise AssertionError("未捕获到 AnnotationEnvelope 请求")
    body = captured[before]
    annotations = body.get("annotations") or []
    if len(annotations) != expected:
        raise AssertionError(
            f"AnnotationEnvelope 数量应为 {expected}，实际 {len(annotations)}"
        )
    wait_run_settled(active)
    return body


def clear_annotations(page: Page, active) -> None:
    more = active.locator("[data-toolbar-more]")
    if more.count() > 0:
        more.click()
    clear = active.locator(".ghost-button", has_text="清除标注")
    if clear.count() > 0:
        clear.first.click()
        page.wait_for_timeout(800)
    expect(active.locator(".annotation-ellipse")).to_have_count(0, timeout=30_000)


def evaluate_case(
    case: dict[str, Any], envelope: dict[str, Any], viewport: dict[str, int]
) -> dict[str, Any]:
    """把单条 AnnotationEnvelope 折算成 hit / IoU。"""
    target = envelope.get("target") or {}
    measured = envelope.get("measured") or {}
    world = envelope.get("worldTarget") or {}
    # 点击类用例比较“命中元素的真实框”；多组件圈选比较“召回元素并集”，
    # 否则用户框选时预留的 padding 会被误算成坐标误差。
    selection_rect = world_to_pixels(world, viewport)
    returned_rect = selection_rect
    expected_rect = case["expected_box"]

    if case["kind"] == "ELEMENTS_IN_BOX":
        elements = measured.get("elements") or []
        rects = [
            item["rect"]
            for item in elements
            if isinstance(item.get("rect"), dict) and item["rect"].get("width")
        ]
        if rects:
            left = min(float(rect["x"]) for rect in rects)
            top = min(float(rect["y"]) for rect in rects)
            right = max(float(rect["x"]) + float(rect["width"]) for rect in rects)
            bottom = max(float(rect["y"]) + float(rect["height"]) for rect in rects)
            returned_rect = {
                "x": left,
                "y": top,
                "width": right - left,
                "height": bottom - top,
            }
        returned_sources = sorted(
            {
                source_key(item.get("source"))
                for item in elements
                if source_key(item.get("source"))
            }
        )
        expected_sources = sorted(case["expected_sources"])
        matched = sorted(set(returned_sources) & set(expected_sources))
        hit = len(matched) == len(expected_sources)
        hit_ratio = len(matched) / len(expected_sources)
        primary_source = source_key(target.get("source"))
    else:
        primary_source = source_key(target.get("source"))
        returned_sources = [primary_source] if primary_source else []
        expected_sources = [case["expected_source"]]
        hit = primary_source == case["expected_source"]
        hit_ratio = 1.0 if hit else 0.0
        matched = [primary_source] if hit else []

    overlap = iou(returned_rect, expected_rect)
    return {
        "id": case["id"],
        "name": case["name"],
        "kind": case["kind"],
        "hit": hit,
        "hit_ratio": round(hit_ratio, 4),
        "expected_sources": expected_sources,
        "returned_sources": returned_sources,
        "matched_sources": matched,
        "primary_source": primary_source,
        "expected_box": expected_rect,
        "selection_box": {
            key: round(value, 2) for key, value in selection_rect.items()
        },
        "returned_box": {key: round(value, 2) for key, value in returned_rect.items()},
        "iou": round(overlap, 4),
    }


def write_report(
    label: str, results: list[dict], thresholds: dict[str, float]
) -> dict[str, Any]:
    hit_rate = sum(item["hit_ratio"] for item in results) / len(results)
    case_hit_rate = sum(1 for item in results if item["hit"]) / len(results)
    mean_iou = sum(item["iou"] for item in results) / len(results)
    worst = min(results, key=lambda item: (item["hit"], item["iou"]))
    report = {
        "label": label,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "caseCount": len(results),
        "hitRate": round(hit_rate, 4),
        "caseHitRate": round(case_hit_rate, 4),
        "meanIoU": round(mean_iou, 4),
        "thresholds": thresholds,
        "passed": hit_rate >= thresholds["hit_rate"] and mean_iou >= thresholds["iou"],
        "worstCase": worst,
        "results": results,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_text = json.dumps(report, ensure_ascii=False, indent=2)
    # 同时保留带标签的历史报告与一份 latest 报告，便于对比基线与修复后。
    (REPORT_DIR / f"canvas-precision-{label}.json").write_text(
        json_text, encoding="utf-8"
    )
    (REPORT_DIR / "canvas-precision-report.json").write_text(
        json_text, encoding="utf-8"
    )

    rows = [
        "| 用例 | 类型 | Hit | 命中率 | IoU | 期望 source | 实际 source |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in results:
        rows.append(
            "| {name} | {kind} | {hit} | {hit_ratio:.2f} | {iou:.3f} | {expected} | {actual} |".format(
                name=item["name"],
                kind=item["kind"],
                hit="✅" if item["hit"] else "❌",
                hit_ratio=item["hit_ratio"],
                iou=item["iou"],
                expected=", ".join(item["expected_sources"]),
                actual=", ".join(item["returned_sources"]) or "(空)",
            )
        )
    markdown = "\n".join(
        [
            f"# 圈选精度评测 · {label}",
            "",
            f"- 用例数：{len(results)}",
            f"- Hit Rate：{hit_rate:.2%}（用例全中率 {case_hit_rate:.2%}）",
            f"- Mean IoU：{mean_iou:.3f}",
            f"- 阈值：Hit ≥ {thresholds['hit_rate']:.2f}，IoU ≥ {thresholds['iou']:.2f}",
            f"- 结论：{'PASS' if report['passed'] else 'FAIL'}",
            f"- 最大误差：{worst['name']}（hit={worst['hit']}, IoU={worst['iou']:.3f}）",
            "",
            *rows,
            "",
        ]
    )
    (REPORT_DIR / f"canvas-precision-{label}.md").write_text(markdown, encoding="utf-8")
    (REPORT_DIR / "canvas-precision-report.md").write_text(markdown, encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="baseline", help="报告标签")
    args = parser.parse_args()
    config = load_cases()
    viewport = config["viewport"]
    cases = config["cases"]
    thresholds = config["thresholds"]

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    captured: list[dict] = []
    sandbox_id: str | None = None

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        # 画布内视口固定 1440×900，浏览器窗口必须留出工具栏与内边距，
        # 否则底部悬浮按钮会落在屏幕外，真实鼠标事件无法命中。
        context = browser.new_context(viewport={"width": 1720, "height": 1320})
        page = context.new_page()
        page.set_default_timeout(30_000)

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

        # 评测只关心前端送出的 AnnotationEnvelope，不关心后端真正的修改
        # 结果。批次提交时把 route 短路成 rejected，既保留已捕获请求，
        # 又避免触发长任务导致画布被运行态覆盖、后续批次无法继续。
        stub_state = {"enabled": False}

        def on_route(route) -> None:
            if not stub_state["enabled"]:
                route.continue_()
                return
            try:
                body = json.loads(route.request.post_data or "{}")
            except json.JSONDecodeError:
                body = {}
            if not body.get("annotations"):
                route.continue_()
                return
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "decision": {
                            "handoff": "rejected",
                            "dispatchable": False,
                            "target": None,
                            "taskType": "canvas_precision_eval",
                            "reason": "评测占位：已采集 AnnotationEnvelope",
                            "confidence": 1,
                            "signals": [],
                            "strategy": "eval",
                            "missing_inputs": [],
                        },
                        "handoffs": [],
                    },
                    ensure_ascii=False,
                ),
            )

        page.route("**/api/v1/supervisor/route", on_route)
        try:
            print("== 1. 登录并生成可交互沙盒 ==")
            page.goto(f"{WEB_URL}workspace", wait_until="networkidle")
            page.get_by_role("button", name="一键体验（推荐）").click()
            expect(page.locator(".workspace-tab")).to_have_count(1)
            active = page.locator(".tab-window.is-active")
            active.locator(".model-picker select").select_option("mock")
            expect(active.locator(".mock-card")).to_be_visible()

            # 先做一次最简圈选，触发沙盒创建；评测本身不依赖这次修改。
            box = active.locator(".mock-cta").bounding_box()
            if not box:
                raise AssertionError("找不到 mock CTA")
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
            popover = active.locator(".canvas-popover")
            expect(popover).to_have_count(1)
            popover.locator("input").fill("评测准备：创建沙盒")
            popover.locator(".popover-send").click()
            expect(
                active.locator(".runtime-result-overlay .status-pill")
            ).to_contain_text("已完成", timeout=180_000)
            sandbox_id = active.locator(".runtime-panel").get_attribute(
                "data-sandbox-id"
            )
            preview_url = (
                active.locator(".runtime-preview-frame").get_attribute("src") or ""
            )
            if not preview_url:
                raise AssertionError("沙盒预览地址为空")
            fixture_url = (
                preview_url.split("?")[0].rstrip("/") + "/" + config["fixture"]
            )
            print(f"   sandbox={sandbox_id}")
            print(f"   fixture={fixture_url}")

            # 演示项目默认 mock 画布；强制切 live 后重新获取 active 容器。
            force_live_canvas(page)
            active = page.locator(".tab-window.is-active")
            expect(active.locator(".canvas-live-frame")).to_be_visible(timeout=60_000)
            # 沙盒创建用的 mock 圈选会留在会话快照里，先清空保证批次可复现。
            print(f"   刷新后已有标注={active.locator('.annotation-ellipse').count()}")
            clear_annotations(page, active)
            stub_state["enabled"] = True

            print("\n== 2. 批次 A：点击命中 + 图标层级 ==")
            frame = prepare_fixture(page, active, fixture_url)
            batch_a_cases = [
                case for case in cases if case["kind"] in {"HIT_TEST", "LAYER_PRIORITY"}
            ][:3]
            for case in batch_a_cases:
                print(f"   {case['id']} -> {case['point']}")
                inspect_hit(page, active, case, frame)
            body_a = submit_batch(page, active, captured, len(batch_a_cases))
            envelope_a = body_a["annotations"]

            print("\n== 3. 批次 B：层级优先级 + 整块圈选 ==")
            clear_annotations(page, active)
            frame = prepare_fixture(page, active, fixture_url)
            layer = next(case for case in cases if case["kind"] == "LAYER_PRIORITY")
            print(f"   {layer['id']} -> {layer['point']}")
            inspect_hit(page, active, layer, frame)
            toggle = active.locator("[data-inspect-toggle]")
            if toggle.get_attribute("aria-pressed") == "true":
                toggle.click()
            expect(toggle).to_have_attribute("aria-pressed", "false")
            # 等 React 把 canvasInspect 写回交互态，否则紧接着的拖拽仍会被
            # Inspect 覆盖层吞掉。
            page.wait_for_timeout(350)

            box_cases = [case for case in cases if case["kind"] == "ELEMENTS_IN_BOX"]
            for case in box_cases:
                print(f"   {case['id']} -> {case['box']}")
                draw_box(page, active, case, frame)
                popovers = active.locator(".canvas-popover")
                expect(popovers).not_to_have_count(0, timeout=10_000)
                # 新画的框总是追加在最后；前面的 Inspect 标注可能仍保留
                # 已折叠的浮层，不能用下标推算。
                popover = popovers.last
                popover.locator("input").fill(f"精度评测 {case['id']}", force=True)
                # 空白圈选默认按 CREATE_NEW 暂存，不会进入批量提交；
                # 评测只需要几何事实，强制切到“修改”让它作为普通标注提交。
                popover.get_by_role("button", name="修改", exact=True).click()
                hide_popovers(active)
            batch_b_cases = [layer, *box_cases]
            body_b = submit_batch(page, active, captured, len(batch_b_cases))
            envelope_b = body_b["annotations"]

            page.screenshot(
                path=str(SCREENSHOT_DIR / f"canvas-precision-{args.label}.png"),
                full_page=True,
            )

            print("\n== 4. 计算指标 ==")
            results = []
            for case, envelope in zip(batch_a_cases, envelope_a):
                results.append(evaluate_case(case, envelope, viewport))
            for case, envelope in zip(batch_b_cases, envelope_b):
                results.append(evaluate_case(case, envelope, viewport))

            report = write_report(args.label, results, thresholds)
            for item in results:
                print(
                    "   {state} {id}: hit={hit} iou={iou:.3f} "
                    "expected={expected} actual={actual}".format(
                        state="OK" if item["hit"] else "MISS",
                        id=item["id"],
                        hit=item["hit_ratio"],
                        iou=item["iou"],
                        expected=",".join(item["expected_sources"]),
                        actual=",".join(item["returned_sources"]) or "(空)",
                    )
                )
            print(
                f"\nHIT RATE={report['hitRate']:.2%} "
                f"CASE HIT={report['caseHitRate']:.2%} "
                f"MEAN IoU={report['meanIoU']:.3f} "
                f"PASSED={report['passed']}"
            )
            print(
                "REPORT: reports/eval/canvas-precision-report.{json,md} "
                f"(screenshot: docs/screenshots/canvas-precision-{args.label}.png)"
            )
            return 0 if report["passed"] else 2
        finally:
            cleanup_sandbox(sandbox_id)
            context.close()
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
