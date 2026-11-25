"""需求 → 决策 → 契约 → 画布 → 代码 的证据链分析。

只读生成基线，不执行生成物代码，也不依赖行业方向。每个契约项都给出
evidence 条目，状态区分：

  - ``ok``      真实实现，且能找到代码/画布锚点；
  - ``mock``    有入口但只是 Mock / 占位响应；
  - ``weak``    只有弱证据（例如仅源码文本出现、方案选择未写入契约）；
  - ``missing`` 契约要求但找不到任何证据。

该模块被 ``scripts/evidence_chain.py`` 与测试复用，输出保持纯 JSON 可序列化。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

_ROUTE_RE = re.compile(
    r"\bapp\.(get|post|put|patch|delete)\(\s*"
    r"(?P<arg>['\"][^'\"]+['\"]|[A-Za-z_$][\w$]*)",
    re.IGNORECASE,
)
_CONST_RE = re.compile(
    r"^\s*const\s+([A-Za-z_$][\w$]*)\s*=\s*['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)
_MOCK_MARKERS = ("沙盒 Mock 响应", "沙盒Mock响应", "mock-", "route:")
_CALL_RE = re.compile(
    r"['\"]endpoint['\"]\s*:\s*['\"](?P<path>[^'\"]+)['\"]"
    r".{0,200}?['\"]method['\"]\s*:\s*['\"](?P<method>[A-Za-z]+)['\"]",
    re.DOTALL,
)
_PARAM_RE = re.compile(r"(?:\{[^/{}]+\}|:[A-Za-z_][A-Za-z0-9_]*)")
_SOURCE_SUFFIXES = {".js", ".jsx", ".mjs", ".ts", ".tsx", ".vue"}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_optional_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = load_json(path)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def normalize_path(path: str) -> str:
    """统一参数段（``{id}`` / ``:id``）并去掉尾部斜杠，用于跨阶段比对。"""
    text = str(path or "/").strip()
    text = _PARAM_RE.sub(":param", text).rstrip("/")
    return text or "/"


def path_aliases(path: str) -> set[str]:
    raw = str(path or "/").strip().rstrip("/") or "/"
    variants: set[str] = set()
    for normalized in {raw, normalize_path(path)}:
        variants.add(normalized)
        for prefix in ("/api/v1", "/api"):
            if normalized.startswith(prefix + "/"):
                variants.add(normalized[len(prefix) :])
        flattened = _PARAM_RE.sub("", normalized).rstrip("/")
        if flattened:
            variants.add(flattened)
    return {item for item in variants if len(item) >= 3}


def parse_frontend_calls(text: str) -> set[tuple[str, str]]:
    """从生成前端读取 ``endpoint + method`` 声明（若有）。"""
    return {
        (match.group("method").upper(), normalize_path(match.group("path")))
        for match in _CALL_RE.finditer(text)
    }


def iter_visual_nodes(node: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield node
    for child in node.get("children") or []:
        if isinstance(child, dict):
            yield from iter_visual_nodes(child)


def visual_pages(document: dict[str, Any]) -> list[dict[str, Any]]:
    pages = [item for item in document.get("pages") or [] if isinstance(item, dict)]
    if pages:
        return pages
    root = document.get("root")
    if isinstance(root, dict):
        return [
            {
                "pageId": root.get("id"),
                "path": "/",
                "title": root.get("name"),
                "root": root,
            }
        ]
    return []


def flatten_visual_nodes(document: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for page in visual_pages(document):
        root = page.get("root")
        if isinstance(root, dict):
            nodes.extend(iter_visual_nodes(root))
    return nodes


def read_source_tree(source_dir: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    if not source_dir.is_dir():
        return files
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file() or path.suffix not in _SOURCE_SUFFIXES:
            continue
        try:
            files[path.relative_to(source_dir).as_posix()] = path.read_text(
                encoding="utf-8"
            )
        except OSError:
            continue
    return files


def parse_server_routes(
    source_files: dict[str, str],
) -> dict[tuple[str, str], dict[str, Any]]:
    routes: dict[tuple[str, str], dict[str, Any]] = {}
    for name, text in source_files.items():
        if not name.startswith("server/"):
            continue
        constants = {key: value for key, value in _CONST_RE.findall(text)}
        matches = list(_ROUTE_RE.finditer(text))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            block = text[match.start() : end]
            method = match.group(1).upper()
            raw_arg = match.group("arg")
            raw_path = (
                raw_arg[1:-1] if raw_arg[:1] in {"'", '"'} else constants.get(raw_arg)
            )
            if not raw_path:
                continue
            path = normalize_path(raw_path)
            routes[(method, path)] = {
                "method": method,
                "path": path,
                "file": name,
                "line": text.count("\n", 0, match.start()) + 1,
                "mock": any(marker in block for marker in _MOCK_MARKERS),
            }
    return routes


def _item(
    kind: str,
    clause: str,
    status: str,
    evidence: str,
    *,
    severity: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "clause": clause,
        "status": status,
        "severity": severity or _severity_for(status),
        "evidence": evidence,
    }


def _severity_for(status: str) -> str:
    if status == "ok":
        return "info"
    if status in {"mock", "weak"}:
        return "warning"
    return "critical"


def _match_component(
    name: str,
    node_names: list[str],
    frontend_text: str,
) -> tuple[str, str]:
    exact = [item for item in node_names if item == name]
    if exact:
        return "ok", f"画布节点命中：{name}"
    loose = [
        item for item in node_names if item and name and (name in item or item in name)
    ]
    if loose:
        return "weak", f"近似画布节点：{loose[0]}"
    if name and name in frontend_text:
        return "weak", "仅源码文本出现，未找到同名画布节点"
    return "missing", "画布与源码均未找到"


def _frontend_references(path: str, frontend_text: str) -> bool:
    return any(alias in frontend_text for alias in path_aliases(path))


def infer_interaction_method(
    event: str,
    component: str,
    api_path: str,
    available: Iterable[str],
) -> str:
    """在路径同时有 GET/POST 时，按事件语义选择最可能的 HTTP 方法。

    上传 / 提交类交互如果只找到 GET 调用，必须判“未命中”，
    否则会把“上传 POST 没接前端”误报成真实闭环。
    """
    methods = [str(item).upper() for item in available]
    if not methods:
        return "GET"
    event_text = str(event or "").lower()
    target = f"{component} {api_path}".lower()
    if event_text in {"submit", "upload", "create", "save", "delete", "remove"}:
        preferred = ["POST", "PUT", "PATCH", "DELETE", "GET"]
    elif any(
        marker in target
        for marker in ("upload", "上传", "附件", "影像", "file", "image")
    ):
        preferred = ["POST", "PUT", "PATCH", "GET"]
    elif event_text == "click":
        preferred = ["GET", "POST", "PUT", "PATCH", "DELETE"]
    else:
        preferred = ["GET", "POST", "PUT", "PATCH", "DELETE"]
    for method in preferred:
        if method in methods:
            return method
    return methods[0]


def _agent_policy_item(contract: dict[str, Any]) -> dict[str, Any]:
    """契约是否冻结了 Agent 协作策略。"""
    policy = contract.get("agentPolicy") or {}
    edges = policy.get("fastPathEdges") or []
    if not edges:
        return _item(
            "policy",
            "Agent 协作策略",
            "weak",
            "契约未冻结快速通道白名单，生成阶段将使用能力包默认",
        )
    sample = "；".join(
        f"{edge[0]}→{edge[1]}({edge[2]})"
        for edge in edges[:3]
        if isinstance(edge, (list, tuple)) and len(edge) == 4
    )
    return _item(
        "policy",
        "Agent 协作策略",
        "ok",
        f"已冻结 {len(edges)} 条快速通道白名单" + (f"；{sample}" if sample else ""),
    )


def _decision_item(
    metadata: dict[str, Any],
    discovery: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    choice = metadata.get("solutionChoice") or {}
    if not isinstance(choice, dict) or not choice:
        return _item(
            "decision",
            "方案选择",
            "weak",
            "基线未记录 solutionChoice，无法证明用户选择进入契约",
        )
    option_id = str(choice.get("optionId") or "")
    options = [
        item for item in discovery.get("options") or [] if isinstance(item, dict)
    ]
    option = next(
        (item for item in options if str(item.get("optionId")) == option_id),
        None,
    )
    needles = [
        option_id,
        str(choice.get("title") or ""),
        str(choice.get("sourceUrl") or ""),
    ]
    haystack = "\n".join(
        [
            str(contract.get("contractMarkdown") or ""),
            json.dumps(contract.get("assumptions") or [], ensure_ascii=False),
            json.dumps(contract.get("citations") or [], ensure_ascii=False),
        ]
    )
    cited = [needle for needle in needles if needle and needle in haystack]
    label = str(choice.get("title") or option_id or "未命名方案")
    if option is None:
        return _item(
            "decision",
            f"方案选择：{label}",
            "weak",
            f"discovery 中找不到 optionId={option_id}",
        )
    if not cited:
        return _item(
            "decision",
            f"方案选择：{label}",
            "weak",
            "选择已记录，但契约正文 / assumptions / citations 未引用该方案",
        )
    return _item(
        "decision",
        f"方案选择：{label}",
        "ok",
        f"optionId={option_id}；契约引用：{cited[0]}",
    )


def analyse_baseline(baseline_dir: Path) -> dict[str, Any]:
    contract = load_optional_json(baseline_dir / "contract.json")
    v0_contract = load_optional_json(baseline_dir / "v0-contract.json")
    metadata = load_optional_json(baseline_dir / "metadata.json")
    discovery = load_optional_json(baseline_dir / "discovery.json")
    visual = load_optional_json(baseline_dir / "v0-visual-frozen.json")
    if not visual:
        visual = contract.get("visualDocument") or {}
    source_dir = baseline_dir / "source"
    source_files = read_source_tree(source_dir)
    frontend_text = "\n".join(
        text for name, text in source_files.items() if not name.startswith("server/")
    )
    server_routes = parse_server_routes(source_files)
    frontend_calls = parse_frontend_calls(frontend_text)
    source_map = load_optional_json(source_dir / "public" / "vibe-source-map.json")
    source_nodes = source_map.get("nodes") or {}
    nodes = flatten_visual_nodes(visual)
    node_names = [str(node.get("name") or "") for node in nodes]
    mapped_node_ids = {
        str(item.get("nodeId"))
        for item in source_nodes.values()
        if isinstance(item, dict) and item.get("nodeId")
    }

    items: list[dict[str, Any]] = []
    contract_routes = [
        normalize_path(str(item.get("path") or "/"))
        for item in contract.get("frontendRoutes") or []
        if isinstance(item, dict)
    ]
    visual_routes = [
        normalize_path(str(page.get("path") or "/")) for page in visual_pages(visual)
    ]

    # 页面：契约 → 画布
    for route in contract.get("frontendRoutes") or []:
        if not isinstance(item := route, dict):
            continue
        path = normalize_path(str(item.get("path") or "/"))
        page = next(
            (
                candidate
                for candidate in visual_pages(visual)
                if normalize_path(str(candidate.get("path") or "/")) == path
            ),
            None,
        )
        name = str(item.get("name") or path)
        if page is None:
            items.append(
                _item("page", f"{name}（{path}）", "missing", "画布无对应页面")
            )
            continue
        root = page.get("root") or {}
        root_id = str(root.get("id") or "")
        if root_id and root_id in mapped_node_ids:
            items.append(
                _item(
                    "page",
                    f"{name}（{path}）",
                    "ok",
                    f"pageId={page.get('pageId')}；源码锚点命中 {root_id}",
                )
            )
        else:
            items.append(
                _item(
                    "page",
                    f"{name}（{path}）",
                    "weak",
                    f"pageId={page.get('pageId')}；无源码映射",
                )
            )

    # 画布页面 vs 最终契约
    for path in visual_routes:
        if path not in contract_routes:
            items.append(
                _item(
                    "stage-drift",
                    f"画布页面 {path}",
                    "missing",
                    "冻结画布存在该页面，但最终契约 frontendRoutes 未收录",
                )
            )
    for path in contract_routes:
        if path not in visual_routes:
            items.append(
                _item(
                    "stage-drift",
                    f"契约路由 {path}",
                    "missing",
                    "契约声明了该路由，但冻结画布中不存在",
                )
            )

    # V0 → 最终契约的路由 / 组件漂移
    v0_routes = [
        normalize_path(str(item.get("path") or "/"))
        for item in v0_contract.get("frontendRoutes") or []
        if isinstance(item, dict)
    ]
    lost_routes = [path for path in v0_routes if path not in contract_routes]
    for path in lost_routes:
        items.append(
            _item(
                "stage-drift",
                f"V0 路由 {path}",
                "missing",
                "V0 契约存在，最终契约丢失",
            )
        )
    v0_components = [
        str(item.get("name") or "").strip()
        for item in v0_contract.get("components") or []
        if isinstance(item, dict)
    ]
    contract_components = [
        item for item in contract.get("components") or [] if isinstance(item, dict)
    ]
    final_component_names = {
        str(item.get("name") or "").strip() for item in contract_components
    }
    lost_components = [
        name for name in v0_components if name and name not in final_component_names
    ]
    for name in lost_components:
        items.append(
            _item(
                "stage-drift",
                f"V0 组件 {name}",
                "missing",
                "V0 契约存在，最终契约丢失",
            )
        )

    # 组件：契约 → 画布 / 源码
    for component in contract_components:
        name = str(component.get("name") or "").strip()
        if not name:
            continue
        status, evidence = _match_component(name, node_names, frontend_text)
        items.append(_item("component", name, status, evidence))

    # API：契约 → server 路由
    route_status: dict[tuple[str, str], str] = {}
    for route in contract.get("apiRoutes") or []:
        if not isinstance(route, dict):
            continue
        method = str(route.get("method") or "GET").upper()
        path = normalize_path(str(route.get("path") or "/"))
        handler = server_routes.get((method, path))
        label = f"{method} {path}"
        if handler is None:
            route_status[(method, path)] = "missing"
            items.append(_item("api", label, "missing", "server 中未找到路由处理器"))
        elif handler.get("mock"):
            route_status[(method, path)] = "mock"
            items.append(
                _item(
                    "api",
                    label,
                    "mock",
                    f"{handler['file']}:{handler['line']}（Mock 响应）",
                )
            )
        else:
            route_status[(method, path)] = "ok"
            items.append(
                _item(
                    "api",
                    label,
                    "ok",
                    f"{handler['file']}:{handler['line']}",
                )
            )

    # 交互：组件 + API + 前端调用三方对齐
    for interaction in contract.get("componentInteractions") or []:
        if not isinstance(interaction, dict):
            continue
        component = str(interaction.get("component") or "").strip()
        api_path = str(interaction.get("apiRoute") or "").strip()
        event = str(interaction.get("event") or "").strip()
        label = f"{component} / {event or '触发'}"
        component_status, _ = _match_component(component, node_names, frontend_text)
        if not api_path:
            status = "ok" if component_status in {"ok", "weak"} else "missing"
            items.append(
                _item(
                    "interaction",
                    label,
                    status,
                    "纯前端逻辑，无 API 绑定" if status == "ok" else "组件无证据",
                )
            )
            continue
        available_methods = [
            str(route.get("method") or "GET").upper()
            for route in contract.get("apiRoutes") or []
            if isinstance(route, dict)
            and normalize_path(str(route.get("path") or "/"))
            == normalize_path(api_path)
        ]
        method = infer_interaction_method(event, component, api_path, available_methods)
        handler_status = route_status.get((method, normalize_path(api_path)), "missing")
        if frontend_calls:
            referenced = (
                method,
                normalize_path(api_path),
            ) in frontend_calls
        else:
            referenced = _frontend_references(api_path, frontend_text)
        if handler_status == "missing":
            status = "missing"
        elif handler_status == "mock":
            status = "mock"
        elif component_status == "missing":
            status = "missing"
        elif not referenced:
            status = "weak"
        else:
            status = "ok"
        evidence = (
            f"组件={component_status}；API={handler_status}；"
            f"前端调用={'命中' if referenced else '未命中'}"
        )
        items.append(_item("interaction", f"{label} → {api_path}", status, evidence))

    items.append(_decision_item(metadata, discovery, contract))
    items.append(_agent_policy_item(contract))

    # 汇总
    def count(kind: str) -> dict[str, int]:
        scoped = [item for item in items if item["kind"] == kind]
        result = {"total": len(scoped)}
        for status in ("ok", "mock", "weak", "missing"):
            result[status] = sum(1 for item in scoped if item["status"] == status)
        return result

    critical = sum(1 for item in items if item["severity"] == "critical")
    warnings = sum(1 for item in items if item["severity"] == "warning")
    total_nodes = len(nodes)
    mapped_count = sum(
        1 for node in nodes if str(node.get("id") or "") in mapped_node_ids
    )
    report: dict[str, Any] = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "baseline": str(baseline_dir),
        "project": str(
            metadata.get("projectName")
            or contract.get("projectName")
            or baseline_dir.name
        ),
        "summary": {
            "critical": critical,
            "warnings": warnings,
            "verdict": "PASS" if critical == 0 else "GAPS",
        },
        "metrics": {
            "pages": count("page"),
            "components": count("component"),
            "apis": count("api"),
            "interactions": count("interaction"),
            "stageDrift": {
                "lostRoutes": lost_routes,
                "lostComponents": lost_components,
                "visualNotContract": [
                    path for path in visual_routes if path not in contract_routes
                ],
                "contractNotVisual": [
                    path for path in contract_routes if path not in visual_routes
                ],
            },
            "decision": next(
                (item for item in items if item["kind"] == "decision"), None
            ),
            "sourceTrace": {
                "totalNodes": total_nodes,
                "mappedNodes": mapped_count,
                "coverage": (
                    round(mapped_count / total_nodes, 4) if total_nodes else 0.0
                ),
            },
        },
        "items": items,
    }
    return report


def render_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    lines = [
        f"# 证据链报告 · {report['project']}",
        "",
        f"- 基线：`{report['baseline']}`",
        f"- 生成时间：{report['generatedAt']}",
        f"- 结论：**{report['summary']['verdict']}**"
        f"（critical {report['summary']['critical']} / "
        f"warning {report['summary']['warnings']}）",
        "",
        "## 覆盖矩阵",
        "",
        "| 维度 | 总数 | 真实 | Mock | 弱证据 | 缺失 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    labels = {
        "pages": "页面",
        "components": "组件",
        "apis": "API",
        "interactions": "交互",
    }
    for key, label in labels.items():
        row = metrics[key]
        lines.append(
            f"| {label} | {row['total']} | {row['ok']} | {row['mock']} | "
            f"{row['weak']} | {row['missing']} |"
        )
    trace = metrics["sourceTrace"]
    lines.extend(
        [
            "",
            f"- 画布节点源码映射：{trace['mappedNodes']}/{trace['totalNodes']}"
            f"（{trace['coverage'] * 100:.1f}%）",
        ]
    )

    drift = metrics["stageDrift"]
    lines.extend(["", "## 阶段漂移（V0 → 最终契约）", ""])
    if drift["lostRoutes"]:
        lines.append(
            "- 丢失路由：" + "、".join(f"`{item}`" for item in drift["lostRoutes"])
        )
    if drift["lostComponents"]:
        lines.append(
            "- 丢失组件：" + "、".join(f"`{item}`" for item in drift["lostComponents"])
        )
    if drift["visualNotContract"]:
        lines.append(
            "- 画布有但契约无："
            + "、".join(f"`{item}`" for item in drift["visualNotContract"])
        )
    if drift["contractNotVisual"]:
        lines.append(
            "- 契约有但画布无："
            + "、".join(f"`{item}`" for item in drift["contractNotVisual"])
        )
    if not any(
        drift[key]
        for key in (
            "lostRoutes",
            "lostComponents",
            "visualNotContract",
            "contractNotVisual",
        )
    ):
        lines.append("- 无结构性漂移")

    decision = metrics.get("decision")
    if decision:
        lines.extend(
            [
                "",
                "## 方案决策",
                "",
                f"- {decision['clause']}：**{decision['status']}**",
                f"- 证据：{decision['evidence']}",
            ]
        )

    gaps = [
        item for item in report["items"] if item["severity"] in {"critical", "warning"}
    ]
    lines.extend(["", "## 缺口（按严重度）", ""])
    if gaps:
        lines.extend(
            [
                "| 严重度 | 类型 | 契约项 | 状态 | 证据 |",
                "|---|---|---|---|---|",
            ]
        )
        for item in gaps:
            evidence = str(item["evidence"]).replace("|", "\\|")
            lines.append(
                f"| {item['severity']} | {item['kind']} | {item['clause']} | "
                f"{item['status']} | {evidence} |"
            )
    else:
        lines.append("- 无")

    lines.extend(
        [
            "",
            "## 复现",
            "",
            "```bash",
            "python3 scripts/evidence_chain.py \\",
            f"  --baseline {report['baseline']}",
            "```",
            "",
        ]
    )
    return "\n".join(lines)
