#!/usr/bin/env python3
"""实例守卫端到端命中取证。

用真实 SDK 采集的 card-2 信封 + Mock Coder（add_button）发起一次
AWAITING_APPROVAL Run，验证 Applier 守卫是否在写盘前拦截，以及拦截后
沙盒工作区是否保持干净。

运行（宿主机）：
    /tmp/vibe-e2e/bin/python scripts/diagnostics/targeting_guard_e2e.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from targeting_env_probe import (
    APP_FILE,
    APP_SOURCE,
    CARD_FILE,
    CARD_SOURCE,
    container_for_sandbox,
    create_sandbox,
    destroy_sandbox,
    git_state,
    http_json,
    run_with_approval,
)

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "reports" / "diagnostics"


def main() -> int:
    project_id = f"diag_guard_e2e_{int(time.time())}"
    sandbox = create_sandbox(project_id)
    sandbox_id = sandbox["sandbox_id"]
    try:
        probe_report = json.loads(
            (OUT_DIR / "targeting-probe-baseline2.json").read_text(encoding="utf-8")
        )
        card_source = probe_report["envelopes"]["picks"][1]["source"]
        annotation = {
            "version": 1,
            "annotationId": f"ann_guard_e2e_{int(time.time())}",
            "type": "visual-draw",
            "action": "MODIFY_EXISTING",
            "target": {
                "mode": "source",
                "selector": '[data-vibe-target="card-action"]',
                "source": card_source,
            },
            "intent": {
                "kind": "UI_ONLY",
                "ui": {
                    "summary": "添加一个详情按钮",
                    "acceptance": ["只有第 2 张卡片出现详情按钮"],
                    "elementRole": "button",
                },
            },
            "payload": {"text": "添加一个按钮"},
        }
        before = git_state(sandbox["container_name"])
        start = time.time()
        # add_button 会无条件把按钮写进目标基础组件，守卫应在写盘前打回。
        run = run_with_approval(
            sandbox_id, project_id, annotation, scenario="add_button"
        )
        elapsed = round(time.time() - start, 2)
        container = (
            container_for_sandbox(run.get("sandboxId") or "")
            or sandbox["container_name"]
        )
        after = git_state(container)

        def tracked_state(raw: str) -> tuple[list[str], list[str]]:
            """拆出 STATUS / SHA 段，忽略 VIBE_CONTEXT.md 这类运行时文件。"""
            status: list[str] = []
            sha: list[str] = []
            section = ""
            for line in raw.splitlines():
                if line.startswith("--- "):
                    section = line.strip("- ").strip()
                    continue
                if section == "STATUS" and line.strip():
                    if "VIBE_CONTEXT.md" in line:
                        continue
                    status.append(line)
                elif section == "SHA" and line.strip():
                    sha.append(line)
            return status, sha

        before_status, before_sha = tracked_state(before["raw"])
        after_status, after_sha = tracked_state(after["raw"])
        guard_messages = [
            message.get("content")
            for message in run.get("messages") or []
            if message.get("node") == "applier"
        ]
        report: dict[str, Any] = {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "generatedAtLocal": datetime.now().astimezone().isoformat(),
            "elapsedSeconds": elapsed,
            "source": card_source,
            "run": run,
            "guard": {
                "errorCode": run.get("errorCode"),
                "error": run.get("error"),
                "status": run.get("status"),
                "applyFailed": run.get("status") == "FAILED",
                "applierMessages": guard_messages,
            },
            "gitBefore": before,
            "gitAfter": after,
            "writeBlocked": before_sha == after_sha and not after_status,
        }
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        output = OUT_DIR / "targeting-guard-e2e.json"
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(
            f"run={run.get('runId')} status={run.get('status')} "
            f"errorCode={run.get('errorCode')} elapsed={elapsed}s"
        )
        print("error=", (run.get("error") or "")[:500])
        print("\n[Run 前] git status:\n" + before["raw"])
        print("\n[Run 后] git status:\n" + after["raw"])
        print(f"writeBlocked={report['writeBlocked']}")
        print(f"report={output}")
        return 0
    finally:
        destroy_sandbox(sandbox_id)


if __name__ == "__main__":
    raise SystemExit(main())
