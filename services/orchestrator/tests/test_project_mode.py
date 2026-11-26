"""双轨制项目模式判定与存量契约门禁测试。"""

from __future__ import annotations

import unittest
from dataclasses import replace

from ai_core.architect.visual import build_visual_document_from_contract
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
from ai_core.domain.visual_document import (
    VisualDocument,
    VisualNode,
    validate_visual_document,
)
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

    def test_declared_existing_without_source_facts_is_new(self) -> None:
        # 仅声明存量不够：没有 files / sandbox 时必须是 NEW_PROJECT，
        # 否则会以“存量模式”跑在没有源码的项目上。
        decision = resolve_project_mode(declared=EXISTING_SOURCE, files={})
        self.assertEqual(decision.mode, NEW_PROJECT)
        self.assertIn("缺少", decision.reason)

    def test_source_facts_override_declared_new(self) -> None:
        self.assertEqual(
            resolve_project_mode(declared=NEW_PROJECT, files={"a.js": "x"}).mode,
            EXISTING_SOURCE,
        )
        self.assertEqual(
            resolve_project_mode(
                declared=NEW_PROJECT,
                files={},
                sandbox_id="sbx_1",
            ).mode,
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
        components=[
            {
                "name": "指标概览",
                "role": "summary",
                "page": "/",
                "region": "main",
                "description": "关键指标",
            },
            {
                "name": "数据列表",
                "role": "list",
                "page": "/",
                "region": "main",
                "description": "核心业务记录",
            },
        ],
        frontend_routes=[{"path": "/", "name": "首页"}],
        implementation_path=[{"phase": "1", "title": "原型", "tasks": []}],
    )


class MultiPageBuilderTests(unittest.TestCase):
    def test_frontend_routes_create_switchable_pages(self) -> None:
        payload = build_visual_document_from_contract(
            title="管理后台",
            page_layout=[
                {"region": "header", "title": "顶部导航"},
                {"region": "main", "title": "主内容"},
            ],
            components=[
                {"name": "UserList", "role": "list", "description": "用户列表"}
            ],
            frontend_routes=[
                {"path": "/", "name": "首页", "description": "总览"},
                {"path": "/users", "name": "用户管理", "description": "用户列表"},
            ],
        )
        document = VisualDocument.model_validate(payload)
        self.assertEqual(len(document.pages), 2)
        self.assertEqual({page.path for page in document.pages}, {"/", "/users"})
        self.assertEqual(document.active_page_id, document.pages[0].page_id)
        report = validate_visual_document(document)
        self.assertTrue(report.ok, report.errors)

    def test_components_follow_page_region_and_sample_data(self) -> None:
        payload = build_visual_document_from_contract(
            title="工单系统",
            page_layout=[
                {"region": "header", "title": "顶部导航"},
                {"region": "sidebar", "title": "侧边栏"},
                {"region": "main", "title": "主内容区"},
            ],
            components=[
                {
                    "name": "工单统计",
                    "role": "summary",
                    "page": "/",
                    "region": "main",
                    "sample_data": [
                        {"label": "待处理", "value": 12},
                        {"label": "已完成", "value": 8},
                    ],
                },
                {
                    "name": "工单列表",
                    "role": "table",
                    "page": "/",
                    "region": "main",
                    "sample_data": [
                        {
                            "编号": "T-1",
                            "标题": "打印机故障",
                            "状态": "待处理",
                        }
                    ],
                },
                {
                    "name": "新建工单",
                    "role": "form",
                    "page": "/new",
                    "region": "main",
                    "fields": ["标题", "优先级"],
                },
            ],
            frontend_routes=[
                {"path": "/", "name": "总览"},
                {"path": "/new", "name": "新建工单"},
            ],
        )
        document = VisualDocument.model_validate(payload)
        self.assertTrue(validate_visual_document(document).ok)
        pages = {page.path: page for page in document.pages}

        def find(node: VisualNode, name: str) -> VisualNode | None:
            if node.name == name:
                return node
            for child in node.children:
                found = find(child, name)
                if found is not None:
                    return found
            return None

        home_main = find(pages["/"].root, "主内容区")
        self.assertIsNotNone(home_main)
        self.assertEqual(
            [node.name for node in home_main.children],
            ["工单统计", "工单列表"],
        )
        table = find(pages["/"].root, "工单列表")
        self.assertEqual(table.type, "table")
        first_row = find(table, "表格行 1")
        self.assertIsNotNone(first_row)
        self.assertTrue(
            any(
                "打印机故障" in str(node.props.get("text") or "")
                for node in first_row.children
            )
        )
        new_main = find(pages["/new"].root, "主内容区")
        self.assertEqual([node.name for node in new_main.children], ["新建工单"])
        form = find(new_main, "新建工单")
        self.assertIn("标题输入框", [node.name for node in form.children])


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

    def test_v0_rejects_blank_secondary_pages(self) -> None:
        draft = replace(
            _visual_v0_draft(),
            frontend_routes=[
                {"path": "/", "name": "首页"},
                {"path": "/reports", "name": "报表"},
            ],
        )
        validation = validate_draft(
            "full_stack_system",
            draft,
            output_phase="VISUAL_V0",
        )
        self.assertFalse(validation.valid)
        self.assertTrue(
            any("/reports" in issue for issue in validation.issues),
            validation.issues,
        )

    def test_v0_requires_two_business_blocks(self) -> None:
        draft = replace(
            _visual_v0_draft(),
            components=[
                {
                    "name": "数据列表",
                    "role": "list",
                    "page": "/",
                    "region": "main",
                    "description": "唯一区块",
                }
            ],
        )
        validation = validate_draft(
            "full_stack_system",
            draft,
            output_phase="VISUAL_V0",
        )
        self.assertFalse(validation.valid)
        self.assertTrue(
            any("至少需要 2 个业务区块" in issue for issue in validation.issues),
            validation.issues,
        )

    def test_v0_skeleton_mode_skips_completeness_gate(self) -> None:
        draft = replace(
            _visual_v0_draft(),
            skeleton_mode=True,
            components=[
                {
                    "name": "待定区域",
                    "role": "placeholder",
                    "is_placeholder": True,
                }
            ],
            frontend_routes=[
                {"path": "/", "name": "首页"},
                {"path": "/pending", "name": "待定"},
            ],
        )
        validation = validate_draft(
            "full_stack_system",
            draft,
            output_phase="VISUAL_V0",
        )
        self.assertTrue(validation.valid, validation.issues)

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

    def test_backend_binding_restores_routes_and_components_from_visual(
        self,
    ) -> None:
        """Iter6 回归：模型重写契约时丢掉的路由 / 组件，必须由冻结树补齐。"""
        visual = build_visual_document_from_contract(
            title="巡检平台",
            page_layout=[{"region": "main", "title": "主内容区"}],
            components=[
                {"name": "设施表单", "role": "form", "description": "新增设施"},
                {"name": "工单列表", "role": "list", "description": "工单列表"},
                {"name": "工单状态按钮", "role": "action", "description": "推进状态"},
            ],
            frontend_routes=[
                {"path": "/", "name": "总览", "description": "首页"},
                {"path": "/tasks", "name": "工单", "description": "工单页"},
            ],
        )
        draft = replace(
            _existing_draft(),
            visual_document={},
            frontend_routes=[{"path": "/", "name": "总览", "description": "首页"}],
            components=[
                {"name": "设施表单", "role": "form", "description": "新增设施"}
            ],
        )
        contract = assemble_contract(
            contract_id="contract_bind_merge",
            run_id="arc_bind_merge",
            shape="full_stack_system",
            confidence_score=0.9,
            threshold=0.85,
            source_hash="sha256:bind_merge",
            draft=draft,
            citations=[],
            output_phase="BACKEND_BINDING",
            frozen_visual_document=visual,
        )
        self.assertEqual(
            [item["path"] for item in contract.frontend_routes],
            ["/", "/tasks"],
        )
        names = [item["name"] for item in contract.components]
        self.assertEqual(
            names,
            ["设施表单", "工单列表", "工单状态按钮"],
        )

    def test_solution_choice_is_written_into_contract_markdown(self) -> None:
        """用户选中的复用方案必须进入契约正文，供契约侧审计。"""
        contract = assemble_contract(
            contract_id="contract_choice",
            run_id="arc_choice",
            shape="full_stack_system",
            confidence_score=0.9,
            threshold=0.85,
            source_hash="sha256:choice",
            draft=_existing_draft(),
            citations=[],
            solution_choice={
                "optionId": "opt_reuse_geoai",
                "kind": "REUSE",
                "title": "opengeos/geoai",
                "sourceUrl": "https://github.com/opengeos/geoai",
                "integrationPlan": "作为依赖引入，业务模型自研。",
            },
        )
        markdown = contract.contract_markdown
        self.assertIn("## 方案选择", markdown)
        self.assertIn("opengeos/geoai", markdown)
        self.assertIn("opt_reuse_geoai", markdown)
        self.assertIn("作为依赖引入", markdown)


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
