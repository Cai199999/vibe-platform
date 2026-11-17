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
from ai_core.domain.visual_document import VisualDocument, VisualNode
from ai_core.error_envelope import envelope_from_code
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


def _visual_document() -> dict:
    document = VisualDocument(
        docId="vdoc_v0",
        projectId="proj_v0",
        root=VisualNode(id="nd_page0001", type="page", name="页面"),
    )
    return document.model_dump(mode="json", by_alias=True)


def _visual_v0_draft() -> ContractDraft:
    return ContractDraft(
        contract_markdown="# V0 虚拟原型\n\n" + ("页面结构说明。" * 60),
        tech_stack={},
        visual_document=_visual_document(),
        implementation_path=[{"phase": "1", "title": "原型", "tasks": []}],
    )


class VisualV0Tests(unittest.TestCase):
    def test_v0_only_allows_visual_document(self) -> None:
        validation = validate_draft(
            "full_stack_system",
            _visual_v0_draft(),
            output_phase="VISUAL_V0",
        )
        self.assertTrue(validation.valid, validation.issues)

        polluted = replace(
            _visual_v0_draft(),
            api_routes=[
                ArchitectApiRoute(
                    method="GET",
                    path="/api/v1/users",
                    request="",
                    response="[]",
                    description="用户",
                )
            ],
            database_ddl="CREATE TABLE users (id INTEGER PRIMARY KEY);",
        )
        polluted_validation = validate_draft(
            "full_stack_system",
            polluted,
            output_phase="VISUAL_V0",
        )
        # 模型多写的后端字段由服务端确定性剥离，不进入 repair。
        self.assertTrue(polluted_validation.valid, polluted_validation.issues)
        self.assertEqual(polluted_validation.draft.api_routes, [])
        self.assertEqual(polluted_validation.draft.database_ddl, "")

    def test_v0_invalid_error_is_not_generic_internal(self) -> None:
        envelope = envelope_from_code("VISUAL_V0_INVALID")
        self.assertIn("V0", envelope.user_message)
        self.assertNotIn("内部问题", envelope.user_message)
        self.assertTrue(envelope.retryable)

    def test_v0_assembly_has_no_backend(self) -> None:
        contract = assemble_contract(
            contract_id="contract_v0",
            run_id="arc_v0",
            shape="full_stack_system",
            confidence_score=0.9,
            threshold=0.85,
            source_hash="sha256:v0",
            draft=_visual_v0_draft(),
            citations=[],
            output_phase="VISUAL_V0",
        )
        self.assertEqual(contract.output_phase, "VISUAL_V0")
        self.assertIsNotNone(contract.visual_document)
        self.assertEqual(contract.api_routes, [])
        self.assertEqual(contract.database_ddl, "")

    def test_backend_binding_reuses_frozen_visual_document(self) -> None:
        draft = replace(
            _existing_draft(),
            visual_document={},
            database_ddl="CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);",
        )
        validation = validate_draft(
            "full_stack_system",
            draft,
            output_phase="BACKEND_BINDING",
        )
        self.assertFalse(
            any("组件交互" in issue for issue in validation.issues),
            validation.issues,
        )
        contract = assemble_contract(
            contract_id="contract_bind",
            run_id="arc_bind",
            shape="full_stack_system",
            confidence_score=0.9,
            threshold=0.85,
            source_hash="sha256:bind",
            draft=draft,
            citations=[],
            output_phase="BACKEND_BINDING",
            frozen_visual_document=_visual_document(),
        )
        self.assertEqual(contract.output_phase, "BACKEND_BINDING")
        self.assertIsNotNone(contract.visual_document)
        self.assertEqual(contract.visual_document.doc_id, "vdoc_v0")


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
        self.assertNotIn(
            "ui.layout",
            missing_slots_for(
                "full_stack_system",
                slots,
                "NEW_PROJECT",
                "BACKEND_BINDING",
            ),
        )
        v0_missing = missing_slots_for(
            "full_stack_system",
            {},
            "NEW_PROJECT",
            "VISUAL_V0",
        )
        self.assertNotIn("backend.framework", v0_missing)
        self.assertNotIn("database.engine", v0_missing)

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
