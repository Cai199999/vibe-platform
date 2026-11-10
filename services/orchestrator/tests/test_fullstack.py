"""FullstackGraph：契约解析、代码生成、SSE 事件与沙盒重启测试。

运行：

    cd services/orchestrator
    PYTHONPATH=. python -m unittest tests.test_fullstack -v

全部使用内存 Fake Sandbox，不依赖 Docker / 模型 / MySQL，可在 CI 中运行。
"""

from __future__ import annotations

import json
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
from ai_core.domain.fullstack import FullstackRunOptions, FullstackRunRequest
from ai_core.fullstack.coders import (
    collection_path,
    contract_database_engine,
    extract_column_defs,
    extract_columns,
    extract_table_name,
    generate_backend_bundle,
    generate_dba_bundle,
    generate_frontend_bundle,
    generate_frontend_skeleton_bundle,
    generate_infra_bundle,
    item_path,
)
from ai_core.fullstack.graph import FullstackConsistencyError, FullstackGraphRunner
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
        "techStack": {
            "backend": "node_express",
            "database": "mysql",
            "databaseEngine": "mysql",
            "orm": "sequelize",
        },
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


def _frontend_contract(**overrides) -> ArchitectContract:
    """对话前置模式的轻量前端契约：没有后端与 DDL，但有页面结构。"""
    payload = {
        "appShape": "lightweight_web",
        "techStack": {"frontend": "react"},
        "apiRoutes": [],
        "databaseDdl": "",
        "componentInteractions": [],
        "frontendRoutes": [
            {"path": "/", "name": "任务总览", "description": "展示全部任务"},
            {"path": "/board", "name": "看板", "description": "按状态分组"},
        ],
        "pageLayout": [
            {"region": "header", "description": "品牌与导航"},
            {"region": "main", "description": "任务看板工作区"},
        ],
        "components": [
            {"name": "TaskBoard", "description": "任务看板容器"},
            {"name": "TaskCard", "description": "单条任务卡片"},
            {"name": "StatusSummary", "description": "状态统计"},
        ],
    }
    payload.update(overrides)
    return _contract(**payload)


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

        defs = dict(extract_column_defs(DDL))
        self.assertEqual(defs["id"], "INTEGER")
        self.assertEqual(defs["name"], "TEXT")
        self.assertEqual(defs["payload"], "TEXT")
        # 契约没有 id 时自动补 SQLite 自增主键。
        self.assertEqual(extract_column_defs("CREATE TABLE t (title TEXT)")[0][0], "id")

    def test_collection_path_prefers_get_collection_route(self) -> None:
        contract = _contract()
        self.assertEqual(collection_path(contract.api_routes), "/api/v1/todos")

    def test_collection_path_never_uses_parameterized_stream_route(self) -> None:
        """回归：``/api/v1/tasks/{task_id}/stream`` 不是集合路由。

        旧逻辑只排除“参数在末尾”的路径，导致 SSE 路由被注入前端，
        浏览器请求 ``/api/v1/tasks/{task_id}/stream``，Express 却把
        花括号当普通字符注册，最终 404。前端必须拿到静态集合根路径。
        """
        routes = [
            ArchitectApiRoute(
                method="POST", path="/api/v1/resumes", description="创建简历"
            ),
            ArchitectApiRoute(
                method="GET", path="/api/v1/resumes/{resume_id}", description="详情"
            ),
            ArchitectApiRoute(
                method="GET", path="/api/v1/tasks/{task_id}/stream", description="SSE"
            ),
            ArchitectApiRoute(
                method="GET", path="/api/v1/tasks/{task_id}", description="任务详情"
            ),
        ]
        self.assertEqual(collection_path(routes), "/api/v1/resumes")
        self.assertEqual(
            item_path(routes, "/api/v1/resumes"), "/api/v1/resumes/{resume_id}"
        )

        # 契约只有参数化 / 流式路由时，也要推导出静态资源根路径，
        # 而不是把带花括号的 URL 直接交给前端。
        stream_only = [
            ArchitectApiRoute(
                method="GET", path="/api/v1/tasks/{task_id}/stream", description="SSE"
            )
        ]
        self.assertEqual(collection_path(stream_only), "/api/v1/tasks")
        self.assertEqual(item_path(stream_only, "/api/v1/tasks"), "/api/v1/tasks/:id")

    def test_backend_and_frontend_share_one_express_route(self) -> None:
        """Task 3 校验：前后端集合路由必须逐字符一致且无花括号参数。"""
        contract = _contract(
            apiRoutes=[
                ArchitectApiRoute(
                    method="POST", path="/api/v1/resumes", description="创建简历"
                ),
                ArchitectApiRoute(
                    method="GET",
                    path="/api/v1/resumes/{resume_id}",
                    description="简历详情",
                ),
                ArchitectApiRoute(
                    method="GET",
                    path="/api/v1/tasks/{task_id}/stream",
                    description="任务 SSE",
                ),
            ]
        )
        backend = generate_backend_bundle(contract)
        collection = backend.metadata["collectionPath"]
        self.assertEqual(collection, "/api/v1/resumes")
        self.assertNotIn("{", collection)
        self.assertIn(
            f"const COLLECTION_PATH = '{collection}'", backend.files["server/index.js"]
        )
        self.assertIn("server/api-routes.json", backend.files)
        self.assertIn(
            '"collection": "/api/v1/resumes"', backend.files["server/api-routes.json"]
        )

        main = "import React from 'react'\nimport App from './App.jsx'\n<App />"
        vite = "export default { server: { port: 5173 } }"
        frontend = generate_frontend_bundle(
            contract,
            existing_main=main,
            existing_vite=vite,
            existing_package="{}",
        )
        panel = frontend.files["src/fullstack-api.jsx"]
        self.assertIn(f"const COLLECTION_PATH = '{collection}'", panel)
        self.assertNotIn("/stream", panel)

    def test_frontend_panel_payload_key_follows_ddl(self) -> None:
        """数据面板必须提交 DDL 中真实存在的可写列。"""
        contract = _contract(
            databaseDdl=(
                "CREATE TABLE articles (\n"
                "  id BIGINT PRIMARY KEY AUTO_INCREMENT,\n"
                "  title VARCHAR(200) NOT NULL,\n"
                "  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP\n"
                ");"
            )
        )
        frontend = generate_frontend_bundle(
            contract,
            existing_main="import App from './App.jsx'\n<App />",
            existing_vite="export default { server: { port: 5173 } }",
            existing_package="{}",
        )
        panel = frontend.files["src/fullstack-api.jsx"]
        self.assertIn("title: name.trim()", panel)
        self.assertNotIn("name: name.trim()", panel)

    def test_contract_database_engine_normalizes_and_defaults(self) -> None:
        self.assertEqual(contract_database_engine(_contract()), "mysql")
        postgres = _contract(
            techStack={"backend": "node_express", "database": "PostgreSQL 16"}
        )
        self.assertEqual(postgres.database_engine, "postgresql")
        self.assertEqual(contract_database_engine(postgres), "postgresql")
        sqlite = _contract(techStack={"backend": "node_express", "database": "SQLite"})
        self.assertEqual(contract_database_engine(sqlite), "sqlite")

    def test_dba_bundle_writes_sqlite_preview_and_target_schema(self) -> None:
        bundle = generate_dba_bundle(_contract())
        self.assertEqual(bundle.metadata["table"], "todos")
        self.assertEqual(bundle.metadata["databaseEngine"], "mysql")
        self.assertEqual(bundle.metadata["orm"], "sequelize")
        sql = bundle.files["server/schema.sql"]
        self.assertIn('CREATE TABLE IF NOT EXISTS "todos"', sql)
        self.assertIn('"payload" TEXT', sql)
        mysql = bundle.files["server/schema.mysql.sql"]
        self.assertIn("CREATE TABLE todos", mysql)
        db_code = bundle.files["server/db.js"]
        # 生产 ORM 与沙盒回退同时存在。
        self.assertIn("node:sqlite", db_code)
        self.assertIn("process.env.DATABASE_URL", db_code)
        self.assertIn("import('sequelize')", db_code)
        self.assertIn("findAndCountAll", db_code)
        self.assertIn("server/migrate.mjs", bundle.files)

        postgres = generate_dba_bundle(
            _contract(techStack={"backend": "node_express", "database": "postgresql"})
        )
        self.assertIn("server/schema.postgresql.sql", postgres.files)
        self.assertIn(
            "dialect = STORAGE === 'postgresql'", postgres.files["server/db.js"]
        )

    def test_backend_bundle_generates_express_sequelize_server(self) -> None:
        bundle = generate_backend_bundle(_contract())
        self.assertIn("server/index.js", bundle.files)
        self.assertIn("server/package.json", bundle.files)
        code = bundle.files["server/index.js"]
        # 真实 REST 行为：健康检查、集合 GET/POST、详情 GET/DELETE、CORS。
        self.assertIn("from 'express'", code)
        self.assertIn("/api/health", code)
        self.assertIn("/api/data", code)
        self.assertIn("/api/v1/todos", code)
        self.assertIn("Access-Control-Allow-Origin", code)
        self.assertIn("process.env.PORT", code)
        self.assertIn("process.env.CORS_ORIGIN", code)
        self.assertEqual(bundle.metadata["collectionPath"], "/api/v1/todos")
        self.assertEqual(bundle.metadata["runtime"], "express-sequelize")
        self.assertEqual(bundle.metadata["databaseEngine"], "mysql")
        package = bundle.files["server/package.json"]
        self.assertIn('"sequelize"', package)
        self.assertIn('"mysql2"', package)
        self.assertIn('"pg"', package)
        self.assertIn('"db:migrate"', package)

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
        panel = bundle.files["src/fullstack-api.jsx"]
        self.assertIn("data-fullstack-success", panel)
        self.assertIn("提交成功", panel)
        # 生产前后端分域部署：面板必须支持 VITE_API_BASE_URL。
        self.assertIn("VITE_API_BASE_URL", panel)
        self.assertIn("apiUrl(COLLECTION_PATH)", panel)
        patched_vite = bundle.files["vite.config.js"]
        self.assertIn("'/api'", patched_vite)
        self.assertIn("127.0.0.1:3000", patched_vite)
        patched_package = bundle.files["package.json"]
        self.assertIn('"dev": "node scripts/dev.mjs"', patched_package)
        self.assertIn('"dev:vite": "vite"', patched_package)
        self.assertIn("server/index.js", bundle.files["scripts/dev.mjs"])

    def test_frontend_bundle_requires_react_main(self) -> None:
        with self.assertRaises(ValueError):
            generate_frontend_bundle(
                _contract(),
                existing_main=None,
                existing_vite="export default {}",
                existing_package="{}",
            )

    def test_frontend_skeleton_renders_contract_routes_and_components(self) -> None:
        """对话前置：契约中的路由 / 布局 / 组件必须落到真实 App.jsx。"""
        bundle = generate_frontend_skeleton_bundle(_frontend_contract())
        self.assertEqual(set(bundle.files), {"src/App.jsx"})
        app = bundle.files["src/App.jsx"]
        self.assertIn("任务总览", app)
        self.assertIn("/board", app)
        self.assertIn("TaskBoard", app)
        self.assertIn("TaskCard", app)
        self.assertIn("品牌与导航", app)
        # 生成页面必须带 data-source 标记，供后续画布标注定位。
        self.assertIn('data-source="app-header"', app)
        self.assertIn("data-source-kind", app)
        self.assertEqual(bundle.metadata["mode"], "frontend")
        self.assertEqual(bundle.metadata["routes"], 2)
        self.assertEqual(bundle.metadata["components"], 3)
        # 侧边栏点击必须切换每个路由的独立内容，而不是共用同一块静态 DOM。
        self.assertIn("const routeComponents = useMemo", app)
        self.assertIn("activeComponents", app)
        self.assertIn("data-route-path", app)
        self.assertIn("routePaths[hash % routePaths.length]", app)

    def test_frontend_skeleton_generates_real_interactions(self) -> None:
        """已确认交互必须生成 onClick + fetch，并带 Loading / Error 三态。"""
        contract = _contract(
            contractId="ctr_interaction_test",
            components=[
                {"name": "TodoList", "section": "核心工作区"},
                {"name": "SubmitButton", "role": "button", "section": "核心工作区"},
                {
                    "name": "PendingArea",
                    "section": "主内容区",
                    "isPlaceholder": True,
                },
            ],
            componentInteractions=[
                ArchitectComponentInteraction(
                    component="TodoList",
                    event="load",
                    api_route="GET /api/v1/todos",
                    logic="加载列表并渲染",
                ),
                ArchitectComponentInteraction(
                    component="SubmitButton",
                    event="click",
                    api_route="/api/v1/todos",
                    logic="提交后保存到数据库并刷新列表",
                ),
                # 占位组件的交互必须被丢弃，不能生成假按钮。
                ArchitectComponentInteraction(
                    component="PendingArea",
                    event="click",
                    api_route="/api/v1/todos",
                    logic="需求未确认",
                ),
            ],
        )
        bundle = generate_frontend_skeleton_bundle(contract)
        app = bundle.files["src/App.jsx"]

        self.assertEqual(bundle.metadata["interactions"], 2)
        self.assertIn("function InteractionPanel", app)
        self.assertIn('"method": "GET"', app)
        self.assertIn('"method": "POST"', app)
        self.assertIn('"apiRoute": "/api/v1/todos"', app)
        self.assertIn('"payloadKey": "name"', app)
        self.assertIn("data-interaction-run", app)
        self.assertIn("处理中…", app)
        self.assertIn('role="alert"', app)
        self.assertIn("重试", app)
        self.assertNotIn('"component": "PendingArea"', app)

    def test_frontend_skeleton_without_interactions_stays_static(self) -> None:
        """没有交互的契约只渲染静态卡片，不伪造 onClick。"""
        contract = _frontend_contract(
            contractId="ctr_static_skeleton",
            components=[{"name": "StaticPanel", "section": "主内容区"}],
            componentInteractions=[],
        )
        bundle = generate_frontend_skeleton_bundle(contract)
        app = bundle.files["src/App.jsx"]

        self.assertEqual(bundle.metadata["interactions"], 0)
        self.assertIn("const INTERACTIONS = []", app)
        self.assertIn("StaticPanel", app)
        # InteractionPanel 渲染函数始终随模板输出，这里断言没有注入任何交互数据，
        # 且卡片分支走静态占位，不会伪造按钮 / onClick。
        self.assertNotIn('"apiRoute"', app)
        self.assertNotIn('"componentId"', app)

    def test_skeleton_mode_renders_clean_shell_and_single_placeholder(self) -> None:
        """骨架模式必须只渲染 Header + Sidebar + Main 与一个占位组件。"""
        contract = _frontend_contract(
            contractId="ctr_skeleton_test",
            skeletonMode=True,
            frontendRoutes=[
                {"path": "/", "name": "工作台", "description": "空白工作区"}
            ],
            pageLayout=[
                {"region": "header", "title": "顶部导航"},
                {"region": "sidebar", "title": "侧边栏"},
                {"region": "main", "title": "主内容区"},
            ],
            components=[
                {
                    "name": "待定区域",
                    "role": "placeholder",
                    "section": "主内容区",
                    "description": "需求未明确",
                    "isPlaceholder": True,
                }
            ],
        )
        bundle = generate_frontend_skeleton_bundle(contract)
        app = bundle.files["src/App.jsx"]

        self.assertTrue(bundle.metadata["skeletonMode"])
        self.assertIn("SKELETON_MODE = true", app)
        self.assertIn("Welcome to your new app", app)
        self.assertIn('data-source="app-sidebar"', app)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", app)
        self.assertIn("min-height: 280px", app)
        self.assertIn("overflow-y: auto", app)
        self.assertIn("isPlaceholder", app)

    def test_skeleton_mode_filters_hallucinated_business_components(self) -> None:
        """即使历史契约混入业务组件，骨架模式也只输出一个占位组件。"""
        contract = _frontend_contract(
            contractId="ctr_skeleton_guard",
            skeletonMode=True,
            frontendRoutes=[{"path": "/", "name": "工作台"}],
            pageLayout=[
                {"region": "header", "title": "顶部导航"},
                {"region": "sidebar", "title": "侧边栏"},
                {"region": "main", "title": "主内容区"},
            ],
            components=[
                {"name": "LoginForm", "role": "form"},
                {"name": "ResumeUploader", "role": "upload"},
                {"name": "JDdetail", "role": "detail"},
            ],
        )
        bundle = generate_frontend_skeleton_bundle(contract)
        app = bundle.files["src/App.jsx"]

        self.assertEqual(bundle.metadata["components"], 1)
        self.assertNotIn("LoginForm", app)
        self.assertNotIn("ResumeUploader", app)
        self.assertNotIn("JDdetail", app)
        self.assertIn("待定区域", app)

    def test_infra_bundle_generates_production_deployment_files(self) -> None:
        bundle = generate_infra_bundle(_contract())
        self.assertEqual(
            set(bundle.files),
            {
                "Dockerfile",
                ".dockerignore",
                "render.yaml",
                ".env.example",
                "DEPLOY.md",
            },
        )
        dockerfile = bundle.files["Dockerfile"]
        self.assertIn("FROM node:22-alpine", dockerfile)
        self.assertIn("COPY server/package.json", dockerfile)
        self.assertIn('CMD ["node", "index.js"]', dockerfile)
        render = bundle.files["render.yaml"]
        self.assertIn("runtime: docker", render)
        self.assertIn("runtime: static", render)
        self.assertIn("DATABASE_URL", render)
        self.assertIn("/api/health", render)
        env = bundle.files[".env.example"]
        self.assertIn("DATABASE_URL=", env)
        self.assertIn("DB_ENGINE=mysql", env)
        self.assertIn("PORT=3000", env)
        self.assertIn("VITE_API_BASE_URL=", env)
        deploy = bundle.files["DEPLOY.md"]
        self.assertIn("Vercel", deploy)
        self.assertIn("Railway", deploy)
        self.assertIn("MySQL 8", deploy)
        self.assertEqual(bundle.metadata["databaseEngine"], "mysql")


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
        self.assertIn("server/db.js", sandbox.files)
        self.assertIn("server/index.js", sandbox.files)
        self.assertIn("src/fullstack-api.jsx", sandbox.files)
        self.assertIn("Dockerfile", sandbox.files)
        self.assertIn("render.yaml", sandbox.files)
        self.assertIn(".env.example", sandbox.files)
        self.assertEqual(sandbox.restarts, [(sandbox.handle.sandbox_id, "/api/health")])
        self.assertEqual(sandbox.verified, [(sandbox.handle.sandbox_id, "build")])
        self.assertEqual(outcome["table"], "todos")
        self.assertEqual(outcome["collection_path"], "/api/v1/todos")
        self.assertIn("server/index.js", outcome["backend_files"])
        self.assertIn("Dockerfile", outcome["infra_files"])

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
                "InfraCoder",
                "ConsistencyVerifier",
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
            any(event.payload["path"] == "server/index.js" for event in outputs)
        )
        self.assertTrue(
            any(event.payload["path"] == "server/schema.sql" for event in outputs)
        )
        # 本次 graph 不发送终态事件，终态由 Service 统一 emit。
        self.assertNotIn("fullstack.completed", [event.type for event in events])

    def test_consistency_failure_self_heals_by_backend_routes(self) -> None:
        """前端请求缺失路由时，BackendCoder 自动补齐后继续构建。

        旧行为是直接阻断，并把“聊天 / 投递”建议成无关的 CRUD 路径；
        新行为必须保持前端业务请求不变，由后端补语义匹配的端点。
        """
        sandbox = _MemorySandbox()
        sandbox.files = _base_files()
        sandbox.files["src/App.jsx"] = (
            "const INTERACTIONS = "
            + json.dumps(
                [
                    {
                        "component": "TaskStream",
                        "method": "GET",
                        # 前端订阅长连接路径，初始后端只注册了 /api/v1/todos。
                        "apiRoute": "/api/v1/tasks/stream",
                        "payloadKey": "",
                    }
                ],
                ensure_ascii=False,
            )
            + "\nexport default function App() { return <main /> }\n"
        )
        buffer = RunEventBuffer("fsk_consistency_self_heal")
        runner = FullstackGraphRunner(sandbox, event_sink=buffer)

        runner.run(
            {
                "run_id": "fsk_consistency_self_heal",
                "project_id": "proj_fullstack",
                "sandbox_id": sandbox.handle.sandbox_id,
                "preview_url": sandbox.handle.preview_url,
                "contract": _contract(),
                "files": [],
            }
        )

        # 补齐后通过一致性检查，才允许构建与重启。
        self.assertEqual(sandbox.verified, [(sandbox.handle.sandbox_id, "build")])
        self.assertEqual(sandbox.restarts, [(sandbox.handle.sandbox_id, "/api/health")])
        manifest = json.loads(sandbox.files["server/api-routes.json"])
        registered = {(item["method"], item["path"]) for item in manifest["routes"]}
        self.assertIn(("GET", "/api/v1/tasks/stream"), registered)
        self.assertIn(
            'app.get("/api/v1/tasks/stream"', sandbox.files["server/index.js"]
        )

        events = buffer.snapshot()
        backend_starts = [
            event
            for event in events
            if event.type == "fullstack.node.started"
            and event.payload["node"] == "BackendCoder"
        ]
        self.assertEqual(len(backend_starts), 2)
        reports = [
            event for event in events if event.type == "fullstack.consistency.report"
        ]
        self.assertFalse(reports[0].payload["ok"])
        self.assertTrue(reports[-1].payload["ok"])
        repairs = [
            event for event in events if event.type == "fullstack.consistency.repair"
        ]
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0].payload["attempt"], 1)
        self.assertEqual(
            repairs[0].payload["missingRoutes"],
            [
                {
                    "method": "GET",
                    "path": "/api/v1/tasks/stream",
                    "description": (
                        "TaskStream：后端未注册该路径" "（Method + Path 完全匹配失败）"
                    ),
                }
            ],
        )
        self.assertFalse(
            any(
                event.type == "fullstack.node.failed"
                and event.payload["node"] == "ConsistencyVerifier"
                for event in events
            )
        )

    def test_consistency_retries_at_most_twice_then_fails(self) -> None:
        """自愈最多 2 次；仍不一致时在一致性格点终止，不进入构建。"""
        sandbox = _MemorySandbox()
        sandbox.files = _base_files()
        sandbox.files["src/App.jsx"] = (
            "const INTERACTIONS = "
            + json.dumps(
                [
                    {
                        "component": "TaskStream",
                        "method": "GET",
                        "apiRoute": "/api/v1/tasks/stream",
                        "payloadKey": "",
                    }
                ],
                ensure_ascii=False,
            )
            + "\nexport default function App() { return <main /> }\n"
        )
        buffer = RunEventBuffer("fsk_consistency_retry_limit")
        runner = FullstackGraphRunner(sandbox, event_sink=buffer)

        from ai_core.fullstack import graph as graph_module
        from ai_core.fullstack.consistency import (
            BackendRoute,
            ConsistencyIssue,
            ConsistencyReport,
        )

        failing_report = ConsistencyReport(
            calls_checked=1,
            backend_routes=(BackendRoute("GET", "/api/v1/todos"),),
            issues=(
                ConsistencyIssue(
                    kind="path",
                    component="TaskStream",
                    method="GET",
                    path="/api/v1/tasks/stream",
                    message="后端未注册该路径（Method + Path 完全匹配失败）",
                    suggestion="由 BackendCoder 补齐",
                    fix_action="ADD_BACKEND_ROUTE",
                ),
            ),
        )
        original = graph_module.verify_consistency
        graph_module.verify_consistency = lambda *_args, **_kwargs: failing_report
        try:
            with self.assertRaises(FullstackConsistencyError) as ctx:
                runner.run(
                    {
                        "run_id": "fsk_consistency_retry_limit",
                        "project_id": "proj_fullstack",
                        "sandbox_id": sandbox.handle.sandbox_id,
                        "preview_url": sandbox.handle.preview_url,
                        "contract": _contract(),
                        "files": [],
                    }
                )
        finally:
            graph_module.verify_consistency = original

        self.assertEqual(ctx.exception.node, "ConsistencyVerifier")
        self.assertEqual(sandbox.verified, [])
        self.assertEqual(sandbox.restarts, [])
        events = buffer.snapshot()
        backend_starts = [
            event
            for event in events
            if event.type == "fullstack.node.started"
            and event.payload["node"] == "BackendCoder"
        ]
        # 初始生成 1 次 + 自愈 2 次。
        self.assertEqual(len(backend_starts), 3)
        repairs = [
            event for event in events if event.type == "fullstack.consistency.repair"
        ]
        self.assertEqual([event.payload["attempt"] for event in repairs], [1, 2])

    def test_resume_assistant_business_actions_are_all_registered(self) -> None:
        """回归用户用例：抓取 / 改写 / 投递 / 对话四个业务动作必须独立对齐。"""
        sandbox = _MemorySandbox()
        sandbox.files = _base_files()
        contract = _contract(
            apiRoutes=[
                ArchitectApiRoute(
                    method="POST",
                    path="/api/v1/jobs/scrape",
                    description="抓取 JD",
                ),
                ArchitectApiRoute(
                    method="POST",
                    path="/api/v1/resumes/{id}/rewrite",
                    description="AI 改写简历",
                ),
                ArchitectApiRoute(
                    method="POST",
                    path="/api/v1/applications/deliver",
                    description="投递职位",
                ),
                ArchitectApiRoute(
                    method="POST",
                    path="/api/v1/chat/messages",
                    description="对话消息",
                ),
                ArchitectApiRoute(
                    method="POST",
                    path="/api/v1/resumes",
                    description="创建简历",
                ),
            ],
            databaseDdl=(
                "CREATE TABLE resumes (\n"
                "  id BIGINT PRIMARY KEY AUTO_INCREMENT,\n"
                "  name VARCHAR(200) NOT NULL,\n"
                "  status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',\n"
                "  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP\n"
                ");"
            ),
            components=[
                {"name": "ScrapeButton"},
                {"name": "RewriteButton"},
                {"name": "DeliverButton"},
                {"name": "ChatPanel"},
            ],
            componentInteractions=[
                ArchitectComponentInteraction(
                    component="ScrapeButton",
                    event="click",
                    api_route="POST /api/v1/jobs/scrape",
                    logic="抓取 JD",
                ),
                ArchitectComponentInteraction(
                    component="RewriteButton",
                    event="click",
                    api_route="POST /api/v1/resumes/{id}/rewrite",
                    logic="AI 改写简历",
                ),
                ArchitectComponentInteraction(
                    component="DeliverButton",
                    event="click",
                    api_route="POST /api/v1/applications/deliver",
                    logic="投递职位",
                ),
                ArchitectComponentInteraction(
                    component="ChatPanel",
                    event="click",
                    api_route="POST /api/v1/chat/messages",
                    logic="对话消息",
                ),
            ],
        )
        # 模拟真实项目流：契约确认后先由 FrontendCoder 生成含 INTERACTIONS 的
        # App.jsx，再做全栈生成。FrontendCoder（fullstack 模式）只挂数据面板，
        # 不会覆盖已有页面，因此这里必须预置真实前端骨架。
        sandbox.files["src/App.jsx"] = generate_frontend_skeleton_bundle(
            contract
        ).files["src/App.jsx"]
        buffer = RunEventBuffer("fsk_resume_assistant")
        runner = FullstackGraphRunner(sandbox, event_sink=buffer)

        runner.run(
            {
                "run_id": "fsk_resume_assistant",
                "project_id": "proj_resume",
                "sandbox_id": sandbox.handle.sandbox_id,
                "preview_url": sandbox.handle.preview_url,
                "contract": contract,
                "files": [],
            }
        )

        manifest = json.loads(sandbox.files["server/api-routes.json"])
        registered = {(item["method"], item["path"]) for item in manifest["routes"]}
        expected = {
            ("POST", "/api/v1/jobs/scrape"),
            ("POST", "/api/v1/resumes/:id/rewrite"),
            ("POST", "/api/v1/applications/deliver"),
            ("POST", "/api/v1/chat/messages"),
            ("POST", "/api/v1/resumes"),
        }
        self.assertTrue(expected.issubset(registered))
        code = sandbox.files["server/index.js"]
        for method, path in expected - {("POST", "/api/v1/resumes")}:
            self.assertIn(f'app.{method.lower()}("{path}"', code)
        # 前端 App.jsx 中四个业务请求与后端注册路径逐字符一致。
        app = sandbox.files["src/App.jsx"]
        for path in (
            "/api/v1/jobs/scrape",
            "/api/v1/resumes/:id/rewrite",
            "/api/v1/applications/deliver",
            "/api/v1/chat/messages",
        ):
            self.assertIn(path, app)
        events = buffer.snapshot()
        reports = [
            event for event in events if event.type == "fullstack.consistency.report"
        ]
        self.assertTrue(reports[0].payload["ok"], reports[0].payload["summary"])
        self.assertFalse(
            any(event.type == "fullstack.consistency.repair" for event in events)
        )
        self.assertEqual(sandbox.verified, [(sandbox.handle.sandbox_id, "build")])
        self.assertEqual(sandbox.restarts, [(sandbox.handle.sandbox_id, "/api/health")])

    def test_frontend_mode_runs_three_nodes_and_probes_root(self) -> None:
        """契约确认后的前端初始化只生成 App.jsx，不写后端 / 部署文件。"""
        sandbox = _MemorySandbox()
        buffer = RunEventBuffer("fsk_frontend_graph")
        runner = FullstackGraphRunner(sandbox, event_sink=buffer)

        outcome = runner.run(
            {
                "run_id": "fsk_frontend_graph",
                "project_id": "proj_frontend",
                "sandbox_id": sandbox.handle.sandbox_id,
                "preview_url": sandbox.handle.preview_url,
                "contract": _frontend_contract(),
                "options": FullstackRunOptions(mode="frontend"),
                "files": [],
            }
        )

        self.assertIn("src/App.jsx", sandbox.files)
        self.assertNotIn("server/index.js", sandbox.files)
        self.assertNotIn("Dockerfile", sandbox.files)
        self.assertIn("TaskBoard", outcome["frontend_entry"])
        self.assertEqual(sandbox.restarts, [(sandbox.handle.sandbox_id, "/")])
        self.assertEqual(sandbox.verified, [(sandbox.handle.sandbox_id, "build")])
        node_order = [
            event.payload["node"]
            for event in buffer.snapshot()
            if event.type == "fullstack.node.started"
        ]
        self.assertEqual(
            node_order, ["FrontendCoder", "BuildVerifier", "SandboxRestart"]
        )
        started = next(
            event
            for event in buffer.snapshot()
            if event.type == "fullstack.node.started"
        )
        self.assertEqual(started.payload["total"], 3)

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

    def test_frontend_bootstrap_accepts_empty_source_and_returns_entry(self) -> None:
        """新项目没有源码：服务端必须能从空 files 建沙盒并回传真实 App.jsx。"""
        sandbox = _MemorySandbox()
        service = FullstackService(sandbox=sandbox)
        run_id = f"fsk_{uuid.uuid4().hex[:12]}"
        request = FullstackRunRequest.model_validate(
            {
                "runId": run_id,
                "projectId": "proj_frontend",
                "contractId": "ctr_fullstack_test",
                "contract": _frontend_contract().model_dump(mode="json", by_alias=True),
                "source": {"files": {}},
                "options": {"mode": "frontend"},
            }
        )
        active = service.start_stream(request)
        self.assertEqual(active.request.options.mode, "frontend")

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            snapshot = service.snapshot(run_id)
            if snapshot.status in {"SUCCEEDED", "FAILED"}:
                break
            time.sleep(0.02)

        snapshot = service.snapshot(run_id)
        self.assertEqual(snapshot.status, "SUCCEEDED", snapshot.error)
        self.assertIn("src/App.jsx", snapshot.files)
        completed = next(
            event
            for event in active.buffer.snapshot()
            if event.type == "fullstack.completed"
        )
        self.assertEqual(completed.payload["mode"], "frontend")
        self.assertIn("TaskBoard", completed.payload["frontendEntry"])
        self.assertEqual(sandbox.restarts[-1][1], "/")

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
        self.assertIn("server/index.js", snapshot.files)
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
        self.assertIn("server/index.js", completed.payload["backendFiles"])
        self.assertIn("server/schema.sql", completed.payload["dbaFiles"])
        self.assertIn("Dockerfile", completed.payload["infraFiles"])
        self.assertEqual(completed.payload["databaseEngine"], "mysql")
        self.assertEqual(completed.payload["orm"], "sequelize")
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
