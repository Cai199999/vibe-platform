"""SkillRegistry：Agent 能力之上的领域技能注册中心。

三层概念：
  - Agent：执行者，拥有状态机与 Handoff 协议；
  - Tool：底层函数，如读文件、写文件、构建、Git 提交；
  - Skill：领域封装 = Prompt + RAG 知识 + Tool 子集。

``SkillDescriptor`` 是纯领域对象，位于 ``ai_core.domain.skills``；本模块
只负责线程安全注册与查询，属于应用层。
"""

from __future__ import annotations

import threading
from typing import Any

from ai_core.domain.skills import SkillDescriptor, SkillRegistryError

__all__ = ["SkillDescriptor", "SkillRegistry", "SkillRegistryError"]


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
