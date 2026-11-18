"""VisualDocument 确定性 CodeGen 与编译门禁测试。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from ai_core.codegen.visual import (
    VisualCodegenError,
    compile_visual_document,
    generate_visual_fallback,
    visual_document_hash,
)
from ai_core.codegen.visual_gate import verify_visual_source
from ai_core.domain.architect import ArchitectContract
from ai_core.domain.fullstack import FullstackRunOptions, FullstackRunRequest
from ai_core.domain.visual_document import (
    VisualBinding,
    VisualDocument,
    VisualInteraction,
    VisualLayout,
    VisualNode,
    VisualPage,
    VisualRepeat,
    VisualStyle,
    freeze_visual_document,
)
from ai_core.fullstack.graph import FullstackGraphRunner
from ai_core.fullstack.service import (
    FullstackContractInvalidError,
    FullstackService,
)
from ai_core.ports.sandbox import SandboxHandle, SandboxProvider, VerifyResult


def _node(
    node_id: str,
    node_type: str,
    *,
    name: str = "",
    children: list[VisualNode] | None = None,
    repeat: VisualRepeat | None = None,
    **kwargs,
) -> VisualNode:
    return VisualNode(
        id=node_id,
        type=node_type,
        name=name or node_type,
        children=children or [],
        repeat=repeat,
        **kwargs,
    )


def _document(*, frozen: bool = True) -> VisualDocument:
    root = _node(
        "nd_page0001",
        "page",
        name="页面",
        layout=VisualLayout(display="block", padding="32px"),
        children=[
            _node(
                "nd_title001",
                "text",
                name="标题",
                props={"text": "订单工作台"},
            ),
            _node(
                "nd_list001",
                "list",
                name="订单列表",
                repeat=VisualRepeat(source="orders", sample_count=2),
                children=[
                    _node(
                        "nd_item001",
                        "text",
                        name="订单",
                        props={"text": "订单行"},
                    )
                ],
            ),
            _node(
                "nd_form001",
                "form",
                name="登录",
                children=[
                    _node(
                        "nd_input001",
                        "input",
                        name="账号",
                        props={"placeholder": "请输入账号"},
                    ),
                    _node(
                        "nd_btn0001",
                        "button",
                        name="提交",
                        props={"text": "登录"},
                    ),
                ],
            ),
        ],
    )
    document = VisualDocument(
        docId="vdoc_codegen",
        projectId="proj_codegen",
        title="订单工作台",
        root=root,
        bindings=[
            VisualBinding(
                id="bind_orders",
                nodeId="nd_list001",
                entity="orders",
                status="CONFIRMED",
            )
        ],
        interactions=[
            VisualInteraction(
                id="int_login",
                nodeId="nd_btn0001",
                action="call_api",
                bindingId="bind_orders",
                status="CONFIRMED",
            )
        ],
    )
    return freeze_visual_document(document) if frozen else document


def _contract(**overrides) -> ArchitectContract:
    payload = {
        "contractId": "contract_codegen",
        "runId": "arc_codegen",
        "appShape": "lightweight_web",
        "confidenceScore": 1.0,
        "contractMarkdown": "# 订单工作台",
        "status": "CONFIRMED",
    }
    payload.update(overrides)
    return ArchitectContract(**payload)


class VisualCodegenCompileTests(unittest.TestCase):
    def test_compile_injects_data_node_id_for_every_node(self) -> None:
        document = _document()
        result = compile_visual_document(document)

        expected = {
            "nd_page0001",
            "nd_title001",
            "nd_list001",
            "nd_item001",
            "nd_form001",
            "nd_input001",
            "nd_btn0001",
        }
        self.assertEqual(set(result.node_ids), expected)
        source = result.files["src/App.jsx"]
        for node_id in expected:
            self.assertEqual(source.count(f'data-node-id="{node_id}"'), 1)
        self.assertRegex(source, r'data-source="src/App\.jsx:\d+"')
        self.assertIn('data-source-file="src/App.jsx"', source)
        self.assertIn('data-source-kind="container"', source)
        self.assertIn('data-source-kind="element"', source)
        self.assertIn("visual-repeat-item", source)
        self.assertNotIn("runVisualAction", source)
        self.assertIn("bg-[var(--color-primary)]", source)
        self.assertIn('import "./visual.css"', source)
        self.assertIn("--color-primary", result.files["src/visual.css"])
        self.assertIn('"vite"', result.files["package.json"])
        self.assertIn('"react"', result.files["package.json"])
        self.assertIn("cdn.tailwindcss.com", result.files["index.html"])
        self.assertIn("/src/main.jsx", result.files["index.html"])
        self.assertIn("/inspect_sdk.js", result.files["index.html"])
        self.assertIn("vibe-inspect", result.files["public/inspect_sdk.js"])
        self.assertIn(
            'data-oid="oid_nd_page0001"',
            source,
        )
        source_map = json.loads(result.files["public/vibe-source-map.json"])
        self.assertEqual(source_map["version"], 1)
        self.assertIn("oid_nd_page0001", source_map["nodes"])
        page = source_map["nodes"]["oid_nd_page0001"]
        self.assertEqual(page["nodeId"], "nd_page0001")
        self.assertEqual(page["file"], "src/App.jsx")
        self.assertTrue(page["isContainer"])
        self.assertGreater(page["line"], 0)
        self.assertEqual(
            source_map["nodes"]["oid_nd_title001"]["parentOid"],
            "oid_nd_page0001",
        )
        self.assertIn("./visual.css", result.files["src/main.jsx"])
        self.assertIn("'/api'", result.files["vite.config.js"])
        self.assertIn("127.0.0.1:3000", result.files["vite.config.js"])

    def test_multiple_pages_compile_to_page_switcher(self) -> None:
        first = _node(
            "nd_home001",
            "page",
            name="首页",
            children=[_node("nd_home_text", "text", props={"text": "首页"})],
        )
        second = _node(
            "nd_users001",
            "page",
            name="用户",
            children=[_node("nd_users_text", "text", props={"text": "用户页"})],
        )
        document = VisualDocument(
            docId="vdoc_pages",
            projectId="proj_pages",
            title="多页面",
            pages=[
                VisualPage(pageId="pg_home", path="/", title="首页", root=first),
                VisualPage(pageId="pg_users", path="/users", title="用户", root=second),
            ],
            activePageId="pg_home",
            root=first,
        )
        frozen = freeze_visual_document(document)
        result = compile_visual_document(frozen)
        app = result.files["src/App.jsx"]
        self.assertIn("VISUAL_PAGES", app)
        self.assertIn("visual-page-nav", app)
        self.assertIn('data-node-id="nd_users001"', app)
        self.assertIn('data-node-id="nd_users_text"', app)
        self.assertEqual(result.metadata["pageCount"], 2)
        report = verify_visual_source(result.files, result.node_ids)
        self.assertTrue(report.ok, report.errors)

    def test_unbound_repeat_warns_but_compiles_with_sample_data(self) -> None:
        root = _node(
            "nd_page0001",
            "page",
            children=[
                _node(
                    "nd_list001",
                    "list",
                    repeat=VisualRepeat(source="orders", sample_count=2),
                )
            ],
        )
        document = VisualDocument(
            docId="vdoc_unbound",
            projectId="proj_unbound",
            root=root,
        )
        frozen = freeze_visual_document(document)
        result = compile_visual_document(frozen)
        self.assertIn('data-node-id="nd_list001"', result.files["src/App.jsx"])
        self.assertTrue(
            any("repeat.source=orders" in item for item in result.warnings),
            result.warnings,
        )

    def test_interactions_are_opt_in(self) -> None:
        result = compile_visual_document(_document(), include_interactions=True)
        self.assertIn("runVisualAction", result.files["src/App.jsx"])
        self.assertFalse(result.metadata["staticOnly"])

    def test_compile_is_deterministic(self) -> None:
        first = compile_visual_document(_document())
        second = compile_visual_document(_document())
        self.assertEqual(first.files, second.files)
        self.assertEqual(first.node_ids, second.node_ids)

    def test_compile_requires_frozen_document(self) -> None:
        with self.assertRaises(VisualCodegenError) as raised:
            compile_visual_document(_document(frozen=False))
        self.assertEqual(raised.exception.code, "VISUAL_CODEGEN_REQUIRES_FROZEN")

    def test_compile_requires_content_hash(self) -> None:
        document = _document().model_copy(deep=True)
        document.metadata.content_hash = ""
        with self.assertRaises(VisualCodegenError) as raised:
            compile_visual_document(document)
        self.assertEqual(raised.exception.code, "VISUAL_CONTENT_HASH_MISSING")

    def test_forbidden_absolute_tailwind_is_rejected(self) -> None:
        document = _document().model_copy(deep=True)
        document.root.children[0].style = VisualStyle(tailwind=["absolute", "top-4"])
        with self.assertRaises(VisualCodegenError) as raised:
            compile_visual_document(document)
        self.assertEqual(raised.exception.code, "VISUAL_CODEGEN_FORBIDDEN_STYLE")

    def test_custom_node_falls_back_to_placeholder_without_llm(self) -> None:
        document = _document().model_copy(deep=True)
        document.root.children[0].codegen.mode = "custom"
        result = compile_visual_document(document)
        self.assertEqual(result.metadata["customNodeCount"], 1)
        self.assertIn("visual-custom-placeholder", result.files["src/App.jsx"])
        self.assertIn('data-node-id="nd_title001"', result.files["src/App.jsx"])

    def test_fallback_bundle_covers_every_node_and_passes_gate(self) -> None:
        document = _document()
        fallback = generate_visual_fallback(document, reason="测试构建失败")
        self.assertTrue(fallback.metadata["fallback"])
        self.assertEqual(
            set(fallback.node_ids), set(compile_visual_document(document).node_ids)
        )
        report = verify_visual_source(fallback.files, fallback.node_ids)
        self.assertTrue(report.ok, report.errors)
        self.assertIn("安全占位", fallback.files["src/App.jsx"])
        self.assertRegex(
            fallback.files["src/App.jsx"],
            r'data-source="src/App\.jsx:\d+"',
        )
        self.assertIn("package.json", fallback.files)
        self.assertIn("index.html", fallback.files)

    def test_server_hash_is_stable(self) -> None:
        document = _document()
        self.assertEqual(
            visual_document_hash(document),
            visual_document_hash(document.model_copy(deep=True)),
        )


class VisualCodegenGateTests(unittest.TestCase):
    def test_gate_detects_missing_node_id(self) -> None:
        result = compile_visual_document(_document())
        tampered = result.files["src/App.jsx"].replace(
            ' data-node-id="nd_title001"', ""
        )
        report = verify_visual_source(
            {"src/App.jsx": tampered},
            result.node_ids,
        )
        self.assertFalse(report.ok)
        self.assertIn("nd_title001", report.missing_node_ids)

    def test_gate_detects_duplicate_node_id(self) -> None:
        result = compile_visual_document(_document())
        source = result.files["src/App.jsx"].replace(
            "</main>", '<div data-node-id="nd_title001" /></main>'
        )
        report = verify_visual_source({"src/App.jsx": source}, result.node_ids)
        self.assertFalse(report.ok)
        self.assertIn("nd_title001", report.duplicate_node_ids)

    def test_gate_rejects_absolute_position_and_imports(self) -> None:
        source = (
            'import axios from "axios"\n'
            "export default function App() {\n"
            '  return <div className="absolute top-0" '
            "data-node-id=\"nd_x0001\" style={{ position: 'absolute' }} "
            'dangerouslySetInnerHTML={{ __html: "x" }} />\n'
            "}\n"
        )
        report = verify_visual_source({"src/App.jsx": source}, ["nd_x0001"])
        self.assertFalse(report.ok)
        joined = "\n".join(report.errors)
        self.assertIn("axios", joined)
        self.assertIn("absolute", joined)
        self.assertIn("dangerouslySetInnerHTML", joined)


class VisualCodegenGraphTests(unittest.TestCase):
    def test_frontend_graph_uses_visual_document(self) -> None:
        sandbox = _MemorySandbox()
        runner = FullstackGraphRunner(sandbox)
        contract = _contract()
        outcome = runner.run(
            {
                "run_id": "fsk_visual",
                "project_id": "proj_codegen",
                "sandbox_id": sandbox.handle.sandbox_id,
                "preview_url": sandbox.handle.preview_url,
                "contract": contract,
                "options": FullstackRunOptions(mode="frontend"),
                "visual_document": _document(),
                "files": [],
            }
        )

        app = sandbox.files["src/App.jsx"]
        self.assertIn('data-node-id="nd_page0001"', app)
        self.assertIn('data-node-id="nd_btn0001"', app)
        self.assertEqual(app, outcome.get("frontend_entry", ""))
        self.assertIn("src/visual.css", sandbox.files)
        self.assertIn("package.json", sandbox.files)
        self.assertIn("index.html", sandbox.files)
        self.assertEqual(
            sandbox.verified,
            [
                (sandbox.handle.sandbox_id, "install"),
                (sandbox.handle.sandbox_id, "build"),
            ],
        )
        self.assertEqual(sandbox.restarts, [(sandbox.handle.sandbox_id, "/")])

    def test_build_failure_repairs_once_with_safe_placeholder(self) -> None:
        sandbox = _FlakyBuildSandbox(failures=1)
        runner = FullstackGraphRunner(sandbox)
        outcome = runner.run(
            {
                "run_id": "fsk_visual_repair",
                "project_id": "proj_codegen",
                "sandbox_id": sandbox.handle.sandbox_id,
                "preview_url": sandbox.handle.preview_url,
                "contract": _contract(),
                "options": FullstackRunOptions(mode="frontend"),
                "visual_document": _document(),
                "files": [],
            }
        )

        self.assertEqual(
            sandbox.verified[-2:],
            [
                (sandbox.handle.sandbox_id, "build"),
                (sandbox.handle.sandbox_id, "build"),
            ],
        )
        self.assertEqual(
            sandbox.verified.count((sandbox.handle.sandbox_id, "build")), 2
        )
        app = sandbox.files["src/App.jsx"]
        self.assertIn("安全占位", app)
        self.assertIn('data-node-id="nd_btn0001"', app)
        self.assertIn("安全占位", outcome.get("frontend_entry", ""))
        self.assertTrue(
            any("安全占位页" in warning for warning in outcome.get("warnings", []))
        )


class VisualCodegenContractTests(unittest.TestCase):
    def setUp(self) -> None:
        # _resolve_contract 不依赖组合根，绕过 Docker 初始化。
        self.service = FullstackService.__new__(FullstackService)

    def test_frontend_mode_with_visual_document_skips_route_requirement(self) -> None:
        request = FullstackRunRequest(
            projectId="proj_codegen",
            contractId="contract_codegen",
            contract=_contract(frontendRoutes=[], components=[]),
            visualDocument=_document(),
            options={"mode": "frontend"},
        )
        resolved = self.service._resolve_contract(request)
        self.assertEqual(resolved.contract_id, "contract_codegen")

    def test_frontend_mode_without_visual_still_requires_contract_shape(self) -> None:
        request = FullstackRunRequest(
            projectId="proj_codegen",
            contractId="contract_codegen",
            contract=_contract(frontendRoutes=[], components=[]),
            options={"mode": "frontend"},
        )
        with self.assertRaises(FullstackContractInvalidError):
            self.service._resolve_contract(request)

    def test_existing_contract_blocks_frontend_codegen(self) -> None:
        request = FullstackRunRequest(
            projectId="proj_existing",
            contractId="contract_codegen",
            contract=_contract(projectMode="EXISTING_SOURCE"),
            options={"mode": "frontend"},
        )
        with self.assertRaises(FullstackContractInvalidError) as raised:
            self.service._resolve_contract(request)
        self.assertIn("存量项目禁止前端 CodeGen", str(raised.exception))

    def test_client_declared_existing_blocks_even_for_new_contract(self) -> None:
        # 旧契约 / 前端回传快照可能仍写着 NEW_PROJECT；本次 Run 的
        # projectMode 是项目当前事实，不能被旧契约覆盖。
        request = FullstackRunRequest(
            projectId="proj_existing",
            contractId="contract_codegen",
            contract=_contract(frontendRoutes=[], components=[]),
            options={"mode": "fullstack", "projectMode": "EXISTING_SOURCE"},
        )
        with self.assertRaises(FullstackContractInvalidError) as raised:
            self.service._resolve_contract(request)
        self.assertIn("存量项目禁止前端 CodeGen", str(raised.exception))

    def test_new_project_with_template_files_still_allows_visual_codegen(self) -> None:
        # 新建项目首次 CodeGen 会携带内置模板 / 当前入口源码，不能因为
        # source.files 非空就误判为存量项目。
        request = FullstackRunRequest(
            projectId="proj_new",
            contractId="contract_codegen",
            contract=_contract(
                appShape="full_stack_system",
                frontendRoutes=[],
                components=[],
            ),
            visualDocument=_document(),
            source={"files": {"src/App.jsx": "export default function App() {}"}},
            options={"mode": "frontend", "projectMode": "NEW_PROJECT"},
        )
        resolved = self.service._resolve_contract(request)
        self.assertEqual(resolved.contract_id, "contract_codegen")

    def test_existing_contract_allows_backend_increment(self) -> None:
        request = FullstackRunRequest(
            projectId="proj_existing",
            contractId="contract_codegen",
            contract=_contract(
                projectMode="EXISTING_SOURCE",
                appShape="full_stack_system",
                apiRoutes=[
                    {
                        "method": "GET",
                        "path": "/api/v1/users",
                        "request": "",
                        "response": "[]",
                        "description": "用户列表",
                    }
                ],
            ),
            source={"files": {"src/App.jsx": "export default function App() {}"}},
            options={"mode": "backend"},
        )
        resolved = self.service._resolve_contract(request)
        self.assertEqual(resolved.contract_id, "contract_codegen")


class _MemorySandbox(SandboxProvider):
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.verified: list[tuple[str, str]] = []
        self.restarts: list[tuple[str, str]] = []
        self.handle = SandboxHandle(
            sandbox_id="sbx_visual_codegen",
            container_id="fake",
            container_name="fake",
            project_id="proj_codegen",
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
            duration_ms=8,
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


class _FlakyBuildSandbox(_MemorySandbox):
    """前 N 次 build 失败，用于验证 Visual CodeGen 的一次性安全降级。"""

    def __init__(self, failures: int = 1) -> None:
        super().__init__()
        self.remaining_failures = failures

    def verify(self, sandbox_id: str, check: str = "build") -> VerifyResult:
        self.verified.append((sandbox_id, check))
        if check == "build" and self.remaining_failures > 0:
            self.remaining_failures -= 1
            return VerifyResult(
                success=False,
                exit_code=1,
                timed_out=False,
                duration_ms=8,
                logs="vite build failed: mock syntax error",
                logs_truncated=False,
            )
        return VerifyResult(
            success=True,
            exit_code=0,
            timed_out=False,
            duration_ms=8,
            logs="vite build ok",
            logs_truncated=False,
        )


if __name__ == "__main__":
    unittest.main()
