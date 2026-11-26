"""把 Architect 的页面布局 / 组件清单确定性编译为 VisualDocument。

模型可以手写 visualDocument，但旧契约、Mock 契约或模型漏字段时，
这里提供无 JSX 的安全兜底：只用 VisualNode 表达层级、文案与布局占位。
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

from ai_core.domain.architect import (
    ArchitectApiRoute,
    ArchitectComponentInteraction,
)
from ai_core.domain.visual_document import (
    VisualA11y,
    VisualCodegen,
    VisualDesignTokens,
    VisualDocument,
    VisualInteraction,
    VisualBinding,
    VisualLayout,
    VisualMetadata,
    VisualNode,
    VisualPage,
    VisualProvenance,
    VisualSemantics,
    VisualStyle,
)


def _stable_id(prefix: str, seed: str) -> str:
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _style(*tailwind: str, class_name: str = "") -> VisualStyle:
    return VisualStyle(
        token_refs=[],
        class_name=class_name,
        tailwind=list(tailwind),
    )


def _text(
    name: str,
    value: str,
    seed: str,
    *,
    tailwind: Iterable[str] = ("text-sm", "text-slate-700"),
    role: str = "content",
) -> VisualNode:
    return VisualNode(
        id=_stable_id("nd", seed),
        type="text",
        name=name,
        props={"text": value},
        layout=VisualLayout(display="block"),
        style=_style(*tailwind),
        semantics=VisualSemantics(role=role, purpose=value),
        provenance=VisualProvenance(created_by="architect"),
    )


def _button(name: str, value: str, seed: str) -> VisualNode:
    return VisualNode(
        id=_stable_id("nd", seed),
        type="button",
        name=name,
        component_ref="Button",
        props={"text": value, "variant": "primary"},
        layout=VisualLayout(display="block"),
        style=_style("rounded-lg", "bg-blue-600", "text-white"),
        semantics=VisualSemantics(role="action", purpose=value),
        codegen=VisualCodegen(mode="primitive", component_name=name),
        provenance=VisualProvenance(created_by="architect"),
    )


def _input(name: str, placeholder: str, seed: str) -> VisualNode:
    return VisualNode(
        id=_stable_id("nd", seed),
        type="input",
        name=name,
        component_ref="Input",
        props={"placeholder": placeholder},
        layout=VisualLayout(display="block"),
        style=_style("rounded-lg", "border", "border-slate-300"),
        a11y=VisualA11y(label=placeholder),
        codegen=VisualCodegen(mode="primitive", component_name=name),
        provenance=VisualProvenance(created_by="architect"),
    )


def _container(
    *,
    name: str,
    seed: str,
    children: list[VisualNode],
    display: str = "flex",
    direction: str = "column",
    gap: str = "12px",
    padding: str = "20px",
    columns: int = 1,
    tailwind: Iterable[str] = ("rounded-xl", "border", "bg-white"),
    role: str = "",
    purpose: str = "",
) -> VisualNode:
    return VisualNode(
        id=_stable_id("nd", seed),
        type="section",
        name=name,
        layout=VisualLayout(
            display=display,  # type: ignore[arg-type]
            direction=direction,  # type: ignore[arg-type]
            gap=gap,
            padding=padding,
            columns=columns,
        ),
        style=_style(*tailwind),
        children=children,
        semantics=VisualSemantics(role=role, purpose=purpose),
        provenance=VisualProvenance(created_by="architect"),
    )


_REGION_ALIASES = {
    "header": "header",
    "top": "header",
    "navbar": "header",
    "nav": "header",
    "topbar": "header",
    "顶部": "header",
    "顶部导航": "header",
    "页头": "header",
    "头部": "header",
    "sidebar": "sidebar",
    "aside": "sidebar",
    "侧边栏": "sidebar",
    "侧栏": "sidebar",
    "左侧导航": "sidebar",
    "导航栏": "sidebar",
    "main": "main",
    "content": "main",
    "body": "main",
    "主内容": "main",
    "主内容区": "main",
    "内容区": "main",
    "内容": "main",
    "工作区": "main",
    "核心工作区": "main",
    "业务工作区": "main",
    "业务区": "main",
    "footer": "footer",
    "页脚": "footer",
    "底部": "footer",
}


def _component_region(component: dict[str, Any]) -> str:
    """组件归属区域；兼容 region / section 两种写法与常见中文区域名。"""
    raw = (
        component.get("region")
        or component.get("section")
        or component.get("area")
        or ""
    )
    value = str(raw).strip().lower()
    if not value:
        return "main"
    return _REGION_ALIASES.get(value, value)


def _component_page(component: dict[str, Any]) -> str:
    """组件归属页面路径；未声明时由调用方决定（默认首页）。"""
    raw = (
        component.get("page")
        or component.get("pagePath")
        or component.get("route")
        or ""
    )
    return str(raw).strip()


def _normalize_path(path: str, fallback: str) -> str:
    value = str(path or "").strip() or fallback
    if not value.startswith("/"):
        value = "/" + value
    return value


def _node_role(node: VisualNode) -> str:
    return str(node.semantics.role or "").strip().lower()


def _organize_main(nodes: list[VisualNode], seed: str) -> list[VisualNode]:
    """把主区域组件排成“真实页面”的横向区块，而不是单列堆叠。

    规则（确定性，不改变节点身份）：
      - 指标类：一行 1-3 列；
      - 数据类（列表 / 表格 / 图表）：与表单并排，最多 2 列；
      - 操作 / 卡片 / AI：2-3 列网格。
    """
    if len(nodes) <= 1:
        return list(nodes)

    metrics_roles = {"summary", "metric", "kpi", "stats", "dashboard"}
    data_roles = {"list", "table", "chart", "kanban", "calendar", "timeline"}
    work_roles = data_roles | {"form"}
    bands: list[VisualNode] = []

    def push_band(
        items: list[VisualNode],
        name: str,
        *,
        max_columns: int = 3,
    ) -> None:
        if not items:
            return
        if len(items) == 1:
            bands.append(items[0])
            return
        bands.append(
            _container(
                name=name,
                seed=f"{seed}:band:{len(bands)}:{name}",
                children=items,
                display="grid",
                columns=min(max_columns, len(items)),
                gap="16px",
                padding="0px",
                tailwind=(),
                role="band",
                purpose=name,
            )
        )

    # 按组件原始顺序分组成行，只改变“并排 / 换行”，不重排组件，
    # 保证契约组件顺序与画布顺序一致。
    index = 0
    while index < len(nodes):
        node = nodes[index]
        role = _node_role(node)
        if role in metrics_roles:
            end = index + 1
            while (
                end < len(nodes)
                and _node_role(nodes[end]) in metrics_roles
                and end - index < 3
            ):
                end += 1
            push_band(nodes[index:end], "指标区", max_columns=3)
        elif role in work_roles:
            end = index + 1
            while (
                end < len(nodes)
                and _node_role(nodes[end]) in work_roles
                and end - index < 2
            ):
                end += 1
            push_band(nodes[index:end], "工作区", max_columns=2)
        else:
            end = index + 1
            while (
                end < len(nodes)
                and _node_role(nodes[end]) not in metrics_roles | work_roles
                and end - index < 3
            ):
                end += 1
            push_band(nodes[index:end], "内容区", max_columns=3)
        index = end
    return bands


def _route_specs(
    default_title: str,
    frontend_routes: list[dict[str, Any]] | None,
) -> list[tuple[str, str, str]]:
    """(path, title, description) 列表；首页始终存在，最多 6 页。"""
    specs: list[tuple[str, str, str]] = [("/", default_title, "")]
    seen = {"/"}
    for index, route in enumerate(frontend_routes or [], start=1):
        if not isinstance(route, dict):
            continue
        path = _normalize_path(route.get("path") or "", f"/page-{index}")
        description = str(route.get("description") or "").strip()
        if path in seen:
            if path == "/" and not specs[0][2] and description:
                specs[0] = ("/", specs[0][1], description)
            continue
        seen.add(path)
        if len(specs) >= 6:
            break
        title = (
            str(route.get("name") or "").strip() or description or f"页面 {index + 1}"
        )
        specs.append((path, title, description))
    return specs


def _sample_rows(component: dict[str, Any]) -> list[Any]:
    rows = (
        component.get("sample_data")
        or component.get("sampleData")
        or component.get("samples")
        or component.get("items")
    )
    if isinstance(rows, list):
        return [row for row in rows if row is not None]
    if isinstance(rows, dict):
        return [rows]
    return []


def _row_cells(row: Any) -> list[str]:
    if isinstance(row, dict):
        return [str(value) for value in row.values()]
    if isinstance(row, (list, tuple)):
        return [str(value) for value in row]
    return [str(row)]


def _sample_field_values(component: dict[str, Any]) -> list[str]:
    """从 sample_data 推导表格列名 / 表单字段名。"""
    rows = _sample_rows(component)
    if not rows:
        return []
    first = rows[0]
    if isinstance(first, dict):
        return [str(key) for key in first.keys()]
    return []


def _row_node(seed: str, cells: list[str], name: str = "数据行") -> VisualNode:
    return VisualNode(
        id=_stable_id("nd", seed),
        type="container",
        name=name,
        layout=VisualLayout(
            display="flex",
            direction="row",
            gap="16px",
            padding="12px 16px",
            align="center",
        ),
        style=_style("rounded-lg", "border", "border-slate-200", "bg-slate-50"),
        children=[
            _text(
                f"字段 {position + 1}",
                value,
                f"{seed}:cell:{position}",
                tailwind=("text-sm", "text-slate-700", "flex-1"),
            )
            for position, value in enumerate(cells)
        ],
        semantics=VisualSemantics(role="row", purpose="示例数据行"),
        provenance=VisualProvenance(created_by="architect"),
    )


def _component_node(
    component: dict[str, Any],
    *,
    index: int,
    parent_seed: str,
) -> VisualNode | None:
    name = str(component.get("name") or component.get("title") or "").strip()
    if not name:
        return None
    role = str(component.get("role") or "").strip().lower()
    description = str(component.get("description") or "").strip()
    seed = f"{parent_seed}:component:{index}:{name}"
    is_placeholder = bool(
        component.get("isPlaceholder") or component.get("is_placeholder")
    )
    semantics = VisualSemantics(
        role=role or "component",
        purpose=description or name,
        importance="core" if role in {"action", "form", "ai"} else "normal",
    )
    provenance = VisualProvenance(created_by="architect")

    if is_placeholder or role == "placeholder":
        return VisualNode(
            id=_stable_id("nd", seed),
            type="container",
            name="待定区域",
            layout=VisualLayout(
                display="flex",
                direction="column",
                gap="8px",
                padding="20px",
            ),
            style=_style(
                "rounded-xl",
                "border",
                "border-dashed",
                "border-slate-300",
                "bg-slate-50",
            ),
            children=[
                _text("占位说明", "需求待补充", f"{seed}:text"),
            ],
            semantics=VisualSemantics(
                role="placeholder",
                purpose=description or "需求未明确，等待补充",
            ),
            provenance=provenance,
        )

    if role == "action":
        return VisualNode(
            id=_stable_id("nd", seed),
            type="button",
            name=name,
            component_ref="Button",
            props={"text": name, "variant": "primary"},
            layout=VisualLayout(display="block"),
            style=_style("rounded-lg", "bg-blue-600", "text-white"),
            semantics=semantics,
            codegen=VisualCodegen(mode="primitive", component_name=name),
            provenance=provenance,
        )

    if role == "list":
        rows = _sample_rows(component)
        if not rows:
            rows = [f"示例记录 {position}" for position in range(1, 4)]
        return VisualNode(
            id=_stable_id("nd", seed),
            type="list",
            name=name,
            layout=VisualLayout(
                display="flex",
                direction="column",
                gap="8px",
                padding="16px",
            ),
            style=_style("rounded-xl", "border", "bg-white"),
            children=[
                _text(
                    "列表标题",
                    name,
                    f"{seed}:title",
                    tailwind=("font-semibold", "text-slate-900"),
                ),
                *[
                    _row_node(
                        f"{seed}:row:{position}",
                        _row_cells(row),
                        name=f"列表项 {position}",
                    )
                    for position, row in enumerate(rows[:6], start=1)
                ],
            ],
            semantics=semantics,
            provenance=provenance,
        )

    if role == "table":
        rows = _sample_rows(component)
        columns = _sample_field_values(component) or ["名称", "状态", "更新时间"]
        if not rows:
            rows = [
                {"名称": f"示例记录 {position}", "状态": "待处理", "更新时间": "—"}
                for position in range(1, 4)
            ]
        header = _row_node(
            f"{seed}:head",
            columns,
            name="表头",
        )
        return VisualNode(
            id=_stable_id("nd", seed),
            type="table",
            name=name,
            layout=VisualLayout(
                display="flex",
                direction="column",
                gap="8px",
                padding="16px",
            ),
            style=_style("rounded-xl", "border", "bg-white"),
            children=[
                _text(
                    "表格标题",
                    name,
                    f"{seed}:title",
                    tailwind=("font-semibold", "text-slate-900"),
                ),
                header,
                *[
                    _row_node(
                        f"{seed}:row:{position}",
                        _row_cells(row),
                        name=f"表格行 {position}",
                    )
                    for position, row in enumerate(rows[:8], start=1)
                ],
            ],
            semantics=semantics,
            provenance=provenance,
        )

    if role == "form":
        fields = component.get("fields") or _sample_field_values(component)
        labels: list[str] = []
        for item in fields or []:
            if isinstance(item, dict):
                label = str(
                    item.get("label") or item.get("name") or item.get("field") or ""
                ).strip()
            else:
                label = str(item).strip()
            if label and label not in labels:
                labels.append(label)
        if not labels:
            labels = ["名称", "状态", "备注"]
        return VisualNode(
            id=_stable_id("nd", seed),
            type="form",
            name=name,
            layout=VisualLayout(
                display="flex",
                direction="column",
                gap="12px",
                padding="20px",
            ),
            style=_style("rounded-xl", "border", "bg-white"),
            children=[
                _text(
                    "表单标题",
                    name,
                    f"{seed}:title",
                    tailwind=("font-semibold", "text-slate-900"),
                ),
                *[
                    _input(
                        f"{label}输入框",
                        f"请输入{label}",
                        f"{seed}:input:{position}",
                    )
                    for position, label in enumerate(labels[:8], start=1)
                ],
                _button("提交", f"提交{name}", f"{seed}:submit"),
            ],
            semantics=semantics,
            provenance=provenance,
        )

    if role in {"summary", "metric", "kpi", "stats", "dashboard"}:
        rows = _sample_rows(component)
        metrics: list[tuple[str, str]] = []
        for item in rows[:4]:
            if isinstance(item, dict):
                label = str(
                    item.get("label") or item.get("name") or item.get("title") or ""
                ).strip()
                value = item.get("value", item.get("count", "—"))
                metrics.append((label or name, str(value)))
            else:
                metrics.append((name, str(item)))
        if not metrics:
            metrics = [
                ("核心指标", "—"),
                ("本月新增", "—"),
                ("完成率", "—"),
            ]
        return VisualNode(
            id=_stable_id("nd", seed),
            type="grid",
            name=name,
            layout=VisualLayout(
                display="grid",
                columns=min(3, max(1, len(metrics))),
                gap="12px",
                padding="16px",
            ),
            style=_style("rounded-xl", "border", "bg-white"),
            children=[
                _container(
                    name=label,
                    seed=f"{seed}:metric:{position}",
                    children=[
                        _text(
                            "指标数值",
                            value,
                            f"{seed}:metric:{position}:value",
                            tailwind=("text-2xl", "font-semibold", "text-slate-900"),
                        ),
                        _text(
                            "指标名称",
                            label,
                            f"{seed}:metric:{position}:label",
                            tailwind=("text-xs", "text-slate-500"),
                        ),
                    ],
                    gap="4px",
                    padding="16px",
                    tailwind=("rounded-lg", "bg-slate-50"),
                    role="metric",
                    purpose=description or name,
                )
                for position, (label, value) in enumerate(metrics, start=1)
            ],
            semantics=semantics,
            provenance=provenance,
        )

    if role == "ai":
        return _container(
            name=name,
            seed=seed,
            children=[
                _text("AI 标题", "AI 工作区", f"{seed}:title"),
                _text("AI 说明", description or "等待业务澄清", f"{seed}:desc"),
                _button("开始", "开始", f"{seed}:action"),
            ],
            tailwind=("rounded-xl", "border", "bg-slate-900", "text-white"),
            role="ai",
            purpose=description,
        )

    sample_lines = []
    for position, row in enumerate(_sample_rows(component)[:5], start=1):
        if isinstance(row, dict):
            line = " · ".join(f"{key}: {value}" for key, value in row.items())
        else:
            line = str(row)
        sample_lines.append(_text("示例数据", line, f"{seed}:sample:{position}"))

    return VisualNode(
        id=_stable_id("nd", seed),
        type="component",
        name=name,
        layout=VisualLayout(
            display="flex",
            direction="column",
            gap="8px",
            padding="16px",
        ),
        style=_style("rounded-xl", "border", "bg-white"),
        children=[
            _text(
                "组件标题",
                name,
                f"{seed}:title",
                tailwind=("font-semibold", "text-slate-900"),
            ),
            *([_text("组件说明", description, f"{seed}:desc")] if description else []),
            *sample_lines,
        ],
        semantics=semantics,
        provenance=provenance,
    )


def _route_path_for_interaction(
    api_route: str,
    api_routes: list[ArchitectApiRoute],
) -> str:
    if not api_route:
        return ""
    for route in api_routes:
        if route.path == api_route:
            return route.method
    return ""


def build_visual_document_from_contract(
    *,
    title: str,
    page_layout: list[dict[str, Any]],
    components: list[dict[str, Any]],
    project_id: str = "",
    interactions: list[ArchitectComponentInteraction] | None = None,
    api_routes: list[ArchitectApiRoute] | None = None,
    frontend_routes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """确定性生成无 JSX 的 VisualDocument。"""
    safe_title = str(title or "未命名页面").strip() or "未命名页面"
    page_seed = f"page:{project_id}:{safe_title}"
    page = VisualNode(
        id=_stable_id("nd", page_seed),
        type="page",
        name=safe_title,
        props={"title": safe_title},
        layout=VisualLayout(
            display="block",
            direction="column",
            padding="32px",
            max_width="1180px",
        ),
        style=_style("bg-white", "min-h-screen"),
        semantics=VisualSemantics(role="page", purpose=safe_title),
        provenance=VisualProvenance(created_by="architect"),
    )

    # ---- 组件分组：按页面路径 + 区域 ---------------------------------
    # 模型可能只写扁平 components；这里把 page / region（或 section）
    # 映射成真实页面与区块，避免所有组件无差别堆在首页主区域。
    normalized: list[tuple[int, dict[str, Any], str, str]] = []
    for index, component in enumerate(components or [], start=1):
        if not isinstance(component, dict):
            continue
        normalized.append(
            (
                index,
                component,
                _component_page(component),
                _component_region(component),
            )
        )
    explicit_pages = any(page_path for _, _, page_path, _ in normalized)

    regions = [
        str(item.get("region") or "").strip().lower()
        for item in page_layout or []
        if isinstance(item, dict)
    ]
    region_titles = {
        str(item.get("region") or "")
        .strip()
        .lower(): str(item.get("title") or item.get("region") or "")
        .strip()
        for item in page_layout or []
        if isinstance(item, dict)
    }
    header_title = region_titles.get("header", "")
    has_sidebar = "sidebar" in regions or any(
        region == "sidebar" for _, _, _, region in normalized
    )
    route_specs = _route_specs(safe_title, frontend_routes)
    nav_titles = [spec[1] for spec in route_specs]

    def components_for(path: str) -> list[tuple[int, dict[str, Any], str]]:
        picked: list[tuple[int, dict[str, Any], str]] = []
        for index, component, page_path, region in normalized:
            target = _normalize_path(page_path, "/") if page_path else "/"
            if explicit_pages and target != path:
                continue
            if not explicit_pages and path != "/":
                continue
            picked.append((index, component, region))
        return picked

    nodes_by_name: dict[str, VisualNode] = {}
    page_nodes: list[VisualNode] = []

    def build_page(
        path: str,
        route_title: str,
        *,
        is_root: bool,
    ) -> VisualNode:
        seed = page_seed if is_root else f"{project_id}:{page_seed}:route:{path}"
        by_region: dict[str, list[VisualNode]] = {}
        for index, component, region in components_for(path):
            node = _component_node(
                component,
                index=index,
                parent_seed=seed,
            )
            if node is None:
                continue
            by_region.setdefault(region, []).append(node)
            nodes_by_name[str(component.get("name") or "").strip()] = node

        header_children = [
            _text(
                "品牌",
                safe_title,
                f"{seed}:brand",
                tailwind=("text-base", "font-semibold", "text-slate-900"),
            ),
            _text(
                "页面标题",
                route_title,
                f"{seed}:subtitle",
                tailwind=("text-sm", "text-slate-500"),
            ),
        ]
        if len(nav_titles) > 1:
            header_children.append(
                _text(
                    "主导航",
                    " · ".join(nav_titles),
                    f"{seed}:nav",
                    tailwind=("text-sm", "text-slate-600"),
                )
            )
        header_children.extend(by_region.pop("header", []))
        children: list[VisualNode] = [
            _container(
                name=header_title or "顶部导航",
                seed=f"{seed}:header",
                children=header_children,
                display="flex",
                direction="row",
                gap="16px",
                padding="20px",
                tailwind=("rounded-xl", "border", "bg-white"),
                role="header",
                purpose="品牌与页面标题",
            )
        ]

        main_nodes = by_region.pop("main", [])
        main_children = _organize_main(main_nodes, f"{seed}:main:bands") or [
            _container(
                name="待定区域",
                seed=f"{seed}:main:empty",
                children=[_text("占位说明", "等待业务澄清", f"{seed}:empty")],
                tailwind=("rounded-xl", "border", "border-dashed", "bg-slate-50"),
                role="placeholder",
                purpose="需求未明确",
            )
        ]
        main = _container(
            name="主内容区",
            seed=f"{seed}:main",
            children=main_children,
            gap="20px",
            padding="0px",
            tailwind=(),
            role="main",
            purpose="核心业务区域",
        )

        if has_sidebar:
            sidebar_children = [
                _text(
                    "导航标题",
                    "导航",
                    f"{seed}:nav-title",
                    tailwind=("font-semibold", "text-slate-900"),
                ),
                *[
                    _text("导航项", title, f"{seed}:nav-item:{position}")
                    for position, title in enumerate(nav_titles, start=1)
                ],
            ]
            sidebar_children.extend(by_region.pop("sidebar", []))
            sidebar = _container(
                name=region_titles.get("sidebar") or "侧边栏",
                seed=f"{seed}:sidebar",
                children=sidebar_children,
                tailwind=("rounded-xl", "border", "bg-slate-50"),
                role="navigation",
                purpose="业务模块导航",
            )
            children.append(
                VisualNode(
                    id=_stable_id("nd", f"{seed}:body"),
                    type="grid",
                    name="主体布局",
                    layout=VisualLayout(
                        display="grid",
                        columns=2,
                        gap="24px",
                        padding="24px 0px",
                    ),
                    style=_style(),
                    children=[sidebar, main],
                    semantics=VisualSemantics(role="layout", purpose="侧栏 + 主内容"),
                    provenance=VisualProvenance(created_by="architect"),
                )
            )
        else:
            children.append(main)

        # footer / 自定义区域：保持契约顺序，并把归属组件放进对应区域。
        custom_order = [
            str(item.get("region") or "").strip().lower()
            for item in page_layout or []
            if isinstance(item, dict)
            and str(item.get("region") or "").strip().lower()
            not in {"header", "sidebar", "main"}
        ]
        ordered_regions = custom_order + [
            region for region in by_region if region not in custom_order
        ]
        for region in ordered_regions:
            remaining = by_region.pop(region, None)
            if not remaining:
                continue
            region_title = region_titles.get(region) or region or "区域"
            children.append(
                _container(
                    name=region_title,
                    seed=f"{seed}:region:{region}",
                    children=remaining,
                    tailwind=("rounded-xl", "border", "bg-white"),
                    role=region or "region",
                    purpose=region_title,
                )
            )

        return VisualNode(
            id=_stable_id("nd", seed) if is_root else _stable_id("nd", f"{seed}:root"),
            type="page",
            name=route_title,
            props=(
                {"title": route_title}
                if is_root
                else {"title": route_title, "path": path}
            ),
            layout=VisualLayout(
                display="block",
                direction="column",
                padding="32px",
                max_width="1180px",
            ),
            style=_style("bg-white", "min-h-screen"),
            children=children,
            semantics=VisualSemantics(role="page", purpose=route_title),
            provenance=VisualProvenance(created_by="architect"),
        )

    for spec_index, (path, route_title, _route_desc) in enumerate(route_specs):
        page_nodes.append(build_page(path, route_title, is_root=spec_index == 0))
    page = page_nodes[0]

    visual_interactions: list[VisualInteraction] = []
    visual_bindings: list[VisualBinding] = []
    route_list = list(api_routes or [])
    for index, item in enumerate(interactions or [], start=1):
        node = nodes_by_name.get(str(item.component or "").strip())
        if node is None:
            continue
        action = "call_api" if item.api_route else "custom"
        interaction_id = _stable_id(
            "act", f"{page_seed}:interaction:{index}:{item.component}:{item.event}"
        )
        binding_id = ""
        if item.api_route:
            binding_id = _stable_id(
                "bind", f"{page_seed}:binding:{index}:{item.api_route}"
            )
            visual_bindings.append(
                VisualBinding(
                    id=binding_id,
                    node_id=node.id,
                    kind="api",
                    endpoint_hint=item.api_route,
                    method=_route_path_for_interaction(item.api_route, route_list),
                    required=True,
                    status="UNRESOLVED",
                    source="architect_contract",
                )
            )
        visual_interactions.append(
            VisualInteraction(
                id=interaction_id,
                node_id=node.id,
                event=str(item.event or "click"),
                action=action,  # type: ignore[arg-type]
                intent=str(item.logic or item.component or ""),
                binding_id=binding_id,
                status="UNRESOLVED",
            )
        )

    first_page_id = _stable_id("pg", f"{project_id}:{page_seed}:home")
    visual_pages: list[VisualPage] = []
    for index, (path, route_title, _route_desc) in enumerate(route_specs):
        if index == 0:
            page_id = first_page_id
        else:
            page_id = _stable_id("pg", f"{project_id}:{page_seed}:route:{path}:id")
        visual_pages.append(
            VisualPage(
                pageId=page_id,
                path=path,
                title=route_title,
                root=page_nodes[index],
            )
        )

    document = VisualDocument(
        docId=_stable_id("vdoc", f"{project_id}:{safe_title}:{page_seed}"),
        projectId=project_id,
        revision=0,
        status="DRAFT",
        title=safe_title,
        designTokens=VisualDesignTokens(
            colors={
                "primary": "#2563eb",
                "surface": "#ffffff",
                "text": "#0f172a",
                "muted": "#64748b",
                "border": "#cbd5e1",
            },
            spacing={"sm": "8px", "md": "16px", "lg": "24px"},
            radius={"card": "12px"},
            typography={"body": "14px", "title": "22px"},
        ),
        pages=visual_pages,
        activePageId=first_page_id,
        root=page,
        interactions=visual_interactions,
        bindings=visual_bindings,
        metadata=VisualMetadata(
            createdBy="architect",
            lastModifiedBy="architect",
        ),
    )
    return document.model_dump(mode="json", by_alias=True)


__all__ = ["build_visual_document_from_contract"]
