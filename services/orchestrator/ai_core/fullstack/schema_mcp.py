"""通过 MCP 对生成 DDL 做真实数据库结构验收。

ConsistencyVerifier 的静态检查只能证明“前端请求的路径出现在后端路由表
里”，不能证明数据库真的能建表、字段真的存在。本模块在构建前启动一个
本地 SQLite MCP Server，把生成 DDL 实际执行到临时数据库，再通过
``sqlite_master`` / ``PRAGMA table_info`` 查询结构。

为什么是 SQLite：
  - 平台沙盒与导出包都生成 ``server/schema.sql``（SQLite 预览 DDL），
    它与 ORM 字段一一对应；
  - MCP Server 使用 Python 标准库，无额外依赖、无网络、可重复；
  - 生产 MySQL / PostgreSQL 迁移文件另有 ``server/migrations/001_init.sql``，
    由云端发布流程执行；这里验证的是同一份契约字段是否真正可落库。

失败策略：
  - ``SCHEMA_MCP_ENABLED=false`` 显式关闭时返回 skipped，不阻断；
  - 默认 strict：MCP 启动失败、DDL 语法错误或字段缺失都会作为一致性
    issue 阻断构建，避免把“表建不起来”的产物交付给用户；
  - ``SCHEMA_MCP_STRICT=false`` 可降级为告警，仅用于排障环境。
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from ai_core.config import Settings, get_settings
from ai_core.domain.architect import ArchitectContract
from ai_core.fullstack.coders import (
    contract_database_engine,
    core_table_for_contract,
    sqlite_schema_sql,
)
from ai_core.mcp.client import McpError, McpServerConfig, McpStdioClient

logger = logging.getLogger(__name__)

SCHEMA_TOOL = "validate_schema"
DEFAULT_SERVER_NAME = "sqlite_schema"


@dataclass(frozen=True)
class SchemaCheck:
    """一次真实数据库结构验收的结果。"""

    enabled: bool
    ok: bool
    table: str
    engine: str
    source: str
    expected_columns: tuple[str, ...] = ()
    actual_columns: tuple[str, ...] = ()
    missing_columns: tuple[str, ...] = ()
    statements: int = 0
    error: str = ""
    skipped: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "ok": self.ok,
            "skipped": self.skipped,
            "table": self.table,
            "engine": self.engine,
            "source": self.source,
            "expectedColumns": list(self.expected_columns),
            "actualColumns": list(self.actual_columns),
            "missingColumns": list(self.missing_columns),
            "statements": self.statements,
            "error": self.error,
            "verifiedBy": "mcp/sqlite-schema",
        }

    def summary(self) -> str:
        if self.skipped:
            return "数据库结构 MCP 校验已跳过（SCHEMA_MCP_ENABLED=false）"
        if self.ok:
            return (
                f"数据库结构 MCP 校验通过：{self.table} "
                f"共 {len(self.actual_columns)} 个字段"
                f"（DDL 已真实执行 {self.statements} 条语句）"
            )
        return (
            f"数据库结构 MCP 校验失败：{self.error or '字段缺失'} "
            f"table={self.table} missing={','.join(self.missing_columns) or '无'}"
        )


ClientFactory = Callable[[McpServerConfig], McpStdioClient]


def _script_path(settings: Settings) -> Path:
    configured = (settings.schema_mcp_script or "").strip()
    if configured:
        return Path(configured).expanduser()
    # ai_core/fullstack/schema_mcp.py -> ai_core/mcp_servers/sqlite_mcp.py
    return Path(__file__).resolve().parents[1] / "mcp_servers" / "sqlite_mcp.py"


def _pick_schema_source(
    files: Mapping[str, str],
    contract: ArchitectContract,
    table: str,
    engine: str,
) -> tuple[str, str]:
    """返回 ``(sql, source_label)``；优先使用生成产物中的真实文件。"""
    candidates = ["server/schema.sql", "server/schema.sqlite.sql"]
    if engine == "sqlite":
        candidates.insert(0, "server/migrations/001_init.sql")
    for path in candidates:
        content = files.get(path)
        if isinstance(content, str) and content.strip():
            return content, path
    # 快照缺少 DDL 时按契约核心业务表确定性生成，仍走真实数据库执行。
    return (
        sqlite_schema_sql(table, list(core_table_for_contract(contract).column_defs)),
        "generated:server/schema.sql",
    )


def _parse_payload(result: dict[str, object]) -> dict[str, object]:
    structured = result.get("structured")
    if isinstance(structured, dict):
        return structured
    text = str(result.get("text") or "")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _as_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def verify_schema_with_mcp(
    files: Mapping[str, str],
    contract: ArchitectContract,
    *,
    settings: Settings | None = None,
    client_factory: ClientFactory | None = None,
) -> SchemaCheck:
    """执行一次真实 DDL → SQLite → 结构查询的 MCP 校验。"""
    settings = settings or get_settings()
    core = core_table_for_contract(contract)
    table = core.name
    engine = contract_database_engine(contract)
    expected = core.column_names
    sql, source = _pick_schema_source(files, contract, table, engine)

    if not settings.schema_mcp_enabled:
        return SchemaCheck(
            enabled=False,
            ok=True,
            skipped=True,
            table=table,
            engine=engine,
            source=source,
            expected_columns=expected,
        )

    config = McpServerConfig(
        name=DEFAULT_SERVER_NAME,
        command=(settings.schema_mcp_python or sys.executable),
        args=(str(_script_path(settings)),),
        timeout_seconds=settings.schema_mcp_timeout_seconds,
        allowed_tools=(SCHEMA_TOOL,),
    )
    client = (client_factory or McpStdioClient)(config)
    try:
        client.start()
        result = client.call_tool(
            SCHEMA_TOOL,
            {
                "ddl": sql,
                "table": table,
                "expected_columns": list(expected),
            },
        )
    except McpError as exc:
        logger.warning("schema MCP 调用失败: %s", exc)
        return SchemaCheck(
            enabled=True,
            ok=False,
            table=table,
            engine=engine,
            source=source,
            expected_columns=expected,
            error=f"MCP 校验不可用: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 - 外部进程故障统一转为校验失败
        logger.warning("schema MCP 未知错误: %s", exc)
        return SchemaCheck(
            enabled=True,
            ok=False,
            table=table,
            engine=engine,
            source=source,
            expected_columns=expected,
            error=f"MCP 校验异常: {exc}",
        )
    finally:
        client.close()

    payload = _parse_payload(result)
    if not payload:
        return SchemaCheck(
            enabled=True,
            ok=False,
            table=table,
            engine=engine,
            source=source,
            expected_columns=expected,
            error=str(result.get("text") or "MCP 返回空结果"),
        )
    actual = _as_names(payload.get("columns"))
    missing = _as_names(payload.get("missingColumns"))
    table_exists = bool(payload.get("tableExists"))
    ok = (
        bool(payload.get("ok"))
        and table_exists
        and not missing
        and not bool(result.get("isError"))
    )
    error = ""
    if not ok:
        error = str(
            payload.get("error")
            or ("目标表不存在" if not table_exists else "字段缺失或 DDL 执行失败")
        )
    return SchemaCheck(
        enabled=True,
        ok=ok,
        table=table,
        engine=engine,
        source=source,
        expected_columns=expected,
        actual_columns=actual,
        missing_columns=missing,
        statements=int(payload.get("statements") or 0),
        error=error,
    )


__all__ = ["SchemaCheck", "verify_schema_with_mcp"]
