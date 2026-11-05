#!/usr/bin/env python3
"""M1 全链路 E2E：前端画布契约 → Java BFF → Python AI Core → Docker 沙盒。

不启动浏览器，直接复用 platform-web 提交给 BFF 的同一份 JSON 契约：
  1. 从 platform-web 读取画布使用的初始源码；
  2. POST http://127.0.0.1:8080/api/v1/runs（与浏览器同源代理的最终目标一致）；
  3. 校验 Agent 闭环结果、预览页面、Diff、Vite 代理；
  4. 清理沙盒。

运行：
    python3 scripts/platform_e2e_test.py
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_APP = ROOT / "apps" / "platform-web" / "src" / "mockProject.js"

BFF_RUN_URL = "http://127.0.0.1:8080/api/v1/runs"
WEB_URL = "http://127.0.0.1:5173/"
# 浏览器实际走同源 /api，由 Vite dev server 代理到 Java BFF。
WEB_RUN_URL = f"{WEB_URL}api/v1/runs"
PY_AI_URL = "http://127.0.0.1:8100"


def fail(message: str) -> None:
    print(f"\nFAIL: {message}")
    sys.exit(1)


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def http_json(method: str, url: str, payload: dict | None = None, timeout: int = 300):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
        return response.status, json.loads(raw) if raw else None


def http_text(url: str, timeout: int = 30) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def load_initial_app() -> str:
    """从 mockProject.js 提取 INITIAL_APP，保证画布与请求使用同一份源码。"""
    source = WEB_APP.read_text(encoding="utf-8")
    match = re.search(r"export const INITIAL_APP = `(.*?)`\n", source, re.S)
    require(match is not None, f"无法从 {WEB_APP} 提取 INITIAL_APP")
    return match.group(1)


def build_annotation(
    initial_app: str,
    run_id: str,
    index: int,
    selector: str,
    instruction: str,
) -> dict:
    """与 platform-web 的 buildAnnotation 保持同一份契约。"""
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    return {
        "version": 1,
        "annotationId": f"ann_{run_id}_{index}",
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
                "line": 11,
                "column": 7,
                "component": "App",
                "sourceKind": "jsx",
            },
            "selector": selector,
            "snippet": {
                "startLine": 1,
                "endLine": len(initial_app.split("\n")),
                "content": initial_app,
                "hash": "sha256:canvas-demo",
            },
        },
        "intent": {
            "kind": "UI_ONLY",
            "ui": {
                "summary": instruction,
                "acceptance": [f"完成指令：{instruction}"],
                "elementRole": "element",
            },
        },
        "payload": {
            "text": instruction,
            "from": None,
            "to": None,
            "strokes": [],
        },
        "createdAt": now,
    }


def build_payload(initial_app: str, run_id: str) -> dict:
    """一次提交 3 个标注：颜色 + 文案 + 尺寸。"""
    return {
        "runId": run_id,
        "tenantId": "tenant_local",
        "projectId": "proj_canvas_demo",
        "annotations": [
            build_annotation(
                initial_app,
                run_id,
                1,
                "[data-vibe-target='cta-button']",
                "改成蓝色",
            ),
            build_annotation(
                initial_app,
                run_id,
                2,
                "[data-vibe-target='title']",
                "文字改为Batch Platform Title",
            ),
            build_annotation(
                initial_app,
                run_id,
                3,
                "[data-vibe-target='copy']",
                "放大一倍",
            ),
        ],
        "files": {"src/App.jsx": initial_app},
        "options": {
            "llmProvider": "mock",
            "mockScenario": "fail_first",
            "maxRetries": 3,
            "keepSandbox": True,
        },
    }


def main() -> None:
    print("== 0. 平台存活检查 ==")
    for name, url in (
        ("python-ai", f"{PY_AI_URL}/api/v1/health"),
        ("java-bff", "http://127.0.0.1:8080/api/v1/health"),
        ("platform-web", WEB_URL),
    ):
        status, _ = http_text(url)
        require(status == 200, f"{name} 不可达: {url} -> {status}")
        print(f"   {name:<13} 200 {url}")

    # Vite 代理必须把 /api 转发到 Java BFF，否则浏览器提交会 404。
    proxy_status, proxy_body = http_json(
        "GET", "http://127.0.0.1:5173/api/v1/health", timeout=30
    )
    print(f"   vite-proxy    {proxy_status} /api/v1/health -> {proxy_body}")
    require(proxy_status == 200, "Vite /api 代理未转发到 Java BFF")

    initial_app = load_initial_app()
    run_id = f"run_platform_e2e_{int(time.time())}"
    payload = build_payload(initial_app, run_id)

    print("\n== 1. 提交画布标注（Vite 同源代理 → Java BFF → Python AI Core）==")
    started = time.time()
    status, body = http_json("POST", WEB_RUN_URL, payload, timeout=300)
    elapsed = time.time() - started
    print(f"   HTTP {status}，耗时 {elapsed:.1f}s")
    if status != 200:
        fail(f"BFF 返回非 200: {body}")

    require(body is not None, "响应体为空")
    print(
        json.dumps(
            {
                key: body.get(key)
                for key in ("runId", "status", "retryCount", "sandboxId", "previewUrl")
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    print("\n== 2. 校验 Agent 闭环结果 ==")
    require(
        body.get("status") in ("SUCCEEDED", "AWAITING_APPROVAL"),
        f"状态异常: {body.get('status')}",
    )
    require(
        body.get("retryCount") == 1, f"应自动修复 1 次，实际 {body.get('retryCount')}"
    )
    verify = body.get("verifyResult") or {}
    require(verify.get("success") is True, f"最终构建未通过: {verify}")
    patch = body.get("generatedPatch") or ""
    for token in (
        "background-color:blue",
        "Batch Platform Title",
        "transform:scale(2)",
    ):
        require(token in patch, f"Diff 缺少批量修改内容: {token}")
    require(
        patch.startswith("---") or patch.startswith("diff "), "Diff 不是 unified diff"
    )
    preview_url = body.get("previewUrl") or ""
    require(
        preview_url.startswith("http://127.0.0.1:"), f"previewUrl 异常: {preview_url}"
    )
    sandbox_id = body.get("sandboxId") or ""
    require(sandbox_id.startswith("sbx_"), f"sandboxId 异常: {sandbox_id}")
    print(f"   status={body['status']} retryCount={body['retryCount']} 构建通过")
    print(f"   previewUrl={preview_url}")

    print("\n== 3. 校验沙盒预览与 HMR 产物 ==")
    preview_status, preview_html = http_text(preview_url)
    require(
        preview_status == 200 and '<div id="root">' in preview_html, "预览页面不可用"
    )
    module_status, module_source = http_text(f"{preview_url}/src/App.jsx")
    require(module_status == 200, "无法读取沙盒 /src/App.jsx")
    for token in (
        "background-color:blue",
        "Batch Platform Title",
        "transform:scale(2)",
    ):
        require(token in module_source, f"沙盒源码缺少批量修改: {token}")
    print("   预览页 200；/src/App.jsx 已同时包含颜色 / 文案 / 尺寸修改")

    print("\n== 4. 清理沙盒 ==")
    try:
        cleanup_status, _ = http_json(
            "DELETE", f"{PY_AI_URL}/api/v1/internal/sandbox/{sandbox_id}", timeout=60
        )
        print(f"   DELETE sandbox -> {cleanup_status}")
    except urllib.error.HTTPError as exc:
        print(f"   DELETE sandbox -> HTTP {exc.code}（可能已自动清理）")

    print(
        "\nPASS: M1 全链路（platform-web 契约 → Java BFF → Python AI Core → 沙盒）验证通过"
    )


if __name__ == "__main__":
    main()
