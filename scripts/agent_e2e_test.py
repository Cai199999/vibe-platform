#!/usr/bin/env python3
"""LangGraph 单 Agent 闭环 E2E。

场景：
  1. fail_first + 3 个批量标注：首次生成语法错误 -> 自动修复 -> 3 处修改
     同时生效 -> SUCCEEDED；
  2. always_fail：持续失败 -> 3 次重试耗尽 -> FAILED；
  3. FULLSTACK 意图：M1 必须显式返回 FULLSTACK_NOT_SUPPORTED；
  4. 超过 3 个标注：请求必须被 422 拒绝。
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


def build_annotation(
    selector: str,
    instruction: str,
    *,
    annotation_id: str,
    intent_kind: str = "UI_ONLY",
) -> dict:
    return {
        "version": 1,
        "annotationId": annotation_id,
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
                "line": 5,
                "column": 7,
                "component": "App",
                "sourceKind": "jsx",
            },
            "selector": selector,
            "snippet": {
                "startLine": 1,
                "endLine": len(INITIAL_APP.splitlines()),
                "content": INITIAL_APP,
                "hash": "sha256:e2e",
            },
        },
        "intent": {
            "kind": intent_kind,
            "ui": {
                "summary": instruction,
                "acceptance": [f"完成指令：{instruction}"],
                "elementRole": "element",
            },
        },
        "payload": {"text": instruction},
        "createdAt": "2026-11-04T10:00:00Z",
    }


def build_annotations(intent_kind: str = "UI_ONLY") -> list[dict]:
    """一次请求携带 3 个不同属性 / 不同目标的标注。"""
    suffix = int(time.time() * 1000)
    return [
        build_annotation(
            "[data-vibe-target='cta-button']",
            "改成蓝色",
            annotation_id=f"ann_e2e_color_{suffix}",
            intent_kind=intent_kind,
        ),
        build_annotation(
            "[data-vibe-target='title']",
            "文字改为Batch Title",
            annotation_id=f"ann_e2e_text_{suffix}",
            intent_kind=intent_kind,
        ),
        build_annotation(
            "[data-vibe-target='copy']",
            "放大一倍",
            annotation_id=f"ann_e2e_scale_{suffix}",
            intent_kind=intent_kind,
        ),
    ]


def execute(
    scenario: str,
    *,
    intent_kind: str = "UI_ONLY",
    run_suffix: str,
) -> tuple[int, object]:
    body = {
        "runId": f"run_e2e_{run_suffix}_{int(time.time())}",
        "projectId": f"project_e2e_{run_suffix}",
        # 新协议：一次性提交 AnnotationEnvelope[]。
        "annotations": build_annotations(intent_kind),
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
        log_step("1. fail_first：首次语法错误 -> 自动修复 -> SUCCEEDED")
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
        assert result["status"] in (
            "SUCCEEDED",
            "AWAITING_APPROVAL",
        ), "最终状态应为 SUCCEEDED（旧版本为 AWAITING_APPROVAL）"
        assert result["retryCount"] == 1, "应恰好发生 1 次自动修复"
        assert result["verifyResult"]["success"] is True, "最终构建应成功"
        assert "构建失败" in trace, "messages 中应记录首次构建失败"
        assert "第 1/3 次修复" in trace, "messages 中应记录修复调度"
        assert (
            "+++ b/src/App.jsx" in result["generatedPatch"]
        ), "最终补丁应为 unified diff"
        # 三个标注必须一次性全部生效，不能只处理第一个。
        for token in (
            "background-color:blue",
            "Batch Title",
            "transform:scale(2)",
        ):
            assert token in result["generatedPatch"], f"最终补丁缺少 {token}"
        assert "3 个标注" in trace, "Planner 消息应明确说明批量标注数量"

        log_step("2. 验证沙盒与 Vite 实时模块")
        preview_status, preview_html = fetch(result["previewUrl"])
        assert preview_status == 200, f"预览地址不可访问: {preview_status}"
        assert 'id="root"' in preview_html, "预览页面不是 Vite index.html"
        module_status, module_source = fetch(f"{result['previewUrl']}/src/App.jsx")
        assert module_status == 200, f"模块请求失败: {module_status}"
        # Vite/esbuild 会把 JSX 转成 JS，并可能把单引号规范化为双引号。
        for token in (
            "background-color:blue",
            "Batch Title",
            "transform:scale(2)",
        ):
            assert token in module_source, f"Vite 未返回批量修改内容: {token}"
        print(f"OK: {result['previewUrl']} 已同时加载 3 个标注的修改")

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

        log_step("5. 超过 3 个标注：Pydantic 必须返回 422")
        annotations = build_annotations()
        annotations.append(
            build_annotation(
                "[data-vibe-target='kicker']",
                "改成绿色",
                annotation_id="ann_e2e_extra",
            )
        )
        status, too_many = call(
            "POST",
            "/api/v1/internal/runs/execute",
            {
                "runId": f"run_e2e_too_many_{int(time.time())}",
                "projectId": "project_e2e_too_many",
                "annotations": annotations,
                "files": {"src/App.jsx": INITIAL_APP},
                "options": {"llmProvider": "mock", "mockScenario": "none"},
            },
        )
        print(f"HTTP {status}: {too_many}")
        assert status == 422, f"4 个标注期望 422，实际 {status}: {too_many}"
        assert "at most 3" in json.dumps(too_many), "错误信息应说明最多 3 个标注"

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
