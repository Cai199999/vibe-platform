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
from ai_core.architect.schemas import (
    ArchitectApiRoute,
    ArchitectCitation,
    ArchitectComponentInteraction,
    ArchitectContract,
    ArchitectImplementationPhase,
)


@dataclass
class ContractValidation:
    valid: bool
    issues: list[str] = field(default_factory=list)
    draft: ContractDraft | None = None


def validate_draft(shape: str, draft: ContractDraft) -> ContractValidation:
    issues: list[str] = []

    if len(draft.contract_markdown.strip()) < 200:
        issues.append("contract_markdown 太短，必须包含完整可执行契约")
    if not draft.implementation_path:
        issues.append("缺少 implementation_path，无法指导后续生成")

    api_routes = list(draft.api_routes)
    database_ddl = draft.database_ddl.strip()
    interactions = list(draft.component_interactions)
    ui_logic = list(draft.ui_logic)
    frontend_data_model = list(draft.frontend_data_model)
    script_spec = dict(draft.script_spec)

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
        ui_logic=ui_logic,
        frontend_data_model=frontend_data_model,
        script_spec=script_spec,
        implementation_path=draft.implementation_path,
        assumptions=draft.assumptions,
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
        "uiLogic": draft.ui_logic,
        "frontendDataModel": draft.frontend_data_model,
        "scriptSpec": draft.script_spec,
        "implementationPath": [
            item.model_dump(by_alias=True) for item in draft.implementation_path
        ],
        "assumptions": draft.assumptions,
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

    return ArchitectContract(
        contract_id=contract_id,
        run_id=run_id,
        app_shape=shape,  # type: ignore[arg-type]
        status="AWAITING_CONFIRMATION",
        confidence_score=confidence_score,
        threshold=threshold,
        source_hash=source_hash,
        contract_markdown=markdown,
        tech_stack=draft.tech_stack,
        api_routes=draft.api_routes,
        database_ddl=draft.database_ddl,
        component_interactions=draft.component_interactions,
        ui_logic=draft.ui_logic,
        frontend_data_model=draft.frontend_data_model,
        script_spec=draft.script_spec,
        implementation_path=draft.implementation_path,
        assumptions=draft.assumptions,
        citations=citations,
        created_at=datetime.now(timezone.utc),
    )
