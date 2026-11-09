#!/usr/bin/env python3
"""全栈生成体检：前端表单 + Express API + SQLite 持久化。

本脚本回答一个问题：平台是否真的能在沙盒里生成并运行一个
“前端 + 后端 API + 数据持久化”的全栈应用？

流程：
  1. 模拟用户请求：“生成一个带表单的页面，点击提交后，数据存入数据库，
     并返回成功提示”；
  2. Mock 模型走完 Architect 澄清 -> 契约 CONFIRM；
  3. 触发 FullstackGraph，收集 DBACoder / BackendCoder / FrontendCoder 事件；
  4. 逐项检查沙盒：server/ 目录、DDL、后端进程、Vite 代理；
  5. 浏览器自动化（Playwright）提交表单，断言成功提示与 API 数据；
  6. 重启沙盒，验证数据仍在（证明真正落库，而不是内存 Map）。

运行：
    docker compose up -d
    /tmp/vibe-browser/bin/python scripts/fullstack_e2e_check.py
    # 无 Playwright 时可只跑 API/容器检查：
    python3 scripts/fullstack_e2e_check.py --no-browser
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

# 复用全栈 E2E 的 SSE / HTTP 基础工具，避免两套解析逻辑漂移。
import fullstack_generation_e2e_test as base  # noqa: E402

USER_PROMPT = "生成一个带表单的页面，点击提交后，数据存入数据库，并返回成功提示"

SOURCE_FILES = {
    "src/App.jsx": (
        "export default function App() {\n"
        "  return (\n"
        "    <main>\n"
        "      <h1>全栈体检</h1>\n"
        '      <form><input placeholder="名称" /><button>提交</button></form>\n'
        "    </main>\n"
        "  )\n"
        "}\n"
    )
}

SLOT_VALUES = {
    "ui.primary_flow": "输入名称 -> 点击提交 -> 显示成功提示",
    "business.key_flows": "提交表单后写入数据库并刷新列表",
    "data.entities": "Todo(id, tenantId, name, status, createdAt)",
}


@dataclass
class CheckResult:
    ok: bool
    title: str
    detail: str = ""
    hint: str = ""


class Reporter:
    def __init__(self) -> None:
        self.results: list[CheckResult] = []

    def add(self, result: CheckResult) -> None:
        self.results.append(result)
        icon = "PASS" if result.ok else "FAIL"
        print(f"  [{icon}] {result.title}")
        if result.detail:
            for line in result.detail.splitlines():
                print(f"         {line}")
        if not result.ok and result.hint:
            print(f"         卡点：{result.hint}")

    def check(self, title: str, fn: Callable[[], tuple[bool, str, str]]) -> bool:
        try:
            ok, detail, hint = fn()
        except Exception as exc:  # noqa: BLE001 - 体检脚本必须继续暴露后续问题
            ok, detail, hint = False, f"{type(exc).__name__}: {exc}", "执行检查时异常"
        self.add(CheckResult(ok, title, detail, hint))
        return ok

    @property
    def failed(self) -> list[CheckResult]:
        return [result for result in self.results if not result.ok]


# ------------------------------------------------------------
# Architect / Fullstack 驱动
# ------------------------------------------------------------
def start_architect() -> tuple[str, dict[str, Any], int]:
    run_id = f"chk_fs_{uuid.uuid4().hex[:12]}"
    payload = {
        "runId": run_id,
        "tenantId": "tenant_e2e",
        "projectId": f"project_{run_id}",
        "projectName": "Fullstack E2E Check",
        "source": {"files": SOURCE_FILES},
        "uiHistory": [{"role": "user", "content": USER_PROMPT}],
        "options": {
            "llmProvider": "mock",
            "confidenceThreshold": 0.85,
            "maxClarificationRounds": 5,
            "ragProvider": "markdown",
            "ragTopK": 3,
        },
    }
    _, terminal = base.read_sse(f"{base.ARCHITECT_ROOT}/runs/stream", payload)
    if terminal.get("type") != "architect.clarification.required":
        raise AssertionError(
            f"首轮应要求澄清，实际 {terminal.get('type')}: {terminal.get('payload')}"
        )
    return run_id, terminal, int(terminal["seq"])


def answer_clarification(run_id: str, clarification: dict[str, Any]) -> None:
    answers = []
    for question in clarification["questions"]:
        slot = question.get("slot")
        if slot == "app.shape":
            value = "full_stack_system"
        elif question.get("options"):
            value = question["options"][0]["value"]
        else:
            value = SLOT_VALUES.get(slot, f"{slot} 的体检取值")
        answers.append({"questionId": question["questionId"], "value": value})
    status, body = base.request_json(
        "POST",
        f"{base.ARCHITECT_ROOT}/runs/{run_id}/clarifications/"
        f"{clarification['clarificationId']}/answers",
        {"answers": answers},
    )
    if status != 202:
        raise AssertionError(f"提交澄清应返回 202，实际 {status}: {body}")


def drive_to_contract() -> tuple[str, dict[str, Any], int]:
    run_id, terminal, last_seq = start_architect()
    for _ in range(10):
        if terminal.get("type") == "architect.contract.ready":
            contract = terminal["payload"]["contract"]
            if contract.get("appShape") != "full_stack_system":
                raise AssertionError(f"契约形态不是全栈: {contract.get('appShape')}")
            if "CREATE TABLE" not in contract.get("databaseDdl", ""):
                raise AssertionError("契约缺少 databaseDdl")
            return run_id, contract, last_seq
        if terminal.get("type") != "architect.clarification.required":
            raise AssertionError(f"预期澄清或契约，实际 {terminal.get('type')}")
        answer_clarification(run_id, terminal["payload"])
        replay, terminal = base.read_sse(
            f"{base.ARCHITECT_ROOT}/runs/{run_id}/events/stream",
            after_seq=last_seq,
        )
        last_seq = max(
            [last_seq] + [int(event["seq"]) for event in replay + [terminal]]
        )
    raise AssertionError("超过 10 轮仍未生成全栈契约")


def confirm_contract(
    run_id: str, contract: dict[str, Any], after_seq: int
) -> dict[str, Any]:
    status, body = base.request_json(
        "POST",
        f"{base.ARCHITECT_ROOT}/runs/{run_id}/contracts/"
        f"{contract['contractId']}/decision",
        {
            "contractId": contract["contractId"],
            "decision": "CONFIRM",
            "comment": "全栈体检确认",
        },
    )
    if status != 202:
        raise AssertionError(f"CONFIRM 应返回 202，实际 {status}: {body}")
    _, terminal = base.read_sse(
        f"{base.ARCHITECT_ROOT}/runs/{run_id}/events/stream", after_seq=after_seq
    )
    if terminal.get("type") != "architect.contract.confirmed":
        raise AssertionError(f"CONFIRM 未生效: {terminal.get('type')}")
    return terminal["payload"]["contract"]


def start_fullstack(run_id: str, contract: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "runId": f"fsk_{run_id}",
        "tenantId": "tenant_e2e",
        "projectId": f"project_{run_id}",
        "contractId": contract["contractId"],
        "contract": contract,
        "source": {"files": SOURCE_FILES},
        "options": {"verifyBuild": True, "restartSandbox": True},
    }
    _, terminal = base.read_sse(base.FULLSTACK_PATH, payload, timeout=300)
    if terminal.get("type") != "fullstack.completed":
        raise AssertionError(
            f"全栈生成失败: {terminal.get('type')} -> {terminal.get('payload')}"
        )
    return terminal["payload"]


# ------------------------------------------------------------
# 沙盒容器检查
# ------------------------------------------------------------
def docker_exec(container: str, script: str, timeout: float = 20) -> tuple[int, str]:
    completed = subprocess.run(
        ["docker", "exec", container, "sh", "-lc", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode, output.strip()


def docker_restart(container: str, timeout: float = 60) -> None:
    subprocess.run(
        ["docker", "restart", container],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def wait_preview_ready(preview_url: str, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            status, body = base.http_get_json(f"{preview_url}/api/health", timeout=5)
            if status == 200 and isinstance(body, dict) and body.get("status") == "ok":
                return
            last = f"HTTP {status}: {body}"
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
        time.sleep(1)
    raise AssertionError(f"预览 /api/health 未就绪: {last}")


def check_server_entry(container: str) -> tuple[bool, str, str]:
    code, output = docker_exec(container, "ls -1 server 2>/dev/null")
    entry_ok = any(name in output for name in ("index.js", "index.mjs"))
    return (
        code == 0 and entry_ok,
        output or "server/ 目录不存在",
        "BackendCoder 未写入 server/ 目录",
    )


def read_sandbox_file(container: str, *paths: str) -> str | None:
    for path in paths:
        code, output = docker_exec(container, f"cat {path} 2>/dev/null")
        if code == 0 and output:
            return output
    return None


def backend_listening(container: str) -> tuple[bool, str]:
    """在容器内请求候选端口；返回命中的端口与健康响应。"""
    script = (
        "for p in 3000 3100; do "
        "node -e \"fetch('http://127.0.0.1:'+process.argv[1]+'/api/health')"
        ".then(r=>r.text()).then(t=>{console.log(process.argv[1]+' '+t);process.exit(0)})"
        '.catch(()=>process.exit(1))" $p && break; done'
    )
    code, output = docker_exec(container, script, timeout=25)
    return code == 0 and bool(output.strip()), output.strip()


# ------------------------------------------------------------
# 浏览器检查
# ------------------------------------------------------------
def browser_submit(preview_url: str, name: str) -> tuple[bool, str]:
    try:
        from playwright.sync_api import expect, sync_playwright
    except ImportError as exc:
        return (
            False,
            f"Playwright 不可用: {exc}（请用 /tmp/vibe-browser/bin/python 运行）",
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        try:
            page.goto(preview_url, wait_until="networkidle", timeout=60_000)
            panel = page.locator("[data-fullstack-panel]")
            expect(panel).to_be_visible(timeout=30_000)
            expect(panel).to_have_attribute("data-fullstack-status", "ready")
            panel.locator("[data-fullstack-input]").fill(name)
            panel.locator("[data-fullstack-submit]").click()
            success = panel.locator("[data-fullstack-success]")
            expect(success).to_be_visible(timeout=20_000)
            text = success.inner_text()
            if "成功" not in text:
                return False, f"成功提示文案异常: {text!r}"
            row = panel.locator("[data-fullstack-row]").filter(has_text=name)
            expect(row).to_be_visible(timeout=20_000)
            page.screenshot(
                path=str(
                    ROOT / "docs" / "screenshots" / "fullstack-e2e-check-submit.png"
                ),
                full_page=True,
            )
            return True, f"表单提交成功，提示：{text}"
        except Exception as exc:  # noqa: BLE001 - 返回可读失败，不中断后续体检
            return False, f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
        finally:
            browser.close()


# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true", help="跳过 Playwright")
    args = parser.parse_args()

    reporter = Reporter()
    sandbox_id = ""
    preview_url = ""
    collection_path = ""
    container = ""

    print(f"BASE_URL={base.BASE_URL}")
    print("\n== 0. Architect 澄清 -> 全栈契约 -> CONFIRM ==")
    run_id, contract, seq = drive_to_contract()
    confirmed = confirm_contract(run_id, contract, seq)
    print(f"  契约已确认：{confirmed['contractId']} / {confirmed.get('appShape')}")

    try:
        print("\n== 1. 触发 FullstackGraph ==")
        payload = start_fullstack(f"chk_{run_id}", confirmed)
        sandbox_id = payload["sandboxId"]
        preview_url = payload["previewUrl"].rstrip("/")
        collection_path = payload["collectionPath"]
        container = f"vibe-sandbox-{sandbox_id}"
        print(f"  sandbox={sandbox_id}")
        print(f"  preview={preview_url}")
        print(f"  collection={collection_path}")
        print(
            "  files="
            + ", ".join(
                payload.get("dbaFiles", [])
                + payload.get("backendFiles", [])
                + payload.get("frontendFiles", [])
            )
        )

        print("\n== 2. 沙盒静态检查 ==")
        reporter.check(
            "1. 后端代码目录 server/",
            lambda: check_server_entry(container),
        )

        server_code = read_sandbox_file(
            container, "server/index.js", "server/index.mjs"
        )
        schema_sql = read_sandbox_file(container, "server/schema.sql")
        db_js = read_sandbox_file(container, "server/db.js")
        express_import = bool(
            server_code
            and re.search(
                r"(from\s+['\"]express['\"]|require\(\s*['\"]express['\"]\s*\))",
                server_code,
            )
        )

        reporter.add(
            CheckResult(
                ok=bool(schema_sql and "CREATE TABLE" in schema_sql.upper()),
                title="2. 真实建表 SQL",
                detail=(schema_sql or "未找到 server/schema.sql")[:400],
                hint="DBACoder 未把 databaseDdl 落盘为可执行 SQL",
            )
        )

        reporter.add(
            CheckResult(
                ok=express_import,
                title="3. 后端使用 Express",
                detail=(
                    "server/index.js 已 import express"
                    if express_import
                    else f"未找到 import express；当前入口：{(server_code or '缺失')[:200]}"
                ),
                hint="BackendCoder 仍使用 node:http 或未生成 Express 入口",
            )
        )

        reporter.add(
            CheckResult(
                ok=bool(db_js and "sqlite" in db_js.lower()),
                title="4. SQLite 持久化层 server/db.js",
                detail=(
                    "server/db.js 包含 sqlite 初始化与 CRUD"
                    if db_js and "sqlite" in db_js.lower()
                    else (db_js or "未找到 server/db.js")[:300]
                ),
                hint="缺少 SQLite 数据层，数据只能存在进程内存",
            )
        )

        ok, listening = backend_listening(container)
        runtime = ""
        if ok:
            try:
                runtime = json.loads(listening.split(" ", 1)[1]).get("runtime", "")
            except (IndexError, json.JSONDecodeError):
                runtime = ""
        reporter.add(
            CheckResult(
                ok=ok,
                title="5. 后端监听端口（容器内 /api/health）",
                detail=listening or "3000 / 3100 均无响应",
                hint="后端进程未启动或启动脚本未拉起 server/index.js",
            )
        )

        reporter.add(
            CheckResult(
                ok=ok and runtime in {"express", "express-sqlite"},
                title="6. 后端运行时为 Express（非 node:http）",
                detail=f"health.runtime={runtime or '未上报'}",
                hint="仍是零依赖 node:http 内存仓储",
            )
        )

        print("\n== 3. 前端 -> Vite 代理 -> 后端 ==")
        net_ok = False
        net_detail = ""
        try:
            status, listing = base.http_get_json(f"{preview_url}{collection_path}")
            net_ok = status == 200
            net_detail = f"GET {collection_path} -> HTTP {status}"
        except Exception as exc:  # noqa: BLE001
            net_detail = f"请求失败: {exc}"
        reporter.add(
            CheckResult(
                ok=net_ok,
                title="7. Vite /api 代理命中沙盒后端",
                detail=net_detail,
                hint="vite.config.js 未配置代理、后端未启动或路径不一致",
            )
        )

        name = f"体检记录 {uuid.uuid4().hex[:8]}"
        post_ok = False
        post_body: Any = None
        if net_ok:
            status, post_body = base.http_post_json(
                f"{preview_url}{collection_path}", {"name": name, "title": name}
            )
            post_ok = status == 201
        reporter.add(
            CheckResult(
                ok=post_ok,
                title="8. POST 写入后端",
                detail=f"HTTP {status if net_ok else '-'} -> {post_body}",
                hint="POST 路由未实现或响应非 201",
            )
        )

        sqlite_file = ""
        if post_ok:
            code, files = docker_exec(
                container,
                "find /app -maxdepth 4 \\( -name '*.sqlite' -o -name '*.sqlite3' -o -name '*.db' \\) "
                "-not -path '*/node_modules/*' 2>/dev/null",
            )
            sqlite_file = files
        reporter.add(
            CheckResult(
                ok=bool(sqlite_file),
                title="9. 数据库文件真实落盘",
                detail=sqlite_file or "POST 后未发现 SQLite 文件",
                hint="写入仍停留在内存数组",
            )
        )

        print("\n== 4. 浏览器表单 -> 成功提示 ==")
        if args.no_browser:
            reporter.add(
                CheckResult(
                    False,
                    "10. Playwright 表单提交",
                    "已通过 --no-browser 跳过",
                    "未验证浏览器闭环",
                )
            )
        else:
            browser_name = f"浏览器提交 {uuid.uuid4().hex[:6]}"
            browser_ok, browser_detail = browser_submit(preview_url, browser_name)
            reporter.add(
                CheckResult(
                    ok=browser_ok,
                    title="10. Playwright 表单提交 + 成功提示",
                    detail=browser_detail,
                    hint="前端缺少成功提示或提交链路中断",
                )
            )
            if browser_ok:
                status, listing = base.http_get_json(f"{preview_url}/api/data")
                items = listing.get("items", []) if isinstance(listing, dict) else []
                found = any(
                    str(item.get("name") or item.get("title") or "") == browser_name
                    for item in items
                )
                reporter.add(
                    CheckResult(
                        ok=status == 200 and found,
                        title="11. 浏览器提交的数据可被 /api/data 查回",
                        detail=f"GET /api/data -> HTTP {status}, found={found}",
                        hint="前端 POST 与 /api/data 列表查询不是同一存储",
                    )
                )

        print("\n== 5. 重启沙盒，验证持久化 ==")
        if post_ok and sqlite_file:
            docker_restart(container)
            try:
                wait_preview_ready(preview_url, timeout=120)
                status, listing = base.http_get_json(f"{preview_url}{collection_path}")
                items = listing.get("items", []) if isinstance(listing, dict) else []
                survived = any(
                    str(item.get("name") or item.get("title") or "") == name
                    for item in items
                )
                reporter.add(
                    CheckResult(
                        ok=status == 200 and survived,
                        title="12. 重启后数据仍在（SQLite 持久化）",
                        detail=f"HTTP {status}, survived={survived}",
                        hint="数据存在内存，重启即丢",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                reporter.add(
                    CheckResult(
                        False,
                        "12. 重启后数据仍在（SQLite 持久化）",
                        str(exc),
                        "重启后服务未恢复",
                    )
                )
        else:
            reporter.add(
                CheckResult(
                    False,
                    "12. 重启后数据仍在（SQLite 持久化）",
                    "未发现 SQLite 文件或 POST 失败，无法验证重启持久化",
                    "先修复数据库写入",
                )
            )

    finally:
        if sandbox_id:
            base.cleanup(sandbox_id)

    print("\n==================================================")
    if reporter.failed:
        print(
            f"体检结论：FAIL（{len(reporter.failed)}/{len(reporter.results)} 项未通过）"
        )
        print("卡点定位（按链路顺序）：")
        for result in reporter.failed:
            print(f"  - {result.title}：{result.hint}")
        return 1
    print(f"体检结论：PASS（{len(reporter.results)}/{len(reporter.results)} 项通过）")
    print("全栈生成器已具备：前端表单 -> Express API -> SQLite 持久化")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except base.E2EFailure as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
