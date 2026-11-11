"""契约结构审计：契约矛盾必须确定性识别，不能归咎于生成代码。"""

from __future__ import annotations

import unittest

from ai_core.domain.architect import (
    ArchitectComponentInteraction,
    ArchitectTableRelation,
)
from ai_core.fullstack.contract_audit import audit_contract
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

    def test_reference_ddl_fixture_is_self_consistent(self) -> None:
        self.assertIn("CREATE TABLE todos", DDL)
        self.assertTrue(audit_contract(_resume_contract()).ok)


if __name__ == "__main__":
    unittest.main()
