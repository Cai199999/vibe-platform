#!/usr/bin/env python3
"""LangGraph 单 Agent 闭环 E2E。

场景：
  1. fail_first：首次生成语法错误 -> verifier 捕获 -> evaluator 触发修复 ->
     第二次生成正确 diff -> AWAITING_APPROVAL；
  2. always_fail：持续失败 -> 3 次重试耗尽 -> FAILED；
  3. FULLSTACK 意图：M1 必须显式返回 FULLSTACK_NOT_SUPPORTED。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

AI_BASE_URL = os.environ.get("AI_BASE_URL", "http://127.0.0.1:8100").rstrip("/")

INITIAL_APP = """export default function App() {
  return (
    <main>
      <h1>Vibe App</h1>
      <button>Click me</button>
    </main>
  )
}
"""


def call(
    method: str,
    path: str,
    payload: dict | None = None,
    timeout: float = 300,
) -> tuple[int, object]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{AI_BASE_URL}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
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


def fetch(url: str, timeout: float = 10) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def log_step(title: str) -> None:
    print(f"\n=== {title} ===")


def build_annotation(intent_kind: str = "UI_ONLY") -> dict:
    return {
        "version": 1,
        "annotationId": f"ann_e2e_{int(time.time() * 1000)}",
        "type": "note",
        "worldTarget": {"x": 120, "y": 80, "width": 200, "height": 48, "zoom": 1.0},
        "target": {
            "mode": "source",
            "source": {
                "file": "src/App.jsx",
                "line": 5,
                "column": 7,
                "component": "App",
                "sourceKind": "jsx",
            },
            "selector": "button",
            "snippet": {
                "startLine": 1,
                "endLine": 9,
                "content": INITIAL_APP,
                "hash": "sha256:e2e",
            },
        },
        "intent": {
            "kind": intent_kind,
            "ui": {
                "summary": "将按钮改为红色",
                "acceptance": ["按钮背景色为红色"],
                "elementRole": "button",
            },
        },
        "payload": {"text": "将按钮改为红色"},
        "createdAt": "2026-11-04T10:00:00Z",
    }


def execute(
    scenario: str,
    *,
    intent_kind: str = "UI_ONLY",
    run_suffix: str,
) -> tuple[int, object]:
    body = {
        "runId": f"run_e2e_{run_suffix}_{int(time.time())}",
        "projectId": f"project_e2e_{run_suffix}",
        "annotation": build_annotation(intent_kind),
        "files": {"src/App.jsx": INITIAL_APP},
        "options": {
            "llmProvider": "mock",
            "mockScenario": scenario,
            "maxRetries": 3,
            "keepSandbox": True,
        },
    }
    return call("POST", "/api/v1/internal/runs/execute", body)


def destroy(sandbox_id: str | None) -> None:
    if not sandbox_id:
        return
    status, body = call("DELETE", f"/api/v1/internal/sandbox/{sandbox_id}", timeout=30)
    if status == 200:
        print(f"[cleanup] 已销毁沙盒 {sandbox_id}")
    else:
        print(f"[cleanup] 销毁沙盒失败 {sandbox_id}: {status} {body}", file=sys.stderr)


def message_text(response: dict) -> str:
    return "\n".join(
        str(item.get("content", "")) for item in response.get("messages", [])
    )


def main() -> int:
    sandbox_ids: list[str] = []
    try:
        log_step("1. fail_first：首次语法错误 -> 自动修复 -> AWAITING_APPROVAL")
        status, result = execute("fail_first", run_suffix="repair")
        assert status == 200, f"execute 期望 200，实际 {status}: {result}"
        sandbox_ids.append(result["sandboxId"])
        print(
            json.dumps(
                {
                    "runId": result["runId"],
                    "status": result["status"],
                    "retryCount": result["retryCount"],
                    "sandboxId": result["sandboxId"],
                    "previewUrl": result["previewUrl"],
                    "verify": result.get("verifyResult"),
                },
                ensure_ascii=False,
            )
        )
        trace = message_text(result)
        print("--- agent messages ---")
        print(trace)
        assert result["status"] == "AWAITING_APPROVAL", "最终状态应为 AWAITING_APPROVAL"
        assert result["retryCount"] == 1, "应恰好发生 1 次自动修复"
        assert result["verifyResult"]["success"] is True, "最终构建应成功"
        assert "构建失败" in trace, "messages 中应记录首次构建失败"
        assert "第 1/3 次修复" in trace, "messages 中应记录修复调度"
        assert (
            "+++ b/src/App.jsx" in result["generatedPatch"]
        ), "最终补丁应为 unified diff"
        assert (
            "backgroundColor: 'red'" in result["generatedPatch"]
        ), "补丁应包含红色样式"

        log_step("2. 验证沙盒与 Vite 实时模块")
        preview_status, preview_html = fetch(result["previewUrl"])
        assert preview_status == 200, f"预览地址不可访问: {preview_status}"
        assert 'id="root"' in preview_html, "预览页面不是 Vite index.html"
        module_status, module_source = fetch(f"{result['previewUrl']}/src/App.jsx")
        assert module_status == 200, f"模块请求失败: {module_status}"
        # Vite/esbuild 会把 JSX 转成 JS，并可能把单引号规范化为双引号。
        assert (
            "backgroundColor" in module_source and "red" in module_source
        ), "Vite 未返回红色按钮代码"
        print(f"OK: {result['previewUrl']} 已加载修复后的代码")

        destroy(result["sandboxId"])
        sandbox_ids.remove(result["sandboxId"])

        log_step("3. always_fail：3 次重试耗尽 -> FAILED")
        status, failed = execute("always_fail", run_suffix="exhausted")
        assert status == 200, f"execute 期望 200，实际 {status}: {failed}"
        sandbox_ids.append(failed["sandboxId"])
        print(
            json.dumps(
                {
                    "runId": failed["runId"],
                    "status": failed["status"],
                    "retryCount": failed["retryCount"],
                    "error": failed["error"],
                },
                ensure_ascii=False,
            )
        )
        failed_trace = message_text(failed)
        assert failed["status"] == "FAILED", "重试耗尽后应为 FAILED"
        assert failed["retryCount"] == 3, "retryCount 应达到硬上限 3"
        assert failed["verifyResult"]["success"] is False, "最终验证应失败"
        assert "重试预算耗尽" in failed_trace, "messages 中应记录预算耗尽"
        destroy(failed["sandboxId"])
        sandbox_ids.remove(failed["sandboxId"])

        log_step("4. FULLSTACK 意图：M1 必须显式拒绝")
        status, rejected = execute(
            "none",
            intent_kind="FULLSTACK",
            run_suffix="fullstack",
        )
        print(f"HTTP {status}: {rejected}")
        assert status == 422, f"FULLSTACK 期望 422，实际 {status}: {rejected}"
        assert rejected["detail"]["code"] == "FULLSTACK_NOT_SUPPORTED"

        print("\nAGENT E2E: PASS")
        return 0
    finally:
        for sandbox_id in list(sandbox_ids):
            destroy(sandbox_id)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"\nAGENT E2E: FAIL - {exc}", file=sys.stderr)
        raise
