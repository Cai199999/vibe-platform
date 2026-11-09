"""FullstackGraph：契约解析、代码生成、SSE 事件与沙盒重启测试。

运行：

    cd services/orchestrator
    PYTHONPATH=. python -m unittest tests.test_fullstack -v

全部使用内存 Fake Sandbox，不依赖 Docker / 模型 / MySQL，可在 CI 中运行。
"""

from __future__ import annotations

import time
import unittest
import uuid
from datetime import datetime, timezone

from ai_core.agent.events import TERMINAL_EVENT_TYPES, RunEventBuffer
from ai_core.domain.architect import (
    ArchitectApiRoute,
    ArchitectComponentInteraction,
    ArchitectContract,
)
from ai_core.domain.fullstack import FullstackRunRequest
from ai_core.fullstack.coders import (
    collection_path,
    extract_columns,
    extract_table_name,
    generate_backend_bundle,
    generate_dba_bundle,
    generate_frontend_bundle,
)
from ai_core.fullstack.graph import FullstackGraphRunner
from ai_core.fullstack.service import FullstackService
from ai_core.ports.sandbox import SandboxHandle, SandboxProvider, VerifyResult

DDL = """CREATE TABLE todos (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  tenant_id VARCHAR(64) NOT NULL,
  name VARCHAR(200) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'todo',
  payload JSON NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  deleted_at TIMESTAMP NULL
);"""


def _contract(**overrides) -> ArchitectContract:
    payload = {
        "contractId": "ctr_fullstack_test",
        "runId": "arc_fullstack_test",
        "appShape": "full_stack_system",
        "status": "CONFIRMED",
        "confidenceScore": 0.94,
        "contractMarkdown": "# Todo 全栈契约",
        "apiRoutes": [
            ArchitectApiRoute(
                method="GET",
                path="/api/v1/todos",
                response="list",
                description="查询待办列表",
            ),
            ArchitectApiRoute(
                method="POST",
                path="/api/v1/todos",
                request="CreateTodo",
                response="Todo",
                description="新建待办",
            ),
            ArchitectApiRoute(
                method="GET",
                path="/api/v1/todos/:id",
                response="Todo",
                description="待办详情",
            ),
            ArchitectApiRoute(
                method="DELETE",
                path="/api/v1/todos/:id",
                description="删除待办",
            ),
        ],
        "databaseDdl": DDL,
        "componentInteractions": [
            ArchitectComponentInteraction(
                component="TodoList",
                event="load",
                api_route="GET /api/v1/todos",
                logic="加载列表并渲染",
            ),
            ArchitectComponentInteraction(
                component="SubmitButton",
                event="click",
                api_route="POST /api/v1/todos",
                logic="提交后刷新列表",
            ),
        ],
    }
    payload.update(overrides)
    return ArchitectContract.model_validate(payload)


class _MemorySandbox(SandboxProvider):
    """只在内存维护文件，并记录 restart / verify 调用。"""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.restarts: list[tuple[str, str]] = []
        self.verified: list[tuple[str, str]] = []
        self._sandbox_id = "sbx_fullstack_test"
        self.handle = SandboxHandle(
            sandbox_id=self._sandbox_id,
            container_id="fake-container",
            container_name="fake-container",
            project_id="proj_fullstack",
            state="running",
            preview_url="http://127.0.0.1:5174",
            host_port=5174,
            internal_port=5173,
            template="react-vite",
            created_at=datetime.now(timezone.utc),
        )

    def create(
        self,
        project_id: str,
        files: dict[str, str],
        template: str = "react-vite",
    ) -> SandboxHandle:
        self.files.update(files)
        return self.handle

    def get(self, sandbox_id: str) -> SandboxHandle:
        return self.handle

    def apply_patch(
        self,
        sandbox_id: str,
        files: dict[str, str] | None = None,
        patch: str | None = None,
        deleted_paths: list[str] | None = None,
    ) -> list[str]:
        incoming = dict(files or {})
        self.files.update(incoming)
        for path in deleted_paths or []:
            self.files.pop(path, None)
        return sorted(incoming)

    def verify(self, sandbox_id: str, check: str = "build") -> VerifyResult:
        self.verified.append((sandbox_id, check))
        return VerifyResult(
            success=True,
            exit_code=0,
            timed_out=False,
            duration_ms=12,
            logs="vite build ok",
            logs_truncated=False,
        )

    def read_file(self, sandbox_id: str, path: str) -> str:
        return self.files[path]

    def restart(self, sandbox_id: str, ready_path: str = "/") -> None:
        self.restarts.append((sandbox_id, ready_path))

    def export_archive(self, sandbox_id: str) -> bytes:
        return b""

    def destroy(self, sandbox_id: str) -> None:
        return None


class CoderTests(unittest.TestCase):
    """确定性代码生成：不依赖 LLM，输出必须可直接写入沙盒。"""

    def test_extract_table_and_columns(self) -> None:
        self.assertEqual(extract_table_name(DDL), "todos")
        columns = extract_columns(DDL)
        self.assertIn("id", columns)
        self.assertIn("tenant_id", columns)
        self.assertIn("name", columns)
        # PRIMARY KEY 等约束关键字不能混入字段列表。
        self.assertNotIn("primary", columns)
        self.assertEqual(extract_table_name("-- 没有 DDL"), "app_records")

    def test_collection_path_prefers_get_collection_route(self) -> None:
        contract = _contract()
        self.assertEqual(collection_path(contract.api_routes), "/api/v1/todos")

    def test_dba_bundle_writes_schema_sql(self) -> None:
        bundle = generate_dba_bundle(_contract())
        self.assertEqual(bundle.metadata["table"], "todos")
        sql = bundle.files["server/schema.sql"]
        self.assertIn("CREATE TABLE todos", sql)
        self.assertIn("MVP 沙盒不常驻 MySQL", sql)

    def test_backend_bundle_generates_zero_dependency_server(self) -> None:
        bundle = generate_backend_bundle(_contract())
        self.assertIn("server/index.mjs", bundle.files)
        self.assertIn("server/package.json", bundle.files)
        code = bundle.files["server/index.mjs"]
        # 真实 REST 行为：健康检查、集合 GET/POST、详情 GET/DELETE、CORS。
        self.assertIn("/api/health", code)
        self.assertIn("/api/v1/todos", code)
        self.assertIn("'POST'", code)
        self.assertIn("'DELETE'", code)
        self.assertIn("Access-Control-Allow-Origin", code)
        self.assertEqual(bundle.metadata["collectionPath"], "/api/v1/todos")

    def test_frontend_bundle_patches_main_vite_and_package(self) -> None:
        main = (
            "import React from 'react'\n"
            "import { createRoot } from 'react-dom/client'\n"
            "import App from './App.jsx'\n"
            "createRoot(document.getElementById('root')).render(\n"
            "  <React.StrictMode><App /></React.StrictMode>,\n"
            ")\n"
        )
        vite = (
            "import { defineConfig } from 'vite'\n"
            "export default defineConfig({\n"
            "  server: {\n"
            "    port: 5173,\n"
            "  },\n"
            "})\n"
        )
        package = '{"scripts": {"dev": "vite"}, "dependencies": {}}'

        bundle = generate_frontend_bundle(
            _contract(),
            existing_main=main,
            existing_vite=vite,
            existing_package=package,
        )

        self.assertIn("src/fullstack-api.jsx", bundle.files)
        self.assertIn("scripts/dev.mjs", bundle.files)
        patched_main = bundle.files["src/main.jsx"]
        self.assertIn("FullstackDataPanel", patched_main)
        self.assertIn("<App /><FullstackDataPanel />", patched_main)
        patched_vite = bundle.files["vite.config.js"]
        self.assertIn("'/api'", patched_vite)
        self.assertIn("127.0.0.1:3100", patched_vite)
        patched_package = bundle.files["package.json"]
        self.assertIn('"dev": "node scripts/dev.mjs"', patched_package)
        self.assertIn('"dev:vite": "vite"', patched_package)

    def test_frontend_bundle_requires_react_main(self) -> None:
        with self.assertRaises(ValueError):
            generate_frontend_bundle(
                _contract(),
                existing_main=None,
                existing_vite="export default {}",
                existing_package="{}",
            )


class GraphTests(unittest.TestCase):
    """FullstackGraph 节点顺序、事件契约与终态。"""

    def test_graph_generates_files_and_restarts_sandbox(self) -> None:
        sandbox = _MemorySandbox()
        sandbox.files = _base_files()
        buffer = RunEventBuffer("fsk_graph_test")
        runner = FullstackGraphRunner(sandbox, event_sink=buffer)

        outcome = runner.run(
            {
                "run_id": "fsk_graph_test",
                "project_id": "proj_fullstack",
                "sandbox_id": sandbox.handle.sandbox_id,
                "preview_url": sandbox.handle.preview_url,
                "contract": _contract(),
                "files": [],
            }
        )

        # 三类产物 + 启动配置全部落盘。
        self.assertIn("server/schema.sql", sandbox.files)
        self.assertIn("server/index.mjs", sandbox.files)
        self.assertIn("src/fullstack-api.jsx", sandbox.files)
        self.assertEqual(sandbox.restarts, [(sandbox.handle.sandbox_id, "/api/health")])
        self.assertEqual(sandbox.verified, [(sandbox.handle.sandbox_id, "build")])
        self.assertEqual(outcome["table"], "todos")
        self.assertEqual(outcome["collection_path"], "/api/v1/todos")
        self.assertIn("server/index.mjs", outcome["backend_files"])

        events = buffer.snapshot()
        node_order = [
            event.payload["node"]
            for event in events
            if event.type == "fullstack.node.started"
        ]
        self.assertEqual(
            node_order,
            [
                "DBACoder",
                "BackendCoder",
                "FrontendCoder",
                "BuildVerifier",
                "SandboxRestart",
            ],
        )
        self.assertEqual(
            [
                event.payload["node"]
                for event in events
                if event.type == "fullstack.node.completed"
            ],
            node_order,
        )
        completed = [
            event for event in events if event.type == "fullstack.node.completed"
        ]
        self.assertTrue(all(event.payload["durationMs"] >= 0 for event in completed))
        # 每个节点的源码输出都通过 node.output 推送，前端据此展示生成日志。
        outputs = [event for event in events if event.type == "fullstack.node.output"]
        self.assertTrue(
            any(event.payload["path"] == "server/index.mjs" for event in outputs)
        )
        self.assertTrue(
            any(event.payload["path"] == "server/schema.sql" for event in outputs)
        )
        # 本次 graph 不发送终态事件，终态由 Service 统一 emit。
        self.assertNotIn("fullstack.completed", [event.type for event in events])

    def test_graph_output_is_truncated(self) -> None:
        sandbox = _MemorySandbox()
        sandbox.files = _base_files()
        buffer = RunEventBuffer("fsk_truncate_test")
        runner = FullstackGraphRunner(sandbox, event_sink=buffer)
        from ai_core.domain.fullstack import FullstackRunOptions

        runner.run(
            {
                "run_id": "fsk_truncate_test",
                "project_id": "proj_fullstack",
                "sandbox_id": sandbox.handle.sandbox_id,
                "preview_url": sandbox.handle.preview_url,
                "contract": _contract(),
                "options": FullstackRunOptions(max_output_chars=500),
                "files": [],
            }
        )
        outputs = [
            event
            for event in buffer.snapshot()
            if event.type == "fullstack.node.output"
        ]
        self.assertTrue(outputs)
        # 至少有一个生成文件超过阈值，证明截断分支确实生效。
        self.assertTrue(any(event.payload["truncated"] for event in outputs))
        for event in outputs:
            self.assertLessEqual(len(event.payload["content"]), 500)
            if event.payload["truncated"]:
                self.assertEqual(len(event.payload["content"]), 500)


class ServiceTests(unittest.TestCase):
    """契约校验、请求解析与后台 Run 的终态事件。"""

    def test_request_accepts_camel_case_and_source_files(self) -> None:
        request = FullstackRunRequest.model_validate(
            {
                "runId": "fsk_request_test",
                "tenantId": "tenant_local",
                "projectId": "proj_fullstack",
                "contractId": "ctr_fullstack_test",
                "sandboxId": "sbx_x",
                "contract": _contract().model_dump(mode="json", by_alias=True),
                "source": {"sandboxId": "sbx_x", "files": {"src/App.jsx": "x"}},
                "options": {"verifyBuild": False, "restartSandbox": False},
            }
        )
        self.assertEqual(request.tenant_id, "tenant_local")
        self.assertEqual(request.contract_id, "ctr_fullstack_test")
        self.assertFalse(request.options.verify_build)
        self.assertFalse(request.options.restart_sandbox)
        self.assertEqual(request.source.sandbox_id, "sbx_x")

    def test_rejects_unconfirmed_contract(self) -> None:
        service = FullstackService(sandbox=_MemorySandbox())
        request = FullstackRunRequest.model_validate(
            {
                "projectId": "proj_fullstack",
                "contractId": "ctr_fullstack_test",
                "contract": _contract(status="AWAITING_CONFIRMATION").model_dump(
                    mode="json", by_alias=True
                ),
                "source": {"files": _base_files()},
            }
        )
        with self.assertRaises(Exception) as ctx:
            service.start_stream(request)
        self.assertEqual(ctx.exception.code, "FULLSTACK_CONTRACT_NOT_CONFIRMED")

    def test_rejects_non_fullstack_shape(self) -> None:
        service = FullstackService(sandbox=_MemorySandbox())
        request = FullstackRunRequest.model_validate(
            {
                "projectId": "proj_fullstack",
                "contractId": "ctr_fullstack_test",
                "contract": _contract(appShape="lightweight_web").model_dump(
                    mode="json", by_alias=True
                ),
                "source": {"files": _base_files()},
            }
        )
        with self.assertRaises(Exception) as ctx:
            service.start_stream(request)
        self.assertEqual(ctx.exception.code, "FULLSTACK_CONTRACT_INVALID")

    def test_run_emits_terminal_completed_and_updates_snapshot(self) -> None:
        sandbox = _MemorySandbox()
        service = FullstackService(sandbox=sandbox)
        run_id = f"fsk_{uuid.uuid4().hex[:12]}"
        request = FullstackRunRequest.model_validate(
            {
                "runId": run_id,
                "projectId": "proj_fullstack",
                "contractId": "ctr_fullstack_test",
                "contract": _contract().model_dump(mode="json", by_alias=True),
                "source": {"files": _base_files()},
            }
        )
        active = service.start_stream(request)
        self.assertEqual(active.sandbox_id, sandbox.handle.sandbox_id)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            snapshot = service.snapshot(run_id)
            if snapshot.status in {"SUCCEEDED", "FAILED"}:
                break
            time.sleep(0.02)

        snapshot = service.snapshot(run_id)
        self.assertEqual(snapshot.status, "SUCCEEDED", snapshot.error)
        self.assertIn("server/index.mjs", snapshot.files)
        events = active.buffer.snapshot()
        types = [event.type for event in events]
        self.assertIn("fullstack.started", types)
        self.assertIn("fullstack.completed", types)
        self.assertIn("fullstack.completed", TERMINAL_EVENT_TYPES)
        completed = next(
            event for event in events if event.type == "fullstack.completed"
        )
        self.assertEqual(completed.payload["status"], "SUCCEEDED")
        self.assertEqual(completed.payload["sandboxId"], sandbox.handle.sandbox_id)
        self.assertEqual(completed.payload["collectionPath"], "/api/v1/todos")
        self.assertIn("server/index.mjs", completed.payload["backendFiles"])
        self.assertIn("server/schema.sql", completed.payload["dbaFiles"])
        self.assertTrue(completed.payload["previewUrl"].startswith("http://127.0.0.1"))
        self.assertEqual(sandbox.restarts[-1][1], "/api/health")


def _base_files() -> dict[str, str]:
    return {
        "src/main.jsx": (
            "import React from 'react'\n"
            "import { createRoot } from 'react-dom/client'\n"
            "import App from './App.jsx'\n"
            "createRoot(document.getElementById('root')).render(<App />)\n"
        ),
        "vite.config.js": (
            "import { defineConfig } from 'vite'\n"
            "export default defineConfig({ server: { port: 5173 } })\n"
        ),
        "package.json": '{"name": "sandbox", "scripts": {"dev": "vite"}}',
        "src/App.jsx": "export default function App() { return <main>Todo</main> }",
    }


if __name__ == "__main__":
    unittest.main()
