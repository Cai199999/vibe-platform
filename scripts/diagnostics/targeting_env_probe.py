#!/usr/bin/env python3
"""实例定位取证：真实 SDK 信封 + 审批前沙盒状态。

三个阶段：
  1. 创建一个受控 React 沙盒：3 个同构 Card（map 渲染）+ 1 个唯一按钮；
  2. 在真实预览页里通过 postMessage 调用 inspect SDK，采集 pick /
     analyze-selection 返回的 instance / callSite / instanceProps；
  3. 用一个唯一目标跑一次 Mock Run（requireManualApproval=true），
     在 AWAITING_APPROVAL 时检查沙盒 git status，证明代码是否已提前写盘；
     随后拒绝审批并核对工作区是否恢复。

运行（宿主机）：
    /tmp/vibe-e2e/bin/python scripts/diagnostics/targeting_env_probe.py
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "reports" / "diagnostics"
PY_AI_URL = "http://127.0.0.1:8100"
APP_FILE = "src/App.jsx"
CARD_FILE = "src/components/Card.jsx"

APP_SOURCE = """import Card from './components/Card.jsx'

// 受控夹具：3 个同构卡片用 map 渲染，用于验证实例级定位。
const CARDS = [
  { id: 'card-1', title: '营收概览', value: '1,240,000' },
  { id: 'card-2', title: '活跃用户', value: '8,420' },
  { id: 'card-3', title: '转化率', value: '3.8%' },
]

export default function App() {
  return (
    <main className="dashboard">
      <header className="dashboard-header">
        <h1>运营看板</h1>
        <button type="button" data-vibe-target="header-action">
          新建报表
        </button>
      </header>
      <section className="card-grid">
        {CARDS.map((card) => (
          <Card key={card.id} card={card} />
        ))}
      </section>
    </main>
  )
}
"""

CARD_SOURCE = """export default function Card({ card }) {
  return (
    <article className="card" data-vibe-target="card">
      <h2 className="card-title">{card.title}</h2>
      <p className="card-value">{card.value}</p>
      <button type="button" className="card-action" data-vibe-target="card-action">
        查看详情
      </button>
    </article>
  )
}
"""


def http_json(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{PY_AI_URL}{path}", data=data, headers=headers, method=method
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else {}


def create_sandbox(project_id: str) -> dict[str, Any]:
    return http_json(
        "POST",
        "/api/v1/internal/sandbox/create",
        {
            "project_id": project_id,
            "files": {APP_FILE: APP_SOURCE, CARD_FILE: CARD_SOURCE},
        },
    )


def destroy_sandbox(sandbox_id: str) -> None:
    try:
        http_json("DELETE", f"/api/v1/internal/sandbox/{sandbox_id}")
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: 销毁沙盒失败 {sandbox_id}: {exc}")


def wait_for_preview(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_selector('[data-vibe-target="card-action"]', timeout=60_000)


def probe(
    page: Page,
    request_type: str,
    payload: dict[str, Any],
    response_type: str,
) -> dict[str, Any]:
    """在预览页内部 postMessage，并等待 SDK 回包。"""
    return page.evaluate(
        """async ({ requestType, payload, responseType }) => {
          return await new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
              window.removeEventListener('message', handler)
              reject(new Error(`timeout waiting ${responseType}`))
            }, 8000)
            function handler(event) {
              const data = event.data
              if (!data || data.channel !== 'vibe-inspect') return
              if (data.type !== responseType) return
              clearTimeout(timer)
              window.removeEventListener('message', handler)
              resolve(data.payload)
            }
            window.addEventListener('message', handler)
            window.postMessage(
              { channel: 'vibe-inspect', type: requestType, ...payload },
              '*',
            )
          })
        }""",
        {
            "requestType": request_type,
            "payload": payload,
            "responseType": response_type,
        },
    )


def element_rects(page: Page, selector: str) -> list[dict[str, float]]:
    return page.evaluate(
        """(selector) => Array.from(document.querySelectorAll(selector)).map((el) => {
          const rect = el.getBoundingClientRect()
          return { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
        })""",
        selector,
    )


def capture_envelopes(url: str) -> dict[str, Any]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 1200})
        wait_for_preview(page, url)
        # enable 是单向消息，SDK 不回包，直接投递后等一帧。
        page.evaluate("""() => window.postMessage(
              { channel: 'vibe-inspect', type: 'enable', enabled: true },
              '*',
            )""")
        page.wait_for_timeout(200)

        card_buttons = element_rects(page, '[data-vibe-target="card-action"]')
        cards = element_rects(page, '[data-vibe-target="card"]')
        header_buttons = element_rects(page, '[data-vibe-target="header-action"]')

        picks: list[dict[str, Any]] = []
        for index, rect in enumerate(card_buttons):
            picks.append(
                probe(
                    page,
                    "pick",
                    {
                        "x": rect["x"] + rect["width"] / 2,
                        "y": rect["y"] + rect["height"] / 2,
                    },
                    "picked",
                )
            )

        header_pick = probe(
            page,
            "pick",
            {
                "x": header_buttons[0]["x"] + header_buttons[0]["width"] / 2,
                "y": header_buttons[0]["y"] + header_buttons[0]["height"] / 2,
            },
            "picked",
        )

        # 圈住第 2 个卡片主体，验证 ADD 的父容器信封。
        second = cards[1]
        analyze = probe(
            page,
            "analyze-selection",
            {
                "payload": {
                    "requestId": "diag-card-2",
                    "rect": {
                        "x": second["x"] + 2,
                        "y": second["y"] + 2,
                        "width": max(4, second["width"] - 4),
                        "height": max(4, second["height"] - 4),
                    },
                }
            },
            "selection-analyzed",
        )
        browser.close()
        return {
            "picks": picks,
            "headerPick": header_pick,
            "analyze": analyze,
            "rects": {
                "cards": cards,
                "cardButtons": card_buttons,
                "headerButtons": header_buttons,
            },
        }


def container_exec(container_name: str, command: str) -> str:
    result = subprocess.run(
        ["docker", "exec", container_name, "sh", "-lc", command],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        return f"<exit {result.returncode}>\n{result.stdout}\n{result.stderr}"
    return result.stdout


def git_state(container_name: str) -> dict[str, str]:
    command = (
        "cd /app && "
        "echo '--- HEAD ---' && git rev-parse HEAD && "
        "echo '--- STATUS ---' && git status --porcelain && "
        "echo '--- DIFFSTAT ---' && git diff --stat && "
        "echo '--- SHA ---' && sha256sum src/App.jsx src/components/Card.jsx"
    )
    return {"raw": container_exec(container_name, command)}


def build_annotation(source: dict[str, Any], text: str, action: str) -> dict[str, Any]:
    return {
        "version": 1,
        "annotationId": f"ann_diag_{int(time.time())}",
        "type": "visual-draw",
        "action": action,
        "target": {
            "mode": "source",
            "selector": source.get("selector") or "",
            "source": source,
        },
        "intent": {
            "kind": "UI_ONLY",
            "ui": {"summary": text, "acceptance": [text], "elementRole": "button"},
        },
        "payload": {"text": text},
    }


def run_with_approval(
    sandbox_id: str,
    project_id: str,
    annotation: dict[str, Any],
    *,
    scenario: str = "none",
) -> dict[str, Any]:
    return http_json(
        "POST",
        "/api/v1/internal/runs/execute",
        {
            "runId": f"run_diag_{int(time.time())}",
            "projectId": project_id,
            "sandboxId": sandbox_id,
            "annotations": [annotation],
            "options": {
                "llmProvider": "mock",
                "mockScenario": scenario,
                "requireManualApproval": True,
                "clarificationMode": "off",
                "maxRetries": 0,
            },
        },
    )


def container_for_sandbox(sandbox_id: str) -> str:
    """把 sandbox_id 映射回容器名，供 docker exec 取证。"""
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    for name in result.stdout.splitlines():
        if sandbox_id and sandbox_id in name:
            return name
    return ""


def decide(run_id: str, approval_id: str, decision: str) -> dict[str, Any]:
    return http_json(
        "POST",
        f"/api/v1/internal/runs/{run_id}/approvals/{approval_id}/decision",
        {"decision": decision, "comment": "诊断：拒绝并回滚"},
    )


def summarize_source(source: dict[str, Any] | None) -> dict[str, Any]:
    source = source or {}
    return {
        "file": source.get("file"),
        "line": source.get("line"),
        "column": source.get("column"),
        "component": source.get("component"),
        "tag": source.get("tag"),
        "instance": source.get("instance"),
        "instanceCount": source.get("instanceCount"),
        "instanceId": source.get("instanceId"),
        "instanceKey": source.get("instanceKey"),
        "instanceProps": source.get("instanceProps"),
        "callSite": source.get("callSite"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--keep-sandbox", action="store_true")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "label": args.label,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }
    sandbox_id = ""
    project_id = f"diag_targeting_{int(time.time())}"
    try:
        sandbox = create_sandbox(project_id)
        sandbox_id = sandbox["sandbox_id"]
        container_name = sandbox["container_name"]
        preview_url = sandbox["preview_url"]
        print(f"sandbox={sandbox_id} preview={preview_url}")
        report["sandbox"] = sandbox

        envelopes = capture_envelopes(preview_url)
        report["envelopes"] = envelopes

        # 真实信封字段缺失率：以 3 个同构按钮 + 1 个唯一按钮 + 1 个 ADD 框为准。
        sources = [item.get("source") for item in envelopes["picks"]]
        sources.append(envelopes["headerPick"].get("source"))
        sources.append((envelopes["analyze"].get("addSource") or {}))
        summaries = [summarize_source(source) for source in sources]
        multi = [item for item in summaries if int(item.get("instanceCount") or 0) > 1]
        multi_complete = [
            item
            for item in multi
            if item.get("callSite")
            and (item.get("instanceKey") or item.get("instanceId"))
        ]
        missing_count_field = [
            item for item in summaries if item.get("instanceCount") is None
        ]
        report["missingInstanceRate"] = {
            "total": len(summaries),
            "instanceCountMissing": len(missing_count_field),
            "multiInstance": len(multi),
            "multiInstanceComplete": len(multi_complete),
            # 真正需要实例守卫的目标里，callSite / instanceKey 齐全的比例。
            "multiInstanceCoverage": round(len(multi_complete) / max(1, len(multi)), 4),
            "items": [item for item in multi if item not in multi_complete],
            "summaries": summaries,
        }
        print(
            "instanceCount 字段覆盖: "
            f"{len(summaries) - len(missing_count_field)}/{len(summaries)}；"
            "多实例目标 callSite+instanceKey 覆盖: "
            f"{len(multi_complete)}/{len(multi)} = "
            f"{report['missingInstanceRate']['multiInstanceCoverage']:.2%}"
        )
        for index, summary in enumerate(summaries):
            print(
                f"  [{index}] {summary['file']}:{summary['line']} "
                f"instance={summary['instance']}/{summary['instanceCount']} "
                f"key={summary['instanceKey']!r} "
                f"props={summary['instanceProps']} "
                f"callSite={summary['callSite']}"
            )

        # 审批前写盘验证：使用唯一按钮，确保实例守卫不会拦截本次 Run。
        unique_source = envelopes["headerPick"]["source"]
        annotation = build_annotation(unique_source, "添加一个按钮", "MODIFY_EXISTING")
        report["approvalAnnotation"] = summarize_source(unique_source)

        before = git_state(container_name)
        report["gitBefore"] = before
        print("\n[审批前] 受控夹具沙盒 git 状态:\n" + before["raw"])

        run = run_with_approval(sandbox_id, project_id, annotation)
        report["run"] = run
        run_sandbox_id = run.get("sandboxId") or ""
        run_container = container_for_sandbox(run_sandbox_id)
        report["runSandbox"] = {
            "sandboxId": run_sandbox_id,
            "reusedSandbox": run.get("reusedSandbox"),
            "container": run_container,
            "previewUrl": run.get("previewUrl"),
        }
        print(
            f"\nrun={run.get('runId')} status={run.get('status')} "
            f"sandbox={run_sandbox_id} reused={run.get('reusedSandbox')} "
            f"container={run_container} "
            f"approval={run.get('approval', {}).get('approvalId') if run.get('approval') else None}"
        )

        if run_container:
            during = git_state(run_container)
            report["gitDuringApproval"] = during
            print("\n[审批暂停时] Run 沙盒 git 状态:\n" + during["raw"])
        else:
            report["gitDuringApproval"] = {"raw": "<container not found>"}
            print("\nWARN: 未能定位 Run 沙盒容器")

        approval = run.get("approval") or {}
        if run.get("status") == "AWAITING_APPROVAL" and approval.get("approvalId"):
            decide(run["runId"], approval["approvalId"], "reject")
            time.sleep(2)
            if run_container:
                after = git_state(run_container)
            else:
                after = {"raw": "<container not found>"}
            report["gitAfterReject"] = after
            print("\n[拒绝审批后] Run 沙盒 git 状态:\n" + after["raw"])
        else:
            report["gitAfterReject"] = {"raw": "<run did not pause for approval>"}
            print("\nWARN: Run 未进入 AWAITING_APPROVAL，无法验证审批前写盘")

        output = OUT_DIR / f"targeting-probe-{args.label}.json"
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"\nreport={output}")
        return 0
    finally:
        if sandbox_id and not args.keep_sandbox:
            destroy_sandbox(sandbox_id)


if __name__ == "__main__":
    raise SystemExit(main())
