"""全栈代码生成器：DBACoder / BackendCoder / FrontendCoder。

设计原则：

1. **确定性优先**：不依赖模型随机输出，契约字段直接映射为可运行代码，
   保证同一份契约生成结果可复现、可测试。
2. **Express + SQLite**：沙盒镜像预装 ``express`` / ``sqlite3``；后端按
   契约生成 ``server/index.js`` 与 ``server/db.js``，数据真实落盘。
3. **DDL 可执行**：``server/schema.sql`` 是沙盒内可直接执行的 SQLite
   DDL，``server/schema.mysql.sql`` 保留契约原始 MySQL DDL。
4. **前端渐进增强**：不重写用户界面，而是在 ``src/main.jsx`` 挂载一个
   ``FullstackDataPanel``，用真实 API 数据替代原先的 Mock 数据展示。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ai_core.domain.architect import (
    ArchitectApiRoute,
    ArchitectContract,
    normalize_database_engine,
)
from ai_core.fullstack.templates import (
    BACKEND_DOCKERFILE_TEMPLATE,
    DEPLOY_GUIDE_TEMPLATE,
    DEV_SCRIPT_TEMPLATE,
    DOCKERIGNORE_TEMPLATE,
    ENV_EXAMPLE_TEMPLATE,
    EXPRESS_SERVER_TEMPLATE,
    MIGRATE_SCRIPT_TEMPLATE,
    RENDER_YAML_TEMPLATE,
    SEQUELIZE_DB_TEMPLATE,
)

API_PORT = 3000
DEFAULT_COLLECTION_PATH = "/api/v1/records"
DEFAULT_TABLE = "app_records"

DATABASE_ENGINE_LABELS = {
    "mysql": "MySQL 8（生产部署）",
    "postgresql": "PostgreSQL（生产部署）",
    "sqlite": "SQLite（仅供本地测试）",
}
PRODUCTION_ENGINES = {"mysql", "postgresql"}
SERVER_DEPENDENCIES = {
    "express": "4.21.2",
    # 生产 ORM 与两套驱动；沙盒未配置 DATABASE_URL 时不会加载它们。
    "sequelize": "6.37.5",
    "mysql2": "3.11.5",
    "pg": "8.13.1",
    "pg-hstore": "2.3.4",
}

_SQL_IDENTIFIER = re.compile(r"[^a-zA-Z0-9_]")
_CREATE_TABLE = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?[`\"]?" r"([a-zA-Z0-9_]+)[`\"]?",
    re.IGNORECASE,
)
_COLUMN_LINE = re.compile(r"^\s*[`\"]?([a-zA-Z0-9_]+)[`\"]?\s+")
_COLUMN_STOPWORDS = {
    "key",
    "unique",
    "primary",
    "constraint",
    "index",
    "foreign",
    "fulltext",
    "spatial",
    "check",
}


@dataclass(frozen=True)
class GeneratedBundle:
    """一次节点生成的产物。"""

    files: dict[str, str]
    summary: str
    metadata: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------
# 契约解析
# ------------------------------------------------------------
def _safe_identifier(value: str, fallback: str) -> str:
    cleaned = _SQL_IDENTIFIER.sub("_", (value or "").strip()).strip("_")
    return cleaned or fallback


def contract_database_engine(contract: ArchitectContract) -> str:
    """解析契约目标数据库；旧契约从 tech_stack 兜底。

    返回值限定为 mysql / postgresql / sqlite。历史契约没有
    ``databaseEngine`` 字段，但只要技术栈写了 MySQL 就应继续生成生产路径，
    绝不能因为字段缺失而静默回退 SQLite。
    """
    engine = normalize_database_engine(contract.database_engine)
    if not engine:
        engine = normalize_database_engine(
            contract.tech_stack.get("databaseEngine")
            or contract.tech_stack.get("database")
        )
    return engine or "mysql"


def extract_table_name(ddl: str) -> str:
    """从 DDL 提取第一张表名；缺失时回退为 app_records。"""
    match = _CREATE_TABLE.search(ddl or "")
    if not match:
        return DEFAULT_TABLE
    return _safe_identifier(match.group(1), DEFAULT_TABLE)


def extract_columns(ddl: str) -> list[str]:
    """从 ``CREATE TABLE (...)`` 中提取字段名。

    只做轻量解析，用于生成种子数据和前端展示；它不是 SQL Parser，
    遇到无法识别的行会跳过，不影响 DDL 原样落盘。
    """
    match = re.search(r"\((.*)\)\s*;?\s*$", ddl or "", re.DOTALL)
    if not match:
        return ["id", "name", "status"]
    columns: list[str] = []
    for raw_line in match.group(1).split(","):
        line = raw_line.strip()
        if not line:
            continue
        name_match = _COLUMN_LINE.match(line)
        if not name_match:
            continue
        name = name_match.group(1).lower()
        if name in _COLUMN_STOPWORDS:
            continue
        if name not in columns:
            columns.append(name)
    return columns or ["id", "name", "status"]


def _split_definitions(body: str) -> list[str]:
    """按顶层逗号切分 CREATE TABLE 字段定义。

    ``DECIMAL(10,2)`` 这类类型内部也有逗号，必须跟踪括号深度，
    否则会把一个字段拆成两段，生成的 SQLite DDL 直接损坏。
    """
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


_SQLITE_INTEGER = {
    "INT",
    "INTEGER",
    "BIGINT",
    "SMALLINT",
    "TINYINT",
    "MEDIUMINT",
    "BOOLEAN",
    "BIT",
    "SERIAL",
}
_SQLITE_REAL = {"REAL", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "MONEY"}
_SQLITE_BLOB = {"BLOB", "BINARY", "VARBINARY", "BYTEA"}


def extract_column_defs(ddl: str) -> list[tuple[str, str]]:
    """把 DDL 字段映射为 SQLite ``(name, type)`` 列表。

    只保留字段名与 SQLite 类型亲和性；``NOT NULL`` / 默认值等约束
    交给生成的 ``server/db.js`` 在写入时补全，避免不同数据库方言
    导致沙盒启动即失败。
    """
    match = re.search(r"\((.*)\)\s*;?\s*$", ddl or "", re.DOTALL)
    if not match:
        return [
            ("id", "INTEGER"),
            ("name", "TEXT"),
            ("status", "TEXT"),
        ]
    defs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for part in _split_definitions(match.group(1)):
        name_match = _COLUMN_LINE.match(part.strip())
        if not name_match:
            continue
        name = name_match.group(1).lower()
        if name in _COLUMN_STOPWORDS or name in seen:
            continue
        rest = part.strip()[name_match.end() :].strip()
        type_match = re.match(r"([A-Za-z]+)", rest)
        raw_type = type_match.group(1).upper() if type_match else "TEXT"
        if raw_type in _SQLITE_INTEGER:
            sqlite_type = "INTEGER"
        elif raw_type in _SQLITE_REAL:
            sqlite_type = "REAL"
        elif raw_type in _SQLITE_BLOB:
            sqlite_type = "BLOB"
        else:
            # JSON / DATETIME / TIMESTAMP / VARCHAR / TEXT 等统一存 TEXT。
            sqlite_type = "TEXT"
        defs.append((name, sqlite_type))
        seen.add(name)
    if not defs:
        return [("id", "INTEGER"), ("name", "TEXT"), ("status", "TEXT")]
    # SQLite 自增主键必须命名为 id 且类型为 INTEGER；契约没有 id 时补一列。
    if "id" not in seen:
        defs.insert(0, ("id", "INTEGER"))
    else:
        defs = [("id", "INTEGER")] + [
            (name, sqlite_type) for name, sqlite_type in defs if name != "id"
        ]
    return defs


def sqlite_schema_sql(table: str, column_defs: list[tuple[str, str]]) -> str:
    """生成可在沙盒内直接执行的 SQLite 建表语句。"""
    lines = ['  "id" INTEGER PRIMARY KEY AUTOINCREMENT']
    for name, sqlite_type in column_defs:
        if name == "id":
            continue
        lines.append(f'  "{name}" {sqlite_type}')
    body = ",\n".join(lines)
    return f'CREATE TABLE IF NOT EXISTS "{table}" (\n{body}\n);\n'


def _route_key(route: ArchitectApiRoute) -> tuple[str, str]:
    return (str(route.method or "GET").upper(), str(route.path or ""))


def _is_item_path(path: str) -> bool:
    return bool(re.search(r"(:\w+|\{\w+\})\s*/?$", path or ""))


def collection_path(routes: Iterable[ArchitectApiRoute]) -> str:
    """选择前端使用的集合路径：优先 GET 集合路由。"""
    candidates = [
        route
        for route in routes
        if str(route.method or "").upper() == "GET"
        and str(route.path or "").startswith("/api/")
        and not _is_item_path(str(route.path or ""))
    ]
    if candidates:
        return str(candidates[0].path)
    for route in routes:
        path = str(route.path or "")
        if path.startswith("/api/"):
            return path
    return DEFAULT_COLLECTION_PATH


def item_path(routes: Iterable[ArchitectApiRoute], base: str) -> str:
    """返回契约中的详情路由；没有显式声明时使用 ``<base>/<id>``。"""
    for route in routes:
        path = str(route.path or "")
        if _is_item_path(path):
            return re.sub(r"\s*/\s*$", "", path)
    return f"{base.rstrip('/')}/:id"


def _entity_label(contract: ArchitectContract, table: str) -> str:
    interactions = contract.component_interactions or []
    for interaction in interactions:
        name = (interaction.component or "").strip()
        if name:
            return name.replace("List", "").replace("Button", "") or table
    return table


def _seed_rows(columns: list[str], table: str, entity: str) -> list[dict[str, Any]]:
    """生成 3 条确定性种子数据，保证联调前后端立刻有内容可渲染。"""
    rows: list[dict[str, Any]] = []
    for index in range(1, 4):
        row: dict[str, Any] = {}
        for column in columns:
            lowered = column.lower()
            if lowered == "id":
                row[column] = index
            elif lowered == "tenant_id":
                row[column] = "tenant_local"
            elif lowered in {"name", "title"}:
                row[column] = f"示例{entity} {index}"
            elif lowered == "status":
                row[column] = "ACTIVE" if index < 3 else "PENDING"
            elif lowered in {"created_at", "updated_at"}:
                row[column] = f"2026-11-09T10:0{index}:00.000Z"
            elif lowered == "deleted_at":
                row[column] = None
            elif "amount" in lowered or "price" in lowered or "total" in lowered:
                row[column] = index * 100
            elif "count" in lowered or "qty" in lowered:
                row[column] = index
            else:
                row[column] = f"{table}_{column}_{index}"
        if "id" not in row:
            row["id"] = index
        rows.append(row)
    return rows


# ------------------------------------------------------------
# DBACoder
# ------------------------------------------------------------
def generate_dba_bundle(contract: ArchitectContract) -> GeneratedBundle:
    """生成 Sequelize ORM 持久化层与可执行的 SQLite 预览 DDL。

    生产环境通过 ``DATABASE_URL`` 连接契约选择的 MySQL / PostgreSQL；
    沙盒没有 DATABASE_URL 时，``server/db.js`` 自动回退到 Node 22 内置
    SQLite，保证预览与 E2E 不依赖外部数据库。
    """
    table = extract_table_name(contract.database_ddl)
    engine = contract_database_engine(contract)
    column_defs = extract_column_defs(contract.database_ddl)
    columns = [name for name, _ in column_defs]
    entity = _entity_label(contract, table)
    ddl = (contract.database_ddl or "").strip()
    if ddl and not ddl.endswith(";"):
        ddl += ";"
    header = (
        "-- 由 DBA Agent 根据《全栈实施契约》生成。\n"
        f"-- contract: {contract.contract_id}\n"
        f"-- table: {table}\n"
        f"-- 目标数据库: {engine}（生产通过 DATABASE_URL 连接）\n"
        "-- 说明：本文件是沙盒 SQLite 的预览建表语句；\n"
        "-- 生产 DDL 见 server/schema.<engine>.sql，ORM 见 server/db.js。\n\n"
    )
    db_code = SEQUELIZE_DB_TEMPLATE
    db_replacements = {
        "__TABLE__": table,
        "__ENTITY__": entity,
        "__DB_ENGINE__": engine,
        "__COLUMN_DEFS_JSON__": json.dumps(column_defs, ensure_ascii=False, indent=2),
        "__SEED_ROWS_JSON__": json.dumps(
            _seed_rows(columns, table, entity),
            ensure_ascii=False,
            indent=2,
        ),
    }
    for key, value in db_replacements.items():
        db_code = db_code.replace(key, value)

    schema_name = (
        "server/schema.sqlite.sql"
        if engine == "sqlite"
        else f"server/schema.{engine}.sql"
    )
    files = {
        "server/schema.sql": header + sqlite_schema_sql(table, column_defs),
        schema_name: (
            f"-- 契约原始 {engine} DDL（contract: {contract.contract_id}）\n\n"
            + ddl
            + "\n"
        ),
        "server/db.js": db_code,
        "server/migrate.mjs": MIGRATE_SCRIPT_TEMPLATE,
    }
    return GeneratedBundle(
        files=files,
        summary=(
            f"生成 {table} Sequelize ORM（目标 {engine}，"
            f"{len(columns)} 个字段）+ SQLite 预览 DDL"
        ),
        metadata={
            "table": table,
            "columns": columns,
            "columnTypes": column_defs,
            "databaseEngine": engine,
            "orm": "sequelize",
            "previewStorage": "sqlite",
        },
    )


# ------------------------------------------------------------
# BackendCoder
# ------------------------------------------------------------
def _express_path(path: str) -> str:
    """把契约里的 ``/todos/{id}`` 转为 Express 的 ``/todos/:id``。"""
    return re.sub(r"\{(\w+)\}", r":\1", path.strip())


def generate_backend_bundle(contract: ArchitectContract) -> GeneratedBundle:
    """按契约 apiRoutes 生成 Express + Sequelize 后端。

    ``server/db.js`` 与 ``server/package.json`` 由 DBACoder 一并生成；
    BackendCoder 负责 HTTP 层、依赖清单与运行说明，保持节点职责清晰。
    """
    routes = contract.api_routes or []
    table = extract_table_name(contract.database_ddl)
    column_defs = extract_column_defs(contract.database_ddl)
    columns = [name for name, _ in column_defs]
    engine = contract_database_engine(contract)
    engine_label = DATABASE_ENGINE_LABELS.get(engine, engine)
    base = collection_path(routes)
    detail = _express_path(item_path(routes, base))
    entity = _entity_label(contract, table)

    replacements = {
        "__API_PORT__": str(API_PORT),
        "__TABLE__": table,
        "__ENTITY__": entity,
        "__COLLECTION_PATH__": base,
        "__ITEM_PATH__": detail,
    }
    server_code = EXPRESS_SERVER_TEMPLATE
    for key, value in replacements.items():
        server_code = server_code.replace(key, value)

    route_lines = []
    for route in routes:
        method = str(route.method or "GET").upper()
        path = str(route.path or "")
        description = route.description or "契约路由"
        route_lines.append(f"- `{method} {path}` — {description}")
    readme = (
        f"# {entity} Backend\n\n"
        "由 BackendCoder 根据契约生成：Express + Sequelize ORM。\n"
        f"目标数据库：**{engine_label}**；生产通过环境变量 `DATABASE_URL` 连接。\n"
        "沙盒未配置 `DATABASE_URL` 时自动回退到 Node 22 内置 SQLite，\n"
        "保证预览与 E2E 不依赖外部数据库。\n\n"
        f"- 表：`{table}`\n"
        f"- 集合路由：`{base}`\n"
        f"- 详情路由：`{detail}`\n"
        f"- 通用数据别名：`/api/data`\n"
        f"- 健康检查：`/api/health`（生产 storage={engine}，沙盒 storage=sqlite）\n"
        f"- 本地预览库：`data/fullstack.sqlite`\n"
        "- 迁移：`npm run db:migrate`（生产需 DATABASE_URL）\n\n"
        "## 契约路由\n\n"
        + ("\n".join(route_lines) if route_lines else "- 无显式路由，使用默认 CRUD")
        + "\n\n## 运行\n\n```bash\nnode server/index.js\n```\n\n"
        "## 生产环境变量\n\n"
        "```bash\n"
        f"DATABASE_URL='<{engine} 连接串>'\n"
        f"DB_ENGINE={engine}\n"
        "DB_AUTO_MIGRATE=true\n"
        "CORS_ORIGIN='https://your-frontend.example.com'\n"
        "```\n"
    )

    files = {
        "server/index.js": server_code,
        "server/README.md": readme,
        "server/package.json": json.dumps(
            {
                "name": "vibe-fullstack-server",
                "private": True,
                "type": "module",
                "dependencies": SERVER_DEPENDENCIES,
                "scripts": {
                    "start": "node index.js",
                    "db:migrate": "node migrate.mjs",
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    }
    return GeneratedBundle(
        files=files,
        summary=(
            f"生成 Express + Sequelize 后端（{engine_label}），"
            f"{len(routes) or 2} 条 REST 路由"
        ),
        metadata={
            "table": table,
            "columns": columns,
            "collectionPath": base,
            "itemPath": detail,
            "entity": entity,
            "runtime": "express-sequelize",
            "previewRuntime": "express-sqlite",
            "databaseEngine": engine,
            "orm": "sequelize",
            "routes": [
                {"method": str(r.method or "GET").upper(), "path": str(r.path or "")}
                for r in routes
            ],
        },
    )


# ------------------------------------------------------------
# FrontendCoder
# ------------------------------------------------------------
_FRONTEND_PANEL = r"""// 由 FrontendCoder 根据契约 componentInteractions 生成。
// 目标：用真实后端数据替代 Mock 列表，并提供一个最小可验证的写入入口。
import { useEffect, useState } from 'react'

const COLLECTION_PATH = '__COLLECTION_PATH__'
const ENTITY = '__ENTITY__'
const TABLE = '__TABLE__'
// 生产前后端通常分域部署：构建时注入 VITE_API_BASE_URL；
// 沙盒未配置时为空字符串，继续走 Vite 同源 /api 代理。
const API_BASE = String(import.meta.env.VITE_API_BASE_URL || '').replace(
  /\/$/,
  '',
)

function apiUrl(path) {
  return `${API_BASE}${path}`
}

function normalizeItems(payload) {
  if (Array.isArray(payload)) return payload
  if (Array.isArray(payload?.items)) return payload.items
  return []
}

function displayName(item) {
  return String(
    item?.name ??
      item?.title ??
      item?.label ??
      `${ENTITY} #${item?.id ?? '?'}`,
  )
}

export default function FullstackDataPanel() {
  const [status, setStatus] = useState('loading')
  const [items, setItems] = useState([])
  const [name, setName] = useState('')
  const [storage, setStorage] = useState('database')
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [submitting, setSubmitting] = useState(false)

  async function load() {
    setStatus('loading')
    setError('')
    setSuccess('')
    try {
      const response = await fetch(apiUrl(COLLECTION_PATH), {
        headers: { Accept: 'application/json' },
      })
      if (!response.ok) {
        throw new Error(`GET ${COLLECTION_PATH} -> HTTP ${response.status}`)
      }
      const payload = await response.json()
      setItems(normalizeItems(payload))
      setStorage(String(payload?.storage || 'database'))
      setStatus('ready')
    } catch (cause) {
      setError(String(cause?.message || cause))
      setStatus('error')
    }
  }

  useEffect(() => {
    load()
  }, [])

  async function submit(event) {
    event.preventDefault()
    if (submitting) return
    setSubmitting(true)
    setError('')
    setSuccess('')
    try {
      const response = await fetch(apiUrl(COLLECTION_PATH), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'application/json',
          'Idempotency-Key': `ui-${Date.now()}`,
        },
        body: JSON.stringify({ name: name.trim() || `新${ENTITY}` }),
      })
      if (!response.ok) {
        throw new Error(`POST ${COLLECTION_PATH} -> HTTP ${response.status}`)
      }
      const payload = await response.json()
      // 兼容直接返回记录与 { data, message } 两种后端响应。
      const created = payload?.data ?? payload
      setItems((previous) => [
        created,
        ...previous.filter((item) => item?.id !== created?.id),
      ])
      setSuccess(payload?.message || '提交成功，数据已写入数据库')
      setName('')
      setStatus('ready')
    } catch (cause) {
      setError(String(cause?.message || cause))
      setStatus('error')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <aside
      data-fullstack-panel
      data-fullstack-status={status}
      data-fullstack-table={TABLE}
      style={{
        position: 'fixed',
        left: 14,
        bottom: 14,
        zIndex: 9999,
        width: 280,
        maxHeight: '72vh',
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
        padding: 12,
        borderRadius: 14,
        background: 'rgba(2,6,23,.92)',
        border: '1px solid rgba(56,189,248,.35)',
        boxShadow: '0 18px 48px rgba(0,0,0,.45)',
        color: '#e2e8f0',
        fontFamily: '"Inter","Segoe UI",sans-serif',
        fontSize: 12,
        boxSizing: 'border-box',
      }}
    >
      <header style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
        <strong style={{ color: '#38bdf8' }}>{ENTITY} · 真实 API</strong>
        <span data-fullstack-count style={{ color: '#94a3b8' }}>
          {items.length}
        </span>
      </header>
      <code style={{ fontSize: 10.5, color: '#64748b' }}>
        {apiUrl(COLLECTION_PATH)}
      </code>

      {status === 'loading' && <span>正在请求后端…</span>}
      {status === 'error' && (
        <span data-fullstack-error style={{ color: '#f87171' }}>
          {error}
        </span>
      )}
      {status === 'ready' && (
        <span
          data-fullstack-ready
          data-fullstack-storage={storage}
          style={{ color: '#34d399' }}
        >
          后端数据已加载 · {storage}
        </span>
      )}
      {success && (
        <span
          data-fullstack-success
          role="status"
          style={{ color: '#4ade80', fontWeight: 600 }}
        >
          {success}
        </span>
      )}

      <div style={{ overflowY: 'auto', display: 'grid', gap: 6, minHeight: 42 }}>
        {items.slice(0, 6).map((item, index) => (
          <div
            key={`${item?.id ?? index}`}
            data-fullstack-row
            data-record-id={String(item?.id ?? '')}
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              gap: 8,
              padding: '6px 8px',
              borderRadius: 8,
              background: 'rgba(30,41,59,.72)',
            }}
          >
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>
              {displayName(item)}
            </span>
            <em style={{ color: '#94a3b8', fontStyle: 'normal' }}>
              {String(item?.status ?? '')}
            </em>
          </div>
        ))}
      </div>

      <form onSubmit={submit} style={{ display: 'flex', gap: 6 }}>
        <input
          data-fullstack-input
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder={`新${ENTITY}名称`}
          style={{
            flex: 1,
            minWidth: 0,
            padding: '7px 9px',
            borderRadius: 8,
            border: '1px solid rgba(148,163,184,.35)',
            background: '#0f172a',
            color: '#e2e8f0',
          }}
        />
        <button
          type="submit"
          data-fullstack-submit
          disabled={submitting}
          style={{
            padding: '7px 10px',
            borderRadius: 8,
            border: 0,
            background: '#2563eb',
            color: '#fff',
            cursor: submitting ? 'wait' : 'pointer',
          }}
        >
          {submitting ? '提交中' : '新增'}
        </button>
      </form>
    </aside>
  )
}
"""


def generate_frontend_bundle(
    contract: ArchitectContract,
    *,
    existing_main: str | None,
    existing_vite: str | None,
    existing_package: str | None,
) -> GeneratedBundle:
    """生成 API 面板、开发启动脚本，并注入 main.jsx / vite / package。"""
    routes = contract.api_routes or []
    table = extract_table_name(contract.database_ddl)
    base = collection_path(routes)
    entity = _entity_label(contract, table)

    panel = (
        _FRONTEND_PANEL.replace("__COLLECTION_PATH__", base)
        .replace("__ENTITY__", entity)
        .replace("__TABLE__", table)
    )
    files: dict[str, str] = {
        "src/fullstack-api.jsx": panel,
        "scripts/dev.mjs": DEV_SCRIPT_TEMPLATE,
    }

    if not existing_main:
        raise ValueError(
            "沙盒缺少 src/main.jsx，无法挂载 FullstackDataPanel；"
            "MVP 全栈生成仅支持 React + Vite 项目"
        )
    if "FullstackDataPanel" not in existing_main:
        if "<App />" not in existing_main:
            raise ValueError("src/main.jsx 中未找到 <App />，无法注入全栈数据面板")
        patched_main = existing_main.replace(
            "<App />",
            "<React.Fragment><App /><FullstackDataPanel /></React.Fragment>",
            1,
        )
        patched_main = (
            "import FullstackDataPanel from './fullstack-api.jsx'\n" + patched_main
        )
        files["src/main.jsx"] = patched_main

    if existing_vite is None:
        raise ValueError("沙盒缺少 vite.config.js，无法配置 /api 反向代理")
    if "proxy" not in existing_vite or "'/api'" not in existing_vite:
        marker = "server: {"
        index = existing_vite.find(marker)
        if index < 0:
            raise ValueError("vite.config.js 缺少 server 配置块，无法注入 /api 代理")
        insert_at = index + len(marker)
        proxy = (
            "\n    // FullstackGraph：把同源 /api 请求转发到沙盒内 Node 后端。\n"
            "    proxy: {\n"
            "      '/api': {\n"
            "        target: process.env.FULLSTACK_API_URL || 'http://127.0.0.1:"
            + str(API_PORT)
            + "',\n"
            "        changeOrigin: true,\n"
            "      },\n"
            "    },"
        )
        files["vite.config.js"] = (
            existing_vite[:insert_at] + proxy + existing_vite[insert_at:]
        )

    if existing_package is None:
        raise ValueError("沙盒缺少 package.json，无法启动前后端双服务")
    try:
        package = json.loads(existing_package)
    except json.JSONDecodeError as exc:
        raise ValueError(f"package.json 不是合法 JSON: {exc}") from exc
    scripts = package.setdefault("scripts", {})
    scripts["dev"] = "node scripts/dev.mjs"
    scripts["dev:vite"] = "vite"
    files["package.json"] = json.dumps(package, ensure_ascii=False, indent=2) + "\n"

    return GeneratedBundle(
        files=files,
        summary=f"挂载 {entity} 数据面板，/api 代理到 {base}",
        metadata={"collectionPath": base, "entity": entity, "table": table},
    )


# ------------------------------------------------------------
# InfraCoder：一键部署配置（Infrastructure as Code）
# ------------------------------------------------------------
def generate_infra_bundle(contract: ArchitectContract) -> GeneratedBundle:
    """生成 Dockerfile、Render Blueprint、环境变量模板与部署指引。

    目标不是“能跑起来的沙盒玩具”，而是可以直接导出、推送到 Git 仓库，
    再由 Render / Railway / Vercel / 任意容器平台部署的生产级项目包。
    """
    table = extract_table_name(contract.database_ddl)
    entity = _entity_label(contract, table)
    engine = contract_database_engine(contract)
    engine_label = DATABASE_ENGINE_LABELS.get(engine, engine)
    base = collection_path(contract.api_routes or [])
    service_name = _safe_identifier(f"vibe_{table}", "vibe_app")
    service_name = service_name.replace("_", "-")[:48].strip("-") or "vibe-app"
    project_name = f"{entity} 全栈应用" if entity else f"{table} 全栈应用"

    replacements = {
        "__PROJECT_NAME__": project_name,
        "__SERVICE_NAME__": service_name,
        "__DB_ENGINE__": engine,
        "__DB_ENGINE_LABEL__": engine_label,
        "__COLLECTION_PATH__": base,
    }

    def render(template: str) -> str:
        result = template
        for key, value in replacements.items():
            result = result.replace(key, value)
        return result

    files = {
        "Dockerfile": render(BACKEND_DOCKERFILE_TEMPLATE),
        ".dockerignore": DOCKERIGNORE_TEMPLATE,
        "render.yaml": render(RENDER_YAML_TEMPLATE),
        ".env.example": render(ENV_EXAMPLE_TEMPLATE),
        "DEPLOY.md": render(DEPLOY_GUIDE_TEMPLATE),
    }
    return GeneratedBundle(
        files=files,
        summary=(
            f"生成一键部署配置（{engine_label}）：Dockerfile、"
            "render.yaml、.env.example、DEPLOY.md"
        ),
        metadata={
            "table": table,
            "entity": entity,
            "databaseEngine": engine,
            "serviceName": service_name,
            "targets": ["docker", "render", "vercel", "railway"],
        },
    )


__all__ = [
    "API_PORT",
    "DATABASE_ENGINE_LABELS",
    "GeneratedBundle",
    "PRODUCTION_ENGINES",
    "collection_path",
    "contract_database_engine",
    "extract_column_defs",
    "extract_columns",
    "extract_table_name",
    "generate_backend_bundle",
    "generate_dba_bundle",
    "generate_frontend_bundle",
    "generate_infra_bundle",
    "item_path",
    "sqlite_schema_sql",
]
