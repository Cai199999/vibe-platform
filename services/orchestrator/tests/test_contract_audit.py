"""契约结构审计：契约矛盾必须确定性识别，不能归咎于生成代码。"""

from __future__ import annotations

import unittest

from ai_core.domain.architect import (
    ArchitectApiRoute,
    ArchitectComponentInteraction,
    ArchitectTableRelation,
)
from ai_core.fullstack.contract_audit import audit_contract, validate_contract
from tests.test_fullstack import DDL, _contract, _resume_contract


class ContractAuditTest(unittest.TestCase):
    def test_valid_contract_passes(self) -> None:
        audit = audit_contract(_contract())
        self.assertTrue(audit.ok, audit.summary())
        self.assertEqual(audit.failure_kind, "CODE")

    def test_missing_ddl_is_contract_problem(self) -> None:
        audit = audit_contract(_contract(databaseDdl=""))
        self.assertFalse(audit.ok)
        self.assertTrue(any(item.code == "missing_ddl" for item in audit.problems))
        self.assertEqual(audit.failure_kind, "CONTRACT")

    def test_missing_api_routes_is_contract_problem(self) -> None:
        audit = audit_contract(_contract(apiRoutes=[]))
        self.assertTrue(
            any(item.code == "missing_api_routes" for item in audit.problems)
        )
        self.assertIn("重新确认", audit.clarification_prompt())

    def test_dba_mode_does_not_require_api_routes(self) -> None:
        audit = audit_contract(_contract(apiRoutes=[]), mode="dba")
        self.assertTrue(audit.ok, audit.summary())

    def test_relation_to_unknown_column_is_contract_problem(self) -> None:
        contract = _contract(
            relations=[
                ArchitectTableRelation(
                    parentTable="todos",
                    childTable="todos",
                    foreignKey="owner_id",
                )
            ]
        )
        audit = audit_contract(contract)
        self.assertTrue(
            any(item.code == "invalid_relation" for item in audit.problems),
            audit.summary(),
        )

    def test_interaction_route_must_be_declared(self) -> None:
        contract = _resume_contract(
            componentInteractions=[
                ArchitectComponentInteraction(
                    component="Ghost",
                    event="click",
                    api_route="GET /api/v1/resumes/missing",
                    logic="未声明接口",
                )
            ]
        )
        audit = audit_contract(contract)
        self.assertFalse(audit.ok)
        problem = next(
            item
            for item in audit.problems
            if item.code == "undeclared_interaction_route"
        )
        self.assertIn("/api/v1/resumes/missing", problem.message)

    def test_kebab_route_matches_snake_case_ddl_table(self) -> None:
        """REST kebab-case 子资源必须能匹配 DDL 的 snake_case 表。"""
        contract = _contract(
            apiRoutes=[
                ArchitectApiRoute(
                    method="GET", path="/api/v1/users", description="用户列表"
                ),
                ArchitectApiRoute(
                    method="GET",
                    path="/api/v1/users/{user_id}/job-descriptions",
                    description="用户职位描述",
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
            databaseDdl=(
                "CREATE TABLE users ("
                "id BIGINT PRIMARY KEY, name VARCHAR(64));"
                "CREATE TABLE job_descriptions ("
                "id BIGINT PRIMARY KEY, user_id BIGINT, title VARCHAR(128));"
            ),
        )

        audit = audit_contract(contract)

        self.assertTrue(audit.ok, audit.summary())

    def test_snake_case_table_still_required_for_nested_route(self) -> None:
        contract = _contract(
            apiRoutes=[
                ArchitectApiRoute(
                    method="GET", path="/api/v1/users", description="用户列表"
                ),
                ArchitectApiRoute(
                    method="GET",
                    path="/api/v1/users/{user_id}/job-descriptions",
                    description="用户职位描述",
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
            databaseDdl=(
                "CREATE TABLE users (id BIGINT PRIMARY KEY, name VARCHAR(64));"
            ),
        )

        audit = audit_contract(contract)

        self.assertFalse(audit.ok)
        self.assertTrue(
            any(item.code == "nested_table_missing" for item in audit.problems),
            audit.summary(),
        )
        problem = next(
            item for item in audit.problems if item.code == "nested_table_missing"
        )
        self.assertIn("job_descriptions", problem.hint)

    def test_auth_action_routes_map_to_users_table(self) -> None:
        """登录 / 注册是 users 表上的动作，不得要求 DDL 存在 auth 表。"""
        contract = _contract(
            apiRoutes=[
                ArchitectApiRoute(
                    method="POST", path="/api/v1/auth/login", description="登录"
                ),
                ArchitectApiRoute(
                    method="POST", path="/api/v1/auth/register", description="注册"
                ),
                ArchitectApiRoute(
                    method="GET", path="/api/v1/auth/me", description="当前用户"
                ),
                ArchitectApiRoute(
                    method="GET", path="/api/v1/resumes", description="简历列表"
                ),
            ],
            componentInteractions=[
                ArchitectComponentInteraction(
                    component="ResumeList",
                    event="load",
                    api_route="GET /api/v1/resumes",
                    logic="加载简历",
                )
            ],
            databaseDdl=(
                "CREATE TABLE users (id BIGINT PRIMARY KEY, name VARCHAR(64));"
                "CREATE TABLE resumes (id BIGINT PRIMARY KEY, user_id BIGINT);"
            ),
        )

        audit = audit_contract(contract)

        self.assertTrue(audit.ok, audit.summary())

    def test_auth_only_contract_does_not_fall_back_to_records_table(self) -> None:
        """只有动作端点时不能把兜底 /records 当成真实集合去报缺表。"""
        contract = _contract(
            apiRoutes=[
                ArchitectApiRoute(
                    method="POST", path="/api/v1/auth/login", description="登录"
                )
            ],
            componentInteractions=[],
            databaseDdl=(
                "CREATE TABLE users (id BIGINT PRIMARY KEY, name VARCHAR(64));"
            ),
        )

        audit = audit_contract(contract)

        self.assertTrue(audit.ok, audit.summary())

    def test_collection_resource_must_exist_in_ddl(self) -> None:
        contract = _contract(
            databaseDdl="CREATE TABLE resumes (id BIGINT PRIMARY KEY, name TEXT);"
        )
        audit = audit_contract(contract)
        self.assertTrue(
            any(item.code == "collection_table_missing" for item in audit.problems),
            audit.summary(),
        )

    def test_ddl_without_create_table_is_contract_problem(self) -> None:
        audit = audit_contract(_contract(databaseDdl="-- 空 DDL"))
        self.assertTrue(any(item.code == "missing_ddl" for item in audit.problems))

    def test_nested_route_without_child_table(self) -> None:
        """契约要求 users/:id/resumes，但 DDL 没有 resumes 表。"""
        contract = _contract(
            apiRoutes=[
                ArchitectApiRoute(
                    method="GET", path="/api/v1/users", description="用户列表"
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
            databaseDdl=(
                "CREATE TABLE users (id BIGINT PRIMARY KEY, name VARCHAR(64));"
            ),
        )
        audit = audit_contract(contract)
        self.assertFalse(audit.ok)
        self.assertTrue(
            any(item.code == "nested_table_missing" for item in audit.problems),
            audit.summary(),
        )
        self.assertEqual(audit.defect_kind, "CONTRACT_DEFECT")

    def test_nested_route_without_relation(self) -> None:
        """两张表都存在，但 DDL 没有外键列、relations 也没声明 JOIN。"""
        ddl = """CREATE TABLE users (
  id BIGINT PRIMARY KEY,
  name VARCHAR(64)
);
CREATE TABLE resumes (
  id BIGINT PRIMARY KEY,
  file_name VARCHAR(255)
);"""
        contract = _contract(
            apiRoutes=[
                ArchitectApiRoute(
                    method="GET", path="/api/v1/users", description="用户列表"
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
            relations=[],
        )
        audit = audit_contract(contract)
        self.assertFalse(audit.ok)
        self.assertTrue(
            any(item.code == "missing_relation" for item in audit.problems),
            audit.summary(),
        )

    def test_action_endpoint_is_not_nested_resource(self) -> None:
        """``users/:id/status`` 是动作端点，不能误判为缺失子表。"""
        contract = _contract(
            apiRoutes=[
                ArchitectApiRoute(
                    method="GET", path="/api/v1/users", description="用户列表"
                ),
                ArchitectApiRoute(
                    method="GET",
                    path="/api/v1/users/:id/status",
                    description="查询用户状态",
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
            databaseDdl=(
                "CREATE TABLE users (id BIGINT PRIMARY KEY, name VARCHAR(64));"
            ),
        )
        audit = audit_contract(contract)
        self.assertTrue(audit.ok, audit.summary())

    def test_architect_validate_contract_matches_runtime_audit(self) -> None:
        audit = validate_contract(_contract())
        self.assertTrue(audit.ok, audit.summary())
        broken = validate_contract(_contract(databaseDdl=""))
        self.assertFalse(broken.ok)
        self.assertEqual(broken.defect_kind, "CONTRACT_DEFECT")

    def test_reference_ddl_fixture_is_self_consistent(self) -> None:
        self.assertIn("CREATE TABLE todos", DDL)
        self.assertTrue(audit_contract(_resume_contract()).ok)


if __name__ == "__main__":
    unittest.main()
