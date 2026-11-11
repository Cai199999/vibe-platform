"""ConsistencyVerifier：跨端 Method / Path / Payload 一致性测试。

全部为纯静态测试，不依赖网络、Docker 或模型；重点覆盖：

  1. 正常生成的前后端（面板 + 契约交互）必须通过；
  2. 前端路径漂移（如 ``/api/v1/tasks/stream`` vs ``/api/tasks``）
     必须被识别，并给出可执行的后端路由建议；
  3. Method 不一致与 Payload 字段不在 DDL 中必须分别报告；
  4. ``LOCAL`` 交互与非 ``/api/`` 路径不参与比对。
"""

from __future__ import annotations

import json
import unittest

from ai_core.domain.architect import (
    ArchitectApiRoute,
    ArchitectComponentInteraction,
    ArchitectContract,
)
from ai_core.fullstack.coders import (
    generate_backend_bundle,
    generate_dba_bundle,
    generate_frontend_skeleton_bundle,
)
from ai_core.fullstack.consistency import (
    ConsistencyError,
    ensure_consistency,
    extract_backend_routes,
    extract_frontend_calls,
    verify_consistency,
)

DDL = """CREATE TABLE tasks (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  tenant_id VARCHAR(64) NOT NULL,
  name VARCHAR(200) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'todo',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  deleted_at TIMESTAMP NULL
);"""


def _contract(
    *,
    routes: list[ArchitectApiRoute] | None = None,
    interactions: list[ArchitectComponentInteraction] | None = None,
    database_ddl: str | None = None,
) -> ArchitectContract:
    return ArchitectContract.model_validate(
        {
            "contractId": "ctr_consistency_test",
            "runId": "arc_consistency_test",
            "appShape": "full_stack_system",
            "status": "CONFIRMED",
            "confidenceScore": 0.93,
            "contractMarkdown": "# 任务系统全栈契约",
            "techStack": {
                "backend": "node_express",
                "databaseEngine": "mysql",
                "orm": "sequelize",
            },
            "apiRoutes": (
                routes
                if routes is not None
                else [
                    ArchitectApiRoute(method="GET", path="/api/v1/tasks"),
                    ArchitectApiRoute(method="POST", path="/api/v1/tasks"),
                    ArchitectApiRoute(method="GET", path="/api/v1/tasks/:id"),
                    ArchitectApiRoute(method="DELETE", path="/api/v1/tasks/:id"),
                ]
            ),
            "databaseDdl": database_ddl or DDL,
            "components": [
                {"name": "TaskList", "section": "主内容区"},
                {"name": "SubmitButton", "role": "button", "section": "主内容区"},
            ],
            "componentInteractions": (
                interactions
                if interactions is not None
                else [
                    ArchitectComponentInteraction(
                        component="TaskList",
                        event="load",
                        api_route="GET /api/v1/tasks",
                        logic="加载任务列表",
                    ),
                    ArchitectComponentInteraction(
                        component="SubmitButton",
                        event="click",
                        api_route="POST /api/v1/tasks",
                        logic="提交后保存并刷新列表",
                    ),
                ]
            ),
        }
    )


def _app_with_interactions(interactions: list[dict[str, object]]) -> str:
    return (
        "import { useState } from 'react'\n"
        f"const INTERACTIONS = {json.dumps(interactions, ensure_ascii=False, indent=2)}\n"
        "export default function App() { return <main /> }\n"
    )


class ConsistencyTest(unittest.TestCase):
    def test_generated_fullstack_frontend_matches_backend(self) -> None:
        contract = _contract()
        backend = generate_backend_bundle(contract)
        skeleton = generate_frontend_skeleton_bundle(contract)
        # 模拟“先初始化前端骨架、再生成全栈”的真实沙盒状态。
        files = {**backend.files, **skeleton.files}

        report = verify_consistency(files, contract)

        self.assertTrue(report.ok, report.failure_message())
        # 骨架里的 load / submit 两个交互；全栈数据面板另行覆盖。
        self.assertEqual(report.calls_checked, 2)
        routes = {(route.method, route.path) for route in report.backend_routes}
        self.assertIn(("GET", "/api/v1/tasks"), routes)
        self.assertIn(("POST", "/api/v1/tasks"), routes)
        self.assertIn(("DELETE", "/api/v1/tasks/:id"), routes)

    def test_path_mismatch_emits_backend_fix_action(self) -> None:
        contract = _contract(
            interactions=[
                ArchitectComponentInteraction(
                    component="TaskList",
                    event="load",
                    api_route="/api/v1/tasks/stream",
                    logic="订阅任务流",
                )
            ]
        )
        backend = generate_backend_bundle(contract)
        skeleton = generate_frontend_skeleton_bundle(contract)
        files = {**backend.files, **skeleton.files}

        with self.assertRaises(ConsistencyError) as ctx:
            ensure_consistency(files, contract)

        report = ctx.exception.report
        self.assertFalse(report.ok)
        self.assertEqual(len(report.issues), 1)
        issue = report.issues[0]
        self.assertEqual(issue.kind, "path")
        self.assertEqual(issue.path, "/api/v1/tasks/stream")
        self.assertEqual(issue.fix_action, "ADD_BACKEND_ROUTE")
        self.assertIn("BackendCoder", issue.suggestion)
        self.assertIn("前端请求路径保持不变", issue.suggestion)
        # 绝不允许通过把业务请求改到无关 CRUD 路径来“消掉”差异。
        self.assertNotIn("把前端请求改为", issue.suggestion)
        self.assertNotIn("把前端请求改为", str(ctx.exception))
        self.assertTrue(report.repairable)
        self.assertEqual(
            report.missing_routes(),
            (
                {
                    "method": "GET",
                    "path": "/api/v1/tasks/stream",
                    "description": "TaskList：后端未注册该路径（Method + Path 完全匹配失败）",
                },
            ),
        )
        self.assertIn("修复动作", str(ctx.exception))

    def test_method_mismatch_lists_allowed_methods(self) -> None:
        contract = _contract(
            routes=[ArchitectApiRoute(method="GET", path="/api/v1/tasks")]
        )
        backend = generate_backend_bundle(contract)
        files = {
            **backend.files,
            "src/App.jsx": _app_with_interactions(
                [
                    {
                        "component": "SubmitButton",
                        "method": "DELETE",
                        "apiRoute": "/api/v1/tasks",
                        "payloadKey": "",
                    }
                ]
            ),
        }

        report = verify_consistency(files, contract)

        self.assertFalse(report.ok)
        issue = report.issues[0]
        self.assertEqual(issue.kind, "method")
        self.assertIn("GET", issue.message)
        self.assertIn("DELETE", issue.message)
        self.assertEqual(issue.fix_action, "ADD_BACKEND_ROUTE")
        self.assertIn("BackendCoder", issue.suggestion)
        self.assertIn("前端 Method 保持不变", issue.suggestion)

    def test_payload_key_must_exist_in_ddl(self) -> None:
        contract = _contract()
        backend = generate_backend_bundle(contract)
        files = {
            **backend.files,
            "src/App.jsx": _app_with_interactions(
                [
                    {
                        "component": "SubmitButton",
                        "method": "POST",
                        "apiRoute": "/api/v1/tasks",
                        # tasks 表没有 priority 列，前端却提交了该字段。
                        "payloadKey": "priority",
                    }
                ]
            ),
        }

        report = verify_consistency(files, contract)

        self.assertFalse(report.ok)
        issue = report.issues[0]
        self.assertEqual(issue.kind, "payload")
        self.assertIn("priority", issue.message)
        self.assertEqual(issue.fix_action, "EXTEND_BACKEND_FIELD")
        self.assertIn("BackendCoder", issue.suggestion)
        self.assertIn("无关业务接口", issue.suggestion)
        # Payload 冲突不能靠新增路由解决，自愈不应空转重试。
        self.assertFalse(report.repairable)

    def test_every_business_action_gets_a_matching_backend_route(self) -> None:
        """回归：抓取 / 改写 / 投递 / 对话不能被一个通用 CRUD 接口替代。"""
        business_routes = [
            ArchitectApiRoute(
                method="POST", path="/api/v1/jobs/scrape", description="抓取 JD"
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
        ]
        contract = _contract(routes=business_routes)
        backend = generate_backend_bundle(contract)
        code = backend.files["server/index.js"]
        manifest = json.loads(backend.files["server/api-routes.json"])

        expected = {
            ("POST", "/api/v1/jobs/scrape"),
            ("POST", "/api/v1/resumes/:id/rewrite"),
            ("POST", "/api/v1/applications/deliver"),
            ("POST", "/api/v1/chat/messages"),
        }
        actual = {(item["method"], item["path"]) for item in manifest["routes"]}
        self.assertTrue(expected.issubset(actual))
        for method, path in expected:
            self.assertIn(f'app.{method.lower()}("{path}"', code)
        self.assertEqual(backend.metadata["businessRouteCount"], len(business_routes))
        # 每个业务路由都有独立处理逻辑，而不是统一 404 或空壳。
        self.assertIn("沙盒 Mock 响应", code)
        self.assertIn("Object.assign", code)

        # 前端交互（来自同一契约）与后端实际路由必须完全对齐。
        interactions = [
            ArchitectComponentInteraction(
                component=f"Action{index}",
                event="click",
                api_route=f"{method} {path}",
                logic=description,
            )
            for index, (method, path, description) in enumerate(
                (
                    ("POST", "/api/v1/jobs/scrape", "抓取 JD"),
                    ("POST", "/api/v1/resumes/{id}/rewrite", "AI 改写简历"),
                    ("POST", "/api/v1/applications/deliver", "投递职位"),
                    ("POST", "/api/v1/chat/messages", "对话消息"),
                ),
                start=1,
            )
        ]
        frontend_contract = contract.model_copy(
            update={
                "components": [{"name": f"Action{index}"} for index in range(1, 5)],
                "component_interactions": interactions,
            }
        )
        skeleton = generate_frontend_skeleton_bundle(frontend_contract)
        report = verify_consistency(
            {**backend.files, **skeleton.files}, frontend_contract
        )
        self.assertTrue(report.ok, report.failure_message())

    def test_local_interactions_are_ignored(self) -> None:
        contract = _contract()
        backend = generate_backend_bundle(contract)
        files = {
            **backend.files,
            "src/App.jsx": _app_with_interactions(
                [
                    {
                        "component": "SubmitButton",
                        "method": "LOCAL",
                        "apiRoute": "",
                        "payloadKey": "",
                    },
                    {
                        "component": "TaskList",
                        "method": "GET",
                        "apiRoute": "/api/v1/tasks",
                        "payloadKey": "",
                    },
                ]
            ),
        }

        report = verify_consistency(files, contract)
        self.assertTrue(report.ok, report.failure_message())
        self.assertEqual(report.calls_checked, 1)

    def test_route_manifest_is_the_source_of_truth(self) -> None:
        """即使契约路径不同，也以后端实际落盘的 api-routes.json 为准。"""
        contract = _contract()
        manifest = {
            "prefix": "/api",
            "collection": "/api/tasks",
            "item": "/api/tasks/:id",
        }
        routes = extract_backend_routes(
            {"server/api-routes.json": json.dumps(manifest)},
            contract,
        )
        shapes = {(route.method, route.path) for route in routes}
        self.assertIn(("GET", "/api/tasks"), shapes)
        self.assertIn(("DELETE", "/api/tasks/:id"), shapes)
        self.assertNotIn(("GET", "/api/v1/tasks"), shapes)

    def test_parameter_names_do_not_affect_matching(self) -> None:
        contract = _contract()
        backend = generate_backend_bundle(contract)
        files = {
            **backend.files,
            "src/App.jsx": _app_with_interactions(
                [
                    {
                        "component": "TaskList",
                        "method": "DELETE",
                        "apiRoute": "/api/v1/tasks/:taskId",
                        "payloadKey": "",
                    }
                ]
            ),
        }

        report = verify_consistency(files, contract)
        self.assertTrue(report.ok, report.failure_message())

    def test_extract_frontend_calls_reads_panel_collection_path(self) -> None:
        contract = _contract()
        backend = generate_backend_bundle(contract)
        panel = (
            "const COLLECTION_PATH = '/api/v1/tasks'\n"
            "async function load() { await fetch(apiUrl(COLLECTION_PATH)) }\n"
            "async function submit() {\n"
            "  await fetch(apiUrl(COLLECTION_PATH), { method: 'POST',"
            " body: JSON.stringify({ name: value }) })\n"
            "}\n"
        )
        calls, checked = extract_frontend_calls(
            {**backend.files, "src/fullstack-api.jsx": panel}
        )
        pairs = {(call.method, call.path) for call in calls}
        self.assertIn(("GET", "/api/v1/tasks"), pairs)
        self.assertIn(("POST", "/api/v1/tasks"), pairs)
        self.assertEqual(checked, ["src/fullstack-api.jsx"])

    def test_orm_schema_mismatch_blocks_build(self) -> None:
        """回归 500 Unknown column：db.js 引用 DDL 不存在的字段必须阻断。

        构造真实事故现场：迁移创建 users + resumes，集合路由是
        /api/v1/resumes，但 ORM 仍按第一张 users 表生成 name/status。
        正确产物必须通过；被污染产物必须产出 REGENERATE_ORM。
        """
        multi_ddl = """CREATE TABLE users (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  iam_sub VARCHAR(128) NOT NULL UNIQUE,
  display_name VARCHAR(128),
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE resumes (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id BIGINT NOT NULL,
  file_name VARCHAR(255),
  content MEDIUMTEXT NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"""
        contract = _contract(
            routes=[
                ArchitectApiRoute(method="GET", path="/api/v1/resumes"),
                ArchitectApiRoute(method="POST", path="/api/v1/resumes"),
                ArchitectApiRoute(method="GET", path="/api/v1/resumes/:id"),
            ],
            interactions=[],
            database_ddl=multi_ddl,
        )
        dba = generate_dba_bundle(contract)
        backend = generate_backend_bundle(
            contract, dba_schema=dba.files["server/schema.core.json"]
        )
        files = {**dba.files, **backend.files}

        baseline = verify_consistency(files, contract)
        self.assertTrue(baseline.ok, baseline.failure_message())

        broken = dict(files)
        broken["server/db.js"] = (
            "const TABLE = 'users'\n"
            'const COLUMN_DEFS = [["id", "INTEGER"],'
            ' ["name", "TEXT"], ["status", "TEXT"]]\n'
        )
        broken["server/index.js"] = broken["server/index.js"].replace(
            "const TABLE = 'resumes'", "const TABLE = 'users'"
        )

        report = verify_consistency(broken, contract)
        self.assertFalse(report.ok)
        self.assertTrue(report.needs_orm_repair)
        self.assertIn(
            ("orm_schema", "REGENERATE_ORM"),
            {(issue.kind, issue.fix_action) for issue in report.issues},
        )
        messages = " ".join(issue.message for issue in report.issues)
        self.assertIn("users", messages)
        self.assertIn("name", messages)
        self.assertIn("server/db.js", report.checked_files)


if __name__ == "__main__":
    unittest.main()
