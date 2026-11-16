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
from ai_core.agent.llm import CodeResult, MockAgentLLM, _parse_edit_result
from ai_core.domain.resolution import (
    ANCHOR_NOT_FOUND,
    DELETE_SCOPE_MISMATCH,
    EDIT_CONTEXT_REQUIRED,
    FILE_NOT_ALLOWED,
    INVALID_INSERTION_POINT,
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

SELF_CLOSING_SOURCE = """export default function App() {
  return (
    <main>
      <img src="/logo.png" />
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

    def test_delete_safety_flags_map_and_route_root(self) -> None:
        source = """export default function App() {
  const items = [1, 2]
  return (
    <main>
      <ul>{items.map((item) => <li>{item}</li>)}</ul>
    </main>
  )
}
"""
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": source})
            code, payload, stderr = _run_script(
                "resolve_target.mjs",
                {
                    "workdir": tmp,
                    "parserRoot": str(TEMPLATE_DIR),
                    "anchors": [
                        _anchor_request("anc_li", selector="li"),
                        _anchor_request("anc_main", selector="main"),
                    ],
                },
            )
        self.assertEqual(code, 0, stderr)
        anchors = {item["anchorId"]: item for item in payload["anchors"]}
        self.assertTrue(anchors["anc_li"]["inMap"])
        self.assertFalse(anchors["anc_li"]["deleteSafe"])
        self.assertTrue(anchors["anc_main"]["isRouteRoot"])
        self.assertFalse(anchors["anc_main"]["deleteSafe"])
        self.assertIn("map", "；".join(anchors["anc_li"]["deleteReasons"]))

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

    def test_container_reports_inside_positions_and_echoes_placement(self) -> None:
        """拖拽落位：容器锚点允许 inside_*，placement 回传并补齐 siblingCount。"""
        placement = {
            "version": 1,
            "targetParentId": "src/App.jsx:3:5#1",
            "insertPosition": "inside_index",
            "siblingIndex": 1,
            "parent": {"label": "main"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": RESOLVER_SOURCE})
            code, payload, stderr = _run_script(
                "resolve_target.mjs",
                {
                    "workdir": tmp,
                    "parserRoot": str(TEMPLATE_DIR),
                    "anchors": [
                        _anchor_request(
                            "anc_ann_main", selector="main", placement=placement
                        )
                    ],
                },
            )
            self.assertEqual(code, 0, stderr)
            anchor = payload["anchors"][0]
            self.assertEqual(anchor["status"], RESOLVED)
            self.assertEqual(
                anchor["allowedInsertPositions"],
                [
                    "before",
                    "after",
                    "inside_start",
                    "inside_end",
                    "inside_index",
                ],
            )
            self.assertEqual(
                anchor["placement"],
                {**placement, "siblingCount": 3},
            )

    def test_sibling_placement_is_converted_to_exact_inside_index(self) -> None:
        """前端 before 是相对兄弟节点：Resolver 必须换算成父容器子序号。"""
        placement = {
            "version": 1,
            "targetParentId": "src/App.jsx:3:5#1",
            "insertPosition": "before",
            "siblingIndex": 1,
            "insertAnchor": {"line": 5, "column": 7, "label": "第二个按钮"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": RESOLVER_SOURCE})
            code, payload, stderr = _run_script(
                "resolve_target.mjs",
                {
                    "workdir": tmp,
                    "parserRoot": str(TEMPLATE_DIR),
                    "anchors": [
                        _anchor_request(
                            "anc_ann_main", selector="main", placement=placement
                        )
                    ],
                },
            )
            self.assertEqual(code, 0, stderr)
            anchor = payload["anchors"][0]
            self.assertEqual(anchor["status"], RESOLVED)
            # 第二个按钮在 main 的子节点里 index = 1；before 即插在它前面。
            self.assertEqual(anchor["placement"]["insertPosition"], "inside_index")
            self.assertEqual(anchor["placement"]["siblingIndex"], 1)
            self.assertEqual(anchor["placement"]["siblingCount"], 3)

    def test_inside_end_placement_appends_after_existing_children(self) -> None:
        """inside_end 的 siblingIndex 必须归一到子节点数量，避免误插中间。"""
        placement = {
            "version": 1,
            "targetParentId": "src/App.jsx:3:5#1",
            "insertPosition": "inside_end",
            "siblingIndex": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": RESOLVER_SOURCE})
            code, payload, stderr = _run_script(
                "resolve_target.mjs",
                {
                    "workdir": tmp,
                    "parserRoot": str(TEMPLATE_DIR),
                    "anchors": [
                        _anchor_request(
                            "anc_ann_main", selector="main", placement=placement
                        )
                    ],
                },
            )
            self.assertEqual(code, 0, stderr)
            anchor = payload["anchors"][0]
            self.assertEqual(anchor["placement"]["insertPosition"], "inside_end")
            self.assertEqual(anchor["placement"]["siblingIndex"], 3)

    def test_self_closing_anchor_only_allows_sibling_insert(self) -> None:
        """自闭合元素没有 children，必须拒绝 inside_*。"""
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": SELF_CLOSING_SOURCE})
            code, payload, stderr = _run_script(
                "resolve_target.mjs",
                {
                    "workdir": tmp,
                    "parserRoot": str(TEMPLATE_DIR),
                    "anchors": [_anchor_request("anc_ann_img", selector="img")],
                },
            )
            self.assertEqual(code, 0, stderr)
            anchor = payload["anchors"][0]
            self.assertEqual(anchor["status"], RESOLVED)
            self.assertEqual(anchor["allowedInsertPositions"], ["before", "after"])

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

    def test_delete_reports_exact_subtree_node_count(self) -> None:
        source = "<main><h1>标题</h1><p>说明</p></main>"
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": source})
            code, payload, stderr = self._edit(
                {
                    "workdir": tmp,
                    "file": "src/App.jsx",
                    "operation": "delete",
                    "anchorRange": _fix_range(source, "<h1>", "</h1>"),
                    "targetRange": _fix_range(source, "<h1>", "</h1>"),
                }
            )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["nodeCountBefore"], 3)
        self.assertEqual(payload["nodeCountAfter"], 2)
        self.assertEqual(payload["targetNodeCount"], 1)

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

    def test_insert_inside_end_appends_child_before_closing_tag(self) -> None:
        """inside_end：按父容器自然文档流追加子节点，缩进比父级深一层。"""
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": EDIT_SOURCE})
            main = _fix_range(EDIT_SOURCE, "<main>", "</main>")
            code, payload, stderr = self._edit(
                {
                    "workdir": tmp,
                    "file": "src/App.jsx",
                    "operation": "insert",
                    "anchorRange": main,
                    "insertPosition": "inside_end",
                    "jsxCode": "<footer>新增</footer>",
                }
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(payload["status"], "OK")
            content = payload["content"]
            self.assertIn("      <footer>新增</footer>\n    </main>", content)
            self.assertLess(content.index("<p>欢迎</p>"), content.index("<footer>"))

    def test_insert_inside_index_places_between_siblings(self) -> None:
        """inside_index：插到第 N 个子节点之前，顺序与前端 siblingIndex 一致。"""
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": EDIT_SOURCE})
            main = _fix_range(EDIT_SOURCE, "<main>", "</main>")
            code, payload, stderr = self._edit(
                {
                    "workdir": tmp,
                    "file": "src/App.jsx",
                    "operation": "insert",
                    "anchorRange": main,
                    "insertPosition": "inside_index",
                    "insertIndex": 1,
                    "jsxCode": "<span>中间</span>",
                }
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(payload["status"], "OK")
            content = payload["content"]
            self.assertLess(content.index("<button"), content.index("<span>"))
            self.assertLess(content.index("<span>"), content.index("<p>欢迎</p>"))

    def test_insert_inside_start_places_before_first_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": EDIT_SOURCE})
            main = _fix_range(EDIT_SOURCE, "<main>", "</main>")
            code, payload, stderr = self._edit(
                {
                    "workdir": tmp,
                    "file": "src/App.jsx",
                    "operation": "insert",
                    "anchorRange": main,
                    "insertPosition": "inside_start",
                    "jsxCode": "<header>顶部</header>",
                }
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(payload["status"], "OK")
            content = payload["content"]
            self.assertLess(content.index("<header>"), content.index("<button"))

    def test_insert_inside_self_closing_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": SELF_CLOSING_SOURCE})
            image = _fix_range(SELF_CLOSING_SOURCE, "<img", "/>")
            code, payload, stderr = self._edit(
                {
                    "workdir": tmp,
                    "file": "src/App.jsx",
                    "operation": "insert",
                    "anchorRange": image,
                    "insertPosition": "inside_end",
                    "jsxCode": "<span>非法</span>",
                }
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(payload["status"], "ERROR")
            self.assertEqual(payload["code"], INVALID_INSERTION_POINT)

    def test_insert_inside_index_out_of_range_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": EDIT_SOURCE})
            main = _fix_range(EDIT_SOURCE, "<main>", "</main>")
            code, payload, stderr = self._edit(
                {
                    "workdir": tmp,
                    "file": "src/App.jsx",
                    "operation": "insert",
                    "anchorRange": main,
                    "insertPosition": "inside_index",
                    "insertIndex": 99,
                    "jsxCode": "<span>越界</span>",
                }
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(payload["status"], "ERROR")
            self.assertEqual(payload["code"], INVALID_INSERTION_POINT)

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

    def test_inside_position_outside_anchor_whitelist_is_rejected(self) -> None:
        """自闭合锚点只给 before/after 时，inside_* 必须在工具层被拒绝。"""
        context, fake = _context(
            _resolution(_anchor(allowedInsertPositions=["before", "after"]))
        )
        result = execute_ast_edit(
            _request(
                operation="insert",
                target_range=None,
                insert_position="inside_end",
                jsx_code="<span />",
            ),
            context,
        )
        self.assertEqual(result["code"], INVALID_INSERTION_POINT)
        self.assertEqual(fake.calls, [])

    def test_inside_index_requires_insert_index(self) -> None:
        context, fake = _context(
            _resolution(
                _anchor(
                    allowedInsertPositions=[
                        "before",
                        "after",
                        "inside_start",
                        "inside_end",
                        "inside_index",
                    ]
                )
            )
        )
        result = execute_ast_edit(
            _request(
                operation="insert",
                target_range=None,
                insert_position="inside_index",
                jsx_code="<span />",
            ),
            context,
        )
        self.assertEqual(result["code"], INVALID_INSERTION_POINT)
        self.assertEqual(fake.calls, [])

    def test_inside_index_passes_index_to_sandbox(self) -> None:
        context, fake = _context(
            _resolution(
                _anchor(
                    allowedInsertPositions=[
                        "before",
                        "after",
                        "inside_start",
                        "inside_end",
                        "inside_index",
                    ]
                )
            )
        )
        result = execute_ast_edit(
            _request(
                operation="insert",
                target_range=None,
                insert_position="inside_index",
                insert_index=2,
                jsx_code="<span />",
            ),
            context,
        )
        self.assertEqual(result["status"], "OK")
        edit = fake.calls[0]["edit"]
        self.assertEqual(edit["insertPosition"], "inside_index")
        self.assertEqual(edit["insertIndex"], 2)

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


class PlacementEditParseTest(unittest.TestCase):
    """拖拽落位事实必须能穿透到工具参数，模型漏填时也不能退化。"""

    def _raw(self, arguments: dict[str, Any]) -> str:
        return json.dumps(
            {
                "summary": "新增组件",
                "tool_calls": [{"name": "apply_ast_edit", "arguments": arguments}],
            },
            ensure_ascii=False,
        )

    def test_placement_defaults_insert_position_and_index(self) -> None:
        anchors = [
            _anchor(
                allowedInsertPositions=[
                    "before",
                    "after",
                    "inside_start",
                    "inside_end",
                    "inside_index",
                ],
                placement={
                    "insertPosition": "inside_index",
                    "siblingIndex": 2,
                    "targetParentId": "src/App.jsx:3:5#1",
                },
            )
        ]
        result = _parse_edit_result(
            self._raw(
                {
                    "anchor_id": ANCHOR_ID,
                    "operation": "insert",
                    "jsx_code": '<div data-vibe-created="ann_1" />',
                }
            ),
            anchors,
            required_markers={"ann_1"},
        )
        arguments = result.edits[0]["arguments"]
        self.assertEqual(arguments["insert_position"], "inside_index")
        self.assertEqual(arguments["insert_index"], 2)

    def test_explicit_position_overrides_placement(self) -> None:
        anchors = [
            _anchor(
                allowedInsertPositions=["before", "after"],
                placement={"insertPosition": "inside_index", "siblingIndex": 2},
            )
        ]
        result = _parse_edit_result(
            self._raw(
                {
                    "anchor_id": ANCHOR_ID,
                    "operation": "insert",
                    "insert_position": "before",
                    "jsx_code": '<div data-vibe-created="ann_1" />',
                }
            ),
            anchors,
            required_markers={"ann_1"},
        )
        self.assertEqual(result.edits[0]["arguments"]["insert_position"], "before")

    def test_position_outside_anchor_whitelist_is_rejected(self) -> None:
        anchors = [
            _anchor(
                allowedInsertPositions=["before", "after"],
                placement={"insertPosition": "inside_end", "siblingIndex": 0},
            )
        ]
        with self.assertRaisesRegex(Exception, "不支持 insert_position"):
            _parse_edit_result(
                self._raw(
                    {
                        "anchor_id": ANCHOR_ID,
                        "operation": "insert",
                        "insert_position": "inside_end",
                        "jsx_code": '<div data-vibe-created="ann_1" />',
                    }
                ),
                anchors,
                required_markers={"ann_1"},
            )


class AnchorReprojectionTest(unittest.TestCase):
    """同文件连续插入的坐标重投影 / 序号顺延纯函数。"""

    def test_insert_before_anchor_shifts_both_edges(self) -> None:
        project = graph_module._project_anchor_range
        self.assertEqual(project(100, 200, [(50, 10)]), (110, 210))
        self.assertEqual(project(100, 200, [(100, 7)]), (107, 207))

    def test_insert_inside_anchor_only_extends_end(self) -> None:
        project = graph_module._project_anchor_range
        self.assertEqual(project(100, 200, [(150, 12)]), (100, 212))

    def test_insert_after_anchor_keeps_range(self) -> None:
        project = graph_module._project_anchor_range
        self.assertEqual(project(100, 200, [(200, 9)]), (100, 200))
        self.assertEqual(project(100, 200, [(260, 9)]), (100, 200))

    def test_sequential_transforms_use_updated_coordinates(self) -> None:
        project = graph_module._project_anchor_range
        # 第一次在 50 插入 10，锚点变为 (110,210)；第二次插入点 150 位于
        # 新坐标系区间内部，只扩展终点。
        self.assertEqual(project(100, 200, [(50, 10), (150, 5)]), (110, 215))

    def test_inside_index_shift_keeps_submission_order(self) -> None:
        normalize = graph_module._normalize_insert_slot
        # 两个组件都落在同一个位置：第二次顺延到第一次之后。
        self.assertEqual(normalize("inside_index", 1, []), ("inside_index", 1))
        self.assertEqual(normalize("inside_index", 1, [1]), ("inside_index", 2))
        # inside_start 统一转成头部区序号，保证先后顺序。
        self.assertEqual(normalize("inside_start", 0, []), ("inside_index", 0))
        self.assertEqual(normalize("inside_start", 0, [0]), ("inside_index", 1))
        # inside_end 由脚本按当前子节点数追加。
        self.assertEqual(normalize("inside_end", None, [1]), ("inside_end", None))


@unittest.skipUnless(NODE, "node 不可用，跳过 Babel 脚本测试")
class SameParentMultiInsertTest(unittest.TestCase):
    """同一父容器连续插入：禁止 ANCHOR_RANGE_MISMATCH，顺序必须稳定。"""

    SOURCE = """export default function App() {
  return (
    <main>
      <p>标题</p>
      <p>正文</p>
    </main>
  );
}
"""

    def _setup(self, addition_context: dict[str, Any] | None = None):
        code = self.SOURCE
        parent_range = _fix_range(code, "<main", "</main>")
        anchors = []
        for annotation_id in ("ann_a", "ann_b"):
            anchors.append(
                {
                    "anchorId": anchor_id_for(annotation_id),
                    "annotationId": annotation_id,
                    "file": "src/App.jsx",
                    "range": dict(parent_range),
                    "nodeType": "JSXElement",
                    "component": "App",
                    "nodeSource": code[
                        parent_range["startOffset"] : parent_range["endOffset"]
                    ],
                    "isShared": False,
                    "protected": False,
                    "sharedReason": "",
                    "componentFile": "",
                    "callSiteFile": "",
                    "allowedFiles": ["src/App.jsx"],
                    "forbiddenRanges": [],
                    "allowedOperations": ["insert", "delete", "modify"],
                    "allowedInsertPositions": [
                        "before",
                        "after",
                        "inside_start",
                        "inside_end",
                        "inside_index",
                    ],
                    # Resolver 归一化后的拖拽落位：两个组件都落在
                    # “正文”之前（index=1）。
                    "placement": {
                        "insertPosition": "inside_index",
                        "siblingIndex": 1,
                        "siblingCount": 2,
                    },
                    "status": RESOLVED,
                    "candidates": [],
                }
            )
        resolution = {"status": RESOLVED, "anchors": anchors}
        sandbox = _Phase1Sandbox({"src/App.jsx": code}, resolution)
        runner = AgentGraphRunner(
            sandbox=sandbox,
            llm=MockAgentLLM(scenario="none"),
            max_retries=0,
            checkpointer_provider=MemoryCheckpointerProvider(),
        )
        state = {
            "sandbox_id": "sbx_multi_insert",
            "target_resolution": resolution,
            "retry_count": 0,
        }
        if addition_context is not None:
            state["annotation_context"] = addition_context
        return runner, state, dict(parent_range)

    @staticmethod
    def _batch_layout_context(order: list[tuple[str, str]]) -> dict[str, Any]:
        """构造 BATCH_LAYOUT 上下文；order 即前端拖入顺序。"""
        additions = []
        for annotation_id, text in order:
            additions.append(
                {
                    "annotationId": annotation_id,
                    "intent": text,
                    "componentType": "button",
                    "annotation": {
                        "annotationId": annotation_id,
                        "type": "visual-draw",
                        "action": "CREATE_NEW",
                        "intent": {"kind": "UI_ONLY", "ui": {"summary": text}},
                        "payload": {"text": text},
                    },
                }
            )
        return {
            "count": 1,
            "annotations": [
                {
                    "annotation_id": "layout_e2e",
                    "annotation": {
                        "annotationId": "layout_e2e",
                        "type": "drag",
                        "action": "BATCH_LAYOUT",
                        "payload": {"text": "统一提交画布变更", "additions": additions},
                    },
                }
            ],
        }

    @staticmethod
    def _edit(annotation_id: str, jsx: str, position: str, index=None):
        return {
            "name": "apply_ast_edit",
            "arguments": {
                "anchor_id": anchor_id_for(annotation_id),
                "operation": "insert",
                "jsx_code": jsx,
                "insert_position": position,
                "insert_index": index,
            },
        }

    def test_two_inside_end_inserts_keep_order_without_range_mismatch(self) -> None:
        runner, state, _ = self._setup()
        edits = [
            self._edit(
                "ann_a", '<button data-vibe-created="ann_a">A</button>', "inside_end"
            ),
            self._edit(
                "ann_b", '<span data-vibe-created="ann_b">B</span>', "inside_end"
            ),
        ]
        outcome = runner._apply_ast_edit_calls(
            state, edits, {"src/App.jsx": self.SOURCE}
        )
        self.assertNotIn("failure", outcome, outcome.get("failure"))
        content = outcome["files"]["src/App.jsx"]
        self.assertLess(content.index("ann_a"), content.index("ann_b"))
        self.assertLess(content.index("<p>正文</p>"), content.index("ann_a"))

    def test_two_inside_index_inserts_shift_to_preserve_order(self) -> None:
        runner, state, _ = self._setup()
        # 都拖到「正文」之前（index=1）：第二个必须顺延到 index=2。
        edits = [
            self._edit(
                "ann_a",
                '<button data-vibe-created="ann_a">A</button>',
                "inside_index",
                1,
            ),
            self._edit(
                "ann_b",
                '<span data-vibe-created="ann_b">B</span>',
                "inside_index",
                1,
            ),
        ]
        outcome = runner._apply_ast_edit_calls(
            state, edits, {"src/App.jsx": self.SOURCE}
        )
        self.assertNotIn("failure", outcome, outcome.get("failure"))
        content = outcome["files"]["src/App.jsx"]
        self.assertLess(content.index("ann_a"), content.index("ann_b"))
        self.assertLess(content.index("<p>标题</p>"), content.index("ann_a"))

    def test_two_inside_start_inserts_keep_order(self) -> None:
        runner, state, _ = self._setup()
        edits = [
            self._edit(
                "ann_a",
                '<button data-vibe-created="ann_a">A</button>',
                "inside_start",
            ),
            self._edit(
                "ann_b",
                '<span data-vibe-created="ann_b">B</span>',
                "inside_start",
            ),
        ]
        outcome = runner._apply_ast_edit_calls(
            state, edits, {"src/App.jsx": self.SOURCE}
        )
        self.assertNotIn("failure", outcome, outcome.get("failure"))
        content = outcome["files"]["src/App.jsx"]
        self.assertLess(content.index("ann_a"), content.index("ann_b"))
        self.assertLess(content.index("ann_b"), content.index("<p>标题</p>"))

    def test_batch_layout_reversed_calls_keep_drag_order(self) -> None:
        """模型把 tool_calls 写反时，源码顺序仍以拖入顺序为准。

        真实事故：用户先拖入“提交”再拖入“重置”，模型先输出重置的
        tool_call。若按模型顺序执行，两个组件都落在同一 inside_index，
        最终源码就会变成“重置、提交”，与画布视觉顺序相反。
        """
        context = self._batch_layout_context(
            [("ann_a", "添加一个提交按钮"), ("ann_b", "添加一个重置按钮")]
        )
        runner, state, _ = self._setup(addition_context=context)
        # 故意反转：先 ann_b（第二次拖入），再 ann_a（第一次拖入）。
        edits = [
            self._edit(
                "ann_b",
                '<span data-vibe-created="ann_b">B</span>',
                "inside_index",
                1,
            ),
            self._edit(
                "ann_a",
                '<button data-vibe-created="ann_a">A</button>',
                "inside_index",
                1,
            ),
        ]
        outcome = runner._apply_ast_edit_calls(
            state, edits, {"src/App.jsx": self.SOURCE}
        )
        self.assertNotIn("failure", outcome, outcome.get("failure"))
        content = outcome["files"]["src/App.jsx"]
        self.assertLess(content.index("ann_a"), content.index("ann_b"))
        self.assertLess(content.index("<p>标题</p>"), content.index("ann_a"))

    def test_batch_layout_ignores_model_precompensated_index(self) -> None:
        """模型自行把第二个 insert_index 写成 base+1 时不得越界。

        真实事故：两次拖拽的 placement.siblingIndex 都是 4，模型为了让
        第二个排在后面写成 5，服务端若再顺延一次就变成 6 > childCount 5，
        工具直接 INVALID_INSERTION_POINT。批次内序号必须由服务端计算。
        """
        context = self._batch_layout_context(
            [("ann_a", "添加一个提交按钮"), ("ann_b", "添加一个重置按钮")]
        )
        runner, state, _ = self._setup(addition_context=context)
        edits = [
            self._edit(
                "ann_a",
                '<button data-vibe-created="ann_a">A</button>',
                "inside_index",
                1,
            ),
            self._edit(
                "ann_b",
                '<span data-vibe-created="ann_b">B</span>',
                "inside_index",
                2,  # 模型预补偿：placement.siblingIndex(1) + 1
            ),
        ]
        outcome = runner._apply_ast_edit_calls(
            state, edits, {"src/App.jsx": self.SOURCE}
        )
        self.assertNotIn("failure", outcome, outcome.get("failure"))
        content = outcome["files"]["src/App.jsx"]
        self.assertLess(content.index("ann_a"), content.index("ann_b"))
        self.assertLess(content.index("<p>标题</p>"), content.index("ann_a"))
        self.assertLess(content.index("ann_b"), content.index("<p>正文</p>"))

    def test_same_anchor_inserts_follow_drag_order_without_batch(self) -> None:
        """缺少 BATCH_LAYOUT 顺序信息时保留原有模型调用顺序（回归）。"""
        runner, state, _ = self._setup()
        edits = [
            self._edit(
                "ann_b",
                '<span data-vibe-created="ann_b">B</span>',
                "inside_index",
                1,
            ),
            self._edit(
                "ann_a",
                '<button data-vibe-created="ann_a">A</button>',
                "inside_index",
                1,
            ),
        ]
        outcome = runner._apply_ast_edit_calls(
            state, edits, {"src/App.jsx": self.SOURCE}
        )
        self.assertNotIn("failure", outcome, outcome.get("failure"))
        content = outcome["files"]["src/App.jsx"]
        self.assertLess(content.index("ann_b"), content.index("ann_a"))

    def test_sibling_inserts_execute_in_reverse_drag_order(self) -> None:
        """before / after 紧贴锚点插入：后执行的更靠近锚点。

        因此同一锚点的兄弟插入必须按拖入序号**降序**执行，最终源码
        顺序才与拖入顺序一致（纯排序键测试，不依赖 node）。
        """
        key = graph_module._edit_order_key
        first = (100, 0, "after", 0, {}, {})
        second = (100, 1, "after", 1, {}, {})
        self.assertLess(key(second), key(first))
        # inside_* 则按升序执行，序号顺延保证最终顺序。
        first_inside = (100, 0, "inside_index", 0, {}, {})
        second_inside = (100, 1, "inside_index", 1, {}, {})
        self.assertLess(key(first_inside), key(second_inside))


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

    def test_ambiguous_target_pauses_for_decision_then_re_resolves(self) -> None:
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
        state["run_id"] = "run_phase2_e2e"

        # Phase 2：第一次解析不再 interrupt，而是返回 pending_decision。
        first = runner._resolver(state)
        self.assertEqual(first["resolver_outcome"], "ambiguous")
        decision = first["pending_decision"]
        self.assertEqual(decision["kind"], "target_ambiguous")
        select_option = next(
            item for item in decision["options"] if item["action"] == "select_target"
        )
        self.assertTrue(
            all(
                item["action"] in {"select_target", "retry", "cancel"}
                for item in decision["options"]
            )
        )

        state.update(first)
        with patch.object(
            graph_module,
            "interrupt",
            return_value={
                "decisionId": decision["decisionId"],
                "optionId": select_option["optionId"],
            },
        ):
            gate_update = runner._decision_gate(state)
        self.assertEqual(gate_update["decision_outcome"], "decided")
        self.assertEqual(
            gate_update["decision_selections"][resolved["anchorId"]]["range"][
                "startOffset"
            ],
            button["startOffset"],
        )

        state.update(gate_update)
        second = runner._resolver(state)
        self.assertEqual(second["resolver_outcome"], "resolved")
        self.assertEqual(len(sandbox.calls), 2)
        self.assertEqual(
            sandbox.calls[1][0]["selectedRange"]["startOffset"],
            button["startOffset"],
        )


@unittest.skipUnless(NODE, "node 不可用，跳过 Babel 脚本测试")
class AstEditMoveNodeTest(unittest.TestCase):
    """Direct Ops MOVE_NODE：同文件容器内部移动，节点数量守恒。"""

    def _move(
        self,
        code: str,
        source: dict[str, int],
        target: dict[str, int],
        *,
        position: str = "inside_end",
        index: int | None = None,
    ) -> tuple[int, dict[str, Any] | None, str]:
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": code})
            return _run_script(
                "apply_ast_edit.mjs",
                {
                    "workdir": tmp,
                    "parserRoot": str(TEMPLATE_DIR),
                    "file": "src/App.jsx",
                    "operation": "move_node",
                    "anchorRange": source,
                    "targetRange": target,
                    "insertPosition": position,
                    "insertIndex": index,
                },
            )

    def test_moves_node_between_containers(self) -> None:
        source = (
            "<main><section><h1>标题</h1></section>"
            "<footer><p>说明</p></footer></main>"
        )
        code, payload, stderr = self._move(
            source,
            _fix_range(source, "<h1>", "</h1>"),
            _fix_range(source, "<footer>", "</footer>"),
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(payload["status"], "OK")
        content = payload["content"]
        self.assertLess(content.index("<footer>"), content.index("<h1>"))
        self.assertEqual(content.count("<h1>"), 1)

    def test_moves_node_within_same_container(self) -> None:
        source = "<main><h1>标题</h1><p>说明</p></main>"
        code, payload, stderr = self._move(
            source,
            _fix_range(source, "<h1>", "</h1>"),
            _fix_range(source, "<main>", "</main>"),
            position="inside_index",
            # 同父容器移动语义：siblingIndex 是移除 source 后的最终序号。
            index=1,
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(payload["status"], "OK")
        content = payload["content"]
        self.assertLess(content.index("<p>说明</p>"), content.index("<h1>标题</h1>"))

    def test_rejects_move_into_self(self) -> None:
        source = "<main><h1>标题</h1></main>"
        code, payload, stderr = self._move(
            source,
            _fix_range(source, "<h1>", "</h1>"),
            _fix_range(source, "<h1>", "</h1>"),
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["code"], "INVALID_MOVE_TARGET")


@unittest.skipUnless(NODE, "node 不可用，跳过 Babel 脚本测试")
class AstEditUpdateTextTest(unittest.TestCase):
    """Direct Ops UPDATE_TEXT：只允许唯一 JSXText 子节点。"""

    def _edit(
        self, code: str, range_: dict[str, int], text: str
    ) -> tuple[int, dict[str, Any] | None, str]:
        with tempfile.TemporaryDirectory() as tmp:
            _write_project(Path(tmp), {"src/App.jsx": code})
            return _run_script(
                "apply_ast_edit.mjs",
                {
                    "workdir": tmp,
                    "parserRoot": str(TEMPLATE_DIR),
                    "file": "src/App.jsx",
                    "operation": "update_text",
                    "anchorRange": range_,
                    "targetRange": range_,
                    "text": text,
                },
            )

    def test_replaces_leaf_text(self) -> None:
        code, payload, stderr = self._edit(
            "<main><h1>待办</h1></main>",
            _fix_range("<main><h1>待办</h1></main>", "<h1>", "</h1>"),
            "我的任务",
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(payload["status"], "OK")
        self.assertIn("<h1>我的任务</h1>", payload["content"])

    def test_preserves_surrounding_whitespace(self) -> None:
        source = "<main>\n  <h1>\n    待办\n  </h1>\n</main>"
        code, payload, stderr = self._edit(
            source,
            _fix_range(source, "<h1>", "</h1>"),
            "我的任务",
        )
        self.assertEqual(code, 0, stderr)
        content = payload["content"]
        self.assertIn("\n    我的任务\n", content)

    def test_rejects_mixed_children(self) -> None:
        source = "<main><button><Icon />保存</button></main>"
        code, payload, stderr = self._edit(
            source,
            _fix_range(source, "<button>", "</button>"),
            "提交",
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["code"], "TEXT_TARGET_NOT_LEAF")

    def test_rejects_expression_child(self) -> None:
        source = "<main><p>{title}</p></main>"
        code, payload, stderr = self._edit(
            source,
            _fix_range(source, "<p>", "</p>"),
            "固定文案",
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(payload["code"], "TEXT_TARGET_NOT_LEAF")

    def test_rejects_jsx_special_chars(self) -> None:
        source = "<main><p>欢迎</p></main>"
        code, payload, stderr = self._edit(
            source,
            _fix_range(source, "<p>", "</p>"),
            "<script>",
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(payload["code"], "INVALID_TEXT")


if __name__ == "__main__":
    unittest.main()
