"""企业自定义 Agent 的受限 Manifest 加载。

安全原则：
  - 只读取 JSON，不 import、不执行、不动态 eval；
  - 提示词与 RAG 目录只能是 Manifest 所在目录内的相对路径；
  - Manifest 体积、字段数量受限，避免启动时被超大文件拖垮。

真正的 CustomAgent 运行时会话（上传 Prompt / 构建私有 RAG / 沙盒执行）
在后续阶段接入；本模块先固定注册与路由契约。
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

from pydantic import Field, field_validator

from ai_core.agent.schemas import CamelModel

MAX_MANIFEST_BYTES = 256 * 1024
AGENT_ID_PATTERN = r"^[a-z][a-z0-9_-]{2,63}$"


class CustomAgentError(ValueError):
    """Manifest 不合法或路径不安全。"""

    code = "CUSTOM_AGENT_MANIFEST_INVALID"


class CustomAgentManifest(CamelModel):
    """企业自定义 Agent 的声明文件。

    ``systemPromptFile`` 与 ``ragKnowledgeDir`` 只做路径安全校验，不在
    注册阶段读取内容；未来由沙盒化 CustomAgent Runtime 消费。
    """

    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    version: str = Field(default="1.0.0", max_length=32)
    task_types: list[str] = Field(min_length=1, max_length=20)
    keywords: list[str] = Field(default_factory=list, max_length=50)
    system_prompt_file: str | None = Field(default=None, max_length=512)
    rag_knowledge_dir: str | None = Field(default=None, max_length=512)
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("task_types", "keywords")
    @classmethod
    def _clean_strings(cls, values: list[str]) -> list[str]:
        """去空、去重并限制单个词长度，避免脏数据进入路由匹配。"""
        result: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = str(raw).strip()
            if not value or len(value) > 128:
                continue
            if value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    @field_validator("system_prompt_file", "rag_knowledge_dir")
    @classmethod
    def _validate_relative_path(cls, value: str | None) -> str | None:
        return validate_relative_path(value)


def validate_relative_path(value: str | None) -> str | None:
    """只允许 Manifest 目录内的相对路径。

    同时拒绝 Windows 盘符、反斜杠、绝对路径与 ``..``，避免跨目录读取。
    """
    if value is None or not value.strip():
        return None
    raw = value.strip()
    if "\\" in raw:
        raise CustomAgentError("自定义 Agent 路径不允许使用反斜杠")
    path = PurePosixPath(raw)
    if path.is_absolute():
        raise CustomAgentError("自定义 Agent 路径必须是相对路径")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise CustomAgentError("自定义 Agent 路径不允许包含 . 或 ..")
    return raw


@runtime_checkable
class CustomAgent(Protocol):
    """未来企业专属 Agent 的运行时协议。

    当前只预留接口：Supervisor 完成 ``handoff_custom`` 后，由后续的
    CustomAgent Runtime 在隔离沙盒中加载 Prompt / RAG 并执行。
    """

    @property
    def descriptor(self) -> Any:  # noqa: ANN401 - 避免循环导入
        """返回注册中心中的 AgentDescriptor。"""

    def handle(self, task: str, context: dict[str, Any]) -> dict[str, Any]:
        """执行一次任务，返回结构化 AgentResult。"""


def read_manifest(path: Path) -> CustomAgentManifest:
    """读取并校验单个 Manifest；错误统一转成 CustomAgentError。"""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CustomAgentError(f"无法读取 Manifest: {path.name}") from exc
    if size > MAX_MANIFEST_BYTES:
        raise CustomAgentError(
            f"Manifest 超过 {MAX_MANIFEST_BYTES} 字节上限: {path.name}"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CustomAgentError(f"Manifest JSON 解析失败: {path.name}") from exc
    if not isinstance(payload, dict):
        raise CustomAgentError(f"Manifest 根节点必须是对象: {path.name}")
    try:
        manifest = CustomAgentManifest.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - Pydantic 错误统一包装
        raise CustomAgentError(f"Manifest 字段校验失败: {path.name}: {exc}") from exc
    if not manifest.task_types:
        raise CustomAgentError(f"Manifest 至少需要一个 taskType: {path.name}")
    return manifest
