"""主从表关联的确定性推导与校验。

契约允许两条来源：

  1. LLM 显式给出 ``relations``（优先，能表达一对一等业务语义）；
  2. DDL 里存在 ``<parent>_id`` 外键列（兜底推导，避免模型漏写）。

本模块只做结构判断，不猜测业务含义：表的列集合必须由调用方提供，
关联只认可“父表真实存在 + 子表真实拥有该外键列”的组合。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ai_core.domain.architect import ArchitectTableRelation

# 系统 / 鉴权表：默认不参与自动推导，避免把 password_hash / iam_sub
# 等敏感列通过关联查询暴露给前端。显式声明或作为核心业务表时仍然允许。
SYSTEM_TABLE_NAMES = {
    "user",
    "users",
    "account",
    "accounts",
    "session",
    "sessions",
    "role",
    "roles",
    "permission",
    "permissions",
    "user_role",
    "user_roles",
    "refresh_token",
    "refresh_tokens",
    "audit_log",
    "audit_logs",
    "_schema_migrations",
    "schema_migrations",
    "migrations",
}
SYSTEM_TABLE_PREFIXES = ("auth_", "oauth_", "session_")

_CONSTRAINT_KEYWORDS = {
    "primary",
    "unique",
    "key",
    "index",
    "constraint",
    "foreign",
    "fulltext",
    "spatial",
    "check",
}
_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+[\"`]?([A-Za-z_][A-Za-z0-9_]*)[\"`]?\s*\(",
    re.IGNORECASE,
)


def is_system_table(name: str) -> bool:
    normalized = (name or "").strip().lower()
    return normalized in SYSTEM_TABLE_NAMES or normalized.startswith(
        SYSTEM_TABLE_PREFIXES
    )


def _split_top_level(body: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    for char in body:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
            current.append(char)
        elif char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth = max(0, depth - 1)
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current))
    return parts


def parse_ddl_table_columns(ddl: str) -> dict[str, tuple[str, ...]]:
    """轻量解析 DDL：``{表名: (列名, ...)}``。

    只用于契约层校验关联与生成前审计；真正的字段类型 / 约束解析由
    ``fullstack.coders.parse_ddl_tables`` 负责。
    """
    text = ddl or ""
    tables: dict[str, tuple[str, ...]] = {}
    for match in _CREATE_TABLE.finditer(text):
        name = match.group(1)
        depth = 1
        index = match.end()
        quote: str | None = None
        while index < len(text) and depth > 0:
            char = text[index]
            if quote:
                if char == quote:
                    quote = None
            elif char in "'\"`":
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1
        body = text[match.end() : index - 1]
        columns: list[str] = []
        for part in _split_top_level(body):
            tokens = part.strip().split()
            if not tokens:
                continue
            first = tokens[0].strip('"`[]')
            if first.lower() in _CONSTRAINT_KEYWORDS:
                continue
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", first):
                continue
            columns.append(first)
        tables[name] = tuple(columns)
    return tables


def _singular(value: str) -> str:
    word = (value or "").strip().lower()
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith("ses") and len(word) > 3:
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 1:
        return word[:-1]
    return word


def _find_table(tables: Mapping[str, Sequence[str]], candidate: str) -> str | None:
    lowered = {name.lower(): name for name in tables}
    direct = lowered.get(candidate.lower())
    if direct:
        return direct
    singular = _singular(candidate)
    for name in tables:
        if _singular(name) == singular:
            return name
    return None


def derive_table_relations(
    tables: Mapping[str, Sequence[str]],
    *,
    core_table: str = "",
) -> list[ArchitectTableRelation]:
    """从 ``<parent>_id`` 列推导一对多关联。

    规则刻意保守：
      - 外键列必须形如 ``xxx_id`` 且父表真实存在；
      - 系统表只有在它是核心业务表时才参与推导，防止关联查询泄露
        ``users.iam_sub`` / ``password_hash``；
      - 同一父表 / 子表只保留一条关系。
    """
    relations: list[ArchitectTableRelation] = []
    seen: set[tuple[str, str, str]] = set()
    core_lower = (core_table or "").lower()
    for child, columns in tables.items():
        for column in columns:
            lowered = column.lower()
            if not lowered.endswith("_id") or lowered in {"id", "parent_id"}:
                continue
            parent = _find_table(tables, column[:-3])
            if not parent or parent.lower() == child.lower():
                continue
            if is_system_table(parent) and parent.lower() != core_lower:
                continue
            key = (parent.lower(), child.lower(), lowered)
            if key in seen:
                continue
            seen.add(key)
            relations.append(
                ArchitectTableRelation(
                    parentTable=parent,
                    childTable=child,
                    foreignKey=column,
                    type="one_to_many",
                )
            )
    return relations


def normalize_table_relations(
    tables: Mapping[str, Sequence[str]],
    raw_relations: Iterable[ArchitectTableRelation | Mapping[str, Any] | Any],
    *,
    core_table: str = "",
    derive_when_empty: bool = True,
) -> tuple[list[ArchitectTableRelation], list[str]]:
    """校验显式关联；缺失时按 DDL 推导。

    返回 ``(可用关联, 问题列表)``。问题列表用于 Architect 契约校验：
    引用不存在的表 / 外键列属于“契约自相矛盾”，必须在确认前修复，
    不能留给 Coder 去猜。
    """
    table_lookup = {name.lower(): name for name in tables}
    accepted: list[ArchitectTableRelation] = []
    problems: list[str] = []
    seen: set[tuple[str, str, str]] = set()

    for raw in raw_relations:
        try:
            relation = (
                raw
                if isinstance(raw, ArchitectTableRelation)
                else ArchitectTableRelation.model_validate(raw)
            )
        except Exception:  # noqa: BLE001 - 单条脏数据转成可读问题
            problems.append(f"关联定义格式非法：{raw!r}")
            continue
        parent = table_lookup.get(relation.parent_table.lower())
        child = table_lookup.get(relation.child_table.lower())
        if not parent:
            problems.append(
                f"关联 {relation.parent_table} → {relation.child_table} "
                f"引用了 DDL 中不存在的父表 {relation.parent_table}"
            )
            continue
        if not child:
            problems.append(
                f"关联 {relation.parent_table} → {relation.child_table} "
                f"引用了 DDL 中不存在的子表 {relation.child_table}"
            )
            continue
        if relation.foreign_key.lower() not in {
            column.lower() for column in tables[child]
        }:
            problems.append(
                f"关联 {parent} → {child} 的外键 "
                f"{relation.foreign_key} 不在子表字段中"
            )
            continue
        if relation.parent_key.lower() not in {
            column.lower() for column in tables[parent]
        }:
            problems.append(
                f"关联 {parent} → {child} 的父键 "
                f"{relation.parent_key} 不在父表字段中"
            )
            continue
        key = (parent.lower(), child.lower(), relation.foreign_key.lower())
        if key in seen:
            continue
        seen.add(key)
        accepted.append(
            relation.model_copy(update={"parent_table": parent, "child_table": child})
        )

    if not accepted and derive_when_empty and tables:
        accepted = derive_table_relations(tables, core_table=core_table)
    return accepted, problems


def relation_audit_issues(
    tables: Mapping[str, Sequence[str]],
    relations: Iterable[ArchitectTableRelation],
) -> list[str]:
    """生成前审计：已落盘的关联必须仍与 DDL 对齐。"""
    _, problems = normalize_table_relations(
        tables,
        relations,
        derive_when_empty=False,
    )
    return problems


__all__ = [
    "SYSTEM_TABLE_NAMES",
    "SYSTEM_TABLE_PREFIXES",
    "derive_table_relations",
    "is_system_table",
    "normalize_table_relations",
    "parse_ddl_table_columns",
    "relation_audit_issues",
]
