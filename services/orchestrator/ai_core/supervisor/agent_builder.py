"""AgentBuilder：把一句自然语言需求落成可注册的企业自定义 Agent。

设计目标：
  - **声明式落盘**：只生成 ``config/custom_agents/<agentId>.json``、
    ``prompts/<agentId>.md`` 与 ``knowledge/<agentId>/``，不执行任何模型
    生成的代码，AgentRegistry 重新扫描后即可被 Supervisor 路由。
  - **可离线演示**：不依赖外部模型也能生成结构完整、可读的 System Prompt；
    需求文本不会被 eval / import / 拼接成可执行代码。
  - **路径安全**：所有文件都限制在 ``config/custom_agents`` 目录内，删除
    时也只允许移除该目录内由 Builder 管理的产物。

知识库当前只做“落盘 + 清单”：
  - 用户上传 ``.md`` / ``.txt`` 后写入 ``knowledge/<agentId>/``；
  - Manifest 的 ``ragKnowledgeDir`` 指向该目录，后续 RAG 运行时直接扫描；
  - 上传会同步更新 Manifest 的 ``metadata.knowledgeFiles``，便于审计。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ai_core.config import get_settings
from ai_core.supervisor.custom import (
    AGENT_ID_PATTERN,
    CustomAgentError,
    CustomAgentManifest,
    read_manifest,
)

# 需求文本与知识文件上限：防止误传超大文件把配置目录拖垮。
MAX_REQUIREMENT_CHARS = 4_000
MAX_KNOWLEDGE_BYTES = 2 * 1024 * 1024
MAX_KNOWLEDGE_NAME_CHARS = 120
ALLOWED_KNOWLEDGE_SUFFIXES = {".md", ".txt"}

_AGENT_ID_RE = re.compile(AGENT_ID_PATTERN)
_KNOWLEDGE_NAME_RE = re.compile(
    r"^[\w一-鿿][\w一-鿿.\- ]{0,119}\.(md|txt)$",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,23}|[一-鿿]{2,8}")


class AgentBuilderError(ValueError):
    """Agent 创建 / 删除 / 知识库操作失败。"""

    code = "AGENT_BUILDER_INVALID"
    status_code = 422

    def __init__(self, message: str, *, code: str | None = None, status: int = 422):
        super().__init__(message)
        if code:
            self.code = code
        self.status_code = status


@dataclass(frozen=True)
class BuiltAgent:
    """一次创建操作产生的文件位置，供接口层返回。"""

    manifest: CustomAgentManifest
    manifest_path: Path
    prompt_path: Path
    knowledge_dir: Path


@dataclass(frozen=True)
class KnowledgeDocument:
    """知识库文档的只读描述。"""

    file_name: str
    size_bytes: int
    updated_at: str
    extension: str

    def to_dict(self) -> dict[str, object]:
        return {
            "fileName": self.file_name,
            "sizeBytes": self.size_bytes,
            "updatedAt": self.updated_at,
            "extension": self.extension,
        }


class AgentBuilder:
    """企业自定义 Agent 的创建者与知识库维护者。"""

    def __init__(self, custom_agents_dir: Path | str | None = None) -> None:
        resolved = get_settings()
        base = (
            Path(custom_agents_dir)
            if custom_agents_dir is not None
            else Path(resolved.supervisor_custom_agents_dir)
        )
        self._base = base

    @property
    def custom_agents_dir(self) -> Path:
        return self._base

    # --------------------------------------------------------
    # 创建
    # --------------------------------------------------------
    def create(
        self,
        *,
        requirement: str,
        display_name: str | None = None,
        agent_id: str | None = None,
        task_types: list[str] | None = None,
        keywords: list[str] | None = None,
    ) -> BuiltAgent:
        """根据需求生成 Manifest + System Prompt 并落盘。

        ``agent_id`` 可由调用方指定；未指定时从需求 / 名称推导 ASCII slug，
        中文需求会回退为内容哈希，保证满足 ``^[a-z][a-z0-9_-]{2,63}$``。
        """
        cleaned_requirement = (requirement or "").strip()
        if not 4 <= len(cleaned_requirement) <= MAX_REQUIREMENT_CHARS:
            raise AgentBuilderError(
                f"需求描述长度必须在 4~{MAX_REQUIREMENT_CHARS} 字之间",
                code="AGENT_REQUIREMENT_INVALID",
            )

        resolved_id = self._normalize_agent_id(
            agent_id or display_name or cleaned_requirement
        )
        manifest_path = self._base / f"{resolved_id}.json"
        if manifest_path.exists():
            raise AgentBuilderError(
                f"Agent 已存在：{resolved_id}",
                code="AGENT_ALREADY_EXISTS",
                status=409,
            )

        resolved_name = (display_name or "").strip() or self._derive_display_name(
            cleaned_requirement
        )
        if len(resolved_name) > 128:
            raise AgentBuilderError(
                "Agent 名称不能超过 128 个字符",
                code="AGENT_NAME_INVALID",
            )

        # 能力标识默认使用独立的 taskType，避免多个自定义 Agent 抢占同一路由。
        normalized_task_types = self._clean_list(
            task_types or [f"{resolved_id}_task"], max_items=20, max_len=128
        )
        if not normalized_task_types:
            raise AgentBuilderError(
                "至少需要一个能力标识（taskType）",
                code="AGENT_TASK_TYPE_REQUIRED",
            )

        # 关键词用于 Supervisor 的规则路由：需求原文分词 + 名称，去重后限量。
        normalized_keywords = self._clean_list(
            (keywords or [])
            + self._extract_keywords(cleaned_requirement, resolved_name),
            max_items=50,
            max_len=128,
        )

        prompt_relative = f"prompts/{resolved_id}.md"
        knowledge_relative = f"knowledge/{resolved_id}"
        manifest = CustomAgentManifest(
            agent_id=resolved_id,
            display_name=resolved_name,
            description=self._short_description(cleaned_requirement),
            version="1.0.0",
            task_types=normalized_task_types,
            keywords=normalized_keywords,
            system_prompt_file=prompt_relative,
            rag_knowledge_dir=knowledge_relative,
            enabled=True,
            metadata={
                "builder": "AgentBuilder",
                "createdAt": _utc_now(),
                "requirement": cleaned_requirement,
                "knowledgeFiles": [],
                "owner": "workspace-user",
            },
        )

        prompt_path = self._base / prompt_relative
        knowledge_dir = self._base / knowledge_relative
        # 任何一步失败都由调用方看到异常；已创建的空目录不会注册 Agent，
        # 因为注册只认 Manifest 文件。
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        knowledge_dir.mkdir(parents=True, exist_ok=True)
        _write_text_atomic(prompt_path, self._build_system_prompt(manifest))
        _write_json_atomic(
            manifest_path,
            manifest.model_dump(by_alias=True, exclude_none=True),
        )
        return BuiltAgent(
            manifest=manifest,
            manifest_path=manifest_path,
            prompt_path=prompt_path,
            knowledge_dir=knowledge_dir,
        )

    # --------------------------------------------------------
    # 删除
    # --------------------------------------------------------
    def delete(self, agent_id: str) -> dict[str, object]:
        """删除一个自定义 Agent 及其托管产物。

        内置 Agent 没有 ``custom_agents_dir`` 下的 Manifest，因此天然不可删；
        这里再做一次 id 与路径校验，避免通过 ``../`` 删除宿主机文件。
        """
        manifest = self._load_manifest(agent_id)
        manifest_path = self._base / f"{agent_id}.json"
        # 生产配置里的 SUPERVISOR_CUSTOM_AGENTS_DIR 是相对路径（config/...），
        # 而 _resolve_inside 返回绝对路径；统一用 resolve() 后再计算相对名。
        base = self._base.resolve()
        removed: list[str] = []

        prompt_path = self._resolve_inside(manifest.system_prompt_file)
        knowledge_dir = self._resolve_inside(manifest.rag_knowledge_dir)

        # 先删附属产物，最后删 Manifest：中途失败时 Agent 仍然可见，
        # 用户可重试；不会出现“Manifest 没了但目录残留”的孤儿状态。
        if prompt_path is not None and prompt_path.is_file():
            prompt_path.unlink()
            removed.append(str(prompt_path.relative_to(base)))
        if knowledge_dir is not None and knowledge_dir.is_dir():
            # rmtree 只作用于已经通过 _resolve_inside 校验的目录。
            shutil.rmtree(knowledge_dir)
            removed.append(str(knowledge_dir.relative_to(base)))
        if manifest_path.is_file():
            manifest_path.unlink()
            removed.append(manifest_path.name)

        return {
            "agentId": agent_id,
            "removed": removed,
        }

    # --------------------------------------------------------
    # 知识库
    # --------------------------------------------------------
    def get_knowledge_dir(self, agent_id: str) -> str | None:
        """返回 Manifest 声明的知识目录（相对 custom_agents 的路径）。"""
        return self._load_manifest(agent_id).rag_knowledge_dir

    def list_knowledge(self, agent_id: str) -> list[KnowledgeDocument]:
        """列出该 Agent 知识目录下的 Markdown / 文本文件。"""
        manifest = self._load_manifest(agent_id)
        directory = self._resolve_inside(manifest.rag_knowledge_dir)
        if directory is None or not directory.is_dir():
            return []
        documents: list[KnowledgeDocument] = []
        for path in sorted(directory.iterdir()):
            if (
                not path.is_file()
                or path.suffix.lower() not in ALLOWED_KNOWLEDGE_SUFFIXES
            ):
                continue
            stat = path.stat()
            documents.append(
                KnowledgeDocument(
                    file_name=path.name,
                    size_bytes=stat.st_size,
                    updated_at=_utc_from_timestamp(stat.st_mtime),
                    extension=path.suffix.lower(),
                )
            )
        return documents

    def save_knowledge(
        self,
        agent_id: str,
        file_name: str,
        content: str,
    ) -> KnowledgeDocument:
        """把上传的文本写入知识目录，并同步 Manifest 元数据。

        当前只落盘，不做向量化；后续 RAG 运行时按 ``ragKnowledgeDir``
        扫描目录即可。文件名只允许单层 basename，防止路径穿越。
        """
        manifest = self._load_manifest(agent_id)
        safe_name = self._validate_knowledge_name(file_name)
        payload = (content or "").encode("utf-8")
        if not payload:
            raise AgentBuilderError(
                "知识文件内容为空",
                code="AGENT_KNOWLEDGE_EMPTY",
            )
        if len(payload) > MAX_KNOWLEDGE_BYTES:
            raise AgentBuilderError(
                f"知识文件不能超过 {MAX_KNOWLEDGE_BYTES // 1024 // 1024} MB",
                code="AGENT_KNOWLEDGE_TOO_LARGE",
                status=413,
            )

        directory = self._resolve_inside(manifest.rag_knowledge_dir)
        if directory is None:
            raise AgentBuilderError(
                "Manifest 的 ragKnowledgeDir 不在 custom_agents 目录内",
                code="AGENT_KNOWLEDGE_PATH_INVALID",
            )
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / safe_name
        _write_bytes_atomic(target, payload)

        # 记录文件清单，方便审计与后续 RAG 增量索引；以磁盘内容为准。
        files = sorted(
            item.name
            for item in directory.iterdir()
            if item.is_file() and item.suffix.lower() in ALLOWED_KNOWLEDGE_SUFFIXES
        )
        manifest.metadata["knowledgeFiles"] = files
        manifest.metadata["knowledgeUpdatedAt"] = _utc_now()
        _write_json_atomic(
            self._base / f"{agent_id}.json",
            manifest.model_dump(by_alias=True, exclude_none=True),
        )
        stat = target.stat()
        return KnowledgeDocument(
            file_name=target.name,
            size_bytes=stat.st_size,
            updated_at=_utc_from_timestamp(stat.st_mtime),
            extension=target.suffix.lower(),
        )

    def delete_knowledge(self, agent_id: str, file_name: str) -> dict[str, object]:
        """删除单个知识文件；文件名同样只允许 basename。"""
        manifest = self._load_manifest(agent_id)
        safe_name = self._validate_knowledge_name(file_name)
        directory = self._resolve_inside(manifest.rag_knowledge_dir)
        if directory is None:
            raise AgentBuilderError(
                "Manifest 的 ragKnowledgeDir 不在 custom_agents 目录内",
                code="AGENT_KNOWLEDGE_PATH_INVALID",
            )
        target = directory / safe_name
        if not target.is_file():
            raise AgentBuilderError(
                f"知识文件不存在：{safe_name}",
                code="AGENT_KNOWLEDGE_NOT_FOUND",
                status=404,
            )
        target.unlink()

        files = sorted(
            item.name
            for item in directory.iterdir()
            if item.is_file() and item.suffix.lower() in ALLOWED_KNOWLEDGE_SUFFIXES
        )
        manifest.metadata["knowledgeFiles"] = files
        manifest.metadata["knowledgeUpdatedAt"] = _utc_now()
        _write_json_atomic(
            self._base / f"{agent_id}.json",
            manifest.model_dump(by_alias=True, exclude_none=True),
        )
        return {"agentId": agent_id, "removed": safe_name}

    # --------------------------------------------------------
    # 内部工具
    # --------------------------------------------------------
    def _load_manifest(self, agent_id: str) -> CustomAgentManifest:
        """只接受合法的自定义 Agent id，并确认 Manifest 位于受管目录。"""
        if not isinstance(agent_id, str) or not _AGENT_ID_RE.match(agent_id):
            raise AgentBuilderError(
                f"Agent id 不合法：{agent_id}",
                code="AGENT_ID_INVALID",
                status=400,
            )
        path = self._base / f"{agent_id}.json"
        if not path.is_file():
            raise AgentBuilderError(
                f"自定义 Agent 不存在：{agent_id}",
                code="AGENT_NOT_FOUND",
                status=404,
            )
        try:
            return read_manifest(path)
        except CustomAgentError as exc:
            raise AgentBuilderError(
                f"Agent Manifest 无法读取：{exc}",
                code="AGENT_MANIFEST_INVALID",
            ) from exc

    def _resolve_inside(self, relative: str | None) -> Path | None:
        """把 Manifest 中的相对路径解析到受管目录内；越界返回 None。"""
        if not relative:
            return None
        candidate = (self._base / relative).resolve()
        base = self._base.resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            return None
        return candidate

    @staticmethod
    def _normalize_agent_id(value: str) -> str:
        """生成满足 Agent id 正则的 ASCII slug。

        仅保留 a-z0-9_-；中文 / 纯符号需求回退为内容哈希，既稳定又不会
        把非 ASCII 字符写进文件名。
        """
        raw = (value or "").strip().lower()
        slug = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
        slug = re.sub(r"_+", "_", slug)
        if not slug or not slug[0].isalpha():
            digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
            slug = f"agent_{digest}"
        if len(slug) < 3:
            slug = f"agent_{slug}"
        if not slug.startswith("custom_"):
            slug = f"custom_{slug}"
        slug = slug[:63].rstrip("_-")
        if not _AGENT_ID_RE.match(slug):
            raise AgentBuilderError(
                f"无法生成合法的 Agent id：{value}",
                code="AGENT_ID_INVALID",
            )
        return slug

    @staticmethod
    def _derive_display_name(requirement: str) -> str:
        """从需求首行推导展示名；去掉常见客套话与 Markdown 标记。"""
        first_line = requirement.strip().splitlines()[0]
        cleaned = re.sub(r"^[#>*\-\s]+", "", first_line).strip()
        cleaned = re.sub(r"^(请|帮我|我想要|想要|创建|新建|生成)+", "", cleaned)
        cleaned = cleaned.strip("，。,.!！?？:：;； ")
        return (cleaned[:32] or "自定义 Agent").strip()

    @staticmethod
    def _short_description(requirement: str) -> str:
        compact = re.sub(r"\s+", " ", requirement).strip()
        return compact[:500]

    @staticmethod
    def _extract_keywords(requirement: str, display_name: str) -> list[str]:
        """轻量关键词抽取：中英文短词 + 名称。

        不引入分词依赖；规则路由只要求“命中即可”，因此 2~8 字的中文
        词块与英文标识符已经足够。真正的语义路由仍由 LLM 分类器负责。
        """
        candidates: list[str] = [display_name]
        candidates.extend(_WORD_RE.findall(requirement))
        return candidates

    @staticmethod
    def _clean_list(
        values: list[str],
        *,
        max_items: int,
        max_len: int,
    ) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = str(raw).strip()
            if not value or len(value) > max_len or value in seen:
                continue
            seen.add(value)
            result.append(value)
            if len(result) >= max_items:
                break
        return result

    @staticmethod
    def _validate_knowledge_name(file_name: str) -> str:
        """只允许单层文件名 + .md/.txt，拒绝路径分隔符与控制字符。"""
        raw = (file_name or "").strip()
        if (
            not raw
            or len(raw) > MAX_KNOWLEDGE_NAME_CHARS
            or "/" in raw
            or "\\" in raw
            or raw.startswith(".")
            or not _KNOWLEDGE_NAME_RE.match(raw)
        ):
            raise AgentBuilderError(
                "知识文件名不合法：只支持单层 .md / .txt 文件名",
                code="AGENT_KNOWLEDGE_NAME_INVALID",
                status=400,
            )
        return raw

    @staticmethod
    def _build_system_prompt(manifest: CustomAgentManifest) -> str:
        """生成确定性的 System Prompt；不依赖外部模型，离线可用。"""
        requirement = str(manifest.metadata.get("requirement") or manifest.description)
        return f"""# 角色

你是「{manifest.display_name}」，由企业用户在工作台内创建的自定义 Agent。
你的能力标识为 `{manifest.task_types[0]}`，每次任务都应围绕这一专长交付结果。

## 用户需求

{requirement}

## 工作准则

1. **先澄清再执行**：如果需求缺少输入、数据来源、输出格式或验收标准，
   先用不超过 3 个问题澄清；不要臆造业务规则。
2. **知识库优先**：检索 `{manifest.rag_knowledge_dir or "knowledge"}`
   下的 Markdown / 文本资料；答案中引用文件时标注来源文件名。
3. **结构化输出**：默认使用 Markdown；需要机器消费时输出合法 JSON，
   字段名与调用方约定保持一致。
4. **不越权**：只处理与上述需求相关的任务；不执行文件删除、系统命令、
   密钥读取或任何未授权操作。
5. **可验证**：给出结论时同时给出依据、假设与风险；无法确认的内容
   明确标注“待确认”，禁止编造事实。

## 交付格式

- 结论 / 结果
- 依据与步骤
- 风险与待确认项
"""


# ------------------------------------------------------------
# 原子写入：避免进程中断留下半个 JSON / Markdown
# ------------------------------------------------------------
def _write_text_atomic(path: Path, content: str) -> None:
    _write_bytes_atomic(path, content.encode("utf-8"))


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    _write_bytes_atomic(path, text.encode("utf-8"))


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_bytes(payload)
    temp.replace(path)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _utc_from_timestamp(value: float) -> str:
    return (
        datetime.fromtimestamp(value, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "AgentBuilder",
    "AgentBuilderError",
    "BuiltAgent",
    "KnowledgeDocument",
]
