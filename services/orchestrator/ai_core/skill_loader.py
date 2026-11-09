"""声明式 Skill 文件加载。

一个 Skill = 领域 System Prompt + 可选 Tool 子集 + 可选 RAG 引用。
与 Agent Manifest 不同，Skill 文件可以携带实际 Prompt；加载时只读取
配置目录内的文本，绝不执行代码，也不从网络拉取内容。

支持两种格式：
  - ``<dir>/*.skill.json``：结构化 JSON，字段支持 camelCase / snake_case；
  - ``<dir>/*.skill.md``  ：YAML frontmatter + Markdown 正文，正文即 Prompt。
    frontmatter 示例::

        ---
        skillId: ui.accessibility
        name: W3C 无障碍规范
        agentId: ui_graph
        priority: 30
        ---
        Must follow WCAG 2.1 ...
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

MAX_SKILL_FILE_BYTES = 128 * 1024
MAX_SYSTEM_PROMPT_CHARS = 24_000
_MARKDOWN_FRONTMATTER = re.compile(
    r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)",
    re.DOTALL,
)


class SkillFileError(ValueError):
    """Skill 文件不合法。"""

    code = "SKILL_FILE_INVALID"


class SkillFile(BaseModel):
    """``*.skill.json`` / ``*.skill.md`` frontmatter 的强类型契约。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    skill_id: str = Field(alias="skillId", min_length=1, max_length=96)
    name: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(alias="agentId", min_length=1, max_length=64)
    description: str = Field(default="", max_length=2000)
    version: str = Field(default="1.0.0", max_length=32)
    enabled: bool = True
    priority: int = Field(default=0, ge=0, le=1000)
    tools: list[str] = Field(default_factory=list, max_length=50)
    keywords: list[str] = Field(default_factory=list, max_length=100)
    system_prompt: str = Field(default="", alias="systemPrompt", max_length=24_000)
    system_prompt_file: str | None = Field(
        default=None,
        alias="systemPromptFile",
        max_length=512,
    )
    rag_refs: list[str] = Field(default_factory=list, alias="ragRefs", max_length=20)

    @field_validator("tools", "keywords", "rag_refs")
    @classmethod
    def _clean_list(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = str(raw).strip()
            if value and value not in seen:
                seen.add(value)
                result.append(value)
        return result


def _read_prompt(base: Path, payload: SkillFile) -> str:
    prompt = payload.system_prompt.strip()
    if payload.system_prompt_file:
        target = (base / payload.system_prompt_file).resolve()
        # 防止 skill.json 通过 ../../ 读取宿主机任意文件。
        try:
            target.relative_to(base.resolve())
        except ValueError as exc:
            raise SkillFileError("systemPromptFile 必须位于 Skill 目录内") from exc
        if not target.is_file():
            raise SkillFileError(
                f"Skill Prompt 文件不存在: {payload.system_prompt_file}"
            )
        if target.stat().st_size > MAX_SKILL_FILE_BYTES:
            raise SkillFileError("Skill Prompt 文件超过大小限制")
        prompt = target.read_text(encoding="utf-8").strip()
    if len(prompt) > MAX_SYSTEM_PROMPT_CHARS:
        raise SkillFileError("Skill System Prompt 超过长度限制")
    return prompt


def _payload_from_markdown(path: Path, text: str) -> SkillFile:
    """解析 YAML frontmatter + Markdown 正文。

    正文被视为 System Prompt；frontmatter 只负责元数据。这样领域规范可以
    用 Markdown 维护（标题、列表、代码块），而不是塞进 JSON 字符串。
    """
    match = _MARKDOWN_FRONTMATTER.match(text)
    if not match:
        raise SkillFileError(f"Markdown Skill 缺少 YAML frontmatter: {path.name}")
    try:
        metadata = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillFileError(
            f"Markdown Skill frontmatter 解析失败 {path.name}: {exc}"
        ) from exc
    if not isinstance(metadata, dict):
        raise SkillFileError(f"Markdown Skill frontmatter 必须是对象: {path.name}")
    body = text[match.end() :].strip()
    if not body:
        raise SkillFileError(f"Markdown Skill 正文不能为空: {path.name}")
    payload = dict(metadata)
    # 正文优先于 frontmatter 中的 systemPrompt，避免出现两份 Prompt。
    payload["systemPrompt"] = body
    return SkillFile.model_validate(payload)


def _descriptor_from_payload(
    path: Path,
    payload: SkillFile,
    *,
    source: str,
) -> SkillDescriptor:
    from ai_core.supervisor.skills import SkillDescriptor

    if not payload.enabled:
        raise SkillFileError(f"Skill 已禁用: {payload.skill_id}")

    return SkillDescriptor(
        skill_id=payload.skill_id,
        name=payload.name,
        agent_id=payload.agent_id,
        description=payload.description,
        prompt_ref=path.name,
        system_prompt=_read_prompt(path.parent, payload) or None,
        rag_refs=tuple(payload.rag_refs),
        tools=tuple(payload.tools),
        metadata={
            "version": payload.version,
            "priority": payload.priority,
            "keywords": payload.keywords,
            "source": source,
            "stage": "M3",
        },
    )


def read_skill_file(path: Path) -> SkillDescriptor:
    """读取并校验单个 Skill 文件（JSON 或 Markdown），转为注册中心描述符。

    说明：``SkillDescriptor`` 在函数内延迟导入。它定义于
    ``supervisor.skills``，而 ``supervisor`` 包的 ``__init__`` 会加载
    Graph/Registry，后者又依赖本模块；顶层导入会形成循环引用。
    """
    try:
        if path.stat().st_size > MAX_SKILL_FILE_BYTES:
            raise SkillFileError(f"Skill 文件超过大小限制: {path.name}")
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".md":
            payload = _payload_from_markdown(path, text)
            source = "skill_markdown"
        else:
            try:
                payload = SkillFile.model_validate_json(text)
            except Exception as exc:  # noqa: BLE001 - Pydantic 错误统一包装
                raise SkillFileError(f"Skill 文件解析失败 {path.name}: {exc}") from exc
            source = "skill_file"
    except SkillFileError:
        raise
    except Exception as exc:  # noqa: BLE001 - IO / 解析统一包装
        raise SkillFileError(f"Skill 文件解析失败 {path.name}: {exc}") from exc

    return _descriptor_from_payload(path, payload, source=source)


def load_skill_directory(directory: Path | str) -> list[SkillDescriptor]:
    """扫描目录，单个坏文件只记录 warning，不阻断其它 Skill。"""
    base = Path(directory)
    if not base.exists() or not base.is_dir():
        return []
    skills: list[SkillDescriptor] = []
    paths = sorted(base.glob("*.skill.json")) + sorted(base.glob("*.skill.md"))
    for path in paths:
        try:
            skills.append(read_skill_file(path))
        except SkillFileError as exc:
            logger.warning("skip invalid skill file %s: %s", path, exc)
    return skills


def load_agent_skill_prompts(
    agent_id: str,
    directory: Path | str,
) -> list[str]:
    """返回某个 Agent 已启用的 Skill Prompt，按 priority 升序拼接。"""
    items = [
        skill
        for skill in load_skill_directory(directory)
        if skill.agent_id == agent_id and skill.system_prompt
    ]
    items.sort(key=lambda item: int(item.metadata.get("priority", 0)))
    return [item.system_prompt for item in items if item.system_prompt]


def skill_prompts_to_system(agent_id: str, directory: Path | str) -> str:
    """把 Skill Prompt 合并为一段可追加到 System Message 的文本。"""
    prompts = load_agent_skill_prompts(agent_id, directory)
    if not prompts:
        return ""
    body = "\n\n".join(prompts)
    return (
        "\n\n以下是本 Agent 挂载的领域 Skill，必须在不违背基础规则的前提下执行：\n"
        f"{body}"
    )
