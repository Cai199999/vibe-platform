"""SkillRegistry：Agent 能力之上的领域技能注册中心（M3 预留）。

三层概念：
  - Agent：执行者，拥有状态机与 Handoff 协议；
  - Tool：底层函数，如读文件、写文件、构建、Git 提交；
  - Skill：领域封装 = Prompt + RAG 知识 + Tool 子集。

当前只登记 Manifest 中的 Skill 声明，不加载 Prompt / RAG，也不执行。
M3 阶段由 SkillRegistry 将 Skill 动态拼装进 Agent 的运行时上下文。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


class SkillRegistryError(ValueError):
    """Skill 注册冲突或不合法。"""

    code = "SKILL_REGISTRY_ERROR"


@dataclass(frozen=True)
class SkillDescriptor:
    """一个已声明但尚未执行的 Skill。"""

    skill_id: str
    name: str
    agent_id: str
    description: str = ""
    prompt_ref: str | None = None
    rag_refs: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_view(self) -> dict[str, Any]:
        return {
            "skillId": self.skill_id,
            "name": self.name,
            "agentId": self.agent_id,
            "description": self.description,
            "promptRef": self.prompt_ref,
            "ragRefs": list(self.rag_refs),
            "tools": list(self.tools),
            "metadata": dict(self.metadata),
        }


class SkillRegistry:
    """线程安全的 Skill 注册中心。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._skills: dict[str, SkillDescriptor] = {}
        self._version = 1

    @property
    def version(self) -> str:
        with self._lock:
            return str(self._version)

    def register(self, skill: SkillDescriptor, *, replace: bool = False) -> None:
        with self._lock:
            if skill.skill_id in self._skills and not replace:
                raise SkillRegistryError(f"Skill 已注册，禁止覆盖: {skill.skill_id}")
            self._skills[skill.skill_id] = skill
            self._version += 1

    def get(self, skill_id: str) -> SkillDescriptor | None:
        with self._lock:
            return self._skills.get(skill_id)

    def list_skills(self) -> list[SkillDescriptor]:
        with self._lock:
            return sorted(self._skills.values(), key=lambda item: item.skill_id)

    def list_for_agent(self, agent_id: str) -> list[SkillDescriptor]:
        return [item for item in self.list_skills() if item.agent_id == agent_id]

    def find_by_tool(self, tool: str) -> list[SkillDescriptor]:
        """为未来动态拼装提供反查：哪些 Skill 需要该 Tool。"""
        return [item for item in self.list_skills() if tool in item.tools]

    def clear(self) -> None:
        with self._lock:
            self._skills.clear()
            self._version += 1

    def to_views(self) -> list[dict[str, Any]]:
        return [item.to_view() for item in self.list_skills()]
