"""AgentRegistry：所有专家 Agent 的能力注册中心。

内置 Agent 不再硬编码在 Python 中，而是由每个代码目录下的
``manifest.json`` 声明。启动时扫描 ``ai_core/*/manifest.json``，解析为
``AgentDescriptor``；Supervisor 只依赖注册中心，不直接 import 子图。

同时维护一个 ``SkillRegistry``：Skill 当前只做声明与展示，M3 阶段再由
运行时把 Prompt + RAG + Tool 子集动态拼装进 Agent。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ai_core.config import Settings, get_settings
from ai_core.skill_loader import load_skill_directory
from ai_core.supervisor.custom import (
    CustomAgentError,
    read_manifest,
)
from ai_core.supervisor.manifest import (
    AgentManifest,
    AgentManifestError,
    discover_agent_manifests,
)
from ai_core.supervisor.schemas import AgentStatus, AgentView, SkillView
from ai_core.supervisor.skills import SkillDescriptor, SkillRegistry

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
    capabilities: tuple[dict[str, Any], ...] = ()
    supported_tools: tuple[str, ...] = ()
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    skills: tuple[SkillDescriptor, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def routable(self) -> bool:
        """只有可派发的 available / custom Agent 可以被 Supervisor 选中。

        ``supervisor`` 自身在团队清单中标记为 available，以便前端展示其
        调度能力；但它属于编排层，不能被当成子 Agent 再次派发。因此额外
        尊重 Manifest 的 ``role=orchestrator`` 与 ``dispatchable=false``
        声明，避免自调用或把编排器当执行器。
        """
        if self.metadata.get("role") == "orchestrator":
            return False
        if self.metadata.get("dispatchable") is False:
            return False
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
            capabilities=[dict(item) for item in self.capabilities],
            supported_tools=list(self.supported_tools),
            input_schema=dict(self.input_schema),
            output_schema=dict(self.output_schema),
            skills=[SkillView.model_validate(item.to_view()) for item in self.skills],
            metadata=dict(self.metadata),
        )


# 每个专家 Agent 独占 <ai_core>/<agent_dir>/manifest.json。
DEFAULT_AGENT_MANIFEST_ROOT = Path(__file__).resolve().parents[1]


def _descriptor_from_manifest(
    manifest: AgentManifest,
    *,
    manifest_path: str,
    source: Literal["builtin", "manifest"],
) -> AgentDescriptor:
    """把声明式 Manifest 转为注册中心描述符。"""
    capabilities = tuple(
        {"id": item.id, "description": item.description}
        for item in manifest.capabilities
    )
    skills = tuple(
        SkillDescriptor(
            skill_id=item.skill_id,
            name=item.name,
            agent_id=manifest.agent_id,
            description=item.description,
            prompt_ref=item.prompt_ref,
            rag_refs=tuple(item.rag_refs),
            tools=tuple(item.tools),
            metadata=dict(item.metadata),
        )
        for item in manifest.skills
    )
    return AgentDescriptor(
        agent_id=manifest.agent_id,
        display_name=manifest.name,
        graph=manifest.graph,
        description=manifest.description,
        task_types=manifest.task_types,
        status=manifest.status,
        source=source,
        version=manifest.version,
        stream_path_template=manifest.stream_path_template,
        keywords=tuple(manifest.keywords),
        manifest_path=manifest_path,
        capabilities=capabilities,
        supported_tools=tuple(manifest.supported_tools),
        input_schema=dict(manifest.input_schema),
        output_schema=dict(manifest.output_schema),
        skills=skills,
        metadata={
            **manifest.metadata,
            "manifestVersion": manifest.metadata.get("manifest_version", 1),
        },
    )


class AgentRegistry:
    """线程安全的 Agent 注册中心。"""

    def __init__(
        self,
        custom_agents_dir: Path | str | None = None,
        manifest_dir: Path | str | None = None,
        skills_dir: Path | str | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._agents: dict[str, AgentDescriptor] = {}
        self._builtin_ids: set[str] = set()
        self._custom_ids: set[str] = set()
        self._version = 1
        self._skills = SkillRegistry()
        self._manifest_dir = (
            Path(manifest_dir) if manifest_dir else DEFAULT_AGENT_MANIFEST_ROOT
        )
        self._custom_agents_dir = Path(custom_agents_dir) if custom_agents_dir else None
        self._skills_dir = Path(skills_dir) if skills_dir else None
        self.reload_builtin_agents()

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

    @property
    def manifest_dir(self) -> str:
        return str(self._manifest_dir)

    @property
    def skill_registry(self) -> SkillRegistry:
        return self._skills

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
            if descriptor.metadata.get("role") == "orchestrator":
                reason = "orchestrator"
            elif descriptor.metadata.get("dispatchable") is False:
                reason = "not_dispatchable"
            else:
                reason = descriptor.status
            raise AgentNotAvailableError(f"Agent 当前不可派发（{reason}）: {agent_id}")
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
    # 内置 Manifest 自动扫描
    # --------------------------------------------------------
    def reload_builtin_agents(
        self, directory: Path | str | None = None
    ) -> list[AgentDescriptor]:
        """重新扫描内置 Agent Manifest，并重建 Skill 注册表。

        任何单个 Manifest 校验失败只记录 warning，不阻断其余 Agent；
        但如果最终一个内置 Agent 都没有，说明部署包不完整，必须快速失败。
        """
        target = Path(directory) if directory is not None else self._manifest_dir
        with self._lock:
            for agent_id in list(self._builtin_ids):
                self._agents.pop(agent_id, None)
            self._builtin_ids.clear()
            self._manifest_dir = target

        loaded: list[AgentDescriptor] = []
        for path, manifest in self._safe_discover(target):
            descriptor = self._safe_builtin_descriptor(path, manifest)
            if descriptor is None:
                continue
            with self._lock:
                if descriptor.agent_id in self._agents:
                    logger.warning(
                        "skip builtin agent %s: id already registered",
                        descriptor.agent_id,
                    )
                    continue
            try:
                self.register(descriptor)
            except AgentRegistryError as exc:
                logger.warning(
                    "skip builtin agent %s: %s",
                    descriptor.agent_id,
                    exc,
                )
                continue
            with self._lock:
                self._builtin_ids.add(descriptor.agent_id)
            loaded.append(descriptor)

        if not loaded:
            raise AgentRegistryError(f"未发现可用的内置 Agent Manifest: {target}")
        self._rebuild_skills()
        logger.info("loaded %s builtin agent(s) from %s", len(loaded), target)
        return loaded

    def _safe_discover(self, target: Path) -> list[tuple[Path, AgentManifest]]:
        """逐文件隔离解析错误，避免一个坏 Manifest 拖垮启动。"""
        if not target.exists() or not target.is_dir():
            logger.error("agent manifest directory not found: %s", target)
            return []
        discovered: list[tuple[Path, AgentManifest]] = []
        try:
            discovered = discover_agent_manifests(target)
        except AgentManifestError as exc:
            # discover 目前会抛首个错误；逐个重扫以隔离坏文件。
            logger.warning("agent manifest discovery failed: %s", exc)
            discovered = []
            for path in sorted(target.glob("*/manifest.json")):
                try:
                    discovered.append((path, _read_one(path)))
                except AgentManifestError as item_exc:
                    logger.warning(
                        "skip invalid agent manifest %s: %s",
                        path,
                        item_exc,
                    )
        return discovered

    def _safe_builtin_descriptor(
        self,
        path: Path,
        manifest: AgentManifest,
    ) -> AgentDescriptor | None:
        try:
            relative = path.relative_to(self._manifest_dir).as_posix()
        except ValueError:
            relative = path.as_posix()
        try:
            return _descriptor_from_manifest(
                manifest,
                manifest_path=relative,
                source="builtin",
            )
        except Exception as exc:  # noqa: BLE001 - 单条 Manifest 失败隔离
            logger.warning("failed to build descriptor from %s: %s", path, exc)
            return None

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
                self._rebuild_skills()
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
                capabilities=tuple(
                    {"id": task_type, "description": ""}
                    for task_type in manifest.task_types
                ),
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
        self._rebuild_skills()
        logger.info(
            "loaded %s custom agent(s) from %s",
            len(loaded),
            target,
        )
        return loaded

    # --------------------------------------------------------
    # SkillRegistry
    # --------------------------------------------------------
    def _rebuild_skills(self) -> None:
        """从 Manifest + config/skills 重建 Skill 视图。

        Manifest 负责“挂载声明”，外部 ``*.skill.json`` 负责提供真实
        System Prompt。同 id 的外部 Skill 会替换 Manifest 中的占位描述，
        这样 Agent 清单与 Skill 内容可以独立迭代。
        """
        with self._lock:
            registry = SkillRegistry()
            for descriptor in self._agents.values():
                for skill in descriptor.skills:
                    try:
                        registry.register(skill)
                    except Exception as exc:  # noqa: BLE001 - Skill 冲突不阻断 Agent
                        logger.warning(
                            "skip duplicate skill %s from %s: %s",
                            skill.skill_id,
                            descriptor.agent_id,
                            exc,
                        )
            if self._skills_dir is not None:
                for skill in load_skill_directory(self._skills_dir):
                    try:
                        registry.register(skill, replace=True)
                    except Exception as exc:  # noqa: BLE001 - 坏 Skill 不阻断 Agent
                        logger.warning(
                            "skip external skill %s from %s: %s",
                            skill.skill_id,
                            self._skills_dir,
                            exc,
                        )
            self._skills = registry

    def list_skills(self) -> list[SkillDescriptor]:
        return self._skills.list_skills()

    def to_views(self) -> list[AgentView]:
        return [item.to_view() for item in self.list_agents()]

    def to_skill_views(self) -> list[SkillView]:
        return [SkillView.model_validate(item.to_view()) for item in self.list_skills()]


def _read_one(path: Path) -> AgentManifest:
    """延迟导入避免模块顶部重复；供坏文件隔离重扫使用。"""
    from ai_core.supervisor.manifest import read_agent_manifest

    return read_agent_manifest(path)


# ------------------------------------------------------------
# 进程级注册中心
# ------------------------------------------------------------
_REGISTRY: AgentRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def build_default_registry(settings: Settings | None = None) -> AgentRegistry:
    """创建并加载内置 Manifest + 企业 CustomAgent Manifest。"""
    resolved = settings or get_settings()
    manifest_dir = resolved.supervisor_agent_manifest_dir or None
    registry = AgentRegistry(
        custom_agents_dir=resolved.supervisor_custom_agents_dir,
        manifest_dir=manifest_dir,
        skills_dir=resolved.skills_dir,
    )
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
