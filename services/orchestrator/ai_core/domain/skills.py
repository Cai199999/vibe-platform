"""Skill（领域技能）的纯领域描述。

Skill = Prompt + RAG 知识 + Tool 子集。本模块只描述“技能是什么”，
不负责线程安全注册、文件读取或运行时注入；这些属于应用层与适配层。
"""

from __future__ import annotations

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
    # 实际注入模型的系统提示词。只在服务端内存中使用，绝不出现在
    # /supervisor/agents 等公开响应里。
    system_prompt: str | None = None
    rag_refs: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_view(self) -> dict[str, Any]:
        """转为可对外展示的只读视图，不泄露 system_prompt。"""
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


__all__ = ["SkillDescriptor", "SkillRegistryError"]
