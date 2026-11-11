"""分级契约校验与装配。

LLM 起草的契约必须先通过确定性校验：
  - lightweight_web 不得出现 REST API / DDL；
  - script_automation 不得出现 UI / API / DDL；
  - full_stack_system 必须有 API、DDL、组件交互与技术栈。

校验失败时 Graph 允许有限次修复；修复后仍失败则终止，绝不把
“看起来像全栈”的错误契约交给用户确认。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ai_core.architect.llm import ContractDraft
from ai_core.domain.architect import (
    ArchitectApiRoute,
    ArchitectCitation,
    ArchitectComponentInteraction,
    ArchitectContract,
    ArchitectImplementationPhase,
    normalize_database_engine,
)


@dataclass
class ContractValidation:
    valid: bool
    issues: list[str] = field(default_factory=list)
    draft: ContractDraft | None = None


def _normalize_component(raw: dict, index: int) -> dict:
    """统一占位组件标记，兼容 LLM 的两种字段写法。

    契约对外使用 camelCase（``isPlaceholder``），但模型偶尔按提示词输出
    ``is_placeholder``。这里一并接受并写成规范字段；占位组件统一标记为
    ``role=placeholder``，FrontendCoder 据此只渲染占位卡，不猜业务表单。
    """
    item = dict(raw)
    flag = item.get("isPlaceholder")
    if flag is None:
        flag = item.get("is_placeholder")
    is_placeholder = bool(flag)
    item["isPlaceholder"] = is_placeholder
    item.pop("is_placeholder", None)
    if is_placeholder:
        item["role"] = "placeholder"
        name = str(item.get("name") or item.get("title") or "待定区域").strip()
        item.setdefault("name", name)
        item.setdefault("placeholder", f'<Placeholder title="{name}" />')
        if not str(item.get("description") or "").strip():
            item["description"] = "需求未明确，等待补充；当前仅渲染占位区域。"
    return item


def validate_draft(shape: str, draft: ContractDraft) -> ContractValidation:
    issues: list[str] = []

    if len(draft.contract_markdown.strip()) < 200:
        issues.append("contract_markdown 太短，必须包含完整可执行契约")
    if not draft.implementation_path:
        issues.append("缺少 implementation_path，无法指导后续生成")

    api_routes = list(draft.api_routes)
    database_ddl = draft.database_ddl.strip()
    interactions = list(draft.component_interactions)
    frontend_routes = list(draft.frontend_routes)
    page_layout = list(draft.page_layout)
    components = [
        _normalize_component(item, index)
        for index, item in enumerate(draft.components or [], start=1)
        if isinstance(item, dict)
    ]
    ui_logic = list(draft.ui_logic)
    frontend_data_model = list(draft.frontend_data_model)
    script_spec = dict(draft.script_spec)

    # 前端骨架由 FrontendCoder 直接消费这三项，缺失时不允许进入确认。
    if shape in {"lightweight_web", "full_stack_system"}:
        if not frontend_routes:
            issues.append("前端契约缺少 frontend_routes，无法生成页面路由")
        if not page_layout:
            issues.append("前端契约缺少 page_layout，无法生成页面布局")
        if not components:
            issues.append("前端契约缺少 components，无法生成组件骨架")
        if draft.skeleton_mode:
            # 骨架模式是“最小化生成”的硬约束：宁可只有一个占位组件，
            # 也绝不允许模型顺手补出登录 / 上传 / 详情等业务 UI。
            if len(components) != 1 or not components[0].get("isPlaceholder"):
                issues.append(
                    "skeleton_mode 契约只能包含一个 isPlaceholder=true 的占位组件"
                )
            business_components = [
                item
                for item in components
                if not item.get("isPlaceholder")
                and str(item.get("role") or "").lower() not in {"placeholder", ""}
            ]
            if business_components:
                issues.append("skeleton_mode 契约禁止生成具体业务组件")

    if shape == "lightweight_web":
        if api_routes:
            issues.append("lightweight_web 不允许包含 REST API 路由")
        if database_ddl:
            issues.append("lightweight_web 不允许包含数据库 DDL")
        if script_spec:
            issues.append("lightweight_web 不允许包含脚本执行规格")
        if not (interactions or ui_logic or frontend_data_model):
            issues.append("lightweight_web 必须包含 UI 交互或前端数据模型")
        # 即使模型多写了，装配时也要裁剪，保证契约分级干净。
        api_routes = []
        database_ddl = ""
        script_spec = {}

    elif shape == "script_automation":
        if api_routes:
            issues.append("script_automation 不允许包含 REST API 路由")
        if database_ddl:
            issues.append("script_automation 不允许包含数据库 DDL")
        if interactions or ui_logic or frontend_data_model:
            issues.append("script_automation 不允许包含 UI / 组件交互")
        if not script_spec:
            issues.append("script_automation 必须包含 script_spec")
        else:
            required = {"runtime", "input", "output", "exitCodes"}
            missing = sorted(required - set(script_spec))
            if missing:
                issues.append(f"script_spec 缺少字段: {', '.join(missing)}")
        api_routes = []
        database_ddl = ""
        interactions = []
        frontend_routes = []
        page_layout = []
        components = []
        ui_logic = []
        frontend_data_model = []

    elif shape == "full_stack_system":
        if not api_routes:
            issues.append("full_stack_system 必须包含 API 路由")
        elif any(not route.path.startswith("/api/") for route in api_routes):
            issues.append("API 路由必须以 /api/ 开头")
        if not database_ddl:
            issues.append("full_stack_system 必须包含数据库 DDL")
        elif not re.search(r"CREATE\s+TABLE", database_ddl, re.IGNORECASE):
            issues.append("数据库 DDL 必须包含 CREATE TABLE")
        if re.search(r"FOREIGN\s+KEY", database_ddl, re.IGNORECASE):
            issues.append("数据库 DDL 禁止物理外键，关联字段由业务代码保证")
        if not interactions:
            issues.append("full_stack_system 必须包含组件交互")
        if not draft.tech_stack.get("backend"):
            issues.append("full_stack_system 技术栈缺少 backend")
        if not draft.tech_stack.get("database"):
            issues.append("full_stack_system 技术栈缺少 database")
        script_spec = {}

    else:
        issues.append(f"未知应用形态: {shape}")

    normalized = ContractDraft(
        contract_markdown=draft.contract_markdown.strip(),
        tech_stack=draft.tech_stack,
        api_routes=api_routes,
        database_ddl=database_ddl,
        component_interactions=interactions,
        frontend_routes=frontend_routes,
        page_layout=page_layout,
        components=components,
        ui_logic=ui_logic,
        frontend_data_model=frontend_data_model,
        script_spec=script_spec,
        implementation_path=draft.implementation_path,
        assumptions=draft.assumptions,
        include_ai_capability=draft.include_ai_capability,
        skeleton_mode=draft.skeleton_mode,
    )
    return ContractValidation(valid=not issues, issues=issues, draft=normalized)


def draft_to_dict(draft: ContractDraft) -> dict:
    """转换为可写入 LangGraph checkpoint 的纯 Python 结构。"""
    return {
        "contractMarkdown": draft.contract_markdown,
        "techStack": draft.tech_stack,
        "apiRoutes": [item.model_dump(by_alias=True) for item in draft.api_routes],
        "databaseDdl": draft.database_ddl,
        "componentInteractions": [
            item.model_dump(by_alias=True) for item in draft.component_interactions
        ],
        "frontendRoutes": draft.frontend_routes,
        "pageLayout": draft.page_layout,
        "components": draft.components,
        "uiLogic": draft.ui_logic,
        "frontendDataModel": draft.frontend_data_model,
        "scriptSpec": draft.script_spec,
        "implementationPath": [
            item.model_dump(by_alias=True) for item in draft.implementation_path
        ],
        "assumptions": draft.assumptions,
        "includeAiCapability": draft.include_ai_capability,
        "skeletonMode": draft.skeleton_mode,
    }


def draft_from_dict(payload: dict) -> ContractDraft:
    """从 checkpoint 恢复契约草稿。"""
    routes = []
    for item in payload.get("apiRoutes") or []:
        if isinstance(item, dict):
            try:
                routes.append(ArchitectApiRoute.model_validate(item))
            except Exception:  # noqa: BLE001
                continue
    interactions = []
    for item in payload.get("componentInteractions") or []:
        if isinstance(item, dict):
            try:
                interactions.append(ArchitectComponentInteraction.model_validate(item))
            except Exception:  # noqa: BLE001
                continue
    phases = []
    for item in payload.get("implementationPath") or []:
        if isinstance(item, dict):
            try:
                phases.append(ArchitectImplementationPhase.model_validate(item))
            except Exception:  # noqa: BLE001
                continue
    return ContractDraft(
        contract_markdown=str(payload.get("contractMarkdown") or ""),
        tech_stack=(
            dict(payload.get("techStack"))
            if isinstance(payload.get("techStack"), dict)
            else {}
        ),
        api_routes=routes,
        database_ddl=str(payload.get("databaseDdl") or ""),
        component_interactions=interactions,
        frontend_routes=[
            dict(item)
            for item in payload.get("frontendRoutes") or []
            if isinstance(item, dict)
        ],
        page_layout=[
            dict(item)
            for item in payload.get("pageLayout") or []
            if isinstance(item, dict)
        ],
        components=[
            dict(item)
            for item in payload.get("components") or []
            if isinstance(item, dict)
        ],
        ui_logic=[
            dict(item)
            for item in payload.get("uiLogic") or []
            if isinstance(item, dict)
        ],
        frontend_data_model=[
            dict(item)
            for item in payload.get("frontendDataModel") or []
            if isinstance(item, dict)
        ],
        script_spec=(
            dict(payload.get("scriptSpec"))
            if isinstance(payload.get("scriptSpec"), dict)
            else {}
        ),
        implementation_path=phases,
        assumptions=[
            str(item) for item in payload.get("assumptions") or [] if str(item).strip()
        ],
        include_ai_capability=bool(
            payload.get("includeAiCapability") or payload.get("include_ai_capability")
        ),
        skeleton_mode=bool(payload.get("skeletonMode") or payload.get("skeleton_mode")),
    )


def assemble_contract(
    *,
    contract_id: str,
    run_id: str,
    shape: str,
    confidence_score: float,
    threshold: float,
    source_hash: str,
    draft: ContractDraft,
    citations: list[ArchitectCitation],
) -> ArchitectContract:
    markdown = draft.contract_markdown.strip()
    if citations and "参考规范" not in markdown:
        lines = ["", "", "## 参考规范", ""]
        for citation in citations:
            lines.append(
                f"- {citation.title} / {citation.section}"
                f"（`{citation.doc_id}`，相关度 {citation.score:.2f}）"
            )
        markdown += "\n".join(lines)

    tech_stack = dict(draft.tech_stack)
    # 全栈契约必须把数据库引擎提升为一级字段：Coder 与部署配置都依赖它。
    # sqlite 仅用于本地测试；生产选型为 mysql / postgresql。
    database_engine = ""
    if shape == "full_stack_system":
        database_engine = (
            normalize_database_engine(
                tech_stack.get("databaseEngine") or tech_stack.get("database")
            )
            or "mysql"
        )
        tech_stack["databaseEngine"] = database_engine

    return ArchitectContract(
        contract_id=contract_id,
        run_id=run_id,
        app_shape=shape,  # type: ignore[arg-type]
        status="AWAITING_CONFIRMATION",
        confidence_score=confidence_score,
        threshold=threshold,
        source_hash=source_hash,
        contract_markdown=markdown,
        tech_stack=tech_stack,
        database_engine=database_engine,
        api_routes=draft.api_routes,
        database_ddl=draft.database_ddl,
        component_interactions=draft.component_interactions,
        frontend_routes=draft.frontend_routes,
        page_layout=draft.page_layout,
        components=draft.components,
        ui_logic=draft.ui_logic,
        frontend_data_model=draft.frontend_data_model,
        script_spec=draft.script_spec,
        implementation_path=draft.implementation_path,
        assumptions=draft.assumptions,
        include_ai_capability=draft.include_ai_capability,
        skeleton_mode=draft.skeleton_mode,
        citations=citations,
        created_at=datetime.now(timezone.utc),
    )
