"""VisualDocument CodeGen 真实沙盒 E2E。

验证链路：

    FROZEN VisualDocument
      → FullstackService (mode=frontend)
      → 确定性 Compiler（data-node-id / visual.css）
      → Babel AST 门禁
      → 真实 Docker 沙盒 npm run build
      → Vite 重启冒烟

运行（宿主已启动 Docker，python-ai 镜像可用）：

    docker run --rm \
      -v "$PWD:/app" -w /app \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -e SANDBOX_IMAGE=vibe-platform/sandbox-react:dev \
      vibe-platform/python-ai:dev \
      python scripts/visual_codegen_e2e.py
"""

from __future__ import annotations

import os
import sys
import time

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
    if "--color-primary" not in css:
        print("[e2e] FAILED: visual.css missing tokens", file=sys.stderr)
        service._destroy_quietly(active.sandbox_id)
        return 1
    if active.preview_url:
        print(f"[e2e] preview: {active.preview_url}")
    print(
        "[e2e] PASS: " f"{len(expected)} data-node-id, visual.css, build & Vite restart"
    )
    service._destroy_quietly(active.sandbox_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
