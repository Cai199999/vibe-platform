#!/usr/bin/env python3
"""Supervisor 路由 / Registry / 子图派发 API E2E。

默认直连 Python AI Core；设置 AI_BASE_URL=http://127.0.0.1:8080
可验证 Java BFF 透传。

覆盖：
  1. AgentRegistry 内置 / 预留 Agent 可查询；
  2. UI、Architect、both、unknown 四类路由；
  3. 显式指定 reserved Agent 返回 AGENT_NOT_AVAILABLE；
  4. dispatch 真实启动 Architect 子图并返回可订阅 SSE；
  5. UI 缺少标注时拒绝派发，不创建沙盒。

前置：
    docker compose up -d
    python3 scripts/supervisor_e2e_test.py
    AI_BASE_URL=http://127.0.0.1:8080 python3 scripts/supervisor_e2e_test.py
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
# AI Core 直连走 internal 路由；指向 BFF 时走公开透传路由。
API_ROOT = os.environ.get(
    "SUPERVISOR_API_ROOT",
    "/api/v1/supervisor" if ":8080" in BASE_URL else "/api/v1/internal/supervisor",
).rstrip("/")
ARCHITECT_API_ROOT = os.environ.get(
    "ARCHITECT_API_ROOT",
    "/api/v1/architect" if ":8080" in BASE_URL else "/api/v1/internal/architect",
).rstrip("/")
STREAM_TIMEOUT = float(os.environ.get("SUPERVISOR_E2E_TIMEOUT", "120"))

SOURCE_APP = """export default function App() {
  return (
    <main>
      <h1>Supervisor E2E</h1>
      <button>提交</button>
    </main>
  )
}
"""

ANNOTATION = {
    "annotationId": "ann_supervisor_e2e",
    "type": "note",
    "action": "MODIFY",
    "target": {
        "mode": "source",
        "source": {"file": "src/App.jsx", "component": "App"},
    },
    "intent": {
        "kind": "UI_ONLY",
        "ui": {"summary": "把提交按钮改成蓝色"},
    },
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


def read_until_sse(path: str, accepted_types: set[str]) -> dict[str, Any]:
    """读取 SSE 直到命中暂停 / 终态事件，用于验证派发后的子 Run 可订阅。"""
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        method="GET",
        headers={"Accept": "text/event-stream"},
    )
    deadline = time.monotonic() + STREAM_TIMEOUT
    data_lines: list[str] = []
    with urllib.request.urlopen(request, timeout=STREAM_TIMEOUT) as response:
        check(response.status == 200, f"SSE HTTP {response.status}")
        for raw_line in response:
            if time.monotonic() > deadline:
                raise E2EFailure("SSE 读取超时")
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                if not data_lines:
                    continue
                raw = "\n".join(data_lines)
                data_lines = []
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if parsed.get("type") in accepted_types:
                    return parsed
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "data":
                data_lines.append(value)
    raise E2EFailure("SSE 在目标事件前关闭")


def internal_stream_path(public_path: str) -> str:
    """直连 AI Core 时把 BFF 公共路径翻译成 internal 路径。

    ``dispatches[].streamPath`` 对外契约始终是 BFF 公共路径，方便前端订阅；
    本地 E2E 直连 :8100 时需要换成 ``/api/v1/internal/...``。
    """
    if ":8080" in BASE_URL:
        return public_path
    if public_path.startswith("/api/v1/architect/"):
        return public_path.replace(
            "/api/v1/architect/",
            "/api/v1/internal/architect/",
            1,
        )
    if public_path.startswith("/api/v1/runs/"):
        return public_path.replace(
            "/api/v1/runs/",
            "/api/v1/internal/runs/",
            1,
        )
    return public_path


def base_payload(run_tag: str) -> dict[str, Any]:
    return {
        "tenantId": "tenant_e2e",
        "projectId": f"project_supervisor_{run_tag}",
        "projectName": f"Supervisor E2E - {run_tag}",
        "sandboxId": None,
        "files": {"src/App.jsx": SOURCE_APP},
        "source": {"files": {"src/App.jsx": SOURCE_APP}},
        "uiHistory": [{"role": "user", "content": "先做一个可运行的页面"}],
        "options": {
            "scopeHint": "auto",
            "ui": {"llmProvider": "mock", "mockScenario": "none"},
            "architect": {
                "llmProvider": "mock",
                "ragProvider": "markdown",
                "ragTopK": 3,
            },
        },
    }


def route_case(
    label: str,
    payload: dict[str, Any],
    expected_target: str,
    expected_targets: list[str] | None = None,
    expected_dispatchable: bool | None = None,
    expected_handoff: str | None = None,
) -> dict[str, Any]:
    status, body = request_json("POST", f"{API_ROOT}/route", payload)
    check(status == 200, f"{label}: route 应返回 200，实际 {status}: {body}")
    decision = body.get("decision") or {}
    check(
        decision.get("target") == expected_target,
        f"{label}: target 应为 {expected_target}，实际 {decision.get('target')}: {decision}",
    )
    if expected_handoff is not None:
        check(
            decision.get("handoff") == expected_handoff,
            f"{label}: handoff 应为 {expected_handoff}，"
            f"实际 {decision.get('handoff')}: {decision}",
        )
    if expected_targets is not None:
        check(
            decision.get("targets") == expected_targets,
            f"{label}: targets 应为 {expected_targets}，实际 {decision.get('targets')}",
        )
    if expected_dispatchable is not None:
        check(
            decision.get("dispatchable") is expected_dispatchable,
            f"{label}: dispatchable 应为 {expected_dispatchable}，"
            f"实际 {decision.get('dispatchable')}",
        )
    print(
        f"   {label}: target={decision.get('target')} "
        f"confidence={decision.get('confidence')} "
        f"dispatchable={decision.get('dispatchable')}"
    )
    return body


def main() -> int:
    print("== 1. AgentRegistry 查询 ==")
    status, agents = request_json("GET", f"{API_ROOT}/agents")
    check(status == 200, f"agents 应返回 200，实际 {status}: {agents}")
    by_id = {item["agentId"]: item for item in agents.get("agents", [])}
    for agent_id in ("ui_graph", "architect_graph", "backend_coder", "script_agent"):
        check(agent_id in by_id, f"注册中心缺少 {agent_id}")
    check(by_id["ui_graph"]["status"] == "available", "ui_graph 应可用")
    check(
        by_id["architect_graph"]["status"] == "available",
        "architect_graph 应可用",
    )
    check(by_id["backend_coder"]["status"] == "reserved", "backend_coder 应 reserved")
    check(by_id["script_agent"]["status"] == "reserved", "script_agent 应 reserved")
    print(f"   registryVersion={agents.get('registryVersion')}")

    status, reloaded = request_json("POST", f"{API_ROOT}/agents/reload", {})
    check(status == 200, f"agents/reload 应返回 200，实际 {status}: {reloaded}")
    reloaded_ids = {item["agentId"] for item in reloaded.get("agents", [])}
    check("ui_graph" in reloaded_ids, "重载后丢失 ui_graph")
    print(f"   reload registryVersion={reloaded.get('registryVersion')}")

    print("\n== 1.5 CustomAgent Manifest 秒级接入 ==")
    check(
        "excel_report_agent" in reloaded_ids,
        "未加载 config/custom_agents/excel_report_agent.json",
    )
    custom = by_id.get("excel_report_agent") or {}
    check(custom.get("source") == "manifest", "excel_report_agent 应来自 manifest")
    check(custom.get("status") == "custom", "excel_report_agent 应为 custom")
    route_case(
        "custom",
        {**base_payload("custom"), "message": "帮我导出一份本月的销售数据"},
        "excel_report_agent",
        ["excel_report_agent"],
        False,
        "handoff_custom",
    )

    print("\n== 2. UI 路由（有标注） ==")
    ui_payload = {
        **base_payload("ui"),
        "message": "把提交按钮改成蓝色",
        "annotations": [ANNOTATION],
    }
    route_case("ui", ui_payload, "ui_graph", ["ui_graph"], True, "handoff_ui")

    print("\n== 3. Architect 路由 ==")
    architect_payload = {
        **base_payload("architect"),
        "message": "帮我设计全栈系统，后端用 Spring Boot，数据库用 MySQL",
    }
    route_case(
        "architect",
        architect_payload,
        "architect_graph",
        ["architect_graph"],
        True,
        "handoff_architect",
    )

    print("\n== 4. both 路由（有序 targets） ==")
    both_payload = {
        **base_payload("both"),
        "message": "把提交按钮改蓝，同时规划全栈后端和数据库",
        "annotations": [ANNOTATION],
    }
    both = route_case(
        "both",
        both_payload,
        "both",
        ["architect_graph", "ui_graph"],
        True,
        "handoff_both",
    )
    handoffs = both.get("handoffs") or []
    check(
        [item.get("agentId") for item in handoffs] == ["architect_graph", "ui_graph"],
        f"both Handoff 顺序错误: {handoffs}",
    )

    print("\n== 5. 无有效信号 => rejected ==")
    unknown_payload = {
        **base_payload("unknown"),
        "message": "今天天气怎么样",
    }
    unknown = route_case(
        "unknown",
        unknown_payload,
        "none",
        [],
        False,
        "rejected",
    )
    check(
        (unknown.get("decision") or {}).get("targets") == [],
        "rejected 不应返回 targets",
    )

    print("\n== 6. reserved Agent 显式路由 => 409 ==")
    reserved_payload = {
        **base_payload("reserved"),
        "message": "生成后端",
        "options": {
            "targetAgent": "backend_coder",
            "ui": {"llmProvider": "mock"},
            "architect": {"llmProvider": "mock"},
        },
    }
    status, body = request_json("POST", f"{API_ROOT}/route", reserved_payload)
    check(status == 409, f"reserved Agent 应返回 409，实际 {status}: {body}")
    detail = body.get("detail") if isinstance(body, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else body.get("code")
    check(
        code == "AGENT_NOT_AVAILABLE",
        f"错误码应为 AGENT_NOT_AVAILABLE，实际 {code}: {body}",
    )
    print("   AGENT_NOT_AVAILABLE")

    print("\n== 7. UI 缺少标注 => 422，且不创建沙盒 ==")
    no_annotation = {
        **base_payload("ui_missing_annotation"),
        "message": "把提交按钮改成蓝色",
    }
    status, body = request_json("POST", f"{API_ROOT}/dispatch", no_annotation)
    check(status == 422, f"缺少标注应返回 422，实际 {status}: {body}")
    detail = body.get("detail") if isinstance(body, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else body.get("code")
    check(
        code == "SUPERVISOR_UI_CONTEXT_REQUIRED",
        f"错误码错误: {body}",
    )
    print("   SUPERVISOR_UI_CONTEXT_REQUIRED")

    print("\n== 8. both 强门控：只启动 Architect，UI 等待契约 ==")
    status, gated = request_json("POST", f"{API_ROOT}/dispatch", both_payload)
    check(status == 202, f"both dispatch 应返回 202，实际 {status}: {gated}")
    gated_dispatches = gated.get("dispatches") or []
    check(
        [item.get("agentId") for item in gated_dispatches] == ["architect_graph"],
        f"both 在门控释放前只应启动 Architect: {gated_dispatches}",
    )
    gate = gated.get("gate") or {}
    gate_id = gate.get("gateId")
    architect_run_id = gated_dispatches[0].get("runId")
    check(bool(gate_id), f"both 必须返回 gateId: {gated}")
    check(
        gate.get("status") == "AWAITING_CONTRACT",
        f"门控初始状态错误: {gate}",
    )
    check(
        gate.get("pendingAgentId") == "ui_graph",
        f"门控应等待 ui_graph: {gate}",
    )
    print(f"   gateId={gate_id} architectRunId={architect_run_id}")

    status, early = request_json("POST", f"{API_ROOT}/gates/{gate_id}/release", {})
    check(status == 409, f"契约未确认时 release 应返回 409，实际 {status}: {early}")
    early_detail = early.get("detail") if isinstance(early, dict) else None
    early_code = early_detail.get("code") if isinstance(early_detail, dict) else None
    check(
        early_code == "SUPERVISOR_CONTRACT_NOT_CONFIRMED",
        f"早释放错误码错误: {early}",
    )

    status, locked = request_json(
        "POST",
        f"{API_ROOT}/dispatch",
        {
            # 必须使用同一个 projectId，才能验证项目级门控锁。
            **both_payload,
            "message": "把按钮改成绿色",
            "annotations": [{**ANNOTATION, "annotationId": "ann_blocked"}],
        },
    )
    check(status == 409, f"门控期间新派发应返回 409，实际 {status}: {locked}")
    locked_detail = locked.get("detail") if isinstance(locked, dict) else None
    locked_code = locked_detail.get("code") if isinstance(locked_detail, dict) else None
    check(
        locked_code == "SUPERVISOR_PROJECT_GATE_ACTIVE",
        f"项目锁错误码错误: {locked}",
    )
    print("   CLOSED: 早释放 / 项目并发派发均被拒绝")

    print("\n== 9. 取消 Architect 自动中止门控 ==")
    status, cancelled = request_json(
        "POST",
        f"{ARCHITECT_API_ROOT}/runs/{architect_run_id}/cancel",
        {"reason": "supervisor both gate e2e cleanup"},
    )
    check(status == 200, f"取消 Architect 失败: {status}: {cancelled}")
    status, aborted = request_json("POST", f"{API_ROOT}/gates/{gate_id}/release", {})
    check(
        status == 409,
        f"已取消 Architect 的 release 应返回 409，实际 {status}: {aborted}",
    )
    aborted_detail = aborted.get("detail") if isinstance(aborted, dict) else None
    aborted_code = (
        aborted_detail.get("code") if isinstance(aborted_detail, dict) else None
    )
    check(
        aborted_code == "SUPERVISOR_GATE_ABORTED",
        f"门控中止错误码错误: {aborted}",
    )
    status, gate_view = request_json("GET", f"{API_ROOT}/gates/{gate_id}")
    check(status == 200, f"查询门控失败: {status}: {gate_view}")
    check(
        (gate_view.get("gate") or {}).get("status") == "FAILED",
        f"Architect 取消后门控应为 FAILED: {gate_view}",
    )
    print("   FAILED: Architect 取消 -> 门控中止 -> 项目锁释放")

    print("\n== 10. dispatch 真实 Architect 子图 ==")
    dispatch_payload = {
        **base_payload("dispatch_architect"),
        "message": "请做全局全栈架构设计和数据库规划",
    }
    status, body = request_json("POST", f"{API_ROOT}/dispatch", dispatch_payload)
    check(status == 202, f"dispatch 应返回 202，实际 {status}: {body}")
    dispatches = body.get("dispatches") or []
    check(len(dispatches) == 1, f"应只有 1 个子 Run，实际 {dispatches}")
    item = dispatches[0]
    check(item.get("agentId") == "architect_graph", "应派发 Architect")
    run_id = item.get("runId")
    stream_path = item.get("streamPath")
    check(bool(run_id) and bool(stream_path), f"dispatch 缺少 Run ID / 流地址: {item}")
    print(f"   runId={run_id} streamPath={stream_path}")

    terminal = read_until_sse(
        internal_stream_path(stream_path),
        {"architect.clarification.required", "architect.failed"},
    )
    check(
        terminal.get("type") == "architect.clarification.required",
        f"Architect 首事件应为 clarification.required，实际 {terminal.get('type')}",
    )
    print(f"   SSE 首事件={terminal.get('type')}")

    print("\n== 11. 清理 Architect Run ==")
    status, body = request_json(
        "POST",
        f"{ARCHITECT_API_ROOT}/runs/{run_id}/cancel",
        {"reason": "supervisor e2e cleanup"},
    )
    check(status == 200, f"取消失败: {status}: {body}")
    print("   CANCELLED")

    print("\nSUPERVISOR E2E: PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except E2EFailure as exc:
        print(f"\nSUPERVISOR E2E: FAIL - {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
