#!/usr/bin/env python3
"""SQLite MCP Server（stdio JSON-RPC 2.0）。

供 ConsistencyVerifier 在构建前做**真实数据库结构验收**：
把生成的 DDL 真正执行到临时 SQLite 数据库，再用 `sqlite_master` /
`PRAGMA table_info` 查询表与字段是否存在。它不解析文本、不假设模型输出
一定正确；DDL 语法错误或字段缺失会直接返回 `ok=false`。

协议只实现平台需要的子集：
  - initialize / notifications/initialized / ping
  - tools/list / tools/call

工具：
  - validate_schema(ddl, table, expected_columns)
  - describe_table(database, table)
  - list_tables(database)

安全边界：
  - 只接受 CREATE TABLE / CREATE INDEX / ALTER TABLE ADD COLUMN 三类
    DDL，拒绝 DROP / DELETE / UPDATE / INSERT / ATTACH；
  - describe / list 以 SQLite 只读 URI（mode=ro）打开数据库；
  - 单条消息与 DDL 均有大小上限，避免内存放大。
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "vibe-sqlite-schema"
MAX_MESSAGE_BYTES = 1_048_576
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_ALLOWED_DDL = re.compile(
    r"^(?:CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)"
    r"|ALTER\s+TABLE\s+\S+\s+ADD\s+(?:COLUMN\s+)?)\b",
    re.IGNORECASE,
)

TOOLS = [
    {
        "name": "validate_schema",
        "description": (
            "在临时 SQLite 数据库中真实执行 DDL，并查询 sqlite_master 与 "
            "PRAGMA table_info，验证目标表与字段是否存在。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ddl": {"type": "string", "description": "要执行的建表 SQL"},
                "table": {"type": "string", "description": "期望存在的表名"},
                "expected_columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "期望存在的字段名列表",
                },
            },
            "required": ["ddl", "table"],
        },
    },
    {
        "name": "describe_table",
        "description": "以只读方式打开 SQLite 数据库，返回表的字段结构。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string", "description": "SQLite 文件路径"},
                "table": {"type": "string", "description": "表名"},
            },
            "required": ["database", "table"],
        },
    },
    {
        "name": "list_tables",
        "description": "以只读方式打开 SQLite 数据库，列出全部用户表。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string", "description": "SQLite 文件路径"},
            },
            "required": ["database"],
        },
    },
]


class ToolInputError(ValueError):
    """工具参数或 SQL 不合法。"""


def _safe_identifier(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not _IDENTIFIER.match(text):
        raise ToolInputError(f"{label} 不是合法标识符: {value!r}")
    return text


def _statements(ddl: str) -> list[str]:
    """按顶层分号切分 DDL，并剔除注释。"""
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


def _validate_ddl(ddl: str) -> list[str]:
    if not isinstance(ddl, str) or not ddl.strip():
        raise ToolInputError("ddl 不能为空")
    if len(ddl.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ToolInputError("ddl 超过大小限制")
    statements = _statements(ddl)
    if not statements:
        raise ToolInputError("ddl 中没有可执行语句")
    for statement in statements:
        if not _ALLOWED_DDL.match(statement):
            raise ToolInputError(
                "迁移验证只允许 CREATE TABLE / CREATE INDEX / "
                f"ALTER TABLE ADD COLUMN，拒绝: {statement[:80]}"
            )
    return statements


def _table_columns(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    # table 已通过标识符白名单，双引号转义仅作纵深防御。
    escaped = table.replace('"', '""')
    rows = connection.execute(f'PRAGMA table_info("{escaped}")').fetchall()
    return [
        {
            "cid": row[0],
            "name": row[1],
            "type": row[2],
            "notnull": bool(row[3]),
            "default": row[4],
            "primaryKey": bool(row[5]),
        }
        for row in rows
    ]


def validate_schema(arguments: dict[str, Any]) -> dict[str, Any]:
    ddl = str(arguments.get("ddl") or "")
    table = _safe_identifier(arguments.get("table") or "", "table")
    expected = [
        str(item).strip()
        for item in (arguments.get("expected_columns") or [])
        if str(item).strip()
    ]
    statements = _validate_ddl(ddl)
    with tempfile.TemporaryDirectory(prefix="vibe-schema-") as directory:
        database = Path(directory) / "schema.sqlite"
        connection = sqlite3.connect(str(database))
        try:
            for statement in statements:
                connection.execute(statement)
            connection.commit()
            exists = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            columns = _table_columns(connection, table) if exists else []
        finally:
            connection.close()
    actual = [item["name"] for item in columns]
    lowered = {name.casefold() for name in actual}
    missing = [name for name in expected if name.casefold() not in lowered]
    extra = (
        [
            name
            for name in actual
            if name.casefold() not in {item.casefold() for item in expected}
        ]
        if expected
        else []
    )
    ok = bool(exists) and not missing
    return {
        "ok": ok,
        "table": table,
        "tableExists": bool(exists),
        "columns": actual,
        "columnDetails": columns,
        "expectedColumns": expected,
        "missingColumns": missing,
        "extraColumns": extra,
        "statements": len(statements),
        "engine": "sqlite",
        "database": "temporary",
    }


def _readonly_connection(raw_path: str) -> sqlite3.Connection:
    path = Path(str(raw_path or "")).expanduser().resolve()
    if not path.is_file():
        raise ToolInputError(f"数据库文件不存在: {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def describe_table(arguments: dict[str, Any]) -> dict[str, Any]:
    table = _safe_identifier(arguments.get("table") or "", "table")
    connection = _readonly_connection(arguments.get("database") or "")
    try:
        exists = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        columns = _table_columns(connection, table) if exists else []
    finally:
        connection.close()
    return {
        "ok": bool(exists),
        "table": table,
        "tableExists": bool(exists),
        "columns": [item["name"] for item in columns],
        "columnDetails": columns,
    }


def list_tables(arguments: dict[str, Any]) -> dict[str, Any]:
    connection = _readonly_connection(arguments.get("database") or "")
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    finally:
        connection.close()
    tables = [str(row[0]) for row in rows]
    return {"ok": True, "tables": tables, "count": len(tables)}


TOOL_HANDLERS = {
    "validate_schema": validate_schema,
    "describe_table": describe_table,
    "list_tables": list_tables,
}


def _write(payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write(body + "\n")
    sys.stdout.flush()


def _result(request_id: Any, result: dict[str, Any]) -> None:
    _write({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: Any, code: int, message: str) -> None:
    _write(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
    )


def _tool_result(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False),
            }
        ],
        "structuredContent": payload,
        "isError": is_error,
    }


def handle(request: dict[str, Any]) -> None:
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    if method == "initialize":
        _result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": "0.1.0"},
            },
        )
        return
    if method == "notifications/initialized":
        return
    if method == "ping":
        _result(request_id, {})
        return
    if method == "tools/list":
        _result(request_id, {"tools": TOOLS})
        return
    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            _result(
                request_id,
                _tool_result(
                    {"ok": False, "error": "arguments 必须是对象"}, is_error=True
                ),
            )
            return
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            _result(
                request_id,
                _tool_result(
                    {"ok": False, "error": f"未知工具: {name}"}, is_error=True
                ),
            )
            return
        try:
            payload = handler(arguments)
        except ToolInputError as exc:
            _result(
                request_id,
                _tool_result({"ok": False, "error": str(exc)}, is_error=True),
            )
            return
        except (sqlite3.Error, OSError) as exc:
            _result(
                request_id,
                _tool_result({"ok": False, "error": str(exc)}, is_error=True),
            )
            return
        _result(request_id, _tool_result(payload))
        return
    _error(request_id, -32601, f"Method not found: {method}")


def main() -> int:
    for line in sys.stdin.buffer:
        if len(line) > MAX_MESSAGE_BYTES:
            _error(None, -32600, "消息超过大小限制")
            continue
        if not line.strip():
            continue
        try:
            request = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _error(None, -32700, "JSON 解析失败")
            continue
        if not isinstance(request, dict):
            _error(None, -32600, "请求必须是对象")
            continue
        try:
            handle(request)
        except Exception as exc:  # noqa: BLE001 - 单条请求故障不终止服务
            _error(request.get("id"), -32603, f"内部错误: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
