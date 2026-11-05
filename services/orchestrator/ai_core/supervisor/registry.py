"""AgentRegistry：所有专家 Agent 的能力注册中心。

Supervisor 不直接 import UI / Architect Graph，只依赖注册中心返回的
``AgentDescriptor``。未来新增 BackendCoder、ScriptAgent 或企业 CustomAgent
时，只需注册描述符，Supervisor 的路由协议保持不变。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ai_core.config import Settings, get_settings
from ai_core.supervisor.custom import (
    CustomAgentError,
    read_manifest,
)
from ai_core.supervisor.schemas import AgentStatus, AgentView

logger = logging.getLogger(__name__)


class AgentRegistryError(ValueError):
    """注册中心领域异常。"""

    code = "AGENT_REGISTRY_ERROR"


class AgentNotFoundError(AgentRegistryError):
    code = "AGENT_NOT_FOUND"


class AgentNotAvailableError(AgentRegistryError):
    code = "AGENT_NOT_AVAILABLE"


@dataclass(frozen=True)
class AgentDescriptor:
    """一个 Agent 的静态能力声明。"""

    agent_id: str
    display_name: str
    graph: str
    description: str = ""
    task_types: tuple[str, ...] = ()
    status: AgentStatus = "available"
    source: Literal["builtin", "manifest"] = "builtin"
    version: str = "1.0.0"
    stream_path_template: str | None = None
    keywords: tuple[str, ...] = ()
    manifest_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def routable(self) -> bool:
        """只有 available 与 custom 可以被 Supervisor 选中。"""
        return self.status in {"available", "custom"}

    def supports(self, task_type: str) -> bool:
        return task_type in self.task_types

    def to_view(self) -> AgentView:
        return AgentView(
            agent_id=self.agent_id,
            display_name=self.display_name,
            graph=self.graph,
            description=self.description,
            task_types=list(self.task_types),
            status=self.status,
            source=self.source,
            version=self.version,
            stream_path_template=self.stream_path_template,
            keywords=list(self.keywords),
            manifest_path=self.manifest_path,
            metadata=dict(self.metadata),
        )


# ------------------------------------------------------------
# 内置 Agent 注册表
# ------------------------------------------------------------
UI_KEYWORDS: tuple[str, ...] = (
    "按钮",
    "样式",
    "颜色",
    "间距",
    "字体",
    "文案",
    "布局",
    "动画",
    "组件",
    "页面",
    "弹窗",
    "对齐",
    "字号",
    "圈选",
    "改一下",
    "局部",
)

ARCHITECT_KEYWORDS: tuple[str, ...] = (
    "全栈",
    "架构",
    "后端",
    "数据库",
    "表结构",
    "接口",
    "api",
    "服务端",
    "技术栈",
    "部署",
    "spring",
    "fastapi",
    "mysql",
    "redis",
    "ddl",
    "迁移",
    "数据实体",
    "业务模型",
)


def _builtin_descriptors() -> list[AgentDescriptor]:
    return [
        AgentDescriptor(
            agent_id="ui_graph",
            display_name="局部 UI 工程师",
            graph="AgentGraphRunner",
            description="处理画布标注、局部组件修改、样式与交互增量调整",
            task_types=("ui_local_edit",),
            status="available",
            source="builtin",
            version="1.0.0",
            stream_path_template="/api/v1/runs/{runId}/events/stream",
            keywords=UI_KEYWORDS,
        ),
        AgentDescriptor(
            agent_id="architect_graph",
            display_name="全局架构师",
            graph="ArchitectGraphRunner",
            description="应用形态判别、全局架构澄清与全栈实施契约生成",
            task_types=("global_architecture",),
            status="available",
            source="builtin",
            version="1.0.0",
            stream_path_template="/api/v1/architect/runs/{runId}/events/stream",
            keywords=ARCHITECT_KEYWORDS,
        ),
        AgentDescriptor(
            agent_id="backend_coder",
            display_name="后端工程师（预留）",
            graph="BackendCoderGraph",
            description="未来根据实施契约生成 Spring Boot 后端",
            task_types=("backend_generation",),
            status="reserved",
            source="builtin",
            version="0.0.0",
            keywords=("后端代码", "spring boot", "controller", "service"),
        ),
        AgentDescriptor(
            agent_id="script_agent",
            display_name="脚本工程师（预留）",
            graph="ScriptAgentGraph",
            description="未来生成与验证后台自动化脚本",
            task_types=("script_automation",),
            status="reserved",
            source="builtin",
            version="0.0.0",
            keywords=("脚本", "定时任务", "批处理"),
        ),
    ]


class AgentRegistry:
    """线程安全的 Agent 注册中心。"""

    def __init__(self, custom_agents_dir: Path | str | None = None) -> None:
        self._lock = threading.RLock()
        self._agents: dict[str, AgentDescriptor] = {}
        self._custom_ids: set[str] = set()
        self._version = 1
        self._custom_agents_dir = Path(custom_agents_dir) if custom_agents_dir else None
        for descriptor in _builtin_descriptors():
            self.register(descriptor)

    @property
    def version(self) -> str:
        with self._lock:
            return str(self._version)

    @property
    def custom_agents_dir(self) -> str | None:
        with self._lock:
            return (
                str(self._custom_agents_dir)
                if self._custom_agents_dir is not None
                else None
            )

    # --------------------------------------------------------
    # 注册 / 查询
    # --------------------------------------------------------
    def register(
        self,
        descriptor: AgentDescriptor,
        *,
        replace: bool = False,
    ) -> None:
        """注册一个 Agent；默认禁止覆盖，防止企业 Manifest 劫持内置能力。"""
        with self._lock:
            existing = self._agents.get(descriptor.agent_id)
            if existing is not None and not replace:
                raise AgentRegistryError(
                    f"Agent 已注册，禁止覆盖: {descriptor.agent_id}"
                )
            self._agents[descriptor.agent_id] = descriptor
            if descriptor.source == "manifest":
                self._custom_ids.add(descriptor.agent_id)
            self._version += 1

    def get(self, agent_id: str) -> AgentDescriptor | None:
        with self._lock:
            return self._agents.get(agent_id)

    def require(self, agent_id: str) -> AgentDescriptor:
        descriptor = self.get(agent_id)
        if descriptor is None:
            raise AgentNotFoundError(f"Agent 未注册: {agent_id}")
        return descriptor

    def require_routable(self, agent_id: str) -> AgentDescriptor:
        descriptor = self.require(agent_id)
        if not descriptor.routable:
            raise AgentNotAvailableError(
                f"Agent 当前不可用（{descriptor.status}）: {agent_id}"
            )
        return descriptor

    def list_agents(self) -> list[AgentDescriptor]:
        with self._lock:
            items = list(self._agents.values())
        priority = {"available": 0, "custom": 1, "reserved": 2}
        return sorted(
            items,
            key=lambda item: (
                priority.get(item.status, 9),
                item.agent_id,
            ),
        )

    def resolve_task_type(self, task_type: str) -> list[AgentDescriptor]:
        """按能力查可用 Agent；返回顺序稳定，供确定性路由使用。"""
        return [
            item
            for item in self.list_agents()
            if item.routable and item.supports(task_type)
        ]

    def match_custom_keywords(self, message: str) -> list[AgentDescriptor]:
        """为未来 LLM 分类器 / 企业路由提供关键词候选，不直接改变路由。"""
        normalized = message.casefold()
        matches: list[AgentDescriptor] = []
        for item in self.list_agents():
            if item.source != "manifest" or not item.routable:
                continue
            if any(keyword.casefold() in normalized for keyword in item.keywords):
                matches.append(item)
        return matches

    # --------------------------------------------------------
    # CustomAgent Manifest 动态加载
    # --------------------------------------------------------
    def reload_custom_agents(
        self, directory: Path | str | None = None
    ) -> list[AgentDescriptor]:
        """重新扫描目录：先移除旧 Manifest Agent，再加载新文件。

        错误按单个文件隔离，不阻断 AI Core 启动；重复 id 会被跳过并记录
        warning，绝不覆盖内置 Agent。
        """
        target = Path(directory) if directory is not None else self._custom_agents_dir
        with self._lock:
            for agent_id in list(self._custom_ids):
                self._agents.pop(agent_id, None)
            self._custom_ids.clear()
            if target is None:
                self._version += 1
                return []
            self._custom_agents_dir = target

        loaded: list[AgentDescriptor] = []
        if not target.exists() or not target.is_dir():
            logger.info("custom agent directory not found, skip: %s", target)
            return loaded

        for path in sorted(target.glob("*.json")):
            try:
                manifest = read_manifest(path)
            except CustomAgentError as exc:
                logger.warning("skip invalid custom agent manifest %s: %s", path, exc)
                continue
            if not manifest.enabled:
                logger.info("skip disabled custom agent: %s", manifest.agent_id)
                continue
            with self._lock:
                if manifest.agent_id in self._agents:
                    logger.warning(
                        "skip custom agent %s: id already registered",
                        manifest.agent_id,
                    )
                    continue
            descriptor = AgentDescriptor(
                agent_id=manifest.agent_id,
                display_name=manifest.display_name,
                graph="CustomAgentRuntime",
                description=manifest.description,
                task_types=tuple(manifest.task_types),
                status="custom",
                source="manifest",
                version=manifest.version,
                stream_path_template=None,
                keywords=tuple(manifest.keywords),
                # 不向公开接口泄露宿主机绝对路径，只保留文件名。
                manifest_path=path.name,
                metadata={
                    **manifest.metadata,
                    "enabled": manifest.enabled,
                    "systemPromptFile": manifest.system_prompt_file,
                    "ragKnowledgeDir": manifest.rag_knowledge_dir,
                },
            )
            try:
                self.register(descriptor)
            except AgentRegistryError as exc:
                logger.warning("skip custom agent %s: %s", manifest.agent_id, exc)
                continue
            loaded.append(descriptor)
        logger.info(
            "loaded %s custom agent(s) from %s",
            len(loaded),
            target,
        )
        return loaded

    def to_views(self) -> list[AgentView]:
        return [item.to_view() for item in self.list_agents()]


# ------------------------------------------------------------
# 进程级注册中心
# ------------------------------------------------------------
_REGISTRY: AgentRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def build_default_registry(settings: Settings | None = None) -> AgentRegistry:
    """创建并加载内置 + 企业 Manifest Agent。"""
    resolved = settings or get_settings()
    registry = AgentRegistry(custom_agents_dir=resolved.supervisor_custom_agents_dir)
    registry.reload_custom_agents()
    return registry


def get_agent_registry() -> AgentRegistry:
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = build_default_registry()
        return _REGISTRY


def reload_agent_registry() -> AgentRegistry:
    """供内部 API 调用：重建全局注册中心并重新扫描 Manifest。"""
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = build_default_registry()
        return _REGISTRY


def reset_agent_registry_for_tests(registry: AgentRegistry | None = None) -> None:
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = registry
