#!/usr/bin/env python3
"""Phase 0 草稿隔离端到端取证。

用真实沙盒 + Mock Coder 发起一次需要审批的 Run，逐阶段核对：
  1. AWAITING_APPROVAL 时主工作树 HEAD / 源文件 SHA 完全不变；
  2. 草稿 worktree 中存在修改，草稿预览可访问；
  3. 批准后主工作树快进合并，草稿目录与预览被回收；
  4. 再次发起 Run 并拒绝，主工作树仍保持零变化。

运行（宿主机）：
    python3 scripts/diagnostics/draft_isolation_e2e.py
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from targeting_env_probe import (
    APP_FILE,
    build_annotation,
    container_for_sandbox,
    create_sandbox,
    decide,
    destroy_sandbox,
    git_state,
    http_json,
    run_with_approval,
)

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "reports" / "diagnostics"
PY_AI_URL = "http://127.0.0.1:8100"


def parse_git_state(raw: str) -> dict[str, Any]:
    """拆分 git_state 的文本报告，忽略运行时文件。"""
    sections: dict[str, list[str]] = {}
    current = ""
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("--- ") and stripped.endswith(" ---"):
            current = stripped.strip("- ").strip()
            sections[current] = []
            continue
        if current:
            sections[current].append(line)

    def lines(name: str) -> list[str]:
        return [item for item in sections.get(name, []) if item.strip()]

    head = lines("HEAD")
    sha_rows = {}
    for item in lines("SHA"):
        parts = item.split()
        if len(parts) >= 2:
            sha_rows[parts[1]] = parts[0]
    return {
        "head": head[0] if head else "",
        "status": lines("STATUS"),
        "diffstat": lines("DIFFSTAT"),
        "sha": sha_rows,
    }


def docker_exec(container: str, command: str) -> tuple[int, str]:
    result = subprocess.run(
        ["docker", "exec", container, "sh", "-lc", command],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def draft_id_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url or "")
    return (urllib.parse.parse_qs(parsed.query).get("draft") or [""])[0]


def http_ok(url: str, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 400
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def wait_status(
    run_id: str, expected: set[str], timeout: float = 120.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = http_json("GET", f"/api/v1/internal/runs/{run_id}")
        if last.get("status") in expected:
            return last
        time.sleep(1.0)
    raise TimeoutError(f"等待 Run 状态 {expected} 超时，最后状态: {last.get('status')}")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    project_id = f"diag_draft_{int(time.time())}"
    sandbox_id = ""
    report: dict[str, Any] = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "checks": {},
    }
    try:
        sandbox = create_sandbox(project_id)
        sandbox_id = sandbox["sandbox_id"]
        container = sandbox["container_name"]
        report["sandbox"] = sandbox

        annotation = build_annotation(
            {
                "file": APP_FILE,
                "line": 15,
                "column": 7,
                "selector": '[data-vibe-target="header-action"]',
                "component": "App",
            },
            "把按钮背景改成蓝色",
            "MODIFY_EXISTING",
        )

        # ---------- 场景 1：审批暂停时主工作树零变化 ----------
        before = parse_git_state(git_state(container)["raw"])
        run = run_with_approval(
            sandbox_id,
            project_id,
            annotation,
            scenario="none",
        )
        run_id = run["runId"]
        approval = run.get("approval") or {}
        draft_url = approval.get("previewUrl") or ""
        draft_id = draft_id_from_url(draft_url)
        main_at_pause = parse_git_state(git_state(container)["raw"])

        report["approval"] = {
            "runId": run_id,
            "status": run.get("status"),
            "previewUrl": draft_url,
            "isDraft": approval.get("isDraft"),
            "draftId": draft_id,
            "filesChanged": ((approval.get("evidence") or {}).get("filesChanged")),
        }
        report["mainAtPause"] = main_at_pause
        report["checks"]["pauseHeadUnchanged"] = before["head"] == main_at_pause["head"]
        report["checks"]["pauseStatusClean"] = main_at_pause["status"] == []
        report["checks"]["pauseSourceUnchanged"] = before["sha"].get(
            APP_FILE
        ) == main_at_pause["sha"].get(APP_FILE)
        report["checks"]["draftMarked"] = bool(approval.get("isDraft"))

        draft_dir = f"/app-drafts/{draft_id}"
        code, draft_grep = docker_exec(
            container,
            f"grep -c 'background-color:blue' {draft_dir}/{APP_FILE} || true",
        )
        report["draftSourceGrep"] = {"code": code, "output": draft_grep.strip()}
        report["checks"]["draftHasChange"] = bool(
            draft_grep.strip() and draft_grep.strip() != "0"
        )
        report["checks"]["draftPreviewReachable"] = bool(
            draft_url and http_ok(draft_url)
        )
        report["checks"]["draftWorktreeExists"] = (
            docker_exec(container, f"test -d {draft_dir}")[0] == 0
        )

        # ---------- 场景 1b：批准后快进合并 ----------
        decide(run_id, approval.get("approvalId") or "", "approve")
        final = wait_status(run_id, {"SUCCEEDED", "FAILED", "REJECTED"})
        after_approve = parse_git_state(git_state(container)["raw"])
        report["afterApprove"] = {
            "status": final.get("status"),
            "head": after_approve["head"],
            "statusLines": after_approve["status"],
            "diffstat": after_approve["diffstat"],
            "sourceSha": after_approve["sha"].get(APP_FILE),
            "draftDirExists": docker_exec(container, f"test -d {draft_dir}")[0] == 0,
            "draftPreviewReachable": bool(draft_url and http_ok(draft_url, 2.0)),
        }
        report["checks"]["approveSucceeded"] = final.get("status") == "SUCCEEDED"
        report["checks"]["approveHeadAdvanced"] = (
            after_approve["head"] != before["head"]
        )
        report["checks"]["approveSourceChanged"] = after_approve["sha"].get(
            APP_FILE
        ) != before["sha"].get(APP_FILE)
        report["checks"]["approveTreeClean"] = after_approve["status"] == []
        report["checks"]["approveDraftRemoved"] = not report["afterApprove"][
            "draftDirExists"
        ]
        report["checks"]["approveDraftPreviewStopped"] = not report["afterApprove"][
            "draftPreviewReachable"
        ]

        # ---------- 场景 2：拒绝后主工作树仍零变化 ----------
        before_reject = parse_git_state(git_state(container)["raw"])
        reject_run = run_with_approval(
            sandbox_id,
            project_id,
            annotation,
            scenario="none",
        )
        reject_run_id = reject_run["runId"]
        reject_approval = reject_run.get("approval") or {}
        reject_draft_id = draft_id_from_url(reject_approval.get("previewUrl") or "")
        main_at_reject_pause = parse_git_state(git_state(container)["raw"])
        report["checks"]["rejectPauseHeadUnchanged"] = (
            main_at_reject_pause["head"] == before_reject["head"]
        )
        decide(
            reject_run_id,
            reject_approval.get("approvalId") or "",
            "reject",
        )
        reject_final = wait_status(
            reject_run_id, {"REJECTED", "FAILED", "SUCCEEDED", "CANCELLED"}
        )
        after_reject = parse_git_state(git_state(container)["raw"])
        report["rejectResult"] = {
            "status": reject_final.get("status"),
            "head": after_reject["head"],
            "statusLines": after_reject["status"],
            "sourceSha": after_reject["sha"].get(APP_FILE),
            "draftId": reject_draft_id,
            "draftDirExists": docker_exec(
                container, f"test -d /app-drafts/{reject_draft_id}"
            )[0]
            == 0,
        }
        report["checks"]["rejectStatus"] = reject_final.get("status") == "REJECTED"
        report["checks"]["rejectHeadUnchanged"] = (
            after_reject["head"] == before_reject["head"]
        )
        report["checks"]["rejectSourceUnchanged"] = after_reject["sha"].get(
            APP_FILE
        ) == before_reject["sha"].get(APP_FILE)
        report["checks"]["rejectTreeClean"] = after_reject["status"] == []
        report["checks"]["rejectDraftRemoved"] = not report["rejectResult"][
            "draftDirExists"
        ]

        report["passed"] = all(report["checks"].values())
        out_path = OUT_DIR / "draft-isolation-e2e.json"
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(report["checks"], ensure_ascii=False, indent=2))
        print(f"report: {out_path}")
        return 0 if report["passed"] else 1
    finally:
        if sandbox_id:
            destroy_sandbox(sandbox_id)


if __name__ == "__main__":
    raise SystemExit(main())
