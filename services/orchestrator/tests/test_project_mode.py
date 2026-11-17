"""双轨制项目模式判定与存量契约门禁测试。"""

from __future__ import annotations

import unittest
from dataclasses import replace

from ai_core.architect.contract import (
    ContractDraft,
    assemble_contract,
    validate_draft,
)
from ai_core.domain.architect import (
    ArchitectApiRoute,
    ArchitectTableRelation,
)
from ai_core.domain.project_mode import (
    EXISTING_SOURCE,
    NEW_PROJECT,
    resolve_project_mode,
)
from ai_core.architect.confidence import (
    apply_confidence_guard,
    missing_slots_for,
)
from ai_core.domain.architect import ArchitectAnalysis
from ai_core.fullstack.contract_audit import audit_contract


class ProjectModeResolverTests(unittest.TestCase):
    def test_empty_project_is_new(self) -> None:
        decision = resolve_project_mode(files={}, sandbox_id=None)
        self.assertEqual(decision.mode, NEW_PROJECT)

    def test_non_empty_files_or_sandbox_is_existing(self) -> None:
        self.assertEqual(
            resolve_project_mode(files={"src/App.jsx": "export default 1"}).mode,
            EXISTING_SOURCE,
        )
        self.assertEqual(
            resolve_project_mode(files={}, sandbox_id="sbx_1").mode,
            EXISTING_SOURCE,
        )

    def test_declared_existing_is_only_a_fallback(self) -> None:
        self.assertEqual(
            resolve_project_mode(declared=EXISTING_SOURCE, files={}).mode,
            EXISTING_SOURCE,
        )
        self.assertEqual(
            resolve_project_mode(declared=NEW_PROJECT, files={"a.js": "x"}).mode,
            EXISTING_SOURCE,
        )


def _existing_draft() -> ContractDraft:
    return ContractDraft(
        contract_markdown="# 存量项目后端增量\n\n" + ("说明。" * 80),
        tech_stack={"backend": "Node", "database": "mysql"},
        api_routes=[
            ArchitectApiRoute(
                method="GET",
                path="/api/v1/users",
                request="",
                response="[]",
                description="用户列表",
            )
        ],
        database_ddl=("CREATE TABLE users (user_id INTEGER PRIMARY KEY, name TEXT);"),
        relations=[
            ArchitectTableRelation(
                parent_table="users",
                child_table="resumes",
                foreign_key="user_id",
            )
        ],
        component_interactions=[],
        implementation_path=[{"phase": "1", "title": "增量后端", "tasks": []}],
    )


class ExistingSourceSlotTests(unittest.TestCase):
    def test_ui_layout_is_not_required_for_existing_source(self) -> None:
        slots = {
            "backend.framework": "Node",
            "database.engine": "mysql",
            "business.key_flows": ["查看用户"],
            "data.entities": ["users"],
        }
        self.assertNotIn(
            "ui.layout",
            missing_slots_for("full_stack_system", slots, "EXISTING_SOURCE"),
        )
        self.assertIn(
            "ui.layout",
            missing_slots_for("full_stack_system", slots, "NEW_PROJECT"),
        )

        analysis = ArchitectAnalysis(
            app_shape="full_stack_system",
            confidence_score=0.95,
            slots=slots,
        )
        updated, _guard = apply_confidence_guard(
            analysis,
            {},
            0.85,
            project_mode="EXISTING_SOURCE",
        )
        self.assertNotIn("ui.layout", updated.missing_slots)
        self.assertGreaterEqual(updated.confidence_score, 0.85)


class ExistingSourceContractTests(unittest.TestCase):
    def test_relation_self_check_is_skipped(self) -> None:
        validation = validate_draft(
            "full_stack_system",
            _existing_draft(),
            project_mode=EXISTING_SOURCE,
        )
        self.assertFalse(
            any("父键" in issue for issue in validation.issues),
            validation.issues,
        )

    def test_existing_mode_can_skip_incremental_ddl(self) -> None:
        draft = replace(
            _existing_draft(),
            database_ddl="",
            tech_stack={"backend": "Node"},
        )
        validation = validate_draft(
            "full_stack_system",
            draft,
            project_mode=EXISTING_SOURCE,
        )
        self.assertFalse(
            any("数据库 DDL" in issue for issue in validation.issues),
            validation.issues,
        )
        self.assertFalse(
            any("技术栈缺少 database" in issue for issue in validation.issues),
            validation.issues,
        )

    def test_contract_has_no_visual_document_in_existing_mode(self) -> None:
        contract = assemble_contract(
            contract_id="contract_existing",
            run_id="arc_existing",
            shape="full_stack_system",
            confidence_score=0.9,
            threshold=0.85,
            source_hash="sha256:abc",
            draft=_existing_draft(),
            citations=[],
            project_mode=EXISTING_SOURCE,
        )
        self.assertEqual(contract.project_mode, EXISTING_SOURCE)
        self.assertIsNone(contract.visual_document)

    def test_fullstack_audit_skips_incremental_ddl_relations(self) -> None:
        contract = assemble_contract(
            contract_id="contract_existing",
            run_id="arc_existing",
            shape="full_stack_system",
            confidence_score=0.9,
            threshold=0.85,
            source_hash="sha256:abc",
            draft=_existing_draft(),
            citations=[],
            project_mode=EXISTING_SOURCE,
        )
        audit = audit_contract(contract, mode="backend")
        self.assertFalse(
            any("父键" in problem.message for problem in audit.problems),
            [problem.message for problem in audit.problems],
        )


if __name__ == "__main__":
    unittest.main()
