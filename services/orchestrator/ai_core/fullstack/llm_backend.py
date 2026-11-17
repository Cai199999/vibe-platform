"""带 Applier 诊断反馈的真实 LLM BackendCoder 修复器。

责任边界：

  - 确定性模板仍是默认生成路径，低延迟、可离线验证；
  - 只有当 Applier AST 门禁判定为 ``CODE_DEFECT``，或一致性检查明确
    要求重建 JOIN 时，才把模板产物交给 LLM；
  - LLM 只能修改 ``server/index.js``，禁止触碰 DDL / ORM / 迁移 /
    路由清单。数据库结构事实永远由 DBACoder 与契约持有；
  - LLM 输出仍然经过 Applier 的 Babel AST 门禁，不存在“LLM 生成即豁免”。

Prompt 中必须包含完整的失败文件与 Applier 诊断（字段不存在、JOIN 别名
不一致等），并要求模型输出完整文件而不是 diff。模型返回非法 JSON、越权
文件路径或空文件时直接失败，由编排层回退到模板重试。
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
from typing import Any, Mapping, Sequence

from ai_core.config import Settings, get_settings
from ai_core.domain.architect import ArchitectContract
from ai_core.fullstack.coders import (
    GeneratedBundle,
    collection_path,
    core_table_for_contract,
)
from ai_core.ports.model import ModelClient, ModelClientError

logger = logging.getLogger(__name__)

# LLM 只能修复 HTTP 编排层；持久化层与迁移必须保持 DBACoder 的权威产物。
REPAIRABLE_FILES = ("server/index.js",)
# 单文件上限：防止模型输出被截断后仍被当成“完整文件”落盘。
MAX_REPAIR_FILE_BYTES = 512_000
MAX_PROMPT_CHARS = 80_000

_REPAIR_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "files": {
            "type": "object",
            "description": "path -> 完整文件源码；当前只允许 server/index.js",
            "additionalProperties": {"type": "string"},
        },
        "reasoning": {
            "type": "string",
            "description": "一句话说明修复了哪个诊断",
        },
    },
    "required": ["files"],
}


class LLMBackendRepairError(RuntimeError):
    """LLM 修复不可用或输出不合法。"""


class LLMBackendRepairUnavailable(LLMBackendRepairError):
    """缺少模型客户端 / 功能被关闭；调用方应回退模板。"""


class LLMBackendCoder:
    """把 Applier 诊断回传给模型，要求输出完整的 ``server/index.js``。"""

    def __init__(
        self,
        settings: Settings | None = None,
        client: ModelClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client

    @property
    def available(self) -> bool:
        if not self.settings.fullstack_llm_repair_enabled:
            return False
        if self._client is not None:
            return True
        return bool(self.settings.deepseek_api_key)

    def _ensure_client(self) -> ModelClient:
        if self._client is not None:
            return self._client
        if not self.settings.deepseek_api_key:
            raise LLMBackendRepairUnavailable(
                "DEEPSEEK_API_KEY 未配置，无法调用 LLM BackendCoder"
            )
        from ai_core.adapters.model.deepseek import DeepSeekClient

        self._client = DeepSeekClient(self.settings)
        return self._client

    def repair(
        self,
        *,
        contract: ArchitectContract,
        bundle: GeneratedBundle,
        dba_schema: str,
        diagnostics: Sequence[str],
        attempt: int = 1,
        max_attempts: int = 1,
    ) -> GeneratedBundle:
        """基于 Applier 诊断修复模板产物。

        返回新的 ``GeneratedBundle``；失败抛 ``LLMBackendRepairError``，
        绝不返回“半修复”的文件集。调用方负责回退到模板产物。
        """
        if not self.available:
            raise LLMBackendRepairUnavailable("LLM BackendCoder 修复未启用")
        current = {
            path: bundle.files[path]
            for path in REPAIRABLE_FILES
            if isinstance(bundle.files.get(path), str)
        }
        if "server/index.js" not in current:
            raise LLMBackendRepairError(
                "模板产物缺少 server/index.js，无法交给 LLM 修复"
            )
        if not any(item.strip() for item in diagnostics):
            raise LLMBackendRepairError("缺少 Applier 诊断，拒绝无依据重写")

        system = self._system_prompt()
        human = self._human_prompt(
            contract=contract,
            current=current,
            dba_schema=dba_schema,
            diagnostics=diagnostics,
            attempt=attempt,
            max_attempts=max_attempts,
        )
        try:
            raw = self._ensure_client().generate_json(
                "coder",
                system,
                human,
                json_schema=_REPAIR_JSON_SCHEMA,
            )
        except ModelClientError as exc:
            raise LLMBackendRepairError(f"LLM BackendCoder 调用失败: {exc}") from exc

        files = _parse_repair_payload(raw, allowed=set(current))
        merged = dict(bundle.files)
        merged.update(files)
        metadata = dict(bundle.metadata)
        metadata["llmRepair"] = {
            "used": True,
            "attempt": attempt,
            "maxAttempts": max_attempts,
            "files": sorted(files),
            "diagnostics": [str(item) for item in diagnostics if str(item).strip()],
        }
        logger.info(
            "LLM BackendCoder 修复完成 attempt=%d/%d files=%s",
            attempt,
            max_attempts,
            ",".join(sorted(files)),
        )
        return GeneratedBundle(
            files=merged,
            summary=(
                f"LLM BackendCoder 按 {len(diagnostics)} 条 Applier 诊断修复 "
                f"{len(files)} 个文件；" + bundle.summary
            ),
            metadata=metadata,
        )

    # --------------------------------------------------------
    # Prompt
    # --------------------------------------------------------
    @staticmethod
    def _system_prompt() -> str:
        return (
            "你是 Vibe 平台的 LLM BackendCoder 修复器。确定性模板生成的 "
            "Express + Sequelize 代码被 AST 门禁拦截，你必须根据诊断修复 "
            "server/index.js。\n\n"
            "【输出铁律】\n"
            "- 只输出一个 JSON 对象，不要 Markdown 围栏、解释或 diff：\n"
            '  {"files":{"server/index.js":"完整文件源码"},"reasoning":"一句话"}\n'
            "- files 的每个值必须是**完整文件内容**，禁止省略号、"
            "“其余不变”、片段或补丁；\n"
            "- 当前只允许修改 server/index.js。禁止输出 server/db.js、"
            "server/migrations/**、server/schema.core.json、"
            "server/api-routes.json、package.json 等文件；\n"
            "- 禁止修改契约中的表名、列名、路径、HTTP 方法与外键字段；\n"
            "- 禁止新增契约未声明的路由，禁止删除已有健康检查与通用 CRUD；\n"
            "- 必须保留 ES Module 语法（import / export）与 Node 22 兼容性。\n\n"
            "【全局工程规范】\n"
            "- DDL 禁止物理外键；JOIN 只使用 relations 中的 <parent>_id 字段；\n"
            "- 后端按 route / service / repository / dto 分层，禁止业务逻辑写进路由；\n"
            "- 依赖单向，禁止循环依赖与巨石文件；\n"
            "- 方案文档引用 03-数据库设计文档 / 04-API接口设计清单 / "
            "05-最终设计方案与开发规范。\n\n"
            "【修复原则】\n"
            "- 严格以 schema.core.json 与 DDL 为唯一事实来源；\n"
            "- JOIN 报错时，按 relations 修正 Sequelize include 的 as 别名、"
            "外键字段与嵌套路由路径；\n"
            "- 字段不存在时，只使用 schema.core.json 中真实存在的列；\n"
            "- 修完后自查：所有 include 别名、嵌套路由路径都能在 relations "
            "中找到对应项。"
        )

    @staticmethod
    def _human_prompt(
        *,
        contract: ArchitectContract,
        current: Mapping[str, str],
        dba_schema: str,
        diagnostics: Sequence[str],
        attempt: int,
        max_attempts: int,
    ) -> str:
        core = core_table_for_contract(contract)
        routes = [
            {
                "method": str(route.method or "GET").upper(),
                "path": str(route.path or ""),
            }
            for route in contract.api_routes or []
        ]
        relations = [
            relation.model_dump(mode="json", by_alias=True)
            for relation in contract.relations or []
        ]
        diagnostic_lines = "\n".join(
            f"{index}. {str(item).strip()}"
            for index, item in enumerate(diagnostics, start=1)
            if str(item).strip()
        )
        current_text = current["server/index.js"]
        truncated = len(current_text) > MAX_PROMPT_CHARS
        if truncated:
            current_text = (
                current_text[:MAX_PROMPT_CHARS]
                + "\n... [当前文件过长，已截断；仍需输出完整文件]"
            )
        human = (
            f"## 修复轮次\n第 {attempt}/{max_attempts} 次 LLM 修复\n\n"
            f"## 核心表\n{core.name}\n\n"
            f"## 契约集合路径\n{collection_path(contract.api_routes or [])}\n\n"
            f"## 契约路由\n{json.dumps(routes, ensure_ascii=False, indent=2)}\n\n"
            f"## 显式 relations\n"
            f"{json.dumps(relations, ensure_ascii=False, indent=2)}\n\n"
            f"## databaseDdl\n```sql\n{contract.database_ddl or '（空）'}\n```\n\n"
            f"## schema.core.json（DBACoder 权威输出）\n```json\n"
            f"{dba_schema}\n```\n\n"
            f"## Applier AST 门禁诊断\n{diagnostic_lines}\n\n"
            f"## 当前 server/index.js（需要修复的完整文件）\n"
            f"```javascript\n{current_text}\n```\n\n"
            "请修复诊断指出的全部错误，并输出完整的 server/index.js。"
        )
        return human


def _parse_repair_payload(raw: str, *, allowed: set[str]) -> dict[str, str]:
    """解析并校验模型输出；任何越权路径或空内容都视为失败。"""
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise LLMBackendRepairError("LLM 未返回 JSON 对象") from None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMBackendRepairError(f"LLM JSON 解析失败: {exc}") from exc
    if not isinstance(payload, dict):
        raise LLMBackendRepairError("LLM JSON 顶层必须是对象")
    raw_files = payload.get("files")
    if not isinstance(raw_files, dict) or not raw_files:
        raise LLMBackendRepairError("LLM 输出缺少非空 files 对象")

    files: dict[str, str] = {}
    for path, content in raw_files.items():
        normalized = posixpath.normpath(str(path or "").replace("\\", "/"))
        if normalized not in allowed:
            raise LLMBackendRepairError(f"LLM 试图修改未授权文件: {path}")
        if not isinstance(content, str) or not content.strip():
            raise LLMBackendRepairError(f"LLM 输出文件内容为空: {normalized}")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_REPAIR_FILE_BYTES:
            raise LLMBackendRepairError(
                f"LLM 输出文件超过 {MAX_REPAIR_FILE_BYTES} 字节: {normalized}"
            )
        # 最小语法自检：完整 Express 文件至少应包含 import / express。
        # 真正的语法与 AST 一致性仍由 Applier 无条件执行。
        if normalized.endswith(".js") and "express" not in content:
            raise LLMBackendRepairError(
                f"LLM 输出不像完整的 Express 服务文件: {normalized}"
            )
        files[normalized] = content
    if "server/index.js" not in files:
        raise LLMBackendRepairError("LLM 必须输出 server/index.js")
    return files


__all__ = [
    "LLMBackendCoder",
    "LLMBackendRepairError",
    "LLMBackendRepairUnavailable",
    "MAX_REPAIR_FILE_BYTES",
    "REPAIRABLE_FILES",
]
