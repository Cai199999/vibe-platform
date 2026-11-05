#!/usr/bin/env python3
"""M3-A Diff 审批 + 取消 Run 全链路 E2E。

覆盖三条最容易回归的路径：
  1. approvalMode=required -> approval.required 携带逐文件 Diff；
     批准后写入 Git checkpoint -> run.succeeded / status=SUCCEEDED；
  2. 拒绝 -> run.rejected / status=REJECTED，沙盒 HEAD 回滚到执行前；
  3. 审批暂停中取消 -> run.cancelled / status=CANCELLED，HEAD 同样回滚。

同时校验 Java BFF 的审批 / 取消代理接口保留 AI Core 的 HTTP 语义。

运行：
    python3 scripts/approval_cancel_e2e_test.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

AI_BASE_URL = os.environ.get("AI_BASE_URL", "http://127.0.0.1:8100").rstrip("/")
BFF_BASE_URL = os.environ.get("BFF_BASE_URL", "http://127.0.0.1:8080").rstrip("/")

INITIAL_APP = """export default function App() {
  return (
    <main>
      <style>{"[data-vibe-target='cta-button']{background:#2563eb;color:#fff}"}</style>
      <section data-vibe-target="card">
        <h1 data-vibe-target="title">Old title</h1>
        <p data-vibe-target="copy">Old copy</p>
        <button data-vibe-target="cta-button">Go</button>
      </section>
    </main>
  )
}
"""


def log_step(title: str) -> None:
    print(f"\n=== {title} ===")


def fail(message: str) -> None:
    print(f"\nFAIL: {message}")
    sys.exit(1)


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def call(
    method: str,
    path: str,
    payload: dict | None = None,
    *,
    base_url: str = AI_BASE_URL,
    timeout: float = 300,
) -> tuple[int, object]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body


def read_sse(url: str, timeout: float = 120) -> list[dict]:
    """读取完整 SSE 流并解析 data 帧。

    终态事件到达后服务端主动关闭连接，因此这里可以同步读完。
    """
    events: list[dict] = []
    request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        buffer = ""
        while True:
            chunk = response.readline()
            if not chunk:
                break
            line = chunk.decode("utf-8", errors="replace").rstrip("\r\n")
            if line == "":
                if buffer:
                    events.append(json.loads(buffer))
                    buffer = ""
                continue
            if line.startswith("data:"):
                buffer += line[5:].lstrip()
    if buffer:
        events.append(json.loads(buffer))
    return events


def sandbox_git(sandbox_id: str, *args: str) -> str:
    container = f"vibe-sandbox-{sandbox_id}"
    return subprocess.check_output(
        ["docker", "exec", container, "git", *args],
        text=True,
    ).strip()


def sandbox_head(sandbox_id: str) -> str:
    return sandbox_git(sandbox_id, "rev-parse", "HEAD")


def sandbox_worktree_dirty(sandbox_id: str) -> bool:
    output = sandbox_git(sandbox_id, "status", "--porcelain", "--untracked-files=no")
    return bool(output.strip())


def build_annotation(run_id: str) -> dict:
    instruction = "改成蓝色"
    return {
        "version": 1,
        "annotationId": f"ann_{run_id}",
        "type": "drag",
        "worldTarget": {
            "x": 0.32,
            "y": 0.61,
            "width": 0.18,
            "height": 0.08,
            "coordinateSpace": "normalized",
        },
        "target": {
            "mode": "source",
            "source": {
                "file": "src/App.jsx",
                "line": 6,
                "column": 7,
                "component": "App",
                "sourceKind": "jsx",
            },
            "selector": "[data-vibe-target='cta-button']",
            "snippet": {
                "startLine": 1,
                "endLine": len(INITIAL_APP.splitlines()),
                "content": INITIAL_APP,
                "hash": "sha256:e2e",
            },
        },
        "intent": {
            "kind": "UI_ONLY",
            "ui": {
                "summary": instruction,
                "acceptance": ["按钮背景变成蓝色"],
                "elementRole": "button",
            },
        },
        "payload": {"text": instruction},
        "createdAt": "2026-11-05T10:00:00Z",
        "threadId": f"thread_{run_id}",
    }


def run_payload(label: str, *, approval_mode: str = "required") -> dict:
    run_id = f"run_apr_{label}_{int(time.time() * 1000)}"
    return {
        "runId": run_id,
        "projectId": f"project_apr_{label}",
        "annotations": [build_annotation(run_id)],
        "files": {"src/App.jsx": INITIAL_APP},
        "options": {
            "llmProvider": "mock",
            # 跳过自动修复演示，直接构建通过进入审批。
            "mockScenario": "none",
            "maxRetries": 3,
            "keepSandbox": True,
            "clarificationMode": "off",
            "approvalMode": approval_mode,
        },
    }


def wait_status_ai(run_id: str, expected: set[str], timeout: float = 180) -> dict:
    deadline = time.time() + timeout
    snapshot: dict = {}
    while time.time() < deadline:
        status, snapshot = call("GET", f"/api/v1/internal/runs/{run_id}")
        if status != 200:
            fail(f"读取 Run 快照失败: HTTP {status} {snapshot}")
        if snapshot.get("status") in expected:
            return snapshot
        time.sleep(0.5)
    fail(f"等待 Run 状态 {expected} 超时，最后快照: {snapshot}")
    return snapshot


def wait_status_bff(run_id: str, expected: set[str], timeout: float = 180) -> dict:
    deadline = time.time() + timeout
    snapshot: dict = {}
    while time.time() < deadline:
        status, snapshot = call("GET", f"/api/v1/runs/{run_id}", base_url=BFF_BASE_URL)
        if status != 200:
            fail(f"读取 BFF Run 快照失败: HTTP {status} {snapshot}")
        if snapshot.get("status") in expected:
            return snapshot
        time.sleep(0.5)
    fail(f"等待 BFF Run 状态 {expected} 超时，最后快照: {snapshot}")
    return snapshot


def start_run(payload: dict, *, base_url: str = AI_BASE_URL) -> dict:
    path = (
        "/api/v1/runs" if base_url == BFF_BASE_URL else "/api/v1/internal/runs/execute"
    )
    status, body = call("POST", path, payload, base_url=base_url)
    require(status == 200, f"启动 Run 失败: HTTP {status} {body}")
    require(
        body.get("status") == "AWAITING_APPROVAL",
        f"approvalMode=required 应暂停等待审批，实际 {body.get('status')}",
    )
    approval = body.get("approval")
    require(bool(approval), f"响应缺少 approval: {body}")
    require(approval.get("status") == "PENDING", f"审批状态异常: {approval}")
    require(bool(approval.get("approvalId")), f"审批缺少 approvalId: {approval}")
    evidence = approval.get("evidence") or {}
    require(evidence.get("filesChanged", 0) >= 1, f"审批证据没有文件变更: {evidence}")
    files = evidence.get("files") or []
    require(bool(files), "审批证据缺少逐文件 Diff")
    diff = files[0].get("diff") or ""
    require("+ " in diff or "+" in diff, f"Diff 内容异常: {diff[:120]}")
    require(
        (evidence.get("insertions", 0) + evidence.get("deletions", 0)) > 0,
        f"Diff 统计为 0: {evidence}",
    )
    require(approval.get("riskLevel") in {"LOW", "MEDIUM", "HIGH"}, "风险级别缺失")
    require(bool(approval.get("expiresAt")), "审批缺少 expiresAt")
    return body


def approve(run_id: str, approval_id: str, *, base_url: str = AI_BASE_URL) -> None:
    path = (
        f"/api/v1/runs/{run_id}/approvals/{approval_id}/decision"
        if base_url == BFF_BASE_URL
        else f"/api/v1/internal/runs/{run_id}/approvals/{approval_id}/decision"
    )
    status, body = call(
        "POST", path, {"decision": "approve", "comment": "E2E 批准"}, base_url=base_url
    )
    require(status == 202, f"批准请求应返回 202，实际 {status}: {body}")


def reject(run_id: str, approval_id: str, *, base_url: str = AI_BASE_URL) -> None:
    path = (
        f"/api/v1/runs/{run_id}/approvals/{approval_id}/decision"
        if base_url == BFF_BASE_URL
        else f"/api/v1/internal/runs/{run_id}/approvals/{approval_id}/decision"
    )
    status, body = call(
        "POST", path, {"decision": "reject", "comment": "E2E 拒绝"}, base_url=base_url
    )
    require(status == 202, f"拒绝请求应返回 202，实际 {status}: {body}")


def cancel(run_id: str, *, base_url: str = AI_BASE_URL) -> dict:
    path = (
        f"/api/v1/runs/{run_id}/cancel"
        if base_url == BFF_BASE_URL
        else f"/api/v1/internal/runs/{run_id}/cancel"
    )
    status, body = call("POST", path, {"reason": "E2E 取消"}, base_url=base_url)
    require(status == 200, f"取消请求失败: HTTP {status} {body}")
    return body


def destroy(sandbox_id: str | None) -> None:
    if not sandbox_id:
        return
    status, body = call("DELETE", f"/api/v1/internal/sandbox/{sandbox_id}", timeout=30)
    if status == 200:
        print(f"[cleanup] 已销毁沙盒 {sandbox_id}")
    else:
        print(f"[cleanup] 销毁沙盒失败 {sandbox_id}: {status} {body}", file=sys.stderr)


def main() -> int:
    sandbox_ids: list[str] = []
    try:
        # --------------------------------------------------
        log_step("1. 审批暂停：payload 携带逐文件 Diff")
        payload = run_payload("approve")
        run_id = payload["runId"]
        paused = start_run(payload)
        sandbox_id = paused["sandboxId"]
        sandbox_ids.append(sandbox_id)
        approval = paused["approval"]
        evidence = approval["evidence"]
        head_before = sandbox_head(sandbox_id)
        print(
            json.dumps(
                {
                    "runId": run_id,
                    "status": paused["status"],
                    "approvalId": approval["approvalId"],
                    "riskLevel": approval["riskLevel"],
                    "filesChanged": evidence["filesChanged"],
                    "insertions": evidence["insertions"],
                    "deletions": evidence["deletions"],
                    "headBefore": head_before[:7],
                },
                ensure_ascii=False,
            )
        )

        # 暂停期间不得产生 commit；工作区可以包含未提交改动。
        require(
            sandbox_head(sandbox_id) == head_before,
            "审批暂停时不应提交 Git checkpoint",
        )

        # --------------------------------------------------
        log_step("2. 批准：写 Git checkpoint -> SUCCEEDED -> run.succeeded")
        events = read_sse(
            f"{AI_BASE_URL}/api/v1/internal/runs/{run_id}/events/stream?afterSeq=0"
        )
        require(
            any(event.get("type") == "approval.required" for event in events),
            f"事件流缺少 approval.required: {[e.get('type') for e in events]}",
        )
        # approval.required 是终止当前连接的暂停事件；决策后从它的 seq 继续重放。
        paused_seq = int(events[-1]["seq"])
        approve(run_id, approval["approvalId"])
        snapshot = wait_status_ai(run_id, {"SUCCEEDED"})
        require(snapshot.get("status") == "SUCCEEDED", f"批准后状态异常: {snapshot}")
        head_after = sandbox_head(sandbox_id)
        require(head_after != head_before, "批准后应生成新的 Git commit")
        require(not sandbox_worktree_dirty(sandbox_id), "批准后工作区不应有未提交改动")
        events = read_sse(
            f"{AI_BASE_URL}/api/v1/internal/runs/{run_id}/events/stream"
            f"?afterSeq={paused_seq}"
        )
        types = [event.get("type") for event in events]
        require("approval.answered" in types, f"事件流缺少 approval.answered: {types}")
        require("run.succeeded" in types, f"事件流缺少 run.succeeded: {types}")
        print(f"   HEAD {head_before[:7]} -> {head_after[:7]}，事件：{types}")

        # --------------------------------------------------
        log_step("3. 拒绝：回滚到执行前 HEAD -> REJECTED -> run.rejected")
        payload = run_payload("reject")
        run_id = payload["runId"]
        paused = start_run(payload)
        sandbox_id = paused["sandboxId"]
        sandbox_ids.append(sandbox_id)
        approval = paused["approval"]
        head_before = sandbox_head(sandbox_id)
        events = read_sse(
            f"{AI_BASE_URL}/api/v1/internal/runs/{run_id}/events/stream?afterSeq=0"
        )
        paused_seq = int(events[-1]["seq"])
        reject(run_id, approval["approvalId"])
        snapshot = wait_status_ai(run_id, {"REJECTED"})
        require(snapshot.get("status") == "REJECTED", f"拒绝后状态异常: {snapshot}")
        require(sandbox_head(sandbox_id) == head_before, "拒绝后 HEAD 未回滚")
        require(not sandbox_worktree_dirty(sandbox_id), "拒绝后工作区应为干净")
        events = read_sse(
            f"{AI_BASE_URL}/api/v1/internal/runs/{run_id}/events/stream"
            f"?afterSeq={paused_seq}"
        )
        types = [event.get("type") for event in events]
        require("run.rejected" in types, f"事件流缺少 run.rejected: {types}")
        print(f"   HEAD 保持 {head_before[:7]}，事件：{types}")

        # --------------------------------------------------
        log_step("4. 审批中取消：立即回滚 -> CANCELLED -> run.cancelled")
        payload = run_payload("cancel")
        run_id = payload["runId"]
        paused = start_run(payload)
        sandbox_id = paused["sandboxId"]
        sandbox_ids.append(sandbox_id)
        head_before = sandbox_head(sandbox_id)
        events = read_sse(
            f"{AI_BASE_URL}/api/v1/internal/runs/{run_id}/events/stream?afterSeq=0"
        )
        paused_seq = int(events[-1]["seq"])
        body = cancel(run_id)
        require(body.get("status") == "CANCELLED", f"取消响应异常: {body}")
        snapshot = wait_status_ai(run_id, {"CANCELLED"})
        require(snapshot.get("status") == "CANCELLED", f"取消后状态异常: {snapshot}")
        require(sandbox_head(sandbox_id) == head_before, "取消后 HEAD 未回滚")
        events = read_sse(
            f"{AI_BASE_URL}/api/v1/internal/runs/{run_id}/events/stream"
            f"?afterSeq={paused_seq}"
        )
        types = [event.get("type") for event in events]
        require("run.cancelled" in types, f"事件流缺少 run.cancelled: {types}")
        print(f"   HEAD 保持 {head_before[:7]}，事件：{types}")

        # --------------------------------------------------
        log_step("5. BFF 代理：审批 -> 批准 -> SUCCEEDED")
        payload = run_payload("bff")
        run_id = payload["runId"]
        paused = start_run(payload, base_url=BFF_BASE_URL)
        sandbox_id = paused["sandboxId"]
        sandbox_ids.append(sandbox_id)
        approval = paused["approval"]
        approve(run_id, approval["approvalId"], base_url=BFF_BASE_URL)
        snapshot = wait_status_bff(run_id, {"SUCCEEDED"})
        status, approvals = call(
            "GET", f"/api/v1/runs/{run_id}/approvals", base_url=BFF_BASE_URL
        )
        require(status == 200, f"BFF 审批列表失败: HTTP {status} {approvals}")
        items = approvals.get("approvals") or []
        require(
            any(item.get("status") == "APPROVED" for item in items),
            f"BFF 审批列表缺少 APPROVED 记录: {approvals}",
        )
        print(f"   BFF 状态={snapshot.get('status')}，审批记录={len(items)} 条")

        # --------------------------------------------------
        log_step("6. BFF 代理：审批中取消 -> CANCELLED")
        payload = run_payload("bff_cancel")
        run_id = payload["runId"]
        paused = start_run(payload, base_url=BFF_BASE_URL)
        sandbox_ids.append(paused["sandboxId"])
        body = cancel(run_id, base_url=BFF_BASE_URL)
        require(body.get("status") == "CANCELLED", f"BFF 取消响应异常: {body}")
        snapshot = wait_status_bff(run_id, {"CANCELLED"})
        require(snapshot.get("status") == "CANCELLED", f"BFF 取消状态异常: {snapshot}")

        print("\n全部通过 ✅")
        return 0
    finally:
        for sandbox_id in sandbox_ids:
            destroy(sandbox_id)


if __name__ == "__main__":
    sys.exit(main())
