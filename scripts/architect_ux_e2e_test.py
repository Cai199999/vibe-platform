#!/usr/bin/env python3
"""E2E：Architect 面板统一聊天流 + 双输入路由 + 全栈一键导出。

覆盖本次收尾的两个交互改造：

  1. 移除“架构确认 / 随意提问”Tab，主状态机消息与影子助手问答合并成
     同一条时间轴；架构师正式提问带 ``data-formal-question=true`` 高亮。
  2. 底部常驻两个输入框：
     - 大输入框只调用 /helper（只读 RAG），绝不触发 answers / decision；
     - 小输入框按状态机状态调用 /answers（本轮用澄清补充说明验证）。
  3. 全栈生成 SUCCEEDED 且一致性通过后，成功卡片出现“📥 导出完整代码”，
     点击直接调用沙盒导出接口并触发浏览器下载。

运行（需要 5173 / 8080 已启动，Playwright 可用）：

    /tmp/e2e-venv/bin/python scripts/architect_ux_e2e_test.py
"""

from __future__ import annotations

import json

from playwright.sync_api import Page, expect, sync_playwright

WEB_URL = "http://127.0.0.1:5173/workspace"
CDP_URL = "http://127.0.0.1:9222"
PROJECT_ID = "proj_architect_ux"


def open_browser(playwright):
    """优先复用 9222 调试 Chromium，失败再启动自带浏览器。"""
    try:
        return playwright.chromium.connect_over_cdp(CDP_URL, timeout=5_000), False
    except Exception:  # noqa: BLE001 - 调试端口未开则回退
        return playwright.chromium.launch(headless=True), True


def seed_script(architect_state: dict) -> str:
    """把工作区 / 登录态 / Architect 快照写入浏览器存储。"""
    workspace = {
        "version": 1,
        "projects": [
            {
                "id": PROJECT_ID,
                "name": "Architect UX E2E",
                "template": "vibe-react",
                "templateVersion": 3,
                "files": {
                    "src/App.jsx": "export default function App() { return null }\n"
                },
                "entryFile": "src/App.jsx",
                "bootstrap": "ready",
                "canvasMode": "live",
                "generated": True,
                "createdAt": "2026-11-10T00:00:00.000Z",
            }
        ],
        "openTabs": [PROJECT_ID],
        "activeTabId": PROJECT_ID,
    }
    session = {
        "token": "vibe_mock_e2e",
        "user": {
            "id": "usr_e2e",
            "name": "E2E",
            "email": "e2e@vibe.local",
            "tenantId": "tenant_local",
            "role": "member",
        },
    }
    return f"""
        localStorage.setItem('vibe.workspace.v1', {json.dumps(json.dumps(workspace))});
        localStorage.setItem('vibe.auth.session', {json.dumps(json.dumps(session))});
        sessionStorage.setItem(
          'vibe.architect.v1',
          {json.dumps(json.dumps({PROJECT_ID: architect_state}))}
        );
    """


def base_architect_state() -> dict:
    return {
        "runId": "arc_e2e",
        "streamPath": None,
        "status": "AWAITING_CLARIFICATION",
        "lastSeq": 1,
        "appShape": "full_stack_system",
        "confidenceScore": 0.62,
        "threshold": 0.85,
        "round": 1,
        "maxRounds": 5,
        "clarification": {
            "clarificationId": "clr_e2e",
            "round": 1,
            "maxRounds": 5,
            "confidenceScore": 0.62,
            "questions": [
                {
                    "questionId": "q1",
                    "prompt": "目标用户是谁？",
                    "type": "text",
                    "required": True,
                }
            ],
        },
        "contract": None,
        "citations": [],
        "messages": [
            {
                "id": "m_clarification",
                "role": "assistant",
                "kind": "clarification",
                "content": "还需要确认以下信息：\n• 目标用户是谁？",
                "createdAt": "2026-11-10T00:00:01.000Z",
            },
            {
                "id": "m_answer",
                "role": "user",
                "content": "• 目标用户是谁？：团队负责人",
                "createdAt": "2026-11-10T00:00:02.000Z",
            },
        ],
        "helper": {
            "messages": [
                {
                    "id": "h_user",
                    "role": "user",
                    "content": "为什么选 Node.js？",
                    "createdAt": "2026-11-10T00:00:03.000Z",
                },
                {
                    "id": "h_assistant",
                    "role": "assistant",
                    "content": "因为与前端共享生态，部署更轻。",
                    "createdAt": "2026-11-10T00:00:04.000Z",
                },
            ],
        },
        "fullstack": {
            "runId": None,
            "status": "IDLE",
            "mode": None,
            "steps": [],
            "outputs": [],
            "result": None,
        },
    }


def open_panel(page: Page) -> None:
    page.goto(WEB_URL, wait_until="domcontentloaded")
    launch = page.locator(".architect-launch")
    expect(launch).to_be_visible(timeout=15_000)
    launch.click()
    expect(page.locator(".architect-panel")).to_be_visible(timeout=10_000)


def test_unified_flow_and_input_routing(page: Page) -> None:
    helper_calls: list[dict] = []
    answer_calls: list[dict] = []

    page.route(
        "**/api/v1/architect/runs/*/helper",
        lambda route: (
            helper_calls.append(json.loads(route.request.post_data or "{}")),
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "answer": "这是影子助手的只读回答：Node.js 与前端共用生态。",
                        "citations": [],
                        "model": "mock",
                    }
                ),
            ),
        ),
    )
    page.route(
        "**/api/v1/architect/runs/*/clarifications/*/answers",
        lambda route: (
            answer_calls.append(json.loads(route.request.post_data or "{}")),
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "clarification": {
                            "clarificationId": "clr_e2e",
                            "status": "ANSWERED",
                            "answeredAt": "2026-11-10T00:00:09Z",
                        },
                        "architectRun": {
                            "runId": "arc_e2e",
                            "status": "ANALYZING",
                        },
                    }
                ),
            ),
        ),
    )

    open_panel(page)

    # 1) Tab 已移除；主状态机与影子助手共用同一条聊天流。
    expect(page.locator("[data-architect-tab]")).to_have_count(0)
    flow = page.locator("[data-architect-flow]")
    expect(flow).to_be_visible()
    expect(flow.locator('[data-flow-source="architect"]')).to_have_count(2)
    expect(flow.locator('[data-flow-source="helper"]')).to_have_count(2)
    expect(flow.locator('[data-formal-question="true"]')).to_have_count(1)

    # 2) 两个输入框文案与常驻位置。
    helper_input = page.locator("[data-helper-input]")
    architect_input = page.locator("[data-architect-input]")
    expect(helper_input).to_have_attribute("placeholder", "有什么不懂的随时问我...")
    expect(architect_input).to_have_attribute(
        "placeholder", "输入架构确认信息或补充需求..."
    )

    # 3) 大输入框只走 helper：回答出现在流里，但绝不触发 answers。
    helper_input.fill("为什么用 MySQL？")
    page.locator("[data-helper-send]").click()
    expect(
        flow.locator('[data-flow-source="helper"]', has_text="只读回答")
    ).to_be_visible(timeout=5_000)
    assert len(helper_calls) == 1, helper_calls
    assert answer_calls == [], "影子助手提问竟然触发了架构回答接口"
    expect(page.locator(".architect-status")).to_contain_text("等待回答")

    # 4) 小输入框走架构 Agent：以 comment-only 正式回答当前澄清。
    architect_input.fill("目标用户是 3-10 人的招聘团队")
    page.locator("[data-architect-input-send]").click()
    expect(page.locator("[data-architect-input]")).to_have_count(1)
    page.wait_for_timeout(600)
    assert len(answer_calls) == 1, answer_calls
    assert answer_calls[0].get("answers") == []
    assert "招聘团队" in (answer_calls[0].get("comment") or "")
    print("  unified flow + input routing: OK")


def confirmed_contract() -> dict:
    return {
        "contractId": "ctr_e2e",
        "status": "CONFIRMED",
        "appShape": "full_stack_system",
        "title": "招聘助手",
        "summary": "抓取 JD、AI 改写、投递与对话。",
        "confidenceScore": 0.95,
        "threshold": 0.85,
        "techStack": {"frontend": "React", "backend": "Express"},
        "apiRoutes": [
            {"method": "POST", "path": "/api/v1/jobs/scrape"},
            {"method": "POST", "path": "/api/v1/resumes/1/rewrite"},
        ],
        "databaseDdl": "CREATE TABLE resumes (id INTEGER PRIMARY KEY);",
        "pages": [],
        "components": [],
    }


def test_success_export_button(page: Page) -> None:
    export_requests: list[str] = []

    def handle_export(route):
        export_requests.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/gzip",
            headers={
                "Content-Disposition": 'attachment; filename="project-export.tar.gz"'
            },
            body=b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00",
        )

    page.route("**/api/v1/sandboxes/sbx_architect_e2e/export", handle_export)

    open_panel(page)

    card = page.locator("[data-architect-export]")
    expect(card).to_be_visible(timeout=10_000)
    button = page.locator("[data-architect-export-button]")
    expect(button).to_be_visible()
    expect(button).to_contain_text("导出完整代码")

    with page.expect_download(timeout=10_000) as download_info:
        button.click()
    download = download_info.value
    assert (
        download.suggested_filename == "project-export.tar.gz"
    ), download.suggested_filename
    assert len(export_requests) == 1, export_requests
    assert "sbx_architect_e2e" in export_requests[0]
    print("  success card export: OK")


def main() -> int:
    with sync_playwright() as playwright:
        browser, owned = open_browser(playwright)
        try:
            # 用例 1：统一聊天流 + 双输入路由。
            context = browser.new_context(
                viewport={"width": 1600, "height": 1000},
                accept_downloads=True,
            )
            context.add_init_script(seed_script(base_architect_state()))
            page = context.new_page()
            test_unified_flow_and_input_routing(page)
            context.close()

            # 用例 2：全栈成功卡片一键导出。
            contract = confirmed_contract()
            state = {
                "runId": "arc_e2e_done",
                "status": "CONFIRMED",
                "lastSeq": 20,
                "appShape": "full_stack_system",
                "confidenceScore": 0.95,
                "threshold": 0.85,
                "round": 2,
                "maxRounds": 5,
                "clarification": None,
                "contract": contract,
                "citations": [],
                "messages": [
                    {
                        "id": "m_contract",
                        "role": "assistant",
                        "kind": "contract",
                        "content": "《全栈实施契约》已生成，请确认。",
                        "createdAt": "2026-11-10T00:00:05.000Z",
                    },
                    {
                        "id": "m_confirmed",
                        "role": "system",
                        "content": "实施契约已确认。",
                        "createdAt": "2026-11-10T00:00:06.000Z",
                    },
                ],
                "helper": {"messages": []},
                "fullstack": {
                    "runId": "fsk_e2e",
                    "status": "SUCCEEDED",
                    "mode": "fullstack",
                    "lastSeq": 42,
                    "sandboxId": "sbx_architect_e2e",
                    "previewUrl": "http://127.0.0.1:1/",
                    "steps": [],
                    "results": [],
                    "result": {
                        "runId": "fsk_e2e",
                        "sandboxId": "sbx_architect_e2e",
                        "table": "resumes",
                        "collectionPath": "/api/v1/resumes",
                    },
                    "consistency": {"ok": True, "issues": [], "callsChecked": 2},
                    "outputs": [],
                },
            }
            context = browser.new_context(
                viewport={"width": 1600, "height": 1000},
                accept_downloads=True,
            )
            context.add_init_script(seed_script(state))
            page = context.new_page()
            test_success_export_button(page)
            context.close()
        finally:
            if owned:
                browser.close()
    print("ARCHITECT_UX_E2E_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
