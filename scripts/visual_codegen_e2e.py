"""VisualDocument CodeGen 真实沙盒 E2E。

验证链路：

    FROZEN VisualDocument
      → FullstackService (mode=frontend)
      → 确定性 Compiler（Tailwind JSX / data-node-id / data-source）
      → Babel AST 门禁
      → 完整 Vite 工程（package.json / index.html / src/main.jsx）
      → 真实 Docker 沙盒 npm install + npm run build + npm run dev
      → Playwright 由宿主机脚本访问预览地址

运行（宿主已启动 Docker，python-ai 镜像可用）：

    docker run --rm --group-add "$(stat -c '%g' /var/run/docker.sock)" \
      -v "$PWD/services/orchestrator:/app" \
      -v "$PWD/scripts:/e2e:ro" -w /app -e PYTHONPATH=/app \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -e SANDBOX_IMAGE=vibe-platform/sandbox-react:dev \
      -e DIRECT_OPS_CHANGELOG_PATH=/tmp/visual-codegen-changelog.jsonl \
      -e KEEP_SANDBOX=1 \
      vibe-platform/python-ai:dev \
      python /e2e/visual_codegen_e2e.py
"""

from __future__ import annotations

import os
import re
import sys
import time

from ai_core.direct_ops.changelog import get_change_log_store
from ai_core.domain.architect import ArchitectContract
from ai_core.domain.fullstack import FullstackRunRequest
from ai_core.domain.visual_document import (
    VisualBinding,
    VisualDocument,
    VisualNode,
    VisualRepeat,
    freeze_visual_document,
)
from ai_core.fullstack.service import FullstackService


def build_document() -> VisualDocument:
    root = VisualNode(
        id="nd_page0001",
        type="page",
        name="页面",
        children=[
            VisualNode(
                id="nd_title001",
                type="text",
                name="标题",
                props={"text": "CodeGen E2E"},
            ),
            VisualNode(
                id="nd_list001",
                type="list",
                name="任务列表",
                repeat=VisualRepeat(source="tasks", sample_count=2),
                children=[
                    VisualNode(
                        id="nd_item001",
                        type="text",
                        name="任务",
                        props={"text": "示例任务"},
                    )
                ],
            ),
            VisualNode(
                id="nd_input001",
                type="input",
                name="输入",
                props={"placeholder": "请输入"},
            ),
            VisualNode(
                id="nd_btn0001",
                type="button",
                name="提交",
                props={"text": "提交"},
            ),
        ],
    )
    document = VisualDocument(
        docId="vdoc_codegen_e2e",
        projectId="proj_codegen_e2e",
        title="CodeGen E2E",
        root=root,
        bindings=[
            VisualBinding(
                id="bind_tasks",
                nodeId="nd_list001",
                entity="tasks",
                status="CONFIRMED",
            )
        ],
    )
    return freeze_visual_document(document)


def main() -> int:
    document = build_document()
    contract = ArchitectContract(
        contractId="contract_codegen_e2e",
        runId="arc_codegen_e2e",
        appShape="lightweight_web",
        confidenceScore=1.0,
        contractMarkdown="# CodeGen E2E",
        status="CONFIRMED",
    )
    request = FullstackRunRequest(
        projectId="proj_codegen_e2e",
        contractId=contract.contract_id,
        contract=contract,
        visualDocument=document,
        options={"mode": "frontend", "verifyBuild": True, "restartSandbox": True},
    )

    service = FullstackService()
    active = service.start_stream(request)
    print(f"[e2e] run started: {active.run_id} sandbox={active.sandbox_id}")
    deadline = time.time() + float(os.environ.get("VISUAL_CODEGEN_E2E_TIMEOUT", "300"))
    while time.time() < deadline:
        if active.is_terminal:
            break
        time.sleep(1)
    else:
        print("[e2e] FAILED: timeout", file=sys.stderr)
        service._destroy_quietly(active.sandbox_id)
        return 1

    for event in active.buffer.snapshot():
        print(f"[event] {event.type}")
    if active.status != "SUCCEEDED":
        print(f"[e2e] FAILED: {active.error}", file=sys.stderr)
        service._destroy_quietly(active.sandbox_id)
        return 1

    sandbox = service._sandbox
    app = sandbox.read_file(active.sandbox_id, "src/App.jsx")
    css = sandbox.read_file(active.sandbox_id, "src/visual.css")
    package = sandbox.read_file(active.sandbox_id, "package.json")
    index_html = sandbox.read_file(active.sandbox_id, "index.html")
    main_jsx = sandbox.read_file(active.sandbox_id, "src/main.jsx")
    expected = [
        "nd_page0001",
        "nd_title001",
        "nd_list001",
        "nd_item001",
        "nd_input001",
        "nd_btn0001",
    ]
    missing = [
        node_id for node_id in expected if f'data-node-id="{node_id}"' not in app
    ]
    if missing:
        print(f"[e2e] FAILED: missing data-node-id: {missing}", file=sys.stderr)
        service._destroy_quietly(active.sandbox_id)
        return 1
    if not re.search(r'data-source="src/App\.jsx:\d+"', app):
        print("[e2e] FAILED: missing data-source=file:line", file=sys.stderr)
        service._destroy_quietly(active.sandbox_id)
        return 1
    if "--color-primary" not in css:
        print("[e2e] FAILED: visual.css missing tokens", file=sys.stderr)
        service._destroy_quietly(active.sandbox_id)
        return 1
    if '"vite"' not in package or '"react"' not in package:
        print("[e2e] FAILED: package.json missing vite/react", file=sys.stderr)
        service._destroy_quietly(active.sandbox_id)
        return 1
    if "cdn.tailwindcss.com" not in index_html or "src/main.jsx" not in index_html:
        print("[e2e] FAILED: index.html incomplete", file=sys.stderr)
        service._destroy_quietly(active.sandbox_id)
        return 1
    if "./visual.css" not in main_jsx:
        print("[e2e] FAILED: main.jsx missing visual.css", file=sys.stderr)
        service._destroy_quietly(active.sandbox_id)
        return 1

    # 显式再执行一次 npm install + dev 重启，证明完整工程可安装可启动。
    install = sandbox.verify(active.sandbox_id, "install")
    print(
        f"[e2e] npm install: success={install.success} "
        f"exit={install.exit_code} durationMs={install.duration_ms}"
    )
    if not install.success:
        print(f"[e2e] FAILED: npm install: {install.logs[-2000:]}", file=sys.stderr)
        service._destroy_quietly(active.sandbox_id)
        return 1
    sandbox.restart(active.sandbox_id, ready_path="/")
    handle = sandbox.get(active.sandbox_id)
    print(f"[e2e] npm run dev restarted, preview={handle.preview_url}")
    print(f"[e2e] preview: {handle.preview_url}")
    print(f"[e2e] port: {handle.host_port}")

    content_hash = document.metadata.content_hash
    entries = get_change_log_store().list_for_project("proj_codegen_e2e", limit=5)
    expected_summary = f"成功从冻结版本 {content_hash} 编译生成真实前端工程"
    if not any(entry.summary == expected_summary for entry in entries):
        print(
            f"[e2e] FAILED: ChangeLog missing: {expected_summary} "
            f"entries={[entry.summary for entry in entries]}",
            file=sys.stderr,
        )
        service._destroy_quietly(active.sandbox_id)
        return 1

    print(
        "[e2e] PASS: "
        f"{len(expected)} data-node-id, data-source, visual.css, "
        "Tailwind project, npm install, build, npm run dev, ChangeLog"
    )
    if os.environ.get("KEEP_SANDBOX", "1").strip() not in {"1", "true", "yes"}:
        service._destroy_quietly(active.sandbox_id)
    else:
        print(f"[e2e] sandbox kept alive: {active.sandbox_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
