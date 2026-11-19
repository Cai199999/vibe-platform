"""FullstackGraph：契约解析、代码生成、SSE 事件与沙盒重启测试。

运行：

    cd services/orchestrator
    PYTHONPATH=. python -m unittest tests.test_fullstack -v

全部使用内存 Fake Sandbox，不依赖 Docker / 模型 / MySQL，可在 CI 中运行。
"""

from __future__ import annotations

import json
import sys
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ai_core.agent.events import TERMINAL_EVENT_TYPES, RunEventBuffer
from ai_core.config import Settings
from ai_core.domain.architect import (
    ArchitectApiRoute,
    ArchitectComponentInteraction,
    ArchitectContract,
    ArchitectTableRelation,
)
from ai_core.domain.fullstack import FullstackRunOptions, FullstackRunRequest
from ai_core.fullstack.coders import (
    GeneratedBundle,
    check_orm_schema,
    collection_path,
    contract_database_engine,
    core_table_for_contract,
    extract_column_defs,
    extract_columns,
    extract_table_name,
    generate_backend_bundle,
    generate_dba_bundle,
    generate_frontend_bundle,
    generate_frontend_skeleton_bundle,
    generate_infra_bundle,
    item_path,
    parse_ddl_tables,
)
from ai_core.fullstack.applier import Applier, ApplierSafetyBlock
from ai_core.fullstack.consistency import verify_consistency
from ai_core.fullstack.diagnostics import OrmDiagnostic, OrmSyntaxReport
from ai_core.fullstack.graph import (
    FullstackConsistencyError,
    FullstackContractContradictionError,
    FullstackGraphRunner,
)
from ai_core.fullstack.mysql_schema import MysqlMigrationCheck
from ai_core.fullstack.quality import (
    L1_WARNING_TEXT,
    L1_PROTOTYPE,
    L2_STANDARD,
    L3_PRODUCTION,
    quality_policy,
)
from ai_core.fullstack.schema_mcp import verify_schema_with_mcp
from ai_core.fullstack.service import FullstackService
from ai_core.mcp.client import McpServerConfig, McpStdioClient
from ai_core.observability.fullstack_metrics import FULLSTACK_METRICS
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


# 真实事故契约：第一条是鉴权表 users，业务表 resumes 在其后。
MULTI_TABLE_DDL = """CREATE TABLE users (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  iam_sub VARCHAR(128) NOT NULL UNIQUE,
  tenant_id VARCHAR(64),
  display_name VARCHAR(128),
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE resumes (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id BIGINT NOT NULL,
  file_name VARCHAR(255),
  content MEDIUMTEXT NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_resumes_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE jds (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id BIGINT NOT NULL,
  title VARCHAR(255),
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"""


def _resume_contract(**overrides) -> ArchitectContract:
    # 交互必须与 apiRoutes 指向同一资源；否则契约审计会（正确地）
    # 判定为“组件调用了未声明接口”。
    overrides.setdefault(
        "componentInteractions",
        [
            ArchitectComponentInteraction(
                component="ResumeList",
                event="load",
                api_route="GET /api/v1/resumes",
                logic="加载简历列表",
            ),
            ArchitectComponentInteraction(
                component="ResumeForm",
                event="submit",
                api_route="POST /api/v1/resumes",
                logic="创建简历后刷新列表",
            ),
        ],
    )
    return _contract(
        apiRoutes=[
            ArchitectApiRoute(
                method="GET", path="/api/v1/resumes", description="简历列表"
            ),
            ArchitectApiRoute(
                method="POST", path="/api/v1/resumes", description="创建简历"
            ),
            ArchitectApiRoute(
                method="GET",
                path="/api/v1/resumes/:id",
                description="简历详情",
            ),
        ],
        databaseDdl=MULTI_TABLE_DDL,
        **overrides,
    )


def _master_detail_contract() -> ArchitectContract:
    """核心表 users 为父表，resumes 为子表；用于 JOIN / Applier 测试。"""
    ddl = """CREATE TABLE users (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(128) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE resumes (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id BIGINT NOT NULL,
  file_name VARCHAR(255),
  content MEDIUMTEXT NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);"""
    return _contract(
        apiRoutes=[
            ArchitectApiRoute(
                method="GET", path="/api/v1/users", description="用户列表"
            ),
            ArchitectApiRoute(
                method="POST", path="/api/v1/users", description="创建用户"
            ),
            ArchitectApiRoute(
                method="GET", path="/api/v1/users/:id", description="用户详情"
            ),
            ArchitectApiRoute(
                method="GET",
                path="/api/v1/users/:id/resumes",
                description="用户简历",
            ),
        ],
        componentInteractions=[
            ArchitectComponentInteraction(
                component="UserList",
                event="load",
                api_route="GET /api/v1/users",
                logic="加载用户列表",
            )
        ],
        databaseDdl=ddl,
        relations=[
            ArchitectTableRelation(
                parentTable="users",
                childTable="resumes",
                foreignKey="user_id",
            )
        ],
    )


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


class _FlakySandbox(_MemorySandbox):
    """按检查类型注入失败的沙盒；用于验证 L1 构建重试与降级。"""

    def __init__(self, fail_checks: tuple[str, ...] = ("build",)) -> None:
        super().__init__()
        self.fail_checks = set(fail_checks)

    def verify(self, sandbox_id: str, check: str = "build") -> VerifyResult:
        self.verified.append((sandbox_id, check))
        if check in self.fail_checks:
            return VerifyResult(
                success=False,
                exit_code=1,
                timed_out=False,
                duration_ms=8,
                logs=f"{check} failed: mock failure",
                logs_truncated=False,
            )
        return VerifyResult(
            success=True,
            exit_code=0,
            timed_out=False,
            duration_ms=8,
            logs=f"{check} ok",
            logs_truncated=False,
        )


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

    def test_multi_table_ddl_selects_collection_business_table(self) -> None:
        """回归 500 Unknown column：多表 DDL 不能把 users 当业务表。

        旧解析器取第一条 CREATE TABLE 且贪婪匹配全部字段，生成
        ``TABLE=users / COLUMN_DEFS=[id,name,status]``，而迁移创建的是
        resumes，线上 GET/POST 直接 500。
        """
        contract = _resume_contract()
        core = core_table_for_contract(contract)
        self.assertEqual(core.name, "resumes")
        self.assertIn("file_name", core.column_names)
        self.assertIn("content", core.column_names)
        self.assertNotIn("iam_sub", core.column_names)

        bundle = generate_dba_bundle(contract)
        db_code = bundle.files["server/db.js"]
        self.assertIn("const TABLE = 'resumes'", db_code)
        self.assertIn('"file_name"', db_code)
        self.assertIn('"content"', db_code)
        self.assertNotIn('"iam_sub"', db_code)
        self.assertNotIn("const TABLE = 'users'", db_code)
        # NOT NULL 列必须带约束元数据，供 buildValues 补默认值。
        self.assertIn('"nullable": false', db_code)
        self.assertIn("function requiredColumnsFor", db_code)
        migration = bundle.files["server/migrations/001_init.sql"]
        self.assertIn("CREATE TABLE IF NOT EXISTS users", migration)
        self.assertIn("CREATE TABLE IF NOT EXISTS resumes", migration)
        core_json = json.loads(bundle.files["server/schema.core.json"])
        self.assertEqual(core_json["table"], "resumes")
        self.assertIn("content", core_json["columnMeta"])

    def test_master_detail_relation_strips_sensitive_columns(self) -> None:
        """核心表为子表：生成 belongsTo users，但绝不暴露 iam_sub。"""
        contract = _resume_contract(
            relations=[
                ArchitectTableRelation(
                    parentTable="users",
                    childTable="resumes",
                    foreignKey="user_id",
                )
            ]
        )
        bundle = generate_dba_bundle(contract)
        db_code = bundle.files["server/db.js"]
        self.assertIn("const TABLE = 'resumes'", db_code)
        self.assertIn("child.belongsTo", db_code)
        self.assertIn('"user"', db_code)
        self.assertNotIn('"iam_sub"', db_code)
        core_json = json.loads(bundle.files["server/schema.core.json"])
        tables = {item["table"]: item for item in core_json["tables"]}
        self.assertIn("users", tables)
        self.assertNotIn("iam_sub", tables["users"]["columns"])
        self.assertEqual(len(core_json["relations"]), 1)

    def test_master_detail_parent_generates_nested_child_routes(self) -> None:
        """核心表为父表：生成 hasMany + /users/:id/resumes 嵌套路由。"""
        ddl = """CREATE TABLE users (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(128) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE resumes (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id BIGINT NOT NULL,
  file_name VARCHAR(255),
  content MEDIUMTEXT NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);"""
        contract = _contract(
            apiRoutes=[
                ArchitectApiRoute(
                    method="GET", path="/api/v1/users", description="用户列表"
                ),
                ArchitectApiRoute(
                    method="POST", path="/api/v1/users", description="创建用户"
                ),
                ArchitectApiRoute(
                    method="GET", path="/api/v1/users/:id", description="用户详情"
                ),
            ],
            databaseDdl=ddl,
            relations=[
                ArchitectTableRelation(
                    parentTable="users",
                    childTable="resumes",
                    foreignKey="user_id",
                )
            ],
        )
        dba = generate_dba_bundle(contract)
        db_code = dba.files["server/db.js"]
        self.assertIn("parent.hasMany", db_code)
        self.assertIn("listRelatedRecords", db_code)
        self.assertIn("createRelatedRecord", db_code)
        core_json = json.loads(dba.files["server/schema.core.json"])

        backend = generate_backend_bundle(contract, dba_schema=core_json)
        index_code = backend.files["server/index.js"]
        self.assertIn("/api/v1/users/:id/resumes", index_code)
        manifest = json.loads(backend.files["server/api-routes.json"])
        relation_routes = [
            route
            for route in manifest["routes"]
            if route.get("source") == "express.relation"
        ]
        self.assertEqual(
            {(route["method"], route["path"]) for route in relation_routes},
            {
                ("GET", "/api/v1/users/:id/resumes"),
                ("POST", "/api/v1/users/:id/resumes"),
            },
        )

    def test_missing_ddl_never_falls_back_to_users_or_name(self) -> None:
        """无 DDL 时兜底字段必须是中性的 title/created_at。"""
        self.assertEqual(extract_table_name("-- 没有 DDL"), "app_records")
        self.assertEqual(extract_columns("-- 没有 DDL"), ["id", "title", "created_at"])
        self.assertEqual(
            extract_column_defs("-- 没有 DDL"),
            [("id", "INTEGER"), ("title", "TEXT"), ("created_at", "TEXT")],
        )

    def test_parse_ddl_tables_keeps_each_table_isolated(self) -> None:
        tables = {table.name: table for table in parse_ddl_tables(MULTI_TABLE_DDL)}
        self.assertEqual(set(tables), {"users", "resumes", "jds"})
        self.assertIn("iam_sub", tables["users"].column_names)
        self.assertIn("file_name", tables["resumes"].column_names)
        self.assertNotIn("file_name", tables["jds"].column_names)
        # INDEX 等表级约束不能混进字段。
        self.assertNotIn("index", tables["resumes"].column_names)

    def test_backend_reads_dba_schema_core_before_generating(self) -> None:
        """BackendCoder 必须显式绑定 DBACoder 产出的核心表结构。"""
        contract = _resume_contract()
        dba = generate_dba_bundle(contract)
        backend = generate_backend_bundle(
            contract,
            dba_schema=dba.files["server/schema.core.json"],
        )
        self.assertIn("const TABLE = 'resumes'", backend.files["server/index.js"])
        manifest = json.loads(backend.files["server/api-routes.json"])
        self.assertEqual(manifest["table"], "resumes")
        self.assertIn("/api/v1/resumes", backend.files["server/index.js"])

    def test_backend_rejects_divergent_dba_schema(self) -> None:
        contract = _resume_contract()
        with self.assertRaises(ValueError):
            generate_backend_bundle(
                contract,
                dba_schema={
                    "table": "users",
                    "columnDefs": [["id", "INTEGER"], ["name", "TEXT"]],
                },
            )

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
        # 生产迁移：SQL 文件 + 显式执行器，禁止 sync() 自动建表。
        migration = bundle.files["server/migrations/001_init.sql"]
        self.assertIn("CREATE TABLE IF NOT EXISTS todos", migration)
        self.assertIn("AUTO_INCREMENT", migration)
        self.assertNotIn("AUTOINCREMENT", migration)
        runner = bundle.files["server/migrations.mjs"]
        self.assertIn("_schema_migrations", runner)
        self.assertIn("checksum", runner)
        self.assertNotIn("await model.sync()", db_code)
        self.assertNotIn("DB_AUTO_MIGRATE", db_code)
        self.assertIn("tableReady", db_code)

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
        self.assertIn("process.env.CORS_ORIGINS", code)
        self.assertIn("process.env.CORS_ORIGIN", code)
        # 生产 CORS 白名单不是硬编码 *：未命中白名单的预检直接 403。
        self.assertIn("CORS_ORIGINS.includes(origin)", code)
        self.assertIn("status(allowedOrigin ? 204 : 403)", code)
        # 健康检查必须真实探测数据库，失败返回 503。
        self.assertIn("response.status(503)", code)
        db_code = generate_dba_bundle(_contract()).files["server/db.js"]
        self.assertIn("tableReady", db_code)
        self.assertEqual(bundle.metadata["collectionPath"], "/api/v1/todos")
        self.assertEqual(bundle.metadata["runtime"], "express-sequelize")
        self.assertEqual(bundle.metadata["databaseEngine"], "mysql")
        package = bundle.files["server/package.json"]
        self.assertIn('"sequelize"', package)
        self.assertIn('"mysql2"', package)
        self.assertIn('"pg"', package)
        self.assertIn('"db:migrate"', package)
        # 普通 CRUD 契约不得被强行注入上传依赖。
        self.assertNotIn("multer", code)
        self.assertNotIn('"multer"', package)
        self.assertNotIn("__UPLOAD_RESPONSE_FIELDS__", code)

    def test_backend_bundle_adds_multer_for_upload_contract(self) -> None:
        ddl = (
            "CREATE TABLE resumes (\n"
            "  id BIGINT PRIMARY KEY AUTO_INCREMENT,\n"
            "  name VARCHAR(64) NOT NULL,\n"
            "  email VARCHAR(128) NOT NULL,\n"
            "  position VARCHAR(128),\n"
            "  file_name VARCHAR(255),\n"
            "  file_path VARCHAR(512),\n"
            "  file_size BIGINT,\n"
            "  created_at DATETIME\n"
            ");"
        )
        contract = _contract(
            contractMarkdown="# 简历投递\n求职者上传 PDF 简历并保存记录。",
            databaseDdl=ddl,
            apiRoutes=[
                ArchitectApiRoute(
                    method="GET",
                    path="/api/v1/resumes",
                    response="list",
                    description="简历列表",
                ),
                ArchitectApiRoute(
                    method="POST",
                    path="/api/v1/resumes",
                    request="multipart/form-data",
                    response="Resume",
                    description="上传简历并写入数据库",
                ),
            ],
        )
        bundle = generate_backend_bundle(contract)
        code = bundle.files["server/index.js"]
        self.assertIn("import multer from 'multer'", code)
        self.assertIn(
            "app.post(COLLECTION_PATH, upload.single('file'), createHandler)", code
        )
        self.assertIn('body["file_name"] = request.file.originalname', code)
        self.assertIn('body["file_path"] = `/uploads/${request.file.filename}`', code)
        self.assertIn('body["file_size"] = request.file.size', code)
        self.assertIn('"multer": "2.5.0"', bundle.files["server/package.json"])
        self.assertIn("upload: {", code)
        self.assertNotIn("__UPLOAD_RESPONSE_FIELDS__", code)
        self.assertTrue(bundle.metadata["upload"]["enabled"])
        self.assertEqual(
            bundle.metadata["upload"]["fileColumns"]["file_name"], "file_name"
        )

    def test_backend_bundle_maps_prefixed_upload_columns(self) -> None:
        """resume_file_name / resume_file_path 等业务前缀字段也必须映射。"""
        ddl = (
            "CREATE TABLE resume_submissions (\n"
            "  id BIGINT PRIMARY KEY AUTO_INCREMENT,\n"
            "  applicant_name VARCHAR(64),\n"
            "  email VARCHAR(128),\n"
            "  position VARCHAR(128),\n"
            "  resume_file_name VARCHAR(255),\n"
            "  resume_file_path VARCHAR(512),\n"
            "  resume_file_size BIGINT,\n"
            "  created_at DATETIME\n"
            ");"
        )
        contract = _contract(
            contractMarkdown="# 简历投递\n求职者上传 PDF 简历并保存记录。",
            databaseDdl=ddl,
            apiRoutes=[
                ArchitectApiRoute(
                    method="GET",
                    path="/api/v1/resumes",
                    response="list",
                    description="简历列表",
                ),
                ArchitectApiRoute(
                    method="POST",
                    path="/api/v1/resumes",
                    request="multipart/form-data",
                    response="Resume",
                    description="上传简历并写入数据库",
                ),
            ],
        )
        bundle = generate_backend_bundle(contract)
        code = bundle.files["server/index.js"]
        self.assertIn('body["resume_file_name"] = request.file.originalname', code)
        self.assertIn(
            'body["resume_file_path"] = `/uploads/${request.file.filename}`', code
        )
        self.assertIn('body["resume_file_size"] = request.file.size', code)
        columns = bundle.metadata["upload"]["fileColumns"]
        self.assertEqual(columns["file_name"], "resume_file_name")
        self.assertEqual(columns["file_path"], "resume_file_path")
        self.assertEqual(columns["file_size"], "resume_file_size")

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
        self.assertIn("src/api-client.jsx", bundle.files)
        self.assertIn("scripts/dev.mjs", bundle.files)
        patched_main = bundle.files["src/main.jsx"]
        self.assertIn("FullstackDataPanel", patched_main)
        self.assertIn("ApiErrorBanner", patched_main)
        self.assertIn("<App /><FullstackDataPanel />", patched_main)
        panel = bundle.files["src/fullstack-api.jsx"]
        self.assertIn("data-fullstack-success", panel)
        self.assertIn("提交成功", panel)
        # 生产前后端分域部署：API_BASE / VITE_API_BASE_URL 与全局错误
        # 拦截器统一收敛在 src/api-client.jsx。
        self.assertIn("import { apiFetch, apiUrl } from './api-client.jsx'", panel)
        self.assertIn("VITE_API_BASE_URL", bundle.files["src/api-client.jsx"])
        self.assertIn("apiUrl(COLLECTION_PATH)", panel)
        patched_vite = bundle.files["vite.config.js"]
        self.assertIn("'/api'", patched_vite)
        self.assertIn("127.0.0.1:3000", patched_vite)
        patched_package = bundle.files["package.json"]
        self.assertIn('"dev": "node scripts/dev.mjs"', patched_package)
        self.assertIn('"dev:vite": "vite"', patched_package)
        self.assertIn("server/index.js", bundle.files["scripts/dev.mjs"])

    def test_visual_frontend_bundle_skips_unanchored_panel(self) -> None:
        """Visual CodeGen 全栈模式不能注入没有源码锚点的旧数据面板。"""
        main = (
            "import React from 'react'\n"
            "import { createRoot } from 'react-dom/client'\n"
            "import App from './App.jsx'\n"
            "createRoot(document.getElementById('root')).render(<App />)\n"
        )
        vite = (
            "import { defineConfig } from 'vite'\n"
            "export default defineConfig({\n  server: {\n    port: 5173,\n  },\n})\n"
        )
        package = '{"scripts": {"dev": "vite"}, "dependencies": {}}'

        bundle = generate_frontend_bundle(
            _contract(),
            existing_main=main,
            existing_vite=vite,
            existing_package=package,
            include_panel=False,
        )

        self.assertNotIn("src/fullstack-api.jsx", bundle.files)
        self.assertNotIn("src/api-client.jsx", bundle.files)
        self.assertNotIn("src/ai-workspace.jsx", bundle.files)
        self.assertNotIn("src/main.jsx", bundle.files)
        self.assertIn("scripts/dev.mjs", bundle.files)
        self.assertIn("'/api'", bundle.files["vite.config.js"])
        self.assertNotIn("FullstackDataPanel", bundle.summary)
        self.assertIn("视觉 CodeGen", bundle.summary)

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
        self.assertEqual(set(bundle.files), {"src/App.jsx", "src/api-client.jsx"})
        app = bundle.files["src/App.jsx"]
        # 全局 fetch 拦截器 / 错误横幅必须随骨架一起生成。
        self.assertIn("ApiErrorBanner", app)
        self.assertIn("installFetchInterceptor", bundle.files["src/api-client.jsx"])
        self.assertIn("VITE_API_BASE_URL", bundle.files["src/api-client.jsx"])
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
        # 多阶段构建：deps 只装生产依赖，runtime 只复制 node_modules 与源码。
        self.assertIn("FROM node:22-alpine AS deps", dockerfile)
        self.assertIn("FROM node:22-alpine AS runtime", dockerfile)
        self.assertIn("COPY server/package.json ./server/package.json", dockerfile)
        self.assertIn(
            "COPY --from=deps --chown=node:node /app/server/node_modules",
            dockerfile,
        )
        # 保留 server/ 层级，SQLite 默认写入 /app/data（../data）。
        self.assertIn("COPY --chown=node:node server/ ./server", dockerfile)
        # 第二次 COPY 以 root 写入，必须在 USER node 之前重新 chown；
        # 否则 multer 上传目录与 SQLite data 目录都会 EACCES。
        self.assertIn("mkdir -p /app/server/uploads /app/data", dockerfile)
        self.assertLess(
            dockerfile.index("COPY --chown=node:node server/ ./server"),
            dockerfile.index("chown -R node:node /app"),
        )
        self.assertLess(
            dockerfile.index("chown -R node:node /app"),
            dockerfile.index("USER node"),
        )
        self.assertIn('CMD ["node", "server/index.js"]', dockerfile)
        render = bundle.files["render.yaml"]
        self.assertIn("runtime: docker", render)
        self.assertIn("runtime: static", render)
        self.assertIn("DATABASE_URL", render)
        self.assertIn("/api/health", render)
        self.assertIn("preDeployCommand: node server/migrate.mjs", render)
        self.assertNotIn("DB_AUTO_MIGRATE", render)
        env = bundle.files[".env.example"]
        self.assertIn("DATABASE_URL=", env)
        self.assertIn("DB_ENGINE=mysql", env)
        self.assertIn("NODE_ENV=production", env)
        self.assertIn("PORT=3000", env)
        self.assertIn("CORS_ORIGINS=", env)
        self.assertIn("VITE_API_BASE_URL=", env)
        self.assertNotIn("DB_AUTO_MIGRATE", env)
        deploy = bundle.files["DEPLOY.md"]
        self.assertIn("Vercel", deploy)
        self.assertIn("Railway", deploy)
        self.assertIn("MySQL 8", deploy)
        self.assertIn("server/migrate.mjs", deploy)
        self.assertNotIn("DB_AUTO_MIGRATE", deploy)
        self.assertNotIn("sync() 建表", deploy)
        self.assertEqual(bundle.metadata["databaseEngine"], "mysql")


class SchemaMcpTests(unittest.TestCase):
    """MCP 真实数据库结构验收：DDL 必须执行，表与字段必须可查询。"""

    SCRIPT = (
        Path(__file__).resolve().parents[1]
        / "ai_core"
        / "mcp_servers"
        / "sqlite_mcp.py"
    )

    def _settings(self) -> Settings:
        return Settings(
            schema_mcp_enabled=True,
            schema_mcp_python=sys.executable,
            schema_mcp_script=str(self.SCRIPT),
            schema_mcp_timeout_seconds=15,
        )

    def test_mcp_executes_generated_ddl_and_finds_table(self) -> None:
        dba = generate_dba_bundle(_contract())
        files = {"server/schema.sql": dba.files["server/schema.sql"]}
        check = verify_schema_with_mcp(files, _contract(), settings=self._settings())
        self.assertTrue(check.enabled)
        self.assertTrue(check.ok, check.error)
        self.assertEqual(check.table, "todos")
        self.assertIn("id", check.actual_columns)
        self.assertIn("payload", check.actual_columns)
        self.assertGreaterEqual(check.statements, 1)
        self.assertEqual(check.missing_columns, ())
        self.assertEqual(check.source, "server/schema.sql")

    def test_mcp_reports_missing_columns_instead_of_text_pass(self) -> None:
        files = {
            "server/schema.sql": (
                'CREATE TABLE IF NOT EXISTS "todos" (\n'
                '  "id" INTEGER PRIMARY KEY AUTOINCREMENT,\n'
                '  "name" TEXT\n'
                ");\n"
            )
        }
        check = verify_schema_with_mcp(files, _contract(), settings=self._settings())
        self.assertFalse(check.ok)
        self.assertIn("tenant_id", check.missing_columns)
        self.assertIn("payload", check.missing_columns)

    def test_mcp_rejects_destructive_sql(self) -> None:
        client = McpStdioClient(
            McpServerConfig(
                name="sqlite_schema_test",
                command=sys.executable,
                args=(str(self.SCRIPT),),
                timeout_seconds=15,
                allowed_tools=("validate_schema",),
            )
        )
        try:
            client.start()
            result = client.call_tool(
                "validate_schema",
                {"ddl": "DROP TABLE todos;", "table": "todos"},
            )
        finally:
            client.close()
        self.assertTrue(result.get("isError"))
        self.assertIn("拒绝", str(result.get("text")))


class GraphTests(unittest.TestCase):
    """FullstackGraph 节点顺序、事件契约与终态。"""

    @staticmethod
    def _node_order(buffer: RunEventBuffer) -> list[str]:
        return [
            event.payload["node"]
            for event in buffer.snapshot()
            if event.type == "fullstack.node.started"
        ]

    def test_dba_mode_only_runs_schema_nodes(self) -> None:
        """Supervisor 派发 dba_agent 时不得触发前端 / 后端 / 构建节点。"""
        sandbox = _MemorySandbox()
        sandbox.files = _base_files()
        buffer = RunEventBuffer("fsk_dba_mode")
        runner = FullstackGraphRunner(sandbox, event_sink=buffer)

        outcome = runner.run(
            {
                "run_id": "fsk_dba_mode",
                "project_id": "proj_fullstack",
                "sandbox_id": sandbox.handle.sandbox_id,
                "preview_url": sandbox.handle.preview_url,
                "contract": _contract(),
                "options": FullstackRunOptions(mode="dba"),
                "files": [],
            }
        )

        self.assertEqual(self._node_order(buffer), ["DBACoder", "SandboxRestart"])
        self.assertNotIn("server/index.js", outcome["files"])
        self.assertNotIn("src/App.jsx", outcome["files"])
        self.assertEqual(sandbox.verified, [])
        self.assertEqual(sandbox.restarts, [(sandbox.handle.sandbox_id, "/")])

    def test_backend_mode_skips_frontend_and_infra(self) -> None:
        """backend 子图只生成 DB + API，前端与部署配置保持原样。"""
        sandbox = _MemorySandbox()
        sandbox.files = _base_files()
        buffer = RunEventBuffer("fsk_backend_mode")
        runner = FullstackGraphRunner(sandbox, event_sink=buffer)

        outcome = runner.run(
            {
                "run_id": "fsk_backend_mode",
                "project_id": "proj_fullstack",
                "sandbox_id": sandbox.handle.sandbox_id,
                "preview_url": sandbox.handle.preview_url,
                "contract": _contract(),
                "options": FullstackRunOptions(mode="backend"),
                "files": [],
            }
        )

        self.assertEqual(
            self._node_order(buffer),
            ["DBACoder", "BackendCoder", "BuildVerifier", "SandboxRestart"],
        )
        self.assertIn("server/index.js", sandbox.files)
        self.assertNotIn("src/fullstack-api.jsx", outcome["files"])
        self.assertNotIn("Dockerfile", sandbox.files)
        self.assertEqual(
            sandbox.verified,
            [
                (sandbox.handle.sandbox_id, "install"),
                (sandbox.handle.sandbox_id, "build"),
            ],
        )
        self.assertEqual(sandbox.restarts, [(sandbox.handle.sandbox_id, "/api/health")])

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
        self.assertEqual(
            sandbox.verified,
            [
                (sandbox.handle.sandbox_id, "install"),
                (sandbox.handle.sandbox_id, "build"),
            ],
        )
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
        # 一致性报告必须包含 MCP 真实数据库结构验收结果。
        reports = [
            event for event in events if event.type == "fullstack.consistency.report"
        ]
        self.assertTrue(reports)
        self.assertTrue(reports[-1].payload["ok"])
        schema = reports[-1].payload["schema"]
        self.assertTrue(schema["ok"], schema)
        self.assertEqual(schema["table"], "todos")
        self.assertIn("id", schema["actualColumns"])
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

        # 补齐后通过一致性检查，才允许安装依赖、构建与重启。
        self.assertEqual(
            sandbox.verified,
            [
                (sandbox.handle.sandbox_id, "install"),
                (sandbox.handle.sandbox_id, "build"),
            ],
        )
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

    def test_orm_mismatch_regenerates_dba_before_build(self) -> None:
        """回归 500 Unknown column：ORM 字段错位必须先重跑 DBACoder。

        模拟“旧模板仍在用 users/name”的事故包：一致性检查报
        ``orm_schema`` 后，图必须回到 DBACoder 按真实 DDL 重写，
        重写通过后才允许 install / build / restart。
        """
        sandbox = _MemorySandbox()
        sandbox.files = _base_files()
        contract = _resume_contract()
        buffer = RunEventBuffer("fsk_orm_self_heal")
        runner = FullstackGraphRunner(sandbox, event_sink=buffer)

        from ai_core.fullstack import graph as graph_module
        from ai_core.fullstack.consistency import (
            BackendRoute,
            ConsistencyIssue,
            ConsistencyReport,
        )

        bad_report = ConsistencyReport(
            calls_checked=1,
            backend_routes=(BackendRoute("GET", "/api/v1/resumes"),),
            issues=(
                ConsistencyIssue(
                    kind="orm_schema",
                    component="DBACoder",
                    method="",
                    path="server/db.js",
                    message="ORM 模型引用了 DDL 中不存在的字段：name",
                    suggestion="由 DBACoder 按 migrations/001_init.sql 重建 db.js",
                    fix_action="REGENERATE_ORM",
                ),
            ),
        )
        original = graph_module.verify_consistency
        calls: list[int] = []

        def fake_verify(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                return bad_report
            return original(*args, **kwargs)

        graph_module.verify_consistency = fake_verify
        try:
            runner.run(
                {
                    "run_id": "fsk_orm_self_heal",
                    "project_id": "proj_resume",
                    "sandbox_id": sandbox.handle.sandbox_id,
                    "preview_url": sandbox.handle.preview_url,
                    "contract": contract,
                    "files": [],
                }
            )
        finally:
            graph_module.verify_consistency = original

        self.assertEqual(len(calls), 2)
        events = buffer.snapshot()
        dba_starts = [
            event
            for event in events
            if event.type == "fullstack.node.started"
            and event.payload["node"] == "DBACoder"
        ]
        self.assertEqual(len(dba_starts), 2)
        repairs = [
            event for event in events if event.type == "fullstack.consistency.repair"
        ]
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0].payload["node"], "DBACoder")
        self.assertEqual(repairs[0].payload["reason"], "ORM_SCHEMA_MISMATCH")
        self.assertEqual(repairs[0].payload["attempt"], 1)
        # 重写后的持久化层必须绑定真实核心业务表，构建才继续。
        self.assertIn("const TABLE = 'resumes'", sandbox.files["server/db.js"])
        self.assertEqual(
            sandbox.verified,
            [
                (sandbox.handle.sandbox_id, "install"),
                (sandbox.handle.sandbox_id, "build"),
            ],
        )
        self.assertEqual(sandbox.restarts, [(sandbox.handle.sandbox_id, "/api/health")])

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

    def test_contract_contradiction_stops_without_retry(self) -> None:
        """前端调用了未声明接口：这是契约矛盾，不能靠重跑 BackendCoder 修复。"""
        sandbox = _MemorySandbox()
        sandbox.files = _base_files()
        contract = _resume_contract(
            componentInteractions=[
                ArchitectComponentInteraction(
                    component="ResumeSummary",
                    event="load",
                    api_route="GET /api/v1/resumes/summary",
                    logic="加载统计卡片",
                )
            ]
        )
        buffer = RunEventBuffer("fsk_contract_contradiction")
        runner = FullstackGraphRunner(sandbox, event_sink=buffer)
        with self.assertRaises(FullstackContractContradictionError) as ctx:
            runner.run(
                {
                    "run_id": "fsk_contract_contradiction",
                    "project_id": "proj_resume",
                    "sandbox_id": sandbox.handle.sandbox_id,
                    "preview_url": sandbox.handle.preview_url,
                    "contract": contract,
                    "files": [],
                }
            )
        self.assertEqual(ctx.exception.code, "CONTRACT_REVISION_REQUIRED")
        self.assertEqual(ctx.exception.failure_kind, "CONTRACT")
        self.assertTrue(ctx.exception.needs_clarification)
        self.assertIn("resumes/summary", ctx.exception.clarification["prompt"])
        self.assertEqual(sandbox.verified, [])
        events = buffer.snapshot()
        repairs = [
            event for event in events if event.type == "fullstack.consistency.repair"
        ]
        self.assertEqual(repairs, [])

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
        self.assertEqual(
            sandbox.verified,
            [
                (sandbox.handle.sandbox_id, "install"),
                (sandbox.handle.sandbox_id, "build"),
            ],
        )
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

    def test_applier_code_defect_triggers_llm_repair_with_feedback(self) -> None:
        """E2E：模板连续产出错误 JOIN -> LLM 携带 Applier 诊断修复。

        第一个断言证明“同一输入模板必然重试出同一错误”；第二个断言证明
        LLM 的输入包含真实诊断与完整文件，输出仍要再次经过 Applier 才能落盘。
        """
        FULLSTACK_METRICS.reset()
        sandbox = _MemorySandbox()
        sandbox.files = _base_files()
        contract = _master_detail_contract()
        buffer = RunEventBuffer("fsk_llm_repair")
        calls: list[dict] = []

        class _FakeLLMBackendCoder:
            available = True

            def repair(
                self,
                *,
                contract,
                bundle,
                dba_schema,
                diagnostics,
                attempt,
                max_attempts,
                authoritative_files=None,
            ):
                calls.append(
                    {
                        "contract": contract,
                        "bundle": bundle,
                        "dba_schema": dba_schema,
                        "diagnostics": list(diagnostics),
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "authoritative_files": dict(authoritative_files or {}),
                    }
                )
                files = dict(bundle.files)
                code = files["server/index.js"]
                self_test.assertIn("/:id/resumez", code)
                files["server/index.js"] = code.replace("/:id/resumez", "/:id/resumes")
                metadata = dict(bundle.metadata)
                metadata["llmRepair"] = {
                    "used": True,
                    "files": ["server/index.js"],
                }
                return GeneratedBundle(
                    files=files,
                    summary="fake LLM repaired",
                    metadata=metadata,
                )

        self_test = self
        runner = FullstackGraphRunner(
            sandbox,
            event_sink=buffer,
            backend_llm=_FakeLLMBackendCoder(),
        )

        from ai_core.fullstack import graph as graph_module

        real_generator = generate_backend_bundle
        template_calls = {"count": 0}

        def generate_with_bad_join(*args, **kwargs):
            bundle = real_generator(*args, **kwargs)
            template_calls["count"] += 1
            files = dict(bundle.files)
            files["server/index.js"] = files["server/index.js"].replace(
                "/:id/resumes", "/:id/resumez"
            )
            return GeneratedBundle(
                files=files,
                summary=bundle.summary,
                metadata=bundle.metadata,
            )

        with patch.object(
            graph_module,
            "generate_backend_bundle",
            side_effect=generate_with_bad_join,
        ):
            result = runner.run(
                {
                    "run_id": "fsk_llm_repair",
                    "project_id": "proj_fullstack",
                    "sandbox_id": sandbox.handle.sandbox_id,
                    "preview_url": sandbox.handle.preview_url,
                    "contract": contract,
                    "files": [],
                }
            )

        # 模板跑了两次（首次 + 重试），第二次仍错误，由 LLM 修复。
        self.assertEqual(template_calls["count"], 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["attempt"], 1)
        self.assertEqual(calls[0]["max_attempts"], 2)
        self.assertEqual(calls[0]["contract"].database_ddl, contract.database_ddl)
        self.assertTrue(calls[0]["dba_schema"])
        feedback = "\n".join(calls[0]["diagnostics"])
        self.assertIn("resumez", feedback)
        self.assertIn("JOIN", feedback.upper())
        # 诊断带 source 前缀，模型能判断缺陷归属文件。
        self.assertIn("[server/index.js]", feedback)
        self.assertIn("/:id/resumez", calls[0]["bundle"].files["server/index.js"])
        # db.js 作为只读事实源随修复请求提供，用于对齐真实 RELATIONS。
        db_source = calls[0]["authoritative_files"].get("server/db.js", "")
        self.assertIn("RELATIONS", db_source)
        self.assertEqual(result["llm_repair_count"], 1)

        self.assertNotIn("resumez", sandbox.files["server/index.js"])
        self.assertIn("/api/v1/users/:id/resumes", sandbox.files["server/index.js"])
        self.assertEqual(sandbox.restarts, [(sandbox.handle.sandbox_id, "/api/health")])

        repair_events = [
            event
            for event in buffer.snapshot()
            if event.type == "fullstack.applier.llm_repair"
        ]
        self.assertEqual(len(repair_events), 1)
        self.assertEqual(repair_events[0].payload["status"], "SUCCEEDED")
        self.assertEqual(repair_events[0].payload["files"], ["server/index.js"])

    def test_applier_db_source_routes_to_dbacoder(self) -> None:
        """db.js 缺陷超出 BackendCoder 权限，必须回 DBACoder 重建而不是空转。"""
        sandbox = _MemorySandbox()
        sandbox.files = _base_files()
        runner = FullstackGraphRunner(sandbox, event_sink=RunEventBuffer("x"))
        report = OrmSyntaxReport(
            applicable=True,
            multi_table=False,
            issues=(
                OrmDiagnostic(
                    code="ORM_SCHEMA_MISMATCH",
                    message="ORM 模型引用了 DDL 中不存在的字段：unknown",
                    source="server/db.js",
                ),
            ),
            tables=("app_records",),
        )
        state = {
            "run_id": "fsk_db_route",
            "contract": _master_detail_contract(),
            "applier_retry_count": 0,
        }
        result = runner._handle_applier_block(
            state,
            ApplierSafetyBlock(report),
            time.monotonic(),
        )
        self.assertFalse(result["needs_backend_retry"])
        self.assertTrue(result["applier_dba_retry"])
        self.assertEqual(result["applier_retry_count"], 1)
        # DBA 重建不需要把旧诊断塞给 LLM。
        self.assertEqual(result["applier_feedback"], [])

    def test_second_repair_uses_previous_failed_candidate(self) -> None:
        """第二轮修复必须以“上一轮被拦截的产物”为基线，而不是重置模板。

        否则模型看不到自己上一次改了什么，两轮都在同一模板上重写，
        修复过程不收敛。
        """
        sandbox = _MemorySandbox()
        sandbox.files = _base_files()
        contract = _master_detail_contract()
        buffer = RunEventBuffer("fsk_llm_repair_previous")
        calls: list[dict] = []

        class _TwoRoundLLM:
            available = True

            def repair(
                self,
                *,
                bundle,
                attempt,
                authoritative_files=None,
                **kwargs,
            ):
                code = bundle.files["server/index.js"]
                calls.append(
                    {
                        "attempt": attempt,
                        "code": code,
                        "authoritative_files": dict(authoritative_files or {}),
                    }
                )
                files = dict(bundle.files)
                if attempt == 1:
                    # 第一轮只加标记，不修复 JOIN -> 仍会被 Applier 拦截。
                    files["server/index.js"] = "// attempt-one-marker\n" + code
                else:
                    self_test.assertIn("attempt-one-marker", code)
                    files["server/index.js"] = code.replace(
                        "/:id/resumez", "/:id/resumes"
                    )
                metadata = dict(bundle.metadata)
                metadata["llmRepair"] = {
                    "used": True,
                    "files": ["server/index.js"],
                }
                return GeneratedBundle(
                    files=files,
                    summary="fake LLM repaired",
                    metadata=metadata,
                )

        self_test = self
        runner = FullstackGraphRunner(
            sandbox,
            event_sink=buffer,
            backend_llm=_TwoRoundLLM(),
        )

        from ai_core.fullstack import graph as graph_module

        real_generator = generate_backend_bundle

        def generate_with_bad_join(*args, **kwargs):
            bundle = real_generator(*args, **kwargs)
            files = dict(bundle.files)
            files["server/index.js"] = files["server/index.js"].replace(
                "/:id/resumes", "/:id/resumez"
            )
            return GeneratedBundle(
                files=files,
                summary=bundle.summary,
                metadata=bundle.metadata,
            )

        with patch.object(
            graph_module,
            "generate_backend_bundle",
            side_effect=generate_with_bad_join,
        ):
            result = runner.run(
                {
                    "run_id": "fsk_llm_repair_previous",
                    "project_id": "proj_fullstack",
                    "sandbox_id": sandbox.handle.sandbox_id,
                    "preview_url": sandbox.handle.preview_url,
                    "contract": contract,
                    "files": [],
                }
            )

        self.assertEqual([call["attempt"] for call in calls], [1, 2])
        # 第二轮拿到的是第一轮产物（含标记），不是重新生成的模板。
        self.assertIn("attempt-one-marker", calls[1]["code"])
        # 两轮都携带只读 db.js 事实源。
        for call in calls:
            self.assertIn("RELATIONS", call["authoritative_files"]["server/db.js"])
        # llm_repair_count 只统计最终成功落盘的 LLM 修复次数（既有语义）。
        self.assertEqual(result["llm_repair_count"], 1)
        self.assertNotIn("resumez", sandbox.files["server/index.js"])
        self.assertIn("attempt-one-marker", sandbox.files["server/index.js"])

    def test_llm_repair_failure_falls_back_to_template(self) -> None:
        """LLM 不可用/失败时不得伪装修复：Applier 继续拦截并最终报错。"""

        class _BrokenLLM:
            available = True

            def repair(self, **kwargs):
                raise RuntimeError("model timeout")

        sandbox = _MemorySandbox()
        sandbox.files = _base_files()
        buffer = RunEventBuffer("fsk_llm_repair_failed")
        runner = FullstackGraphRunner(
            sandbox,
            event_sink=buffer,
            backend_llm=_BrokenLLM(),
        )

        from ai_core.fullstack import graph as graph_module

        real_generator = generate_backend_bundle

        def generate_with_bad_join(*args, **kwargs):
            bundle = real_generator(*args, **kwargs)
            files = dict(bundle.files)
            files["server/index.js"] = files["server/index.js"].replace(
                "/:id/resumes", "/:id/resumez"
            )
            return GeneratedBundle(
                files=files,
                summary=bundle.summary,
                metadata=bundle.metadata,
            )

        with patch.object(
            graph_module,
            "generate_backend_bundle",
            side_effect=generate_with_bad_join,
        ):
            with self.assertRaises(Exception) as ctx:
                runner.run(
                    {
                        "run_id": "fsk_llm_repair_failed",
                        "project_id": "proj_fullstack",
                        "sandbox_id": sandbox.handle.sandbox_id,
                        "preview_url": sandbox.handle.preview_url,
                        "contract": _master_detail_contract(),
                        "files": [],
                    }
                )

        self.assertIn("Applier AST 门禁连续拦截", str(ctx.exception))
        # 沙盒从未写入被拦截的 server/index.js。
        self.assertNotIn("server/index.js", sandbox.files)
        failed_events = [
            event
            for event in buffer.snapshot()
            if event.type == "fullstack.applier.llm_repair"
            and event.payload["status"] == "FAILED"
        ]
        self.assertEqual(len(failed_events), 2)

    def test_consistency_join_repair_passes_feedback_to_llm(self) -> None:
        """一致性检查发现 JOIN 关系错位时，同样把诊断交给 LLM。

        Applier 静态门禁通常会更早拦截同类错误；这里注入一致性报告，
        单独验证 ``ConsistencyVerifier -> applier_feedback -> LLM`` 链路。
        """

        class _RecordingLLM:
            available = True

            def __init__(self) -> None:
                self.diagnostics: list[list[str]] = []

            def repair(self, *, bundle, diagnostics, **kwargs):
                self.diagnostics.append(list(diagnostics))
                return bundle

        llm = _RecordingLLM()
        sandbox = _MemorySandbox()
        sandbox.files = _base_files()
        buffer = RunEventBuffer("fsk_join_feedback")
        runner = FullstackGraphRunner(
            sandbox,
            event_sink=buffer,
            backend_llm=llm,
        )

        from ai_core.fullstack import graph as graph_module
        from ai_core.fullstack.consistency import (
            BackendRoute,
            ConsistencyIssue,
            ConsistencyReport,
        )

        bad_report = ConsistencyReport(
            calls_checked=1,
            backend_routes=(BackendRoute("GET", "/api/v1/users"),),
            issues=(
                ConsistencyIssue(
                    kind="join_schema",
                    component="BackendCoder",
                    method="",
                    path="server/index.js",
                    message=(
                        "JOIN 报错：resumez 字段不存在；include 别名必须与 "
                        "RELATIONS 的 as 完全一致"
                    ),
                    suggestion="按 relations 重建 include 与嵌套路由",
                    fix_action="REPAIR_BACKEND_JOIN",
                ),
            ),
        )
        original = graph_module.verify_consistency
        calls: list[int] = []

        def fake_verify(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                return bad_report
            return original(*args, **kwargs)

        with patch.object(graph_module, "verify_consistency", side_effect=fake_verify):
            runner.run(
                {
                    "run_id": "fsk_join_feedback",
                    "project_id": "proj_fullstack",
                    "sandbox_id": sandbox.handle.sandbox_id,
                    "preview_url": sandbox.handle.preview_url,
                    "contract": _master_detail_contract(),
                    "files": [],
                }
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual(len(llm.diagnostics), 1)
        self.assertTrue(any("resumez" in item for item in llm.diagnostics[0]))
        repairs = [
            event
            for event in buffer.snapshot()
            if event.type == "fullstack.consistency.repair"
        ]
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0].payload["reason"], "JOIN_SCHEMA_MISMATCH")
        self.assertEqual(sandbox.restarts, [(sandbox.handle.sandbox_id, "/api/health")])

    def test_applier_blocks_bad_join_then_backend_self_heals(self) -> None:
        """E2E：JOIN 别名写错 -> Applier 拦截 -> 代码缺陷 -> 重试成功。

        第一次 BackendCoder 产物把嵌套路由别名写成 ``resumez``；Applier
        必须在落盘前拦截。``audit_contract`` 确认契约本身有 users→resumes
        关系，因此归因为 ``CODE_DEFECT``，只重跑 BackendCoder；第二次产物
        正确后正常构建与重启。
        """
        FULLSTACK_METRICS.reset()
        sandbox = _MemorySandbox()
        sandbox.files = _base_files()
        contract = _master_detail_contract()
        buffer = RunEventBuffer("fsk_applier_join_repair")
        runner = FullstackGraphRunner(sandbox, event_sink=buffer)

        from ai_core.fullstack import graph as graph_module

        real_generator = generate_backend_bundle
        calls = {"count": 0}

        def generate_with_bad_join(*args, **kwargs):
            bundle = real_generator(*args, **kwargs)
            calls["count"] += 1
            if calls["count"] != 1:
                return bundle
            files = dict(bundle.files)
            code = files["server/index.js"]
            self.assertIn("/:id/resumes", code)
            files["server/index.js"] = code.replace("/:id/resumes", "/:id/resumez")
            return GeneratedBundle(
                files=files,
                summary=bundle.summary,
                metadata=bundle.metadata,
            )

        with patch.object(
            graph_module,
            "generate_backend_bundle",
            side_effect=generate_with_bad_join,
        ):
            result = runner.run(
                {
                    "run_id": "fsk_applier_join_repair",
                    "project_id": "proj_fullstack",
                    "sandbox_id": sandbox.handle.sandbox_id,
                    "preview_url": sandbox.handle.preview_url,
                    "contract": contract,
                    "files": [],
                }
            )

        # 第一次被拦截并重试，第二次落盘成功。
        self.assertEqual(calls["count"], 2)
        self.assertNotIn("resumez", sandbox.files["server/index.js"])
        self.assertIn("/api/v1/users/:id/resumes", sandbox.files["server/index.js"])
        self.assertIn("server/index.js", result["files"])
        self.assertEqual(sandbox.restarts, [(sandbox.handle.sandbox_id, "/api/health")])

        events = buffer.snapshot()
        blocked = [
            event for event in events if event.type == "fullstack.applier.blocked"
        ]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0].payload["defectKind"], "CODE_DEFECT")
        self.assertTrue(blocked[0].payload["issues"])
        self.assertFalse(
            any(
                event.type == "fullstack.node.failed"
                and event.payload["node"] == "ConsistencyVerifier"
                for event in events
            )
        )

        metrics = FULLSTACK_METRICS.snapshot()
        applier_rate = metrics["Applier_Block_Rate"]
        self.assertEqual(applier_rate["numerator"], 1)
        self.assertEqual(applier_rate["denominator"], 3)
        self.assertAlmostEqual(applier_rate["rate"], 1 / 3, places=5)
        self.assertEqual(metrics["Contract_Misretry_Count"], 0)
        pass_rate = metrics["MultiTable_AST_Pass_Rate"]
        self.assertGreaterEqual(pass_rate["numerator"], 2)
        self.assertGreaterEqual(pass_rate["denominator"], 3)


class RiskAdaptiveGuardrailTests(unittest.TestCase):
    """L1 / L2 / L3 三种质量分级的门禁行为差异。"""

    @staticmethod
    def _state(sandbox: _MemorySandbox, contract: ArchitectContract) -> dict:
        return {
            "run_id": f"fsk_quality_{uuid.uuid4().hex[:6]}",
            "project_id": "proj_fullstack",
            "sandbox_id": sandbox.handle.sandbox_id,
            "preview_url": sandbox.handle.preview_url,
            "contract": contract,
            "files": [],
            "quality_tier": contract.quality_tier,
            "warnings": [],
            "build_retry_count": 0,
        }

    def test_l1_applier_skips_ast_gate_l2_still_blocks(self) -> None:
        """L1 允许带语法缺陷的 Demo 产物落盘；L2 必须继续拦截。"""
        bad_files = {"server/db.js": "const broken = ;"}

        l1_sandbox = _MemorySandbox()
        Applier(l1_sandbox, _contract(qualityTier=L1_PROTOTYPE)).apply(
            l1_sandbox.handle.sandbox_id,
            bad_files,
        )
        self.assertIn("server/db.js", l1_sandbox.files)

        l2_sandbox = _MemorySandbox()
        with self.assertRaises(ApplierSafetyBlock):
            Applier(l2_sandbox, _contract(qualityTier=L2_STANDARD)).apply(
                l2_sandbox.handle.sandbox_id,
                bad_files,
            )
        self.assertNotIn("server/db.js", l2_sandbox.files)

    def test_l1_skips_orm_schema_dimension(self) -> None:
        """L1 不执行 check_orm_schema，允许 ORM 字段模糊匹配。"""
        contract = _contract(qualityTier=L1_PROTOTYPE)
        called: list[bool] = []
        original = sys.modules["ai_core.fullstack.consistency"].check_orm_syntax

        def _spy(*_args, **_kwargs):
            called.append(True)
            return original(*_args, **_kwargs)

        sys.modules["ai_core.fullstack.consistency"].check_orm_syntax = _spy
        try:
            verify_consistency({}, contract)
        finally:
            sys.modules["ai_core.fullstack.consistency"].check_orm_syntax = original
        self.assertEqual(called, [])

        l2_called: list[bool] = []
        l2_contract = _contract(qualityTier=L2_STANDARD)
        sys.modules["ai_core.fullstack.consistency"].check_orm_syntax = (
            lambda *args, **kwargs: (
                l2_called.append(True) or original(*args, **kwargs)
            )
        )
        try:
            verify_consistency({}, l2_contract)
        finally:
            sys.modules["ai_core.fullstack.consistency"].check_orm_syntax = original
        self.assertEqual(l2_called, [True])

    def test_l1_build_failure_retries_once_then_warns(self) -> None:
        """L1 构建失败最多重试 1 次；仍失败则告警并继续重启沙盒。"""
        sandbox = _FlakySandbox(fail_checks=("build",))
        sandbox.files = _base_files()
        buffer = RunEventBuffer("fsk_l1_build_retry")
        runner = FullstackGraphRunner(sandbox, event_sink=buffer)
        contract = _contract(qualityTier=L1_PROTOTYPE)

        outcome = runner.run(self._state(sandbox, contract))

        build_calls = [item for item in sandbox.verified if item[1] == "build"]
        self.assertEqual(len(build_calls), 2)  # 首次 + 1 次重试
        self.assertEqual(outcome.get("build_retry_count"), 1)
        self.assertTrue(
            any(L1_WARNING_TEXT in item for item in outcome.get("warnings") or [])
        )
        self.assertEqual(len(sandbox.restarts), 1)
        event_types = [event.type for event in buffer.snapshot()]
        self.assertIn("fullstack.build.retry", event_types)
        self.assertIn("fullstack.quality.warning", event_types)

    def test_l1_consistency_issues_are_warnings_not_blocking(self) -> None:
        """L1 一致性问题降级为告警，不再触发 BackendCoder 自愈重试。"""
        sandbox = _MemorySandbox()
        sandbox.files = _base_files()
        buffer = RunEventBuffer("fsk_l1_consistency")
        runner = FullstackGraphRunner(sandbox, event_sink=buffer)
        contract = _contract(qualityTier=L1_PROTOTYPE)

        from ai_core.fullstack import graph as graph_module
        from ai_core.fullstack.consistency import (
            BackendRoute,
            ConsistencyIssue,
            ConsistencyReport,
        )

        failing = ConsistencyReport(
            calls_checked=1,
            backend_routes=(BackendRoute("GET", "/api/v1/todos"),),
            issues=(
                ConsistencyIssue(
                    kind="path",
                    component="TaskStream",
                    method="GET",
                    path="/api/v1/tasks/stream",
                    message="后端未注册该路径",
                    suggestion="由 BackendCoder 补齐",
                    fix_action="ADD_BACKEND_ROUTE",
                ),
            ),
        )
        original = graph_module.verify_consistency
        graph_module.verify_consistency = lambda *_args, **_kwargs: failing
        try:
            outcome = runner.run(self._state(sandbox, contract))
        finally:
            graph_module.verify_consistency = original

        started = [
            event.payload["node"]
            for event in buffer.snapshot()
            if event.type == "fullstack.node.started"
        ]
        # 只跑一轮 BackendCoder，没有触发一致性自愈循环。
        self.assertEqual(started.count("BackendCoder"), 1)
        self.assertTrue(outcome.get("consistency_ok"))
        self.assertTrue(
            any("L1 一致性告警" in item for item in outcome.get("warnings") or [])
        )

    def test_l3_runs_real_mysql_check_and_l2_does_not(self) -> None:
        """L3 必须调用真实 MySQL 验收；L2 明确跳过。"""
        from ai_core.fullstack import graph as graph_module

        successful = MysqlMigrationCheck(
            enabled=True,
            ok=True,
            database="vibe_l3_verify_test",
            table="todos",
            expected_columns=("id", "name"),
            actual_columns=("id", "name"),
            statements=1,
        )
        calls: list[str] = []

        def _fake_mysql(_files, contract, **_kwargs):
            calls.append(contract.quality_tier)
            return successful

        original = graph_module.verify_mysql_migration
        graph_module.verify_mysql_migration = _fake_mysql
        try:
            l3_sandbox = _MemorySandbox()
            l3_sandbox.files = _base_files()
            l3_outcome = FullstackGraphRunner(l3_sandbox).run(
                self._state(l3_sandbox, _contract(qualityTier=L3_PRODUCTION))
            )
            self.assertEqual(calls, [L3_PRODUCTION])
            self.assertTrue(l3_outcome.get("mysql_check", {}).get("ok"))

            calls.clear()
            l2_sandbox = _MemorySandbox()
            l2_sandbox.files = _base_files()
            FullstackGraphRunner(l2_sandbox).run(
                self._state(l2_sandbox, _contract(qualityTier=L2_STANDARD))
            )
            self.assertEqual(calls, [])
        finally:
            graph_module.verify_mysql_migration = original

    def test_quality_policies_match_requested_levels(self) -> None:
        l1 = quality_policy(L1_PROTOTYPE)
        self.assertFalse(l1.ast_gate)
        self.assertFalse(l1.orm_schema_check)
        self.assertFalse(l1.blocking)
        self.assertEqual(l1.build_retry_limit, 1)

        l2 = quality_policy(L2_STANDARD)
        self.assertTrue(l2.ast_gate)
        self.assertTrue(l2.sqlite_schema_check)
        self.assertFalse(l2.mysql_migration_check)
        self.assertTrue(l2.blocking)

        l3 = quality_policy(L3_PRODUCTION)
        self.assertTrue(l3.orm_schema_check)
        self.assertTrue(l3.mysql_migration_check)
        self.assertTrue(l3.blocking)


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

    def test_contract_contradiction_failure_carries_clarification(self) -> None:
        """契约矛盾必须在生成前失败，并把“重新澄清”信息送到前端。"""
        sandbox = _MemorySandbox()
        service = FullstackService(sandbox=sandbox)
        run_id = f"fsk_{uuid.uuid4().hex[:12]}"
        contract = _resume_contract(
            componentInteractions=[
                ArchitectComponentInteraction(
                    component="GhostButton",
                    event="click",
                    api_route="GET /api/v1/ghost",
                    logic="不存在的接口",
                )
            ]
        )
        request = FullstackRunRequest.model_validate(
            {
                "runId": run_id,
                "projectId": "proj_fullstack",
                "contractId": contract.contract_id,
                "contract": contract.model_dump(mode="json", by_alias=True),
                "source": {"files": _base_files()},
            }
        )
        active = service.start_stream(request)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            snapshot = service.snapshot(run_id)
            if snapshot.status in {"SUCCEEDED", "FAILED"}:
                break
            time.sleep(0.02)

        snapshot = service.snapshot(run_id)
        self.assertEqual(snapshot.status, "FAILED")
        self.assertEqual(snapshot.error_code, "CONTRACT_REVISION_REQUIRED")
        self.assertEqual(snapshot.failure_kind, "CONTRACT")
        self.assertEqual(snapshot.defect_kind, "CONTRACT_DEFECT")
        self.assertTrue(snapshot.needs_clarification)
        self.assertIn("/api/v1/ghost", snapshot.clarification["prompt"])
        failed = next(
            event
            for event in active.buffer.snapshot()
            if event.type == "fullstack.failed"
        )
        self.assertTrue(failed.payload["needsClarification"])
        self.assertEqual(failed.payload["failureKind"], "CONTRACT")
        self.assertEqual(failed.payload["defectKind"], "CONTRACT_DEFECT")
        self.assertIn("metrics", failed.payload)
        self.assertEqual(sandbox.verified, [])

    def test_nested_route_without_child_table_requires_contract_revision(
        self,
    ) -> None:
        """契约要查询 users/:id/resumes，但 DDL 没建 resumes 表。

        这不是代码拼写问题：任何重试都无法凭空建出子表。服务层必须在
        生成前判 ``CONTRACT_DEFECT``，直接要求前端重修契约，且不触发
        任何沙盒构建。
        """
        sandbox = _MemorySandbox()
        service = FullstackService(sandbox=sandbox)
        run_id = f"fsk_{uuid.uuid4().hex[:12]}"
        only_users_ddl = """CREATE TABLE users (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(128) NOT NULL
);"""
        contract = _contract(
            apiRoutes=[
                ArchitectApiRoute(
                    method="GET", path="/api/v1/users", description="用户列表"
                ),
                ArchitectApiRoute(
                    method="GET",
                    path="/api/v1/users/:id/resumes",
                    description="用户简历（子表未建）",
                ),
            ],
            componentInteractions=[
                ArchitectComponentInteraction(
                    component="UserList",
                    event="load",
                    api_route="GET /api/v1/users",
                    logic="加载用户列表",
                )
            ],
            databaseDdl=only_users_ddl,
        )
        request = FullstackRunRequest.model_validate(
            {
                "runId": run_id,
                "projectId": "proj_fullstack",
                "contractId": contract.contract_id,
                "contract": contract.model_dump(mode="json", by_alias=True),
                "source": {"files": _base_files()},
            }
        )
        active = service.start_stream(request)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            snapshot = service.snapshot(run_id)
            if snapshot.status in {"SUCCEEDED", "FAILED"}:
                break
            time.sleep(0.02)

        snapshot = service.snapshot(run_id)
        self.assertEqual(snapshot.status, "FAILED")
        self.assertEqual(snapshot.error_code, "CONTRACT_REVISION_REQUIRED")
        self.assertEqual(snapshot.defect_kind, "CONTRACT_DEFECT")
        self.assertTrue(snapshot.needs_clarification)
        self.assertTrue(
            any(
                problem["code"] == "nested_table_missing"
                for problem in snapshot.clarification["problems"]
            )
        )
        failed = next(
            event
            for event in active.buffer.snapshot()
            if event.type == "fullstack.failed"
        )
        self.assertEqual(failed.payload["defectKind"], "CONTRACT_DEFECT")
        self.assertFalse(
            any(
                event.type == "fullstack.node.started"
                for event in active.buffer.snapshot()
            )
        )
        self.assertEqual(sandbox.verified, [])

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
        # L2 不做真实 MySQL 迁移验收，事件里必须是 null 而不是空对象，
        # 否则前端会把 {} 误渲染成“验收失败”。
        self.assertIsNone(completed.payload["mysqlCheck"])


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
