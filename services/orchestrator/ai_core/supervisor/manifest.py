"""声明式 Agent Manifest。

每个专家 Agent 在自己的代码目录放置 ``manifest.json``，由 AgentRegistry
启动时扫描。Manifest 只描述能力，不执行代码；Agent 的实现仍由代码提供。

字段约定（snake_case）：
  - ``agent_id`` / ``name`` / ``description`` / ``version``
  - ``capabilities``：能力列表，``id`` 同时作为路由 taskType
  - ``supported_tools``：该 Agent 可使用的底层工具名
  - ``input_schema`` / ``output_schema``：JSON Schema
  - ``skills``：预留的 Skill 挂载示例（Prompt + RAG + Tool 子集）
  - ``keywords`` / ``stream_path_template`` / ``status``：路由与展示元数据

安全边界与 CustomAgent Manifest 一致：只解析 JSON，不 import、不执行、
不加载 Prompt / RAG 内容。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AGENT_ID_PATTERN = r"^[a-z][a-z0-9_-]{2,63}$"
MAX_AGENT_MANIFEST_BYTES = 256 * 1024


class AgentManifestError(ValueError):
    """Agent Manifest 不合法。"""

    code = "AGENT_MANIFEST_INVALID"


class AgentCapability(BaseModel):
    """一项可路由能力；``id`` 会被用作 ``taskType``。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)


class AgentSkill(BaseModel):
    """Skill = 领域 Prompt + RAG 知识 + Tool 子集的声明式封装。

    当前只做注册与展示，不执行；M3 由 SkillRegistry 动态拼装进 Agent。
    """

    model_config = ConfigDict(extra="allow")

    skill_id: str = Field(min_length=1, max_length=96)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1000)
    prompt_ref: str | None = Field(default=None, max_length=512)
    rag_refs: list[str] = Field(default_factory=list, max_length=20)
    tools: list[str] = Field(default_factory=list, max_length=50)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("rag_refs", "tools")
    @classmethod
    def _clean_list(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = str(raw).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result


class AgentManifest(BaseModel):
    """一个专家 Agent 的静态声明。"""

    model_config = ConfigDict(extra="allow")

    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    version: str = Field(default="1.0.0", max_length=32)
    status: Literal["available", "reserved"] = "available"
    graph: str = Field(min_length=1, max_length=128)
    capabilities: list[AgentCapability] = Field(min_length=1, max_length=20)
    supported_tools: list[str] = Field(default_factory=list, max_length=100)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    skills: list[AgentSkill] = Field(default_factory=list, max_length=50)
    keywords: list[str] = Field(default_factory=list, max_length=100)
    stream_path_template: str | None = Field(default=None, max_length=512)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("supported_tools", "keywords")
    @classmethod
    def _clean_strings(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = str(raw).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    @field_validator("input_schema", "output_schema")
    @classmethod
    def _require_object_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value and value.get("type") != "object":
            raise ValueError("input_schema / output_schema 顶层 type 必须为 object")
        return value

    @model_validator(mode="after")
    def _check_unique_ids(self) -> "AgentManifest":
        capability_ids = [item.id for item in self.capabilities]
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("capabilities.id 不允许重复")
        skill_ids = [item.skill_id for item in self.skills]
        if len(skill_ids) != len(set(skill_ids)):
            raise ValueError("skills.skill_id 不允许重复")
        return self

    @property
    def task_types(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.capabilities)


def read_agent_manifest(path: Path) -> AgentManifest:
    """读取并校验单个 Agent Manifest，统一包装异常。"""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise AgentManifestError(f"无法读取 Agent Manifest: {path.name}") from exc
    if size > MAX_AGENT_MANIFEST_BYTES:
        raise AgentManifestError(
            f"Agent Manifest 超过 {MAX_AGENT_MANIFEST_BYTES} 字节上限: {path.name}"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentManifestError(f"Agent Manifest 解析失败: {path.name}") from exc
    if not isinstance(payload, dict):
        raise AgentManifestError(f"Agent Manifest 根节点必须是对象: {path.name}")
    try:
        return AgentManifest.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - Pydantic 错误统一包装
        raise AgentManifestError(
            f"Agent Manifest 字段校验失败: {path.name}: {exc}"
        ) from exc


def discover_agent_manifests(root: Path | str) -> list[tuple[Path, AgentManifest]]:
    """扫描 ``<root>/*/manifest.json``。

    约定：每个专家 Agent 独占一个顶层代码目录，Manifest 放在目录根部。
    非法文件由调用方逐条记录 warning，不阻断整个注册中心。
    """
    base = Path(root)
    if not base.exists() or not base.is_dir():
        return []
    discovered: list[tuple[Path, AgentManifest]] = []
    for path in sorted(base.glob("*/manifest.json")):
        discovered.append((path, read_agent_manifest(path)))
    return discovered
