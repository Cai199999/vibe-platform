"""Supervisor 内部 API：路由、派发与 AgentRegistry 查询。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ai_core.supervisor.agent_builder import AgentBuilder, AgentBuilderError
from ai_core.supervisor.gates import SupervisorGateError
from ai_core.supervisor.registry import (
    AgentNotAvailableError,
    AgentNotFoundError,
    get_agent_registry,
    reload_agent_registry,
)
from ai_core.domain.supervisor import (
    AgentCreateRequest,
    AgentKnowledgeDocument,
    AgentKnowledgeListResponse,
    AgentKnowledgeUploadRequest,
    AgentListResponse,
    AgentView,
    SupervisorDispatchResponse,
    SupervisorGateResponse,
    SupervisorRequest,
    SupervisorRouteResponse,
    SupervisorToolCallRequest,
    SupervisorToolCallResponse,
    SupervisorToolListResponse,
)
from ai_core.supervisor.service import (
    SupervisorService,
    SupervisorServiceError,
)
from ai_core.supervisor.tools import (
    SupervisorToolError,
    SupervisorToolService,
)

router = APIRouter(prefix="/api/v1/internal/supervisor", tags=["supervisor"])


@router.post("/route", response_model=SupervisorRouteResponse)
def route_request(request: SupervisorRequest) -> SupervisorRouteResponse:
    """只做意图路由，不启动任何子图。"""
    service = SupervisorService(get_agent_registry())
    try:
        return service.route(request)
    except Exception as exc:  # noqa: BLE001 - 统一映射领域异常
        _raise_http(exc)


@router.post(
    "/dispatch",
    response_model=SupervisorDispatchResponse,
    status_code=202,
)
def dispatch_request(request: SupervisorRequest) -> SupervisorDispatchResponse:
    """按路由计划启动子图，返回子 Run 与 SSE 订阅路径。"""
    service = SupervisorService(get_agent_registry())
    try:
        return service.dispatch(request)
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)


@router.get("/gates/{gate_id}", response_model=SupervisorGateResponse)
def get_gate(gate_id: str) -> SupervisorGateResponse:
    """查询 both 门控；Architect 取消 / 失败会自动转为 FAILED。"""
    service = SupervisorService(get_agent_registry())
    try:
        return service.get_gate(gate_id)
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)


@router.post("/gates/{gate_id}/release", response_model=SupervisorGateResponse)
def release_gate(gate_id: str) -> SupervisorGateResponse:
    """契约确认后放行 UI Graph；重复调用幂等，返回同一个 UI Run。"""
    service = SupervisorService(get_agent_registry())
    try:
        return service.release_gate(gate_id)
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)


@router.get("/agents", response_model=AgentListResponse)
def list_agents() -> AgentListResponse:
    """列出内置、预留与企业 Manifest Agent。"""
    service = SupervisorService(get_agent_registry())
    return service.list_agents()


@router.get("/tools", response_model=SupervisorToolListResponse)
def list_tools() -> SupervisorToolListResponse:
    """列出本地 Tool 与 MCP 远端 Tool；只返回描述，不返回密钥。"""
    tools, servers = SupervisorToolService().list_tools()
    return SupervisorToolListResponse(tools=tools, mcp_servers=servers)


@router.post(
    "/tools/{tool_name}/invoke",
    response_model=SupervisorToolCallResponse,
)
def invoke_tool(
    tool_name: str,
    request: SupervisorToolCallRequest,
) -> SupervisorToolCallResponse:
    """受控调用一个已注册 Tool（search_web / MCP / 受控 shell）。"""
    try:
        payload = SupervisorToolService().invoke_tool(
            tool_name,
            request.arguments,
        )
    except SupervisorToolError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return SupervisorToolCallResponse.model_validate(payload)


@router.post("/agents/reload", response_model=AgentListResponse)
def reload_agents() -> AgentListResponse:
    """重新扫描内置 Agent Manifest 与企业 CustomAgent Manifest。

    仅限内部网络调用；Manifest 只做声明解析，不允许执行代码，也不会
    覆盖已注册的内置 Agent。
    """
    registry = reload_agent_registry()
    return SupervisorService(registry).list_agents()


@router.post("/agents", response_model=AgentView, status_code=201)
def create_agent(request: AgentCreateRequest) -> AgentView:
    """根据自然语言需求创建一个企业自定义 Agent。

    AgentBuilder 只生成 Manifest / System Prompt / 知识目录三类声明式
    产物，不执行任何模型生成的代码；落盘后立即重扫注册中心，前端无需
    重启 AI Core 就能看到新 Agent。
    """
    builder = AgentBuilder()
    try:
        built = builder.create(
            requirement=request.requirement,
            display_name=request.display_name,
            agent_id=request.agent_id,
            task_types=request.task_types or None,
            keywords=request.keywords or None,
        )
    except AgentBuilderError as exc:
        _raise_builder_http(exc)
    registry = reload_agent_registry()
    descriptor = registry.get(built.manifest.agent_id)
    if descriptor is None:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "AGENT_REGISTRATION_FAILED",
                "message": "Agent 文件已生成，但注册中心未能加载",
            },
        )
    return descriptor.to_view()


@router.delete("/agents/{agent_id}")
def delete_agent(agent_id: str) -> dict[str, object]:
    """删除自定义 Agent；内置 Agent 没有托管 Manifest，天然不可删。"""
    builder = AgentBuilder()
    try:
        result = builder.delete(agent_id)
    except AgentBuilderError as exc:
        _raise_builder_http(exc)
    reload_agent_registry()
    return result


@router.get(
    "/agents/{agent_id}/knowledge",
    response_model=AgentKnowledgeListResponse,
)
def list_agent_knowledge(agent_id: str) -> AgentKnowledgeListResponse:
    """列出某个自定义 Agent 已落盘的知识文件。"""
    builder = AgentBuilder()
    try:
        documents = builder.list_knowledge(agent_id)
        knowledge_dir = builder.get_knowledge_dir(agent_id)
    except AgentBuilderError as exc:
        _raise_builder_http(exc)
    return AgentKnowledgeListResponse(
        agent_id=agent_id,
        knowledge_dir=knowledge_dir,
        documents=[
            AgentKnowledgeDocument.model_validate(item.to_dict()) for item in documents
        ],
    )


@router.post(
    "/agents/{agent_id}/knowledge",
    response_model=AgentKnowledgeDocument,
    status_code=201,
)
def upload_agent_knowledge(
    agent_id: str,
    request: AgentKnowledgeUploadRequest,
) -> AgentKnowledgeDocument:
    """上传 .md / .txt 知识文件并写入该 Agent 的 RAG 目录。

    当前只落盘 + 更新 Manifest 清单；向量化与检索由后续 RAG 运行时按
    ``ragKnowledgeDir`` 扫描完成。
    """
    builder = AgentBuilder()
    try:
        document = builder.save_knowledge(
            agent_id,
            request.file_name,
            request.content,
        )
    except AgentBuilderError as exc:
        _raise_builder_http(exc)
    return AgentKnowledgeDocument.model_validate(document.to_dict())


@router.delete("/agents/{agent_id}/knowledge/{file_name}")
def delete_agent_knowledge(
    agent_id: str,
    file_name: str,
) -> dict[str, object]:
    """删除单个知识文件。"""
    builder = AgentBuilder()
    try:
        return builder.delete_knowledge(agent_id, file_name)
    except AgentBuilderError as exc:
        _raise_builder_http(exc)


def _raise_builder_http(exc: AgentBuilderError) -> None:
    """把 AgentBuilder 领域异常映射为带错误码的 HTTP 响应。

    注意：FastAPI 在处理器抛出 HTTPException 后不会再执行后续语句；
    调用方在 ``_raise_builder_http`` 后使用 ``return`` 兜底以便类型检查。
    """
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    ) from exc


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, AgentNotFoundError):
        raise HTTPException(
            status_code=404,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, AgentNotAvailableError):
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, SupervisorGateError):
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, SupervisorServiceError):
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    raise HTTPException(
        status_code=500,
        detail={
            "code": "SUPERVISOR_INTERNAL_ERROR",
            "message": "Supervisor 处理失败",
        },
    ) from exc
