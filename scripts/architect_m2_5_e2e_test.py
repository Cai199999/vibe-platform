#!/usr/bin/env python3
"""M2.5 全局架构澄清 / 分级契约 API E2E。

不需要 DeepSeek Key、MySQL 或沙盒：请求显式使用 mock provider，源码走
``source.files`` 回退。默认直连 Python AI Core，也可以指向 Java BFF。

前置：
    docker compose up -d
    python3 scripts/architect_m2_5_e2e_test.py
    # 或验证 BFF 透传：
    AI_BASE_URL=http://127.0.0.1:8080 python3 scripts/architect_m2_5_e2e_test.py
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
    "ARCHITECT_API_ROOT",
    "/api/v1/architect" if ":8080" in BASE_URL else "/api/v1/internal/architect",
).rstrip("/")
STREAM_TIMEOUT = float(os.environ.get("ARCHITECT_E2E_TIMEOUT", "120"))

SOURCE_APP = """export default function App() {
  return (
    <main>
      <h1>架构澄清 E2E</h1>
      <button>添加待办</button>
    </main>
  )
}
"""

SLOT_VALUES = {
    "ui.primary_flow": "输入待办 -> 勾选完成 -> 保存到 localStorage",
    "script.input": "一个 CSV 文件路径",
    "script.output": "按日期聚合的 JSON 文件",
    "script.logic": "解析 CSV、去重并按日期聚合，异常返回退出码 1",
    "business.key_flows": "点击提交后创建订单并扣减库存",
    "data.entities": "Order(id, userId, total, status)；User(id, name)",
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


def read_sse(
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    after_seq: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """读取一条 SSE，直到暂停 / 终态事件后由服务端关闭连接。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    query = f"?afterSeq={after_seq}" if after_seq > 0 else ""
    request = urllib.request.Request(
        f"{BASE_URL}{path}{query}",
        data=data,
        method="POST" if payload is not None else "GET",
        headers={
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Last-Event-ID": str(after_seq),
        },
    )
    events: list[dict[str, Any]] = []
    data_lines: list[str] = []
    event_name = "message"
    deadline = time.monotonic() + STREAM_TIMEOUT

    def flush() -> dict[str, Any] | None:
        nonlocal data_lines, event_name
        if not data_lines:
            event_name = "message"
            return None
        raw = "\n".join(data_lines)
        data_lines = []
        event_name = "message"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        parsed.setdefault("type", event_name)
        events.append(parsed)
        return parsed

    with urllib.request.urlopen(request, timeout=STREAM_TIMEOUT) as response:
        check(response.status == 200, f"SSE HTTP {response.status}")
        for raw_line in response:
            if time.monotonic() > deadline:
                raise E2EFailure("SSE 读取超时")
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                event = flush()
                if event and event.get("type") in {
                    "architect.clarification.required",
                    "architect.contract.ready",
                    "architect.contract.confirmed",
                    "architect.cancelled",
                    "architect.failed",
                }:
                    return events, event
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "data":
                data_lines.append(value)
            elif field == "event":
                event_name = value
        event = flush()
        if event:
            return events, event
    raise E2EFailure("SSE 在终态事件前关闭")


def event_types(events: list[dict[str, Any]]) -> list[str]:
    return [str(event.get("type")) for event in events]


def start_run(
    shape_label: str,
) -> tuple[str, str, dict[str, Any], list[dict[str, Any]]]:
    run_id = f"e2e_arch_{uuid.uuid4().hex[:12]}"
    payload = {
        "runId": run_id,
        "tenantId": "tenant_e2e",
        "projectId": f"project_{run_id}",
        "projectName": f"M2.5 E2E - {shape_label}",
        "source": {"files": {"src/App.jsx": SOURCE_APP}},
        "uiHistory": [{"role": "user", "content": "先做一个能运行的界面"}],
        "options": {
            "llmProvider": "mock",
            "confidenceThreshold": 0.85,
            "maxClarificationRounds": 5,
            "ragProvider": "markdown",
            "ragTopK": 3,
        },
    }
    events, terminal = read_sse(f"{API_ROOT}/runs/stream", payload)
    check(
        terminal.get("type") == "architect.clarification.required",
        f"首轮应暂停在 clarification.required，实际 {terminal.get('type')}",
    )
    clarification = terminal["payload"]
    question = clarification["questions"][0]
    check(question["slot"] == "app.shape", "首轮必须先询问 app.shape")
    expected_prompt = (
        "您是希望做一个只有网页特效的轻量工具，还是需要能保存数据的全栈系统，"
        "或者是一个后台自动运行的脚本？"
    )
    check(
        question["prompt"] == expected_prompt,
        f"应用形态问题文案不符合产品约定: {question['prompt']!r}",
    )
    check(clarification["confidenceScore"] <= 0.42, "未知形态置信度必须封顶")
    return run_id, clarification["clarificationId"], terminal, events


def answer_questions(
    run_id: str,
    clarification: dict[str, Any],
    shape: str,
) -> None:
    answers = []
    for question in clarification["questions"]:
        slot = question["slot"]
        if slot == "app.shape":
            value = shape
        elif question.get("options"):
            value = question["options"][0]["value"]
        else:
            value = SLOT_VALUES.get(slot, f"{slot} 的 E2E 取值")
        answers.append({"questionId": question["questionId"], "value": value})
    status, body = request_json(
        "POST",
        f"{API_ROOT}/runs/{run_id}/clarifications/"
        f"{clarification['clarificationId']}/answers",
        {"answers": answers},
    )
    check(status == 202, f"提交澄清应返回 202，实际 {status}: {body}")


def drive_to_contract(
    shape: str,
    label: str,
) -> tuple[str, dict[str, Any], int, list[dict[str, Any]]]:
    run_id, _, terminal, events = start_run(label)
    last_seq = int(terminal["seq"])
    all_events = list(events)
    for _ in range(8):
        if terminal["type"] == "architect.contract.ready":
            return (
                run_id,
                terminal["payload"]["contract"],
                int(terminal["seq"]),
                all_events,
            )
        check(
            terminal["type"] == "architect.clarification.required",
            f"预期澄清或契约，实际 {terminal['type']}: {terminal.get('payload')}",
        )
        clarification = terminal["payload"]
        answer_questions(run_id, clarification, shape)
        replay, terminal = read_sse(
            f"{API_ROOT}/runs/{run_id}/events/stream",
            after_seq=last_seq,
        )
        all_events.extend(replay)
        last_seq = max(
            [last_seq] + [int(event["seq"]) for event in replay + [terminal]]
        )
    raise E2EFailure(f"{label}: 超过 8 轮仍未生成契约")


def decide(
    run_id: str,
    contract: dict[str, Any],
    decision: str,
    comment: str = "",
    *,
    after_seq: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    status, body = request_json(
        "POST",
        f"{API_ROOT}/runs/{run_id}/contracts/" f"{contract['contractId']}/decision",
        {
            "contractId": contract["contractId"],
            "decision": decision,
            "comment": comment or None,
        },
    )
    check(status == 202, f"{decision} 应返回 202，实际 {status}: {body}")
    # 从契约就绪事件之后回放，避免把旧暂停事件误当成决策结果。
    events, terminal = read_sse(
        f"{API_ROOT}/runs/{run_id}/events/stream",
        after_seq=after_seq,
    )
    check(
        terminal["type"]
        in {
            "architect.contract.confirmed",
            "architect.cancelled",
            "architect.contract.ready",
        },
        f"决策后事件异常: {terminal['type']}",
    )
    return terminal, events


def assert_lightweight(contract: dict[str, Any]) -> None:
    check(contract["appShape"] == "lightweight_web", "形态应为 lightweight_web")
    check(not contract["apiRoutes"], "轻量工具不允许 REST API")
    check(not contract["databaseDdl"], "轻量工具不允许数据库 DDL")
    check(not contract["scriptSpec"], "轻量工具不允许脚本规格")
    check(
        bool(contract["uiLogic"])
        or bool(contract["frontendDataModel"])
        or bool(contract["componentInteractions"]),
        "轻量工具必须有 UI 逻辑或前端数据模型",
    )
    check(contract["confidenceScore"] >= 0.85, "轻量契约置信度应达标")
    check(contract["citations"], "契约应带知识库引用")


def assert_script(contract: dict[str, Any]) -> None:
    check(contract["appShape"] == "script_automation", "形态应为 script_automation")
    check(not contract["apiRoutes"], "脚本不允许 REST API")
    check(not contract["databaseDdl"], "脚本不允许数据库 DDL")
    check(not contract["uiLogic"], "脚本不追问 UI")
    for key in ("runtime", "entry", "dependencies", "input", "output", "exitCodes"):
        check(key in contract["scriptSpec"], f"脚本契约缺少 {key}")


def assert_full_stack(contract: dict[str, Any]) -> None:
    check(contract["appShape"] == "full_stack_system", "形态应为 full_stack_system")
    check(contract["apiRoutes"], "全栈契约必须有 API")
    check(
        all(route["path"].startswith("/api/") for route in contract["apiRoutes"]),
        "API 路径必须以 /api/ 开头",
    )
    check("CREATE TABLE" in contract["databaseDdl"], "全栈契约必须有 CREATE TABLE")
    check(
        "FOREIGN KEY" not in contract["databaseDdl"].upper(),
        "DDL 禁止物理外键",
    )
    check(contract["componentInteractions"], "全栈契约必须有组件交互")
    check(contract["techStack"].get("backend"), "技术栈缺少 backend")
    check(contract["techStack"].get("database"), "技术栈缺少 database")


def main() -> int:
    started = time.time()
    print(f"BASE_URL={BASE_URL}")

    print("\n== 1. 轻量网页：只问 UI 流程 -> 契约 -> CONFIRM ==")
    run_id, contract, contract_seq, events = drive_to_contract(
        "lightweight_web", "轻量网页"
    )
    assert_lightweight(contract)
    check("architect.rag.retrieved" in event_types(events), "应发生 RAG 检索")
    terminal, _ = decide(run_id, contract, "CONFIRM", after_seq=contract_seq)
    check(
        terminal["type"] == "architect.contract.confirmed",
        f"CONFIRM 未生效: {terminal['type']}",
    )
    check(
        terminal["payload"]["contract"]["status"] == "CONFIRMED",
        "确认后的契约状态错误",
    )
    print("   OK：UI 流程 -> 契约 -> CONFIRMED")

    print("\n== 2. 自动化脚本：不问 UI / 后端 / 数据库 -> CANCEL ==")
    run_id, contract, contract_seq, _ = drive_to_contract(
        "script_automation", "脚本自动化"
    )
    assert_script(contract)
    terminal, _ = decide(
        run_id,
        contract,
        "CANCEL",
        "本次不生成脚本",
        after_seq=contract_seq,
    )
    check(terminal["type"] == "architect.cancelled", "CANCEL 未生效")
    print("   OK：脚本规格 -> CANCELLED")

    print("\n== 3. 全栈系统：REST + DDL + 交互 -> REVISE -> CONFIRM ==")
    run_id, contract, contract_seq, _ = drive_to_contract(
        "full_stack_system", "全栈系统"
    )
    assert_full_stack(contract)
    first_id = contract["contractId"]
    terminal, _ = decide(
        run_id,
        contract,
        "REVISE",
        "增加订单导出接口",
        after_seq=contract_seq,
    )
    check(
        terminal["type"] == "architect.contract.ready",
        f"REVISE 后应重新等待确认，实际 {terminal['type']}",
    )
    revised = terminal["payload"]["contract"]
    check(revised["contractId"] == first_id, "修订应沿用同一 contractId")
    check("修订记录" in revised["contractMarkdown"], "修订版应包含修订记录")
    terminal, _ = decide(
        run_id,
        revised,
        "CONFIRM",
        after_seq=int(terminal["seq"]),
    )
    check(terminal["type"] == "architect.contract.confirmed", "修订后 CONFIRM 失败")
    print("   OK：全栈契约 -> REVISE -> CONFIRMED")

    print("\n== 4. 取消暂停中的 Architect Run ==")
    run_id, clarification_id, terminal, _ = start_run("取消测试")
    status, body = request_json(
        "POST",
        f"{API_ROOT}/runs/{run_id}/cancel",
        {"reason": "E2E 取消"},
    )
    check(status == 200, f"取消应返回 200，实际 {status}: {body}")
    check(body["status"] == "CANCELLED", "暂停中的 Run 应立即取消")
    events, terminal = read_sse(
        f"{API_ROOT}/runs/{run_id}/events/stream",
        after_seq=int(terminal["seq"]),
    )
    check(terminal["type"] == "architect.cancelled", "缺少 cancelled 事件")
    print("   OK：暂停中的 Run 可取消")

    elapsed = time.time() - started
    print(f"\nALL PASSED in {elapsed:.2f}s")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except E2EFailure as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
