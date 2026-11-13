"""P0 硬门禁测试：结构化删除 + 实例作用域 AST 描述。"""

from __future__ import annotations

from typing import Any

from ai_core.agent.graph import (
    _deleted_spans_patch,
    _instance_scope_specs,
    _unstructured_delete_violation,
)

APP_FILE = "src/App.jsx"
CARD_FILE = "src/components/Card.jsx"

APP_SOURCE = "\n".join(
    [
        "import Card from './components/Card.jsx'",
        "",
        "const CARDS = [",
        "  { id: 'card-1', title: '营收' },",
        "  { id: 'card-2', title: '用户' },",
        "  { id: 'card-3', title: '转化' },",
        "]",
        "",
        "export default function App() {",
        "  return (",
        "    <main>",
        "      <header><h1>看板</h1></header>",
        "      <section>",
        "        {CARDS.map((card) => (",
        "          <Card key={card.id} card={card} />",
        "        ))}",
        "      </section>",
        "    </main>",
        "  )",
        "}",
        "",
    ]
)

CARD_SOURCE = "\n".join(
    [
        "export default function Card({ card }) {",
        "  return (",
        "    <article>",
        "      <h2>{card.title}</h2>",
        "      <button>查看详情</button>",
        "    </article>",
        "  )",
        "}",
        "",
    ]
)


def card_source(instance_count: int = 3, call_site: bool = True) -> dict[str, Any]:
    source: dict[str, Any] = {
        "file": CARD_FILE,
        "line": 5,
        "column": 7,
        "component": "Card",
        "instance": 2,
        "instanceCount": instance_count,
        "instanceKey": "card-2",
        "instanceProps": {"card.id": "card-2"},
    }
    if call_site:
        source["callSite"] = {
            "file": APP_FILE,
            "line": 15,
            "component": "Card",
        }
    return source


def state_with(
    source: dict[str, Any] | None = None,
    *,
    delete_range: dict[str, Any] | None = None,
    action: str = "MODIFY_EXISTING",
) -> dict[str, Any]:
    annotation: dict[str, Any] = {
        "version": 1,
        "annotationId": "ann_gate",
        "type": "visual-draw",
        "action": action,
        "target": {"mode": "source", "source": source or card_source()},
        "intent": {"kind": "UI_ONLY", "ui": {"summary": "改一下"}},
        "payload": {},
    }
    if delete_range:
        annotation["payload"]["deleteRange"] = delete_range
    return {
        "annotation_context": {"annotations": [{"annotation": annotation}]},
        "current_files": {APP_FILE: APP_SOURCE, CARD_FILE: CARD_SOURCE},
        "target_files": [APP_FILE, CARD_FILE],
    }


def test_truncation_without_range_is_blocked() -> None:
    state = state_with()
    truncated = "export default function App() {\n  return <main />\n}\n"
    result = _unstructured_delete_violation(state, {APP_FILE: truncated}, None)
    assert result is not None
    assert result["error_code"] == "UNSTRUCTURED_DELETE_BLOCKED"
    assert "deleteRange" in result["verify_result"]["error_logs"]


def test_small_deletion_is_allowed() -> None:
    state = state_with()
    # 只删掉空行与一行注释，远低于阈值。
    lines = APP_SOURCE.splitlines()
    after = "\n".join(lines[:3] + lines[4:]) + "\n"
    result = _unstructured_delete_violation(state, {APP_FILE: after}, None)
    assert result is None


def test_structured_range_allows_deletion() -> None:
    state = state_with(delete_range={"file": APP_FILE, "startLine": 4, "endLine": 6})
    lines = APP_SOURCE.splitlines()
    after = "\n".join(lines[:3] + lines[6:]) + "\n"
    result = _unstructured_delete_violation(state, {APP_FILE: after}, None)
    assert result is None


def test_range_mismatch_is_blocked() -> None:
    state = state_with(delete_range={"file": APP_FILE, "startLine": 1, "endLine": 2})
    truncated = "export default function App() {\n  return <main />\n}\n"
    result = _unstructured_delete_violation(state, {APP_FILE: truncated}, None)
    assert result is not None
    assert result["error_code"] == "DELETE_RANGE_MISMATCH"


def test_export_removal_is_blocked_even_when_small() -> None:
    state = state_with()
    # 只去掉 export default，行数几乎不变，但组件对外契约被破坏。
    after = APP_SOURCE.replace("export default function App", "function App")
    result = _unstructured_delete_violation(state, {APP_FILE: after}, None)
    assert result is not None
    assert result["error_code"] == "UNSTRUCTURED_DELETE_BLOCKED"
    assert "App" in result["verify_result"]["error_logs"]


def test_diff_mode_deletion_uses_minus_lines_only() -> None:
    # 只加一行的 hunk 不应被误判为删除。
    insertion_patch = (
        "--- a/src/App.jsx\n"
        "+++ b/src/App.jsx\n"
        "@@ -10,3 +10,4 @@\n"
        "   return (\n"
        "     <main>\n"
        "+      <p>新增</p>\n"
        "     </main>\n"
    )
    spans = _deleted_spans_patch(insertion_patch)
    assert spans == {}

    deletion_patch = (
        "--- a/src/App.jsx\n"
        "+++ b/src/App.jsx\n"
        "@@ -10,6 +10,2 @@\n"
        "   return (\n"
        "-    <header><h1>看板</h1></header>\n"
        "-    <section>\n"
        "-      {CARDS.map((card) => (\n"
        "-        <Card key={card.id} card={card} />\n"
        "-      ))}\n"
        "-    </section>\n"
        "   )\n"
    )
    spans = _deleted_spans_patch(deletion_patch)
    assert spans[APP_FILE]["deleted"] == 6
    assert spans[APP_FILE]["spans"] == [(11, 16)]


def test_diff_mode_large_deletion_is_blocked() -> None:
    state = state_with()
    patch = (
        "--- a/src/App.jsx\n"
        "+++ b/src/App.jsx\n"
        "@@ -1,20 +1,2 @@\n"
        + "".join(f"-{line}\n" for line in APP_SOURCE.splitlines()[:20])
        + "+export default function App() { return null }\n"
    )
    result = _unstructured_delete_violation(state, {}, patch)
    assert result is not None
    assert result["error_code"] == "UNSTRUCTURED_DELETE_BLOCKED"


def test_specs_include_reused_component() -> None:
    specs = _instance_scope_specs(state_with(card_source()))
    assert len(specs) == 1
    spec = specs[0]
    assert spec["protectBase"] is True
    assert spec["requireGuard"] is True
    assert spec["callSiteFile"] == APP_FILE


def test_specs_skip_unique_target_without_callsite() -> None:
    unique = {
        "file": APP_FILE,
        "line": 12,
        "instance": 1,
        "instanceCount": 1,
    }
    assert _instance_scope_specs(state_with(unique)) == []


def test_specs_keep_same_file_multi_instance_guard() -> None:
    same_file = {
        "file": APP_FILE,
        "line": 15,
        "component": "App",
        "instance": 2,
        "instanceCount": 3,
        "instanceKey": "card-2",
        "instanceProps": {"card.id": "card-2"},
        "callSite": {"file": APP_FILE, "line": 15, "component": "App"},
    }
    specs = _instance_scope_specs(state_with(same_file))
    assert len(specs) == 1
    assert specs[0]["protectBase"] is False
    assert specs[0]["requireGuard"] is True


def test_specs_protect_base_when_callsite_missing() -> None:
    specs = _instance_scope_specs(state_with(card_source(call_site=False)))
    assert len(specs) == 1
    assert specs[0]["protectBase"] is True
    assert specs[0]["requireGuard"] is True
