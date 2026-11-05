#!/usr/bin/env python3
"""Supervisor both 模式真实混合 E2E：UI 修改 + 全栈架构澄清。

验证顺序门控的完整业务流：

  1. POST /supervisor/dispatch 判定 both；
  2. 只启动 Architect，UI 不创建沙盒；
  3. 契约未确认时 release 被 409 拒绝；
  4. 完成澄清并 CONFIRM 契约；
  5. release 门控，UI Graph 使用同一请求启动并真实修改沙盒；
  6. 重复 release 幂等，返回同一个 UI Run；
  7. 清理沙盒。

默认直连 Python AI Core；设置 AI_BASE_URL=http://127.0.0.1:8080 验证
Java BFF 全链路。

前置：
    docker compose up -d
    python3 scripts/supervisor_both_e2e_test.py
    AI_BASE_URL=http://127.0.0.1:8080 python3 scripts/supervisor_both_e2e_test.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

BASE_URL = os.environ.get("AI_BASE_URL", "http://127.0.0.1:8100").rstrip("/")
IS_BFF = ":8080" in BASE_URL
SUPERVISOR_API_ROOT = os.environ.get(
    "SUPERVISOR_API_ROOT",
    "/api/v1/supervisor" if IS_BFF else "/api/v1/internal/supervisor",
).rstrip("/")
ARCHITECT_API_ROOT = os.environ.get(
    "ARCHITECT_API_ROOT",
    "/api/v1/architect" if IS_BFF else "/api/v1/internal/architect",
).rstrip("/")
STREAM_TIMEOUT = float(os.environ.get("SUPERVISOR_BOTH_E2E_TIMEOUT", "240"))

INITIAL_APP = """export default function App() {
  return (
    <main>
      <style>{"[data-vibe-target='cta-button']{background:#2563eb;color:#fff}"}</style>
      <section data-vibe-target="card">
        <h1 data-vibe-target="title">Supervisor both E2E</h1>
        <p data-vibe-target="copy">契约确认后再改 UI</p>
        <button data-vibe-target="cta-button">提交</button>
      </section>
    </main>
  )
}
"""

SLOT_VALUES = {
    "business.key_flows": "用户提交订单 -> 校验库存 -> 创建订单并扣减库存",
    "data.entities": "Order(id, userId, total, status)；User(id, name, email)",
}


class E2EFailure(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise E2EFailure(message)


def request_json(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 30,
) -> tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
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


def error_code(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    detail = body.get("detail")
    if isinstance(detail, dict):
        return detail.get("code")
    return body.get("code")


def public_stream_path(path: str) -> str:
    """dispatch 返回 BFF 公共路径；直连 AI Core 时翻译为 internal 路径。"""
    if IS_BFF:
        return path
    if path.startswith("/api/v1/architect/"):
        return path.replace("/api/v1/architect/", "/api/v1/internal/architect/", 1)
    if path.startswith("/api/v1/runs/"):
        return path.replace("/api/v1/runs/", "/api/v1/internal/runs/", 1)
    return path


def read_sse(
    path: str,
    accepted_types: set[str],
    *,
    after_seq: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    query = f"?afterSeq={after_seq}" if after_seq > 0 else ""
    request = urllib.request.Request(
        f"{BASE_URL}{path}{query}",
        method="GET",
        headers={
            "Accept": "text/event-stream",
            "Last-Event-ID": str(after_seq),
        },
    )
    events: list[dict[str, Any]] = []
    data_lines: list[str] = []
    deadline = time.monotonic() + STREAM_TIMEOUT

    def flush() -> dict[str, Any] | None:
        nonlocal data_lines
        if not data_lines:
            return None
        raw = "\n".join(data_lines)
        data_lines = []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        events.append(parsed)
        return parsed

    with urllib.request.urlopen(request, timeout=STREAM_TIMEOUT) as response:
        check(response.status == 200, f"SSE HTTP {response.status}: {path}")
        for raw_line in response:
            if time.monotonic() > deadline:
                raise E2EFailure(f"SSE 读取超时: {path}")
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                event = flush()
                if event and event.get("type") in accepted_types:
                    return events, event
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "data":
                data_lines.append(value)
        event = flush()
        if event:
            return events, event
    raise E2EFailure(f"SSE 在目标事件前关闭: {path}")


def annotation() -> dict[str, Any]:
    instruction = "把提交按钮改成蓝色"
    return {
        "version": 1,
        "annotationId": f"ann_both_{uuid.uuid4().hex[:10]}",
        "type": "drag",
        "worldTarget": {
            "x": 0.2,
            "y": 0.2,
            "width": 0.2,
            "height": 0.1,
            "coordinateSpace": "normalized",
        },
        "target": {
            "mode": "source",
            "source": {
                "file": "src/App.jsx",
                "line": 7,
                "column": 7,
                "component": "App",
                "sourceKind": "jsx",
            },
            "selector": "[data-vibe-target='cta-button']",
            "snippet": {
                "startLine": 1,
                "endLine": len(INITIAL_APP.splitlines()),
                "content": INITIAL_APP,
                "hash": "sha256:supervisor-both-e2e",
            },
        },
        "intent": {
            "kind": "UI_ONLY",
            "ui": {
                "summary": instruction,
                "acceptance": [f"完成指令：{instruction}"],
                "elementRole": "button",
            },
        },
        "payload": {"text": instruction},
        "createdAt": "2026-11-05T10:00:00Z",
    }


def both_payload(tag: str) -> dict[str, Any]:
    return {
        "tenantId": "tenant_e2e",
        "projectId": f"project_supervisor_both_{tag}",
        "projectName": f"Supervisor both E2E - {tag}",
        "message": "把提交按钮改成蓝色，同时规划全栈后端、REST 接口和 MySQL 数据库",
        "annotations": [annotation()],
        "files": {"src/App.jsx": INITIAL_APP},
        "source": {"files": {"src/App.jsx": INITIAL_APP}},
        "uiHistory": [{"role": "user", "content": "先做一个订单管理页面"}],
        "options": {
            "scopeHint": "auto",
            "ui": {
                "llmProvider": "mock",
                "mockScenario": "none",
                "clarificationMode": "off",
                "requireManualApproval": False,
                "keepSandbox": True,
            },
            "architect": {
                "llmProvider": "mock",
                "confidenceThreshold": 0.85,
                "maxClarificationRounds": 5,
                "ragProvider": "markdown",
                "ragTopK": 3,
            },
        },
    }


def answer_questions(
    run_id: str,
    clarification: dict[str, Any],
) -> None:
    answers = []
    for question in clarification["questions"]:
        slot = question["slot"]
        if slot == "app.shape":
            value = "full_stack_system"
        elif question.get("options"):
            value = question["options"][0]["value"]
        else:
            value = SLOT_VALUES.get(slot, f"{slot} 的混合 E2E 取值")
        answers.append({"questionId": question["questionId"], "value": value})
    status, body = request_json(
        "POST",
        f"{ARCHITECT_API_ROOT}/runs/{run_id}/clarifications/"
        f"{clarification['clarificationId']}/answers",
        {"answers": answers},
    )
    check(status == 202, f"提交澄清应返回 202，实际 {status}: {body}")


def drive_architect_to_contract(
    stream_path: str,
    run_id: str,
) -> tuple[dict[str, Any], int]:
    events, terminal = read_sse(
        stream_path,
        {"architect.clarification.required", "architect.failed"},
    )
    check(
        terminal.get("type") == "architect.clarification.required",
        f"Architect 首事件错误: {terminal.get('type')}",
    )
    last_seq = int(terminal["seq"])
    for _ in range(8):
        clarification = terminal.get("payload") or {}
        answer_questions(run_id, clarification)
        events, terminal = read_sse(
            stream_path,
            {
                "architect.clarification.required",
                "architect.contract.ready",
                "architect.failed",
            },
            after_seq=last_seq,
        )
        last_seq = max([last_seq] + [int(item.get("seq", 0)) for item in events])
        if terminal.get("type") == "architect.contract.ready":
            return terminal["payload"]["contract"], last_seq
        check(
            terminal.get("type") == "architect.clarification.required",
            f"预期澄清或契约，实际 {terminal.get('type')}: {terminal.get('payload')}",
        )
    raise E2EFailure("超过 8 轮仍未生成契约")


def confirm_contract(
    stream_path: str,
    run_id: str,
    contract: dict[str, Any],
    after_seq: int,
) -> dict[str, Any]:
    status, body = request_json(
        "POST",
        f"{ARCHITECT_API_ROOT}/runs/{run_id}/contracts/"
        f"{contract['contractId']}/decision",
        {
            "contractId": contract["contractId"],
            "decision": "CONFIRM",
        },
    )
    check(status == 202, f"CONFIRM 应返回 202，实际 {status}: {body}")
    _, terminal = read_sse(
        stream_path,
        {"architect.contract.confirmed", "architect.failed"},
        after_seq=after_seq,
    )
    check(
        terminal.get("type") == "architect.contract.confirmed",
        f"契约未确认: {terminal.get('type')}: {terminal.get('payload')}",
    )
    return terminal


def destroy_sandbox(sandbox_id: str | None) -> None:
    if not sandbox_id:
        return
    path = (
        f"/api/v1/sandboxes/{sandbox_id}"
        if IS_BFF
        else f"/api/v1/internal/sandbox/{sandbox_id}"
    )
    status, body = request_json("DELETE", path, timeout=60)
    if status in {200, 204, 404}:
        print(f"   cleanup sandbox={sandbox_id}")
    else:
        print(
            f"   cleanup warning sandbox={sandbox_id}: {status} {body}", file=sys.stderr
        )


def main() -> int:
    tag = uuid.uuid4().hex[:8]
    payload = both_payload(tag)
    sandbox_id: str | None = None
    try:
        print("== 1. both dispatch：只启动 Architect ==")
        status, dispatch = request_json(
            "POST", f"{SUPERVISOR_API_ROOT}/dispatch", payload
        )
        check(status == 202, f"dispatch 应返回 202，实际 {status}: {dispatch}")
        decision = dispatch.get("decision") or {}
        check(decision.get("handoff") == "handoff_both", f"未判定 both: {decision}")
        dispatches = dispatch.get("dispatches") or []
        check(
            [item.get("agentId") for item in dispatches] == ["architect_graph"],
            f"门控前只应启动 Architect: {dispatches}",
        )
        gate = dispatch.get("gate") or {}
        gate_id = gate.get("gateId")
        check(bool(gate_id), f"缺少 gateId: {gate}")
        check(gate.get("status") == "AWAITING_CONTRACT", f"门控状态错误: {gate}")
        architect_run_id = dispatches[0]["runId"]
        architect_stream = public_stream_path(dispatches[0]["streamPath"])
        print(f"   gate={gate_id} architectRun={architect_run_id} " "uiRun=<blocked>")

        print("\n== 2. 契约未确认：UI 派发被强门控拦截 ==")
        status, early = request_json(
            "POST", f"{SUPERVISOR_API_ROOT}/gates/{gate_id}/release", {}
        )
        check(status == 409, f"早释放应 409，实际 {status}: {early}")
        check(
            error_code(early) == "SUPERVISOR_CONTRACT_NOT_CONFIRMED",
            f"错误码错误: {early}",
        )
        print("   409 SUPERVISOR_CONTRACT_NOT_CONFIRMED")

        print("\n== 3. 完成全栈架构澄清并 CONFIRM 契约 ==")
        contract, contract_seq = drive_architect_to_contract(
            architect_stream, architect_run_id
        )
        check(
            contract.get("appShape") == "full_stack_system",
            f"应用形态错误: {contract.get('appShape')}",
        )
        check(bool(contract.get("apiRoutes")), "全栈契约缺少 API")
        check(
            "CREATE TABLE" in contract.get("databaseDdl", ""),
            "全栈契约缺少 DDL",
        )
        confirmed = confirm_contract(
            architect_stream, architect_run_id, contract, contract_seq
        )
        check(
            (confirmed.get("payload") or {}).get("contract", {}).get("status")
            == "CONFIRMED",
            "契约确认事件状态错误",
        )
        print(f"   contract={contract['contractId']} CONFIRMED")

        print("\n== 4. 契约确认后释放门控：启动真实 UI Graph ==")
        status, released = request_json(
            "POST", f"{SUPERVISOR_API_ROOT}/gates/{gate_id}/release", {}
        )
        check(status == 200, f"release 应返回 200，实际 {status}: {released}")
        released_gate = released.get("gate") or {}
        check(released_gate.get("status") == "RELEASED", f"门控未释放: {released_gate}")
        ui_dispatches = released.get("dispatches") or []
        check(
            [item.get("agentId") for item in ui_dispatches] == ["ui_graph"],
            f"release 应只启动 UI: {ui_dispatches}",
        )
        ui_run_id = ui_dispatches[0]["runId"]
        ui_stream = public_stream_path(ui_dispatches[0]["streamPath"])
        check(ui_run_id != architect_run_id, "UI 与 Architect 必须使用独立 Run")
        print(f"   gate=RELEASED uiRun={ui_run_id}")

        print("\n== 5. 消费 UI SSE，等待真实沙盒执行终态 ==")
        events, terminal = read_sse(
            ui_stream,
            {
                "run.succeeded",
                "run.failed",
                "run.cancelled",
                "run.rejected",
                "approval.required",
                "clarification.required",
            },
        )
        event_types = [item.get("type") for item in events]
        check(
            "run.started" in event_types or "node.started" in event_types,
            f"未收到 UI 执行事件: {event_types[:8]}",
        )
        check(
            terminal.get("type") == "run.succeeded",
            f"UI Run 未成功: {terminal.get('type')}: {terminal.get('payload')}",
        )
        result = terminal.get("payload") or {}
        sandbox_id = result.get("sandboxId")
        check(bool(sandbox_id), f"UI Run 未返回 sandboxId: {result}")
        check(
            bool(result.get("previewUrl")),
            f"UI Run 未返回 previewUrl: {result}",
        )
        print(
            f"   run.succeeded sandbox={sandbox_id} "
            f"commit={result.get('commitHash')}"
        )

        print("\n== 6. release 幂等：不重复创建 UI Run / 沙盒 ==")
        status, duplicate = request_json(
            "POST", f"{SUPERVISOR_API_ROOT}/gates/{gate_id}/release", {}
        )
        check(status == 200, f"重复 release 应返回 200，实际 {status}: {duplicate}")
        duplicate_dispatches = duplicate.get("dispatches") or []
        check(
            len(duplicate_dispatches) == 1
            and duplicate_dispatches[0].get("runId") == ui_run_id,
            f"重复 release 返回了不同 Run: {duplicate_dispatches}",
        )
        print(f"   idempotent uiRun={ui_run_id}")

        print("\nSUPERVISOR BOTH E2E: PASS")
        return 0
    finally:
        destroy_sandbox(sandbox_id)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except E2EFailure as exc:
        print(f"\nSUPERVISOR BOTH E2E: FAIL - {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
