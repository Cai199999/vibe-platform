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


def _text(name: str, value: str, seed: str) -> VisualNode:
    return VisualNode(
        id=_stable_id("nd", seed),
        type="text",
        name=name,
        props={"text": value},
        layout=VisualLayout(display="block"),
        style=_style("text-sm", "text-slate-700"),
        semantics=VisualSemantics(role="content", purpose=value),
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
            children=[_text("列表占位", "列表内容", f"{seed}:text")],
            semantics=semantics,
            provenance=provenance,
        )

    if role == "form":
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
                _text("表单标题", name, f"{seed}:title"),
                _input("输入项", f"请输入{name}", f"{seed}:input"),
                _button("提交", "提交", f"{seed}:submit"),
            ],
            semantics=semantics,
            provenance=provenance,
        )

    if role == "summary":
        return VisualNode(
            id=_stable_id("nd", seed),
            type="grid",
            name=name,
            layout=VisualLayout(
                display="grid",
                columns=3,
                gap="12px",
                padding="16px",
            ),
            style=_style("rounded-xl", "border", "bg-slate-50"),
            children=[
                _text("指标标题", name, f"{seed}:title"),
                _text("指标说明", description or "关键指标", f"{seed}:desc"),
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
            _text("组件标题", name, f"{seed}:title"),
            *([_text("组件说明", description, f"{seed}:desc")] if description else []),
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

    component_nodes: list[VisualNode] = []
    nodes_by_name: dict[str, VisualNode] = {}
    for index, component in enumerate(components or [], start=1):
        node = _component_node(
            component,
            index=index,
            parent_seed=page_seed,
        )
        if node is None:
            continue
        component_nodes.append(node)
        nodes_by_name[str(component.get("name") or "").strip()] = node

    component_children = component_nodes or [
        _container(
            name="待定区域",
            seed=f"{page_seed}:main:empty",
            children=[_text("占位说明", "等待业务澄清", f"{page_seed}:empty")],
            tailwind=("rounded-xl", "border", "border-dashed", "bg-slate-50"),
            role="placeholder",
            purpose="需求未明确",
        )
    ]

    regions = [
        str(item.get("region") or "").strip().lower()
        for item in page_layout or []
        if isinstance(item, dict)
    ]
    header_title = next(
        (
            str(item.get("title") or "").strip()
            for item in page_layout or []
            if str(item.get("region") or "").lower() == "header"
        ),
        "",
    )
    header = _container(
        name=header_title or "顶部导航",
        seed=f"{page_seed}:header",
        children=[
            _text("品牌", safe_title, f"{page_seed}:brand"),
            _text("页面说明", "由 Architect 生成的虚拟骨架", f"{page_seed}:subtitle"),
        ],
        display="flex",
        direction="row",
        gap="16px",
        padding="20px",
        tailwind=("rounded-xl", "border", "bg-white"),
        role="header",
        purpose="品牌与页面标题",
    )
    page.children.append(header)

    main_components = component_children
    if "sidebar" in regions:
        sidebar = _container(
            name="侧边栏",
            seed=f"{page_seed}:sidebar",
            children=[
                _text("导航标题", "导航", f"{page_seed}:nav-title"),
                _text("导航占位", "模块入口待补充", f"{page_seed}:nav-hint"),
            ],
            tailwind=("rounded-xl", "border", "bg-slate-50"),
            role="navigation",
            purpose="业务模块导航",
        )
        body = VisualNode(
            id=_stable_id("nd", f"{page_seed}:body"),
            type="grid",
            name="主体布局",
            layout=VisualLayout(
                display="grid",
                columns=2,
                gap="24px",
                padding="24px 0px",
            ),
            style=_style(),
            children=[
                sidebar,
                _container(
                    name="主内容区",
                    seed=f"{page_seed}:main",
                    children=main_components,
                    tailwind=(),
                    role="main",
                    purpose="核心业务区域",
                ),
            ],
            semantics=VisualSemantics(role="layout", purpose="侧栏 + 主内容"),
            provenance=VisualProvenance(created_by="architect"),
        )
        page.children.append(body)
    else:
        page.children.append(
            _container(
                name="主内容区",
                seed=f"{page_seed}:main",
                children=main_components,
                tailwind=(),
                role="main",
                purpose="核心业务区域",
            )
        )

    # 其它自定义 region 依次追加，保持契约里的顺序。
    for index, item in enumerate(page_layout or [], start=1):
        region = str(item.get("region") or "").strip().lower()
        if region in {"header", "sidebar", "main"}:
            continue
        region_title = str(item.get("title") or region or "区域").strip()
        page.children.append(
            _container(
                name=region_title,
                seed=f"{page_seed}:region:{index}:{region_title}",
                children=[
                    _text("区域占位", region_title, f"{page_seed}:region:{index}")
                ],
                tailwind=("rounded-xl", "border", "bg-white"),
                role=region or "region",
                purpose=str(item.get("description") or ""),
            )
        )

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
