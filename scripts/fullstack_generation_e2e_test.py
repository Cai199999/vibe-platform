#!/usr/bin/env python3
"""契约确认 -> 全栈代码生成 -> 沙盒联调 E2E。

覆盖链路：

    Architect（mock）全栈契约 -> CONFIRM
      -> POST /api/v1/runs/fullstack
      -> DBACoder / BackendCoder / FrontendCoder 事件
      -> BuildVerifier + SandboxRestart
      -> fullstack.completed
      -> 预览地址 GET /api/health、CRUD 真实接口验证

默认走 Java BFF（验证跨语言透传）；AI_BASE_URL 可直连 Python AI Core。

前置：
    docker compose up -d
    python3 scripts/fullstack_generation_e2e_test.py
    AI_BASE_URL=http://127.0.0.1:8100 python3 scripts/fullstack_generation_e2e_test.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

BASE_URL = os.environ.get("AI_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
VIA_BFF = ":8080" in BASE_URL
ARCHITECT_ROOT = "/api/v1/architect" if VIA_BFF else "/api/v1/internal/architect"
FULLSTACK_PATH = (
    "/api/v1/runs/fullstack" if VIA_BFF else "/api/v1/internal/fullstack/runs/stream"
)
STREAM_TIMEOUT = float(os.environ.get("FULLSTACK_E2E_TIMEOUT", "300"))

SOURCE_APP = """export default function App() {
  return (
    <main>
      <h1>全栈生成 E2E</h1>
      <button>添加待办</button>
    </main>
  )
}
"""

SLOT_VALUES = {
    "ui.primary_flow": "输入待办 -> 勾选完成 -> 保存",
    "business.key_flows": "点击提交后创建待办并刷新列表",
    "data.entities": "Todo(id, tenantId, name, status)",
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
    timeout: float = 60,
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
    timeout: float = STREAM_TIMEOUT,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """读取一条 SSE，直到服务端写入终态后关闭连接。"""
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
    deadline = time.monotonic() + timeout
    terminal_types = {
        "architect.clarification.required",
        "architect.contract.ready",
        "architect.contract.confirmed",
        "architect.cancelled",
        "architect.failed",
        "fullstack.completed",
        "fullstack.failed",
    }

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

    with urllib.request.urlopen(request, timeout=timeout) as response:
        check(response.status == 200, f"SSE HTTP {response.status}")
        for raw_line in response:
            if time.monotonic() > deadline:
                raise E2EFailure("SSE 读取超时")
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                event = flush()
                if event and event.get("type") in terminal_types:
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


def start_architect() -> tuple[str, dict[str, Any], int, list[dict[str, Any]]]:
    run_id = f"e2e_fs_{uuid.uuid4().hex[:12]}"
    payload = {
        "runId": run_id,
        "tenantId": "tenant_e2e",
        "projectId": f"project_{run_id}",
        "projectName": "Fullstack E2E",
        "source": {"files": {"src/App.jsx": SOURCE_APP}},
        "uiHistory": [{"role": "user", "content": "先做一个能运行的待办界面"}],
        "options": {
            "llmProvider": "mock",
            "confidenceThreshold": 0.85,
            "maxClarificationRounds": 5,
            "ragProvider": "markdown",
            "ragTopK": 3,
        },
    }
    events, terminal = read_sse(f"{ARCHITECT_ROOT}/runs/stream", payload)
    check(
        terminal.get("type") == "architect.clarification.required",
        f"首轮应要求澄清，实际 {terminal.get('type')}",
    )
    return run_id, terminal, int(terminal["seq"]), events


def answer_clarification(run_id: str, clarification: dict[str, Any]) -> None:
    answers = []
    for question in clarification["questions"]:
        slot = question.get("slot")
        if slot == "app.shape":
            value = "full_stack_system"
        elif question.get("options"):
            value = question["options"][0]["value"]
        else:
            value = SLOT_VALUES.get(slot, f"{slot} 的 E2E 取值")
        answers.append({"questionId": question["questionId"], "value": value})
    status, body = request_json(
        "POST",
        f"{ARCHITECT_ROOT}/runs/{run_id}/clarifications/"
        f"{clarification['clarificationId']}/answers",
        {"answers": answers},
    )
    check(status == 202, f"提交澄清应返回 202，实际 {status}: {body}")


def drive_to_fullstack_contract() -> (
    tuple[str, dict[str, Any], int, list[dict[str, Any]]]
):
    run_id, terminal, last_seq, events = start_architect()
    all_events = list(events)
    for _ in range(10):
        if terminal["type"] == "architect.contract.ready":
            contract = terminal["payload"]["contract"]
            check(
                contract.get("appShape") == "full_stack_system",
                f"契约形态错误: {contract.get('appShape')}",
            )
            check(bool(contract.get("apiRoutes")), "全栈契约缺少 apiRoutes")
            check("CREATE TABLE" in contract.get("databaseDdl", ""), "缺少 DDL")
            check(
                contract.get("databaseEngine") in {"mysql", "postgresql", "sqlite"},
                f"契约缺少 databaseEngine: {contract.get('databaseEngine')}",
            )
            return run_id, contract, last_seq, all_events
        check(
            terminal["type"] == "architect.clarification.required",
            f"预期澄清或契约，实际 {terminal['type']}: {terminal.get('payload')}",
        )
        answer_clarification(run_id, terminal["payload"])
        replay, terminal = read_sse(
            f"{ARCHITECT_ROOT}/runs/{run_id}/events/stream",
            after_seq=last_seq,
        )
        all_events.extend(replay)
        last_seq = max(
            [last_seq] + [int(event["seq"]) for event in replay + [terminal]]
        )
    raise E2EFailure("超过 10 轮仍未生成全栈契约")


def confirm_contract(
    run_id: str,
    contract: dict[str, Any],
    after_seq: int,
) -> dict[str, Any]:
    status, body = request_json(
        "POST",
        f"{ARCHITECT_ROOT}/runs/{run_id}/contracts/"
        f"{contract['contractId']}/decision",
        {
            "contractId": contract["contractId"],
            "decision": "CONFIRM",
            "comment": "E2E 确认，进入全栈生成",
        },
    )
    check(status == 202, f"CONFIRM 应返回 202，实际 {status}: {body}")
    _, terminal = read_sse(
        f"{ARCHITECT_ROOT}/runs/{run_id}/events/stream",
        after_seq=after_seq,
    )
    check(
        terminal["type"] == "architect.contract.confirmed",
        f"CONFIRM 未生效: {terminal['type']}",
    )
    confirmed = terminal["payload"]["contract"]
    check(confirmed["status"] == "CONFIRMED", "确认后的契约状态错误")
    return confirmed


def start_fullstack(
    run_id: str,
    contract: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = {
        "runId": run_id,
        "tenantId": "tenant_e2e",
        "projectId": f"project_{run_id}",
        "contractId": contract["contractId"],
        "contract": contract,
        "source": {"files": {"src/App.jsx": SOURCE_APP}},
        "options": {"verifyBuild": True, "restartSandbox": True},
    }
    return read_sse(FULLSTACK_PATH, payload, timeout=STREAM_TIMEOUT)


def http_get_json(url: str, *, timeout: float = 20) -> tuple[int, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
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


def http_post_json(url: str, payload: dict[str, Any]) -> tuple[int, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Idempotency-Key": f"e2e-{uuid.uuid4().hex[:10]}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body


def wait_preview(preview_url: str, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            status, _ = http_get_json(f"{preview_url}/api/health", timeout=5)
            if status == 200:
                return
            last_error = f"HTTP {status}"
        except Exception as exc:  # noqa: BLE001 - 轮询阶段记录最后错误
            last_error = str(exc)
        time.sleep(1)
    raise E2EFailure(f"预览 /api/health 未就绪: {last_error}")


def verify_crud(preview_url: str, collection_path: str) -> None:
    status, health = http_get_json(f"{preview_url}/api/health")
    check(status == 200 and health.get("status") == "ok", f"健康检查失败: {health}")
    # 全栈后端必须是 Express + SQLite，而不是内存仓储。
    check(
        health.get("storage") == "sqlite",
        f"后端未使用 SQLite: {health}",
    )
    check(
        health.get("runtime") == "express-sqlite",
        f"后端运行时不是 Express: {health}",
    )

    collection_url = f"{preview_url}{collection_path}"
    status, listing = http_get_json(collection_url)
    check(status == 200, f"GET {collection_path} -> HTTP {status}")
    items = listing.get("items") if isinstance(listing, dict) else listing
    check(isinstance(items, list), f"列表响应格式错误: {listing}")

    name = f"E2E 待办 {uuid.uuid4().hex[:6]}"
    status, created = http_post_json(
        collection_url,
        {"name": name, "title": name, "status": "todo"},
    )
    check(status == 201, f"POST {collection_path} -> HTTP {status}: {created}")
    check(created.get("name") == name, f"创建响应错误: {created}")

    status, listing = http_get_json(collection_url)
    check(status == 200, f"再次 GET {collection_path} -> HTTP {status}")
    items = listing.get("items") if isinstance(listing, dict) else listing
    check(
        any(item.get("name") == name for item in items),
        "POST 后列表未包含新建记录",
    )

    # 通用别名 /api/data 必须指向同一张 SQLite 表。
    status, alias_listing = http_get_json(f"{preview_url}/api/data")
    alias_items = (
        alias_listing.get("items") if isinstance(alias_listing, dict) else alias_listing
    )
    check(status == 200, f"GET /api/data -> HTTP {status}")
    check(
        any(item.get("name") == name for item in (alias_items or [])),
        "/api/data 未查到刚写入的记录",
    )


def cleanup(sandbox_id: str) -> None:
    if not sandbox_id or not VIA_BFF:
        return
    request = urllib.request.Request(
        f"{BASE_URL}/api/v1/sandboxes/{urllib.parse.quote(sandbox_id)}",
        method="DELETE",
    )
    try:
        urllib.request.urlopen(request, timeout=20).close()
    except Exception:  # noqa: BLE001 - 清理失败不影响 E2E 结论
        print(f"WARN: 清理沙盒失败 {sandbox_id}")


def main() -> int:
    started = time.time()
    print(f"BASE_URL={BASE_URL} via_bff={VIA_BFF}")

    print("\n== 1. Architect 澄清 -> 全栈契约 -> CONFIRM ==")
    run_id, contract, contract_seq, _ = drive_to_fullstack_contract()
    confirmed = confirm_contract(run_id, contract, contract_seq)
    print(f"   OK：{confirmed['contractId']} 已确认")

    sandbox_id = ""
    try:
        print("\n== 2. 全栈生成 SSE：DBACoder -> BackendCoder -> FrontendCoder ==")
        events, terminal = start_fullstack(f"fsk_{run_id}", confirmed)
        types = [event.get("type") for event in events]
        check(
            terminal.get("type") == "fullstack.completed",
            f"全栈生成失败: {terminal.get('type')} -> {terminal.get('payload')}",
        )
        started_nodes = [
            event["payload"]["node"]
            for event in events
            if event.get("type") == "fullstack.node.started"
        ]
        check(
            started_nodes[:4]
            == ["DBACoder", "BackendCoder", "FrontendCoder", "InfraCoder"],
            f"节点顺序错误: {started_nodes}",
        )
        generated_paths = {
            event["payload"]["path"]
            for event in events
            if event.get("type") == "fullstack.node.output"
        }
        check("server/schema.sql" in generated_paths, "缺少 schema.sql 生成日志")
        check("server/index.js" in generated_paths, "缺少后端代码生成日志")
        check("server/db.js" in generated_paths, "缺少 Sequelize ORM 生成日志")
        check("src/fullstack-api.jsx" in generated_paths, "缺少前端接入生成日志")
        check("Dockerfile" in generated_paths, "缺少 Dockerfile 生成日志")
        check("render.yaml" in generated_paths, "缺少 render.yaml 生成日志")
        check(".env.example" in generated_paths, "缺少 .env.example 生成日志")
        check("fullstack.sandbox.ready" in types, "缺少沙盒就绪事件")
        payload = terminal["payload"]
        check(payload["status"] == "SUCCEEDED", "终态不是 SUCCEEDED")
        check(
            payload.get("databaseEngine") in {"mysql", "postgresql", "sqlite"},
            f"终态缺少 databaseEngine: {payload.get('databaseEngine')}",
        )
        check(payload.get("orm") == "sequelize", "终态未声明 Sequelize ORM")
        for path in ("Dockerfile", "render.yaml", ".env.example", "DEPLOY.md"):
            check(
                path in (payload.get("infraFiles") or []),
                f"infraFiles 缺少 {path}: {payload.get('infraFiles')}",
            )
        check(
            payload["previewUrl"].startswith("http"),
            f"previewUrl 非法: {payload['previewUrl']}",
        )
        sandbox_id = payload["sandboxId"]
        print(f"   OK：{len(generated_paths)} 个文件，sandbox={sandbox_id}")

        print("\n== 3. 预览联调：Vite /api 代理 -> Node 后端 CRUD ==")
        preview_url = payload["previewUrl"].rstrip("/")
        wait_preview(preview_url)
        verify_crud(preview_url, payload["collectionPath"])
        print(f"   OK：{preview_url}{payload['collectionPath']} GET/POST 正常")

        elapsed = time.time() - started
        print(f"\nALL PASSED in {elapsed:.2f}s")
        return 0
    finally:
        cleanup(sandbox_id)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except E2EFailure as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
