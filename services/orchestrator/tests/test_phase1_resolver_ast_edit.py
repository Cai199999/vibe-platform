"""Phase 1 验收测试：确定性 Resolver + 唯一写工具 apply_ast_edit。

覆盖四层契约：
  1. Babel 解析脚本：唯一命中 / 歧义候选 / 公共组件保护 / 行列回退；
  2. Babel 编辑脚本：modify / insert / delete 的精确切片与删除同构校验；
  3. 工具硬门禁：白名单、保护标识、范围一致性、上下文缺失均 fail-closed；
  4. Graph 链路：style_check -> resolver -> coder(tool_calls) -> applier，
     以及歧义时经 interrupt 让用户选择后重新解析。

运行：

    docker exec vibe-python-ai python -m unittest tests.test_phase1_resolver_ast_edit -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from ai_core.adapters.checkpoint import MemoryCheckpointerProvider
from ai_core.agent import graph as graph_module
from ai_core.agent.graph import AgentGraphRunner
from ai_core.agent.llm import CodeResult, MockAgentLLM
from ai_core.domain.resolution import (
    ANCHOR_NOT_FOUND,
    DELETE_SCOPE_MISMATCH,
    EDIT_CONTEXT_REQUIRED,
    FILE_NOT_ALLOWED,
    INVALID_OPERATION,
    OPERATION_NOT_ALLOWED,
    PROTECTED_COMPONENT,
    RANGE_NOT_ALLOWED,
    RESOLVED,
    TARGET_AMBIGUOUS,
    TARGET_NOT_FOUND,
    anchor_id_for,
)
from ai_core.ports.sandbox import SandboxProvider, VerifyResult
from ai_core.tools.ast_edit_tool import (
    AstEditContext,
    build_apply_ast_edit_tool,
    execute_ast_edit,
)

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "sandbox_template"
NODE = shutil.which("node")

RESOLVER_SOURCE = """export default function App() {
  return (
    <main>
      <button className="primary">登录</button>
      <button className="primary">注册</button>
      <p>欢迎</p>
    </main>
  );
}
"""

EDIT_SOURCE = """export default function App() {
  return (
    <main>
      <button className="primary">登录</button>
      <p>欢迎</p>
    </main>
  );
}
"""


# ------------------------------------------------------------
# Node 脚本执行辅助
# ------------------------------------------------------------
def _run_script(
    script: str, spec: dict[str, Any]
) -> tuple[int, dict[str, Any] | None, str]:
    """在临时目录写 spec.json 后执行 Babel 脚本，返回 (exit_code, payload, stderr)。"""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(spec, handle, ensure_ascii=False)
        spec_path = handle.name
    try:
        proc = subprocess.run(
            [NODE, str(TEMPLATE_DIR / script), spec_path],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        os.unlink(spec_path)
    payload = None
    if proc.returncode == 0 and proc.stdout.strip():
        payload = json.loads(proc.stdout)
    return proc.returncode, payload, proc.stderr


def _write_project(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _anchor_request(anchor_id: str, **overrides: Any) -> dict[str, Any]:
    request = {
        "anchorId": anchor_id,
        "annotationId": anchor_id.removeprefix("anc_"),
        "file": "src/App.jsx",
        "selector": "",
        "line": None,
        "column": None,
        "component": "",
        "instance": None,
        "instanceCount": None,
        "callSiteFile": "",
    }
    request.update(overrides)
    return request


def _fix_range(code: str, start_token: str, end_token: str) -> dict[str, int]:
    """按 token 计算源码区间，避免在测试里硬编码 offset。"""
    start = code.index(start_token)
    end = code.index(end_token, start) + len(end_token)
    return {"startOffset": start, "endOffset": end}


@unittest.skipUnless(NODE, "node 不可用，跳过 Babel 脚本测试")
class ResolverScriptTest(unittest.TestCase):
    """确定性解析节点：不依赖 LLM，只依据 AST + 前端定位信息。"""

    def test_selector_with_instance_resolves_exact_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": RESOLVER_SOURCE})
            code, payload, stderr = _run_script(
                "resolve_target.mjs",
                {
                    "workdir": tmp,
                    "parserRoot": str(TEMPLATE_DIR),
                    "anchors": [
                        _anchor_request(
                            "anc_ann_reg",
                            selector=".primary",
                            instance=2,
                            instanceCount=2,
                        )
                    ],
                },
            )
            self.assertEqual(code, 0, stderr)
            anchor = payload["anchors"][0]
            self.assertEqual(anchor["status"], RESOLVED)
            self.assertIn("注册", anchor["nodeSource"])
            self.assertIn("登录", RESOLVER_SOURCE)
            # 同一源码位置渲染两次 = 公共组件，必须具备保护标识。
            self.assertTrue(anchor["isShared"])
            self.assertTrue(anchor["protected"])
            self.assertIn("2 次", anchor["sharedReason"])
            self.assertTrue(anchor["forbiddenRanges"])
            self.assertEqual(anchor["allowedFiles"], ["src/App.jsx"])

    def test_multiple_matches_without_instance_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": RESOLVER_SOURCE})
            code, payload, stderr = _run_script(
                "resolve_target.mjs",
                {
                    "workdir": tmp,
                    "parserRoot": str(TEMPLATE_DIR),
                    "anchors": [_anchor_request("anc_ann_x", selector=".primary")],
                },
            )
            self.assertEqual(code, 0, stderr)
            anchor = payload["anchors"][0]
            self.assertEqual(anchor["status"], TARGET_AMBIGUOUS)
            self.assertEqual(len(anchor["candidates"]), 2)
            previews = [item["preview"] for item in anchor["candidates"]]
            self.assertTrue(any("登录" in item for item in previews))
            self.assertTrue(any("注册" in item for item in previews))
            # 歧义时不得给出可写锚点范围，模型无从猜测。
            self.assertNotIn("range", anchor)

    def test_line_column_fallback_when_selector_misses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": RESOLVER_SOURCE})
            code, payload, stderr = _run_script(
                "resolve_target.mjs",
                {
                    "workdir": tmp,
                    "parserRoot": str(TEMPLATE_DIR),
                    "anchors": [
                        _anchor_request(
                            "anc_ann_p",
                            selector="[data-vibe-missing]",
                            line=6,
                            column=7,
                        )
                    ],
                },
            )
            self.assertEqual(code, 0, stderr)
            anchor = payload["anchors"][0]
            self.assertEqual(anchor["status"], RESOLVED)
            self.assertIn("欢迎", anchor["nodeSource"])

    def test_importer_marks_component_shared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(
                Path(tmp),
                {
                    "src/App.jsx": RESOLVER_SOURCE,
                    "src/Other.jsx": "import App from './App'\n",
                },
            )
            code, payload, stderr = _run_script(
                "resolve_target.mjs",
                {
                    "workdir": tmp,
                    "parserRoot": str(TEMPLATE_DIR),
                    "anchors": [_anchor_request("anc_ann_main", selector="main")],
                },
            )
            self.assertEqual(code, 0, stderr)
            anchor = payload["anchors"][0]
            self.assertEqual(anchor["status"], RESOLVED)
            self.assertTrue(anchor["protected"])
            self.assertIn("src/Other.jsx", anchor["sharedReason"])

    def test_missing_file_is_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": RESOLVER_SOURCE})
            code, payload, stderr = _run_script(
                "resolve_target.mjs",
                {
                    "workdir": tmp,
                    "parserRoot": str(TEMPLATE_DIR),
                    "anchors": [
                        _anchor_request(
                            "anc_ann_gone", file="src/Gone.jsx", selector="main"
                        )
                    ],
                },
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(payload["anchors"][0]["status"], TARGET_NOT_FOUND)


@unittest.skipUnless(NODE, "node 不可用，跳过 Babel 脚本测试")
class AstEditScriptTest(unittest.TestCase):
    """编辑器只做源码切片；删除必须通过 AST 同构校验。"""

    def _edit(self, spec: dict[str, Any]) -> tuple[int, dict[str, Any] | None, str]:
        return _run_script(
            "apply_ast_edit.mjs",
            {"parserRoot": str(TEMPLATE_DIR), **spec},
        )

    def test_modify_replaces_only_anchor_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": EDIT_SOURCE})
            button = _fix_range(EDIT_SOURCE, "<button", "</button>")
            code, payload, stderr = self._edit(
                {
                    "workdir": tmp,
                    "file": "src/App.jsx",
                    "operation": "modify",
                    "anchorRange": button,
                    "targetRange": button,
                    "jsxCode": '<button className="primary bg-blue-600">登录</button>',
                }
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(payload["status"], "OK")
            content = payload["content"]
            self.assertIn("bg-blue-600", content)
            self.assertIn("<p>欢迎</p>", content)

    def test_insert_sibling_uses_anchor_indent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": EDIT_SOURCE})
            button = _fix_range(EDIT_SOURCE, "<button", "</button>")
            code, payload, stderr = self._edit(
                {
                    "workdir": tmp,
                    "file": "src/App.jsx",
                    "operation": "insert",
                    "anchorRange": button,
                    "targetRange": button,
                    "insertPosition": "after",
                    "jsxCode": "<span>新增</span>",
                }
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(payload["status"], "OK")
            content = payload["content"]
            # 兄弟节点必须和锚点同级缩进（6 空格），不能多一层。
            self.assertIn("      <span>新增</span>", content)
            self.assertLess(content.index("<button"), content.index("<span>"))

    def test_delete_removes_subtree_and_passes_isomorphism(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": EDIT_SOURCE})
            button = _fix_range(EDIT_SOURCE, "<button", "</button>")
            code, payload, stderr = self._edit(
                {
                    "workdir": tmp,
                    "file": "src/App.jsx",
                    "operation": "delete",
                    "anchorRange": button,
                    "targetRange": button,
                }
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(payload["status"], "OK")
            self.assertNotIn("<button", payload["content"])
            self.assertIn("<p>欢迎</p>", payload["content"])

    def test_delete_parent_fails_isomorphism(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": EDIT_SOURCE})
            main = _fix_range(EDIT_SOURCE, "<main>", "</main>")
            code, payload, stderr = self._edit(
                {
                    "workdir": tmp,
                    "file": "src/App.jsx",
                    "operation": "delete",
                    "anchorRange": main,
                    "targetRange": main,
                }
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(payload["status"], "ERROR")
            self.assertEqual(payload["code"], DELETE_SCOPE_MISMATCH)

    def test_expanded_target_range_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": EDIT_SOURCE})
            button = _fix_range(EDIT_SOURCE, "<button", "</button>")
            main = _fix_range(EDIT_SOURCE, "<main>", "</main>")
            code, payload, stderr = self._edit(
                {
                    "workdir": tmp,
                    "file": "src/App.jsx",
                    "operation": "modify",
                    "anchorRange": button,
                    "targetRange": main,
                    "jsxCode": "<div />",
                }
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(payload["status"], "ERROR")
            self.assertEqual(payload["code"], RANGE_NOT_ALLOWED)

    def test_invalid_jsx_snippet_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": EDIT_SOURCE})
            button = _fix_range(EDIT_SOURCE, "<button", "</button>")
            code, payload, stderr = self._edit(
                {
                    "workdir": tmp,
                    "file": "src/App.jsx",
                    "operation": "modify",
                    "anchorRange": button,
                    "targetRange": button,
                    "jsxCode": "const x = 1",
                }
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(payload["status"], "ERROR")
            self.assertEqual(payload["code"], "INVALID_JSX_SNIPPET")

    def test_stale_anchor_range_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": EDIT_SOURCE})
            stale = {"startOffset": 1, "endOffset": 5}
            code, payload, stderr = self._edit(
                {
                    "workdir": tmp,
                    "file": "src/App.jsx",
                    "operation": "modify",
                    "anchorRange": stale,
                    "targetRange": stale,
                    "jsxCode": "<div />",
                }
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(payload["status"], "ERROR")
            self.assertEqual(payload["code"], "ANCHOR_RANGE_MISMATCH")


# ------------------------------------------------------------
# 工具层硬门禁
# ------------------------------------------------------------
ANCHOR_ID = anchor_id_for("ann_1")
ANCHOR_RANGE = {
    "startLine": 4,
    "startColumn": 7,
    "endLine": 4,
    "endColumn": 45,
    "startOffset": 60,
    "endOffset": 120,
}


class _FakeApplySandbox:
    """只实现 apply_ast_edit，用于验证工具层门禁的调用 / 短路。"""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result or {"status": "OK", "content": "<edited />"}

    def apply_ast_edit(
        self,
        sandbox_id: str,
        edit: dict[str, Any],
        draft_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {"sandbox_id": sandbox_id, "edit": edit, "draft_id": draft_id}
        )
        return self.result


def _anchor(**overrides: Any) -> dict[str, Any]:
    anchor = {
        "anchorId": ANCHOR_ID,
        "annotationId": "ann_1",
        "file": "src/App.jsx",
        "range": dict(ANCHOR_RANGE),
        "nodeType": "JSXElement",
        "component": "App",
        "nodeSource": '<button className="primary">登录</button>',
        "isShared": False,
        "protected": False,
        "sharedReason": "",
        "componentFile": "",
        "callSiteFile": "",
        "allowedFiles": ["src/App.jsx"],
        "forbiddenRanges": [],
        "allowedOperations": ["insert", "delete", "modify"],
        "status": RESOLVED,
        "candidates": [],
    }
    anchor.update(overrides)
    return anchor


def _resolution(*anchors: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": RESOLVED,
        "reason": "",
        "evidence": {},
        "anchors": list(anchors) or [_anchor()],
    }


def _context(
    resolution: dict[str, Any] | None = None,
    sandbox: _FakeApplySandbox | None = None,
) -> tuple[AstEditContext, _FakeApplySandbox]:
    fake = sandbox or _FakeApplySandbox()
    return (
        AstEditContext(
            sandbox=fake,  # type: ignore[arg-type]
            sandbox_id="sbx_1",
            draft_id=None,
            resolution=resolution or _resolution(),
        ),
        fake,
    )


def _request(**overrides: Any) -> dict[str, Any]:
    request = {
        "anchor_id": ANCHOR_ID,
        "operation": "modify",
        "target_range": dict(ANCHOR_RANGE),
        "jsx_code": '<button className="primary">登录</button>',
    }
    request.update(overrides)
    return request


class AstEditGateTest(unittest.TestCase):
    """工具层是唯一权威：模型给什么参数都要过白名单与范围校验。"""

    def test_happy_path_returns_tool_content(self) -> None:
        context, fake = _context()
        result = execute_ast_edit(_request(), context)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["anchorId"], ANCHOR_ID)
        self.assertEqual(result["content"], "<edited />")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["edit"]["operation"], "modify")

    def test_tool_without_context_fails_closed(self) -> None:
        tool = build_apply_ast_edit_tool()
        raw = tool.invoke(
            {
                "anchor_id": ANCHOR_ID,
                "operation": "modify",
                "target_range": dict(ANCHOR_RANGE),
                "jsx_code": "<button />",
            }
        )
        payload = json.loads(raw)
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["code"], EDIT_CONTEXT_REQUIRED)

    def test_unknown_anchor_is_rejected(self) -> None:
        context, fake = _context()
        result = execute_ast_edit(_request(anchor_id="anc_fake"), context)
        self.assertEqual(result["code"], ANCHOR_NOT_FOUND)
        self.assertEqual(fake.calls, [])

    def test_invalid_operation_is_rejected(self) -> None:
        context, fake = _context()
        result = execute_ast_edit(_request(operation="replace"), context)
        self.assertEqual(result["code"], INVALID_OPERATION)
        self.assertEqual(fake.calls, [])

    def test_file_outside_whitelist_is_rejected(self) -> None:
        context, fake = _context(_resolution(_anchor(allowedFiles=["src/Other.jsx"])))
        result = execute_ast_edit(_request(), context)
        self.assertEqual(result["code"], FILE_NOT_ALLOWED)
        self.assertEqual(fake.calls, [])

    def test_operation_not_allowed_is_rejected(self) -> None:
        context, fake = _context(
            _resolution(_anchor(allowedOperations=["insert", "modify"]))
        )
        result = execute_ast_edit(_request(operation="delete", jsx_code=""), context)
        self.assertEqual(result["code"], OPERATION_NOT_ALLOWED)
        self.assertEqual(fake.calls, [])

    def test_protected_component_definition_is_rejected(self) -> None:
        context, fake = _context(
            _resolution(
                _anchor(
                    isShared=True,
                    protected=True,
                    componentFile="src/App.jsx",
                    callSiteFile="src/Page.jsx",
                    allowedFiles=["src/App.jsx"],
                )
            )
        )
        result = execute_ast_edit(_request(), context)
        self.assertEqual(result["code"], PROTECTED_COMPONENT)
        self.assertEqual(fake.calls, [])

    def test_expanded_range_is_rejected(self) -> None:
        context, fake = _context()
        widened = {
            **ANCHOR_RANGE,
            "startOffset": ANCHOR_RANGE["startOffset"] - 10,
            "endOffset": ANCHOR_RANGE["endOffset"] + 10,
        }
        result = execute_ast_edit(_request(target_range=widened), context)
        self.assertEqual(result["code"], RANGE_NOT_ALLOWED)
        self.assertEqual(fake.calls, [])

    def test_forbidden_range_overlap_is_rejected(self) -> None:
        context, fake = _context(
            _resolution(
                _anchor(
                    forbiddenRanges=[
                        {
                            "startOffset": ANCHOR_RANGE["startOffset"] - 5,
                            "endOffset": ANCHOR_RANGE["endOffset"] + 5,
                        }
                    ]
                )
            )
        )
        result = execute_ast_edit(_request(), context)
        self.assertEqual(result["code"], PROTECTED_COMPONENT)
        self.assertEqual(fake.calls, [])

    def test_insert_accepts_zero_width_anchor_edge(self) -> None:
        context, fake = _context()
        edge = {
            "startOffset": ANCHOR_RANGE["endOffset"],
            "endOffset": ANCHOR_RANGE["endOffset"],
        }
        result = execute_ast_edit(
            _request(operation="insert", target_range=edge, jsx_code="<span />"),
            context,
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(len(fake.calls), 1)

    def test_sandbox_error_code_is_passed_through(self) -> None:
        sandbox = _FakeApplySandbox(
            result={
                "status": "ERROR",
                "code": DELETE_SCOPE_MISMATCH,
                "message": "删除范围不一致",
                "details": {"target": ANCHOR_RANGE},
            }
        )
        context, _ = _context(sandbox=sandbox)
        result = execute_ast_edit(_request(operation="delete", jsx_code=""), context)
        self.assertEqual(result["code"], DELETE_SCOPE_MISMATCH)


# ------------------------------------------------------------
# Graph 链路
# ------------------------------------------------------------
ANNOTATION_SELECTOR = ".primary"


def _annotation(text: str = "把登录按钮改成蓝色") -> dict[str, Any]:
    return {
        "annotationId": "ann_e2e",
        "threadId": "thread_e2e",
        "action": "MODIFY",
        "type": "visual-click",
        "intent": {"kind": "UI_ONLY", "ui": {"summary": text, "acceptance": []}},
        "payload": {"text": text},
        "target": {
            "selector": ANNOTATION_SELECTOR,
            "source": {"file": "src/App.jsx", "line": 4, "column": 7},
        },
    }


def _context_payload(annotation: dict[str, Any]) -> dict[str, Any]:
    return {
        "count": 1,
        "annotations": [
            {
                "annotation_id": annotation["annotationId"],
                "thread_id": annotation.get("threadId"),
                "annotation": annotation,
                "instruction": annotation["payload"]["text"],
                "ui_summary": annotation["payload"]["text"],
            }
        ],
    }


def _e2e_state(context: dict[str, Any], code: str) -> dict[str, Any]:
    return {
        "run_id": "run_phase1_e2e",
        "tenant_id": "tenant_e2e",
        "project_id": "project_e2e",
        "sandbox_id": "sbx_e2e",
        "target_file": "src/App.jsx",
        "target_files": ["src/App.jsx"],
        "annotation_context": context,
        "messages": [],
        "current_code": code,
        "current_files": {"src/App.jsx": code},
        "plan": "",
        "generated_patch": "",
        "generated_files": {},
        "verify_result": {},
        "apply_failed": False,
        "retry_count": 0,
        "max_retries": 0,
        "history_summary": "",
        "clarification_mode": "off",
        "max_clarification_rounds": 3,
        "confidence_threshold": 0.7,
        "clarification_round": 0,
        "clarification_answers": [],
        "clarification": None,
        "approval_required": False,
        "status": "RUNNING",
        "error": None,
        "error_code": None,
    }


class _Phase1Sandbox(SandboxProvider):
    """内存沙盒 + 真实 Babel 编辑：E2E 不依赖 Docker / npm。"""

    def __init__(self, files: dict[str, str], resolution: dict[str, Any]) -> None:
        self.files = dict(files)
        self.resolution = resolution
        self.resolve_calls: list[list[dict[str, Any]]] = []
        self._lock = threading.Lock()

    # ---- Resolver ----
    def resolve_targets(
        self,
        sandbox_id: str,
        anchors: list[dict[str, Any]],
        draft_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.resolve_calls.append(anchors)
        return list(self.resolution.get("anchors") or [])

    # ---- 编辑 ----
    def apply_ast_edit(
        self,
        sandbox_id: str,
        edit: dict[str, Any],
        draft_id: str | None = None,
    ) -> dict[str, Any]:
        file = str(edit["file"])
        code = edit.get("contentOverride")
        if not isinstance(code, str):
            code = self.files[file]
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {file: code})
            exit_code, payload, stderr = _run_script(
                "apply_ast_edit.mjs",
                {
                    **edit,
                    "workdir": tmp,
                    "parserRoot": str(TEMPLATE_DIR),
                },
            )
        if exit_code == 2 or payload is None:
            raise RuntimeError(f"AST 编辑脚本异常: {stderr}")
        return payload

    # ---- SandboxProvider 其余能力 ----
    def create(self, project_id, files, template="react-vite"):  # type: ignore[override]
        raise NotImplementedError

    def apply_patch(
        self,
        sandbox_id: str,
        files: dict[str, str] | None = None,
        patch: str | None = None,
        deleted_paths: list[str] | None = None,
    ) -> list[str]:
        with self._lock:
            incoming = dict(files or {})
            self.files.update(incoming)
            for path in deleted_paths or []:
                self.files.pop(path, None)
            return sorted(incoming)

    def verify(self, sandbox_id: str, check: str = "build") -> VerifyResult:
        return VerifyResult(
            success=True,
            exit_code=0,
            timed_out=False,
            duration_ms=1,
            logs="ok",
            logs_truncated=False,
        )

    def read_file(self, sandbox_id: str, path: str) -> str:
        return self.files[path]

    def export_archive(self, sandbox_id: str) -> bytes:
        return b""

    def destroy(self, sandbox_id: str) -> None:
        return None


class _ScriptedEditLLM(MockAgentLLM):
    """绕过真实模型，直接提交工具调用，验证 Graph 侧协议接线。"""

    def __init__(self, edits: list[dict[str, Any]]) -> None:
        super().__init__(scenario="none")
        self._edits = edits
        self.generate_calls: list[dict[str, Any]] = []

    def generate(self, **kwargs: Any) -> CodeResult:  # type: ignore[override]
        self.generate_calls.append(kwargs)
        return CodeResult(summary="按锚点修改按钮", edits=self._edits)


def _resolved_anchor(code: str) -> dict[str, Any]:
    button = _fix_range(code, "<button", "</button>")
    return {
        "anchorId": anchor_id_for("ann_e2e"),
        "annotationId": "ann_e2e",
        "file": "src/App.jsx",
        "range": button,
        "nodeType": "JSXElement",
        "component": "App",
        "nodeSource": '<button className="primary">登录</button>',
        "isShared": False,
        "protected": False,
        "sharedReason": "",
        "componentFile": "",
        "callSiteFile": "",
        "allowedFiles": ["src/App.jsx"],
        "forbiddenRanges": [],
        "allowedOperations": ["insert", "delete", "modify"],
        "status": RESOLVED,
        "candidates": [],
    }


@unittest.skipUnless(NODE, "node 不可用，跳过 Babel 脚本测试")
class Phase1GraphE2ETest(unittest.TestCase):
    """style_check -> resolver -> coder(tool_calls) -> applier -> verifier。"""

    def _runner(
        self,
        sandbox: SandboxProvider,
        llm: MockAgentLLM,
    ) -> AgentGraphRunner:
        return AgentGraphRunner(
            sandbox=sandbox,
            llm=llm,
            max_retries=0,
            checkpointer_provider=MemoryCheckpointerProvider(),
        )

    def test_resolver_to_coder_tool_chain(self) -> None:
        anchor = _resolved_anchor(EDIT_SOURCE)
        sandbox = _Phase1Sandbox(
            {"src/App.jsx": EDIT_SOURCE},
            {"status": RESOLVED, "anchors": [anchor]},
        )
        llm = _ScriptedEditLLM(
            [
                {
                    "name": "apply_ast_edit",
                    "arguments": {
                        "anchor_id": anchor["anchorId"],
                        "operation": "modify",
                        "target_range": anchor["range"],
                        "jsx_code": (
                            '<button className="primary bg-blue-600">登录</button>'
                        ),
                    },
                }
            ]
        )
        runner = self._runner(sandbox, llm)
        outcome = runner.run(
            _e2e_state(_context_payload(_annotation()), EDIT_SOURCE),
            thread_id="thread_phase1_e2e",
        )

        self.assertFalse(outcome.interrupted)
        state = outcome.state
        self.assertEqual(state.get("status"), "SUCCEEDED", state.get("error"))
        self.assertEqual(state.get("resolver_outcome"), "resolved")
        self.assertEqual(len(sandbox.resolve_calls), 1)
        # Coder 必须收到解析结果，且最终内容来自工具而非模型。
        self.assertIn("target_resolution", llm.generate_calls[-1])
        self.assertIn("bg-blue-600", sandbox.files["src/App.jsx"])
        self.assertEqual(len(state.get("ast_edit_calls") or []), 1)
        self.assertTrue(state.get("generated_patch"))

    def test_ambiguous_target_pauses_then_re_resolves(self) -> None:
        code = EDIT_SOURCE
        button = _fix_range(code, "<button", "</button>")
        resolved = _resolved_anchor(code)
        ambiguous = {
            "anchorId": resolved["anchorId"],
            "annotationId": "ann_e2e",
            "status": TARGET_AMBIGUOUS,
            "file": "src/App.jsx",
            "reason": "选择器命中 2 个节点",
            "candidates": [
                {
                    "candidateId": "anc_ann_e2e_c1",
                    "file": "src/App.jsx",
                    "range": button,
                    "nodeType": "JSXElement",
                    "component": "App",
                    "preview": "<button>登录</button>",
                },
                {
                    "candidateId": "anc_ann_e2e_c2",
                    "file": "src/App.jsx",
                    "range": button,
                    "nodeType": "JSXElement",
                    "component": "App",
                    "preview": "<button>注册</button>",
                },
            ],
        }

        class _AmbiguousSandbox:
            """首次返回歧义；收到 selectedRange 后再返回唯一锚点。"""

            def __init__(self) -> None:
                self.calls: list[list[dict[str, Any]]] = []

            def resolve_targets(self, sandbox_id, anchors, draft_id=None):
                self.calls.append([dict(item) for item in anchors])
                if len(self.calls) == 1:
                    return [ambiguous]
                return [resolved]

        sandbox = _AmbiguousSandbox()
        runner = AgentGraphRunner.__new__(AgentGraphRunner)
        runner._sandbox = sandbox  # type: ignore[assignment]
        runner._emit = lambda *args, **kwargs: None  # type: ignore[method-assign]
        state = _e2e_state(_context_payload(_annotation()), code)
        state["clarification_mode"] = "auto"
        state["max_clarification_rounds"] = 3

        with patch.object(
            graph_module,
            "interrupt",
            return_value=[
                {
                    "questionId": f"resolution_{resolved['anchorId']}",
                    "value": "anc_ann_e2e_c1",
                }
            ],
        ):
            update = runner._resolver(state)

        self.assertEqual(update["resolver_outcome"], "resolved")
        self.assertEqual(len(sandbox.calls), 2)
        self.assertEqual(
            sandbox.calls[1][0]["selectedRange"]["startOffset"],
            button["startOffset"],
        )


if __name__ == "__main__":
    unittest.main()
