#!/usr/bin/env python3
"""Sandbox 端到端集成测试。

流程：
  1. 创建 React 沙盒并验证预览端口映射（HTTP 200）；
  2. 通过全量文件注入语法错误，verify(build) 必须失败并返回错误日志；
  3. 通过 unified diff 修复代码，verify(build) 必须成功；
  4. 拉取 Vite 实时模块，确认补丁已被开发服务器读取（HMR 链路）；
  5. 销毁沙盒，并确认后续操作返回 404。
"""

from __future__ import annotations

import difflib
import json
import os
import sys
import time
import urllib.error
import urllib.request

AI_BASE_URL = os.environ.get("AI_BASE_URL", "http://127.0.0.1:8100").rstrip("/")

BROKEN_APP = """export default function App() {
  const broken = ;
  return (
    <main>
      <h1>Vibe Sandbox</h1>
    </main>
  )
}
"""

FIXED_APP = """export default function App() {
  return (
    <main>
      <h1>Vibe Sandbox Patched</h1>
      <p>Sandbox is ready.</p>
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
    """调用 AI Core；返回 (status_code, parsed_body)。"""
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


def main() -> int:
    project_id = f"e2e-project-{int(time.time())}"
    sandbox_id: str | None = None
    destroyed = False

    try:
        log_step("1. 创建沙盒")
        status, created = call(
            "POST",
            "/api/v1/internal/sandbox/create",
            {
                "project_id": project_id,
                "files": {"src/App.jsx": FIXED_APP},
                "template": "react-vite",
            },
        )
        assert status == 201, f"create 期望 201，实际 {status}: {created}"
        sandbox_id = created["sandbox_id"]
        preview_url = created["preview_url"]
        print(
            json.dumps(
                {
                    "sandbox_id": sandbox_id,
                    "state": created["state"],
                    "preview_url": preview_url,
                    "host_port": created["host_port"],
                    "internal_port": created["internal_port"],
                },
                ensure_ascii=False,
            )
        )

        log_step("2. 验证端口映射（宿主 -> 容器 5173）")
        preview_status, preview_html = fetch(preview_url)
        assert preview_status == 200, f"预览地址不可访问: {preview_status}"
        assert 'id="root"' in preview_html, "预览页面不是 Vite index.html"
        print(f"OK: {preview_url} -> HTTP {preview_status}")

        log_step("3. 注入语法错误代码（全量文件模式）")
        status, patched = call(
            "POST",
            "/api/v1/internal/sandbox/apply-patch",
            {"sandbox_id": sandbox_id, "files": {"src/App.jsx": BROKEN_APP}},
        )
        assert status == 200, f"apply-patch 期望 200，实际 {status}: {patched}"
        print(
            f"changed_files={patched['changed_files']} hot_reload={patched['hot_reload']}"
        )

        log_step("4. verify(build) 必须抓到语法错误")
        status, broken_result = call(
            "GET",
            f"/api/v1/internal/sandbox/verify?sandbox_id={sandbox_id}&check=build",
        )
        assert status == 200, f"verify 期望 200，实际 {status}: {broken_result}"
        print(
            json.dumps(
                {
                    "success": broken_result["success"],
                    "exit_code": broken_result["exit_code"],
                    "duration_ms": broken_result["duration_ms"],
                },
                ensure_ascii=False,
            )
        )
        print("--- build logs (tail) ---")
        print(broken_result["logs"][-2000:])
        assert broken_result["success"] is False, "语法错误未被构建捕获"
        assert broken_result["exit_code"] != 0, "失败构建退出码应为非 0"
        assert "src/App.jsx" in broken_result["logs"], "日志中缺少出错文件路径"

        log_step("5. 用 unified diff 修复代码")
        patch = "".join(
            difflib.unified_diff(
                BROKEN_APP.splitlines(keepends=True),
                FIXED_APP.splitlines(keepends=True),
                fromfile="a/src/App.jsx",
                tofile="b/src/App.jsx",
            )
        )
        assert patch, "生成的 diff 为空"
        status, patched = call(
            "POST",
            "/api/v1/internal/sandbox/apply-patch",
            {"sandbox_id": sandbox_id, "patch": patch},
        )
        assert status == 200, f"apply-patch(diff) 期望 200，实际 {status}: {patched}"
        print(f"changed_files={patched['changed_files']}")

        log_step("6. verify(build) 必须成功")
        status, fixed_result = call(
            "GET",
            f"/api/v1/internal/sandbox/verify?sandbox_id={sandbox_id}&check=build",
        )
        assert status == 200, f"verify 期望 200，实际 {status}: {fixed_result}"
        print(
            json.dumps(
                {
                    "success": fixed_result["success"],
                    "exit_code": fixed_result["exit_code"],
                    "duration_ms": fixed_result["duration_ms"],
                },
                ensure_ascii=False,
            )
        )
        print("--- build logs (tail) ---")
        print(fixed_result["logs"][-2000:])
        assert fixed_result["success"] is True, "修复后构建仍然失败"
        assert fixed_result["exit_code"] == 0, "成功构建退出码应为 0"

        log_step("7. 确认 Vite 已读取最新文件（HMR 链路）")
        module_url = f"{preview_url}/src/App.jsx"
        module_status, module_source = fetch(module_url)
        assert module_status == 200, f"模块请求失败: {module_status}"
        assert "Vibe Sandbox Patched" in module_source, "Vite 未返回修复后的代码"
        print(f"OK: {module_url} 已返回最新模块内容")

        log_step("8. 销毁沙盒")
        status, destroyed_body = call(
            "DELETE", f"/api/v1/internal/sandbox/{sandbox_id}"
        )
        assert status == 200, f"destroy 期望 200，实际 {status}: {destroyed_body}"
        destroyed = True
        print(json.dumps(destroyed_body, ensure_ascii=False))

        status, gone = call(
            "GET",
            f"/api/v1/internal/sandbox/verify?sandbox_id={sandbox_id}&check=build",
        )
        assert status == 404, f"销毁后 verify 期望 404，实际 {status}: {gone}"
        print("OK: 销毁后资源已不可访问 (404)")

        print("\nSANDBOX E2E: PASS")
        return 0
    finally:
        if sandbox_id and not destroyed:
            try:
                call("DELETE", f"/api/v1/internal/sandbox/{sandbox_id}", timeout=30)
                print(f"[cleanup] 已销毁残留沙盒 {sandbox_id}")
            except Exception as exc:  # noqa: BLE001 - 清理阶段不掩盖原始异常
                print(f"[cleanup] 销毁失败: {exc}", file=sys.stderr)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"\nSANDBOX E2E: FAIL - {exc}", file=sys.stderr)
        raise
