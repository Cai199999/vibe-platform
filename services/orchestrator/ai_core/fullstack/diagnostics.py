"""ORM / Express 产物的只读静态诊断。

职责边界：

  - 只调用 Babel AST 引擎做静态分析，**不修改任何文件**；
  - 输出结构化 ``OrmDiagnostic``，由 ConsistencyVerifier 决定是否触发
    自愈、由 Applier 决定是否阻断落盘；
  - 多表场景统一计入 ``MultiTable_AST_Pass_Rate`` 指标。

把诊断从 CodeGenerator 中拆出来后，BackendCoder 不再“自己检查自己”，
ConsistencyVerifier 也不再拥有写代码的能力。所有校验共用同一入口。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Mapping

from ai_core.domain.architect import ArchitectContract
from ai_core.fullstack.coders import (
    check_orm_schema,
    core_table_for_contract,
    parse_ddl_tables,
)
from ai_core.fullstack.schema_ast import SchemaAstUnavailable
from ai_core.observability.fullstack_metrics import FULLSTACK_METRICS

logger = logging.getLogger(__name__)

DIAG_ORM_SCHEMA = "ORM_SCHEMA_MISMATCH"
DIAG_JOIN_ALIAS = "JOIN_ALIAS_MISMATCH"
DIAG_AST_UNAVAILABLE = "AST_UNAVAILABLE"
DIAG_AST_SYNTAX = "AST_SYNTAX_ERROR"


@dataclass(frozen=True)
class OrmDiagnostic:
    """一条可定位的静态诊断。"""

    code: str
    message: str
    source: str = "server/db.js"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "source": self.source}


@dataclass(frozen=True)
class OrmSyntaxReport:
    """一次 ``check_orm_syntax`` 的完整结果。"""

    applicable: bool
    multi_table: bool
    issues: tuple[OrmDiagnostic, ...] = ()
    tables: tuple[str, ...] = ()
    engine: str = "babel-ast"

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def has_join_issue(self) -> bool:
        return any(issue.code == DIAG_JOIN_ALIAS for issue in self.issues)

    def messages(self) -> list[str]:
        return [issue.message for issue in self.issues]

    def summary(self) -> str:
        if not self.applicable:
            return "未发现 ORM 产物，跳过 AST 诊断"
        if self.ok:
            scope = "多表" if self.multi_table else "单表"
            return f"{scope} ORM AST 诊断通过（{', '.join(self.tables) or '无表'}）"
        return f"ORM AST 诊断发现 {len(self.issues)} 处缺陷"

    def to_dict(self) -> dict[str, object]:
        return {
            "applicable": self.applicable,
            "ok": self.ok,
            "multiTable": self.multi_table,
            "tables": list(self.tables),
            "engine": self.engine,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _diagnostic_code(message: str) -> str:
    if "JOIN 别名" in message or "关联" in message:
        return DIAG_JOIN_ALIAS
    if "AST 校验失败" in message or "语法" in message:
        return DIAG_AST_SYNTAX
    return DIAG_ORM_SCHEMA


def _expected_tables(
    contract: ArchitectContract,
    files: Mapping[str, str],
) -> dict[str, Iterable[str]]:
    """迁移 SQL 优先，其次契约 DDL；核心表始终补齐。"""
    migration = files.get("server/migrations/001_init.sql")
    source = migration if isinstance(migration, str) and migration.strip() else ""
    source = source or (contract.database_ddl or "")
    tables = {table.name: table.column_names for table in parse_ddl_tables(source)}
    core = core_table_for_contract(contract)
    if core.name.lower() not in {name.lower() for name in tables}:
        tables[core.name] = core.column_names
    return tables


def check_orm_syntax(
    files: Mapping[str, str],
    contract: ArchitectContract,
    *,
    mode: str = "fullstack",
) -> OrmSyntaxReport:
    """只读诊断 ORM 与 Express 会话层。

    - 无 ``server/db.js``：``applicable=False``（前端 / Infra 写入不适用）；
    - 有 ORM 文件：无条件交给 Babel AST，禁止正则回退；
    - 多表契约：自动计入 ``MultiTable_AST_Pass_Rate``。
    """
    db_js = files.get("server/db.js")
    if not isinstance(db_js, str) or not db_js.strip():
        return OrmSyntaxReport(applicable=False, multi_table=False)

    expected = _expected_tables(contract, files)
    core = core_table_for_contract(contract)
    expected_core = list(expected.get(core.name, core.column_names))
    multi_table = len(expected) > 1
    try:
        messages = check_orm_schema(
            files,
            table=core.name,
            expected_columns=expected_core,
            expected_tables=expected,
        )
    except SchemaAstUnavailable as exc:
        if multi_table:
            FULLSTACK_METRICS.record_multi_table_ast(passed=False)
        logger.error("ORM AST 校验器不可用：%s", exc)
        return OrmSyntaxReport(
            applicable=True,
            multi_table=multi_table,
            issues=(
                OrmDiagnostic(
                    code=DIAG_AST_UNAVAILABLE,
                    message=f"ORM/DDL AST 校验器不可用：{exc}",
                    source="schema_ast",
                ),
            ),
            tables=tuple(expected),
            engine="unavailable",
        )

    issues = tuple(
        OrmDiagnostic(
            code=_diagnostic_code(message),
            message=message,
            source=(
                "server/index.js"
                if message.startswith("server/index.js")
                else "server/db.js"
            ),
        )
        for message in messages
    )
    if multi_table:
        FULLSTACK_METRICS.record_multi_table_ast(passed=not issues)
    return OrmSyntaxReport(
        applicable=True,
        multi_table=multi_table,
        issues=issues,
        tables=tuple(expected),
    )


__all__ = [
    "DIAG_AST_SYNTAX",
    "DIAG_AST_UNAVAILABLE",
    "DIAG_JOIN_ALIAS",
    "DIAG_ORM_SCHEMA",
    "OrmDiagnostic",
    "OrmSyntaxReport",
    "check_orm_syntax",
]
