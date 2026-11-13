"""L3 生产级：真实 MySQL 迁移验收。

L2 只在临时 SQLite 中验证 DDL 可执行；L3 必须证明同一份迁移文件能落到
真实 MySQL。做法：

  1. 连接 ``MYSQL_HOST:MYSQL_PORT``（默认复用 ~/Workspace/infra 的实例）；
  2. 创建一次性数据库 ``vibe_l3_verify_<uuid>``，绝不触碰业务库；
  3. 真实执行 ``server/migrations/001_init.sql``（只允许 CREATE TABLE /
     CREATE INDEX / ALTER TABLE ADD COLUMN）；
  4. 通过 ``information_schema.COLUMNS`` 核对核心业务表与契约字段；
  5. 无论成功失败都删除临时库。

失败归因：
  - 连接失败 / 驱动缺失：``environment_error=True``，属于环境问题，
    不能误判为代码或契约缺陷；
  - DDL 执行失败 / 表或字段缺失：``environment_error=False``，属于
    生成产物缺陷，交给 ConsistencyVerifier 触发修复或退回契约。
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from ai_core.config import Settings, get_settings
from ai_core.domain.architect import ArchitectContract
from ai_core.fullstack.coders import core_table_for_contract

logger = logging.getLogger(__name__)

MIGRATION_PATH = "server/migrations/001_init.sql"
TEMP_DATABASE_PREFIX = "vibe_l3_verify_"

_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_ALLOWED_DDL = re.compile(
    r"^(?:CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)"
    r"|ALTER\s+TABLE\s+\S+\s+ADD\s+(?:COLUMN\s+)?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MysqlMigrationCheck:
    """一次真实 MySQL 迁移验收结果。"""

    enabled: bool
    ok: bool
    database: str
    table: str
    source: str = MIGRATION_PATH
    expected_columns: tuple[str, ...] = ()
    actual_columns: tuple[str, ...] = ()
    missing_columns: tuple[str, ...] = ()
    statements: int = 0
    skipped: bool = False
    environment_error: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "ok": self.ok,
            "skipped": self.skipped,
            "database": self.database,
            "table": self.table,
            "source": self.source,
            "expectedColumns": list(self.expected_columns),
            "actualColumns": list(self.actual_columns),
            "missingColumns": list(self.missing_columns),
            "statements": self.statements,
            "environmentError": self.environment_error,
            "error": self.error,
            "verifiedBy": "mysql/information_schema",
        }

    def summary(self) -> str:
        if self.skipped:
            return "真实 MySQL 迁移验收已跳过（QUALITY_MYSQL_VERIFY_ENABLED=false）"
        if self.ok:
            return (
                f"真实 MySQL 迁移验收通过：{self.table} "
                f"共 {len(self.actual_columns)} 个字段"
                f"（已执行 {self.statements} 条 DDL，临时库已清理）"
            )
        return (
            f"真实 MySQL 迁移验收失败：{self.error or '字段缺失'} "
            f"table={self.table} missing={','.join(self.missing_columns) or '无'}"
        )


def _split_statements(ddl: str) -> list[str]:
    """按顶层分号切分 SQL，并剔除注释；引号内的分号不切。"""
    cleaned = _BLOCK_COMMENT.sub("", _LINE_COMMENT.sub("", ddl or ""))
    parts: list[str] = []
    current: list[str] = []
    quote = ""
    for char in cleaned:
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"', "`"}:
            quote = char
            current.append(char)
            continue
        if char == ";":
            statement = "".join(current).strip()
            if statement:
                parts.append(statement)
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _allowed_statements(ddl: str) -> list[str]:
    statements = _split_statements(ddl)
    for statement in statements:
        if not _ALLOWED_DDL.match(statement):
            raise ValueError(
                "L3 迁移验收只允许 CREATE TABLE / CREATE INDEX / "
                f"ALTER TABLE ADD COLUMN，拒绝: {statement[:80]}"
            )
    return statements


def _connect(**kwargs: Any):
    """延迟导入 pymysql：未安装时返回明确的環境错误，而不是导入崩溃。"""
    import pymysql  # noqa: PLC0415 - 可选依赖，按需加载

    return pymysql.connect(**kwargs)


def verify_mysql_migration(
    files: Mapping[str, str],
    contract: ArchitectContract,
    *,
    settings: Settings | None = None,
    connect: Any | None = None,
) -> MysqlMigrationCheck:
    """把迁移文件真实执行到临时 MySQL 库，并核对核心表字段。"""
    settings = settings or get_settings()
    core = core_table_for_contract(contract)
    table = core.name
    expected = tuple(core.column_names)
    ddl = files.get(MIGRATION_PATH) or ""
    database = f"{TEMP_DATABASE_PREFIX}{uuid.uuid4().hex[:12]}"

    if not getattr(settings, "quality_mysql_verify_enabled", True):
        return MysqlMigrationCheck(
            enabled=False,
            ok=True,
            skipped=True,
            database="",
            table=table,
            expected_columns=expected,
        )
    if not isinstance(ddl, str) or not ddl.strip():
        return MysqlMigrationCheck(
            enabled=True,
            ok=False,
            database=database,
            table=table,
            expected_columns=expected,
            error=f"{MIGRATION_PATH} 缺失或为空，无法执行真实迁移验收",
        )

    try:
        statements = _allowed_statements(ddl)
    except ValueError as exc:
        return MysqlMigrationCheck(
            enabled=True,
            ok=False,
            database=database,
            table=table,
            expected_columns=expected,
            error=str(exc),
        )

    connector = connect or _connect
    connection = None
    created = False
    try:
        kwargs: dict[str, Any] = {
            "host": settings.mysql_host,
            "port": int(settings.mysql_port),
            "user": settings.mysql_user,
            "password": settings.mysql_password,
            "charset": "utf8mb4",
            "autocommit": True,
            "connect_timeout": float(
                getattr(settings, "quality_mysql_timeout_seconds", 8.0)
            ),
        }
        if connect is None:
            # 只有真实 pymysql 需要 DictCursor；测试注入的连接器自行实现。
            import pymysql  # noqa: PLC0415

            kwargs["cursorclass"] = pymysql.cursors.DictCursor
        connection = connector(**kwargs)
        with connection.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE `{database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            created = True
            cursor.execute(f"USE `{database}`")
            for statement in statements:
                cursor.execute(statement)

            cursor.execute(
                "SELECT COLUMN_NAME AS name FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                (database, table),
            )
            rows = cursor.fetchall() or []
            actual = tuple(
                str(row.get("name") if isinstance(row, dict) else row[0])
                for row in rows
                if (row.get("name") if isinstance(row, dict) else row[0])
            )
    except Exception as exc:  # noqa: BLE001 - 统一转成结构化验收结果
        message = str(exc)
        environment_error = _is_environment_error(exc)
        logger.warning(
            "真实 MySQL 迁移验收失败 database=%s environment=%s error=%s",
            database,
            environment_error,
            message,
        )
        return MysqlMigrationCheck(
            enabled=True,
            ok=False,
            database=database,
            table=table,
            expected_columns=expected,
            statements=len(statements),
            environment_error=environment_error,
            error=(
                f"MySQL 连接 / 驱动不可用: {message}"
                if environment_error
                else f"迁移 DDL 执行失败: {message}"
            ),
        )
    finally:
        if connection is not None:
            try:
                if created:
                    with connection.cursor() as cursor:
                        cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
            except Exception as exc:  # noqa: BLE001 - 清理失败不改变验收结论
                logger.warning("清理临时 MySQL 库失败 %s: %s", database, exc)
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass

    lower_actual = {name.casefold(): name for name in actual}
    missing = tuple(
        column for column in expected if column.casefold() not in lower_actual
    )
    if not actual:
        return MysqlMigrationCheck(
            enabled=True,
            ok=False,
            database=database,
            table=table,
            expected_columns=expected,
            statements=len(statements),
            error=f"迁移后未在 information_schema 中找到表 {table}",
        )
    if missing:
        return MysqlMigrationCheck(
            enabled=True,
            ok=False,
            database=database,
            table=table,
            expected_columns=expected,
            actual_columns=actual,
            missing_columns=missing,
            statements=len(statements),
            error=f"核心表 {table} 缺少字段: {', '.join(missing)}",
        )
    return MysqlMigrationCheck(
        enabled=True,
        ok=True,
        database=database,
        table=table,
        expected_columns=expected,
        actual_columns=actual,
        statements=len(statements),
    )


def _is_environment_error(exc: Exception) -> bool:
    """连接 / 驱动 / 权限问题属于环境；SQL 语法与约束错误属于产物缺陷。"""
    name = type(exc).__name__
    message = str(exc).lower()
    if name in {
        "OperationalError",
        "InterfaceError",
        "ModuleNotFoundError",
        "ImportError",
    }:
        return True
    markers = (
        "can't connect",
        "connection refused",
        "unknown host",
        "timed out",
        "access denied",
        "not installed",
        "no module named",
    )
    return any(marker in message for marker in markers)


__all__ = ["MIGRATION_PATH", "MysqlMigrationCheck", "verify_mysql_migration"]
