"""全栈代码生成器：DBACoder / BackendCoder / FrontendCoder。

设计原则（MVP）：

1. **确定性优先**：不依赖模型随机输出，契约字段直接映射为可运行代码，
   保证同一份契约生成结果可复现、可测试。
2. **零后端依赖**：沙盒镜像内只保证 Node 20，不联网安装 Express /
   mysql2；因此后端使用 Node 标准库 ``node:http`` 实现 REST CRUD。
3. **DDL 先落盘**：MVP 不要求沙盒内常驻 MySQL，先把契约 DDL 写为
   ``server/schema.sql``，后端用内存仓储保证前后端能真实联调。
4. **前端渐进增强**：不重写用户界面，而是在 ``src/main.jsx`` 挂载一个
   ``FullstackDataPanel``，用真实 API 数据替代原先的 Mock 数据展示。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ai_core.domain.architect import ArchitectApiRoute, ArchitectContract

API_PORT = 3100
DEFAULT_COLLECTION_PATH = "/api/v1/records"
DEFAULT_TABLE = "app_records"

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
    """把契约 DDL 落为可执行的 ``server/schema.sql``。"""
    table = extract_table_name(contract.database_ddl)
    columns = extract_columns(contract.database_ddl)
    ddl = (contract.database_ddl or "").strip()
    if ddl and not ddl.endswith(";"):
        ddl += ";"
    header = (
        "-- 由 DBA Agent 根据《全栈实施契约》生成。\n"
        f"-- contract: {contract.contract_id}\n"
        f"-- table: {table}\n"
        "-- 说明：MVP 沙盒不常驻 MySQL；本文件可直接在目标 MySQL 执行。\n\n"
    )
    files = {"server/schema.sql": header + ddl + "\n"}
    return GeneratedBundle(
        files=files,
        summary=f"生成 {table} 建表 SQL（{len(columns)} 个字段）",
        metadata={"table": table, "columns": columns},
    )


# ------------------------------------------------------------
# BackendCoder
# ------------------------------------------------------------
_BACKEND_SERVER = r"""// 由 BackendCoder 根据《全栈实施契约》生成。
// 零依赖 Node HTTP 服务：不引入 Express / mysql2，沙盒镜像无需联网安装依赖。
// 生产替换点：把 records 数组替换为 MySQL Repository 即可，REST 契约不变。
import { createServer } from 'node:http'
import { URL } from 'node:url'

const PORT = Number(process.env.FULLSTACK_API_PORT || __API_PORT__)
const TABLE = '__TABLE__'
const ENTITY = '__ENTITY__'
const COLLECTION_PATH = '__COLLECTION_PATH__'
const ITEM_PATH = '__ITEM_PATH__'
const COLUMNS = __COLUMNS_JSON__

const now = () => new Date().toISOString()
let nextId = __NEXT_ID__
let records = __SEED_ROWS__

function sendJson(response, status, payload) {
  const body = JSON.stringify(payload)
  response.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type, Authorization, Idempotency-Key',
    'Access-Control-Allow-Methods': 'GET,POST,PUT,PATCH,DELETE,OPTIONS',
  })
  response.end(body)
}

function readBody(request) {
  return new Promise((resolve, reject) => {
    let raw = ''
    request.on('data', (chunk) => {
      raw += chunk
      if (raw.length > 1_000_000) {
        reject(new Error('request body too large'))
        request.destroy()
      }
    })
    request.on('end', () => {
      if (!raw) return resolve({})
      try {
        resolve(JSON.parse(raw))
      } catch {
        reject(new Error('invalid JSON body'))
      }
    })
    request.on('error', reject)
  })
}

function itemId(pathname) {
  const prefix = `${COLLECTION_PATH.replace(/\/$/, '')}/`
  if (!pathname.startsWith(prefix)) return null
  const rest = pathname.slice(prefix.length)
  return /^\d+$/.test(rest) ? Number(rest) : null
}

function pickName(body) {
  return String(body.name ?? body.title ?? body.label ?? `新${ENTITY}`)
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url || '/', 'http://127.0.0.1')
  const pathname = url.pathname.replace(/\/$/, '') || '/'

  if (request.method === 'OPTIONS') {
    return sendJson(response, 204, {})
  }

  if (request.method === 'GET' && pathname === '/api/health') {
    return sendJson(response, 200, {
      status: 'ok',
      table: TABLE,
      entity: ENTITY,
      runtime: 'node-http',
      records: records.length,
    })
  }

  if (request.method === 'GET' && pathname === COLLECTION_PATH) {
    const page = Math.max(1, Number(url.searchParams.get('page') || 1))
    const size = Math.min(100, Math.max(1, Number(url.searchParams.get('size') || 20)))
    const start = (page - 1) * size
    return sendJson(response, 200, {
      items: records.slice(start, start + size),
      total: records.length,
      page,
      size,
      table: TABLE,
    })
  }

  if (request.method === 'POST' && pathname === COLLECTION_PATH) {
    let body
    try {
      body = await readBody(request)
    } catch (error) {
      return sendJson(response, 400, {
        code: 'INVALID_BODY',
        message: error.message,
      })
    }
    const id = nextId++
    const record = { ...body, id }
    if (COLUMNS.includes('tenant_id') && !record.tenant_id) {
      record.tenant_id = 'tenant_local'
    }
    if (COLUMNS.includes('name') && !record.name) {
      record.name = pickName(body)
    }
    if (COLUMNS.includes('status') && !record.status) {
      record.status = 'ACTIVE'
    }
    if (COLUMNS.includes('created_at') && !record.created_at) {
      record.created_at = now()
    }
    if (COLUMNS.includes('updated_at') && !record.updated_at) {
      record.updated_at = record.created_at
    }
    records = [record, ...records]
    return sendJson(response, 201, record)
  }

  const id = itemId(pathname)
  if (id !== null) {
    const index = records.findIndex((item) => Number(item.id) === id)
    if (index < 0) {
      return sendJson(response, 404, {
        code: 'RECORD_NOT_FOUND',
        message: `记录不存在: ${id}`,
      })
    }
    if (request.method === 'GET') {
      return sendJson(response, 200, records[index])
    }
    if (request.method === 'PUT' || request.method === 'PATCH') {
      let body
      try {
        body = await readBody(request)
      } catch (error) {
        return sendJson(response, 400, { code: 'INVALID_BODY', message: error.message })
      }
      const updated = {
        ...records[index],
        ...body,
        id,
        updated_at: COLUMNS.includes('updated_at') ? now() : records[index].updated_at,
      }
      records = records.map((item, itemIndex) => (itemIndex === index ? updated : item))
      return sendJson(response, 200, updated)
    }
    if (request.method === 'DELETE') {
      records = records.filter((item) => Number(item.id) !== id)
      return sendJson(response, 200, { deleted: true, id })
    }
  }

  return sendJson(response, 404, {
    code: 'ROUTE_NOT_FOUND',
    message: `未实现的路由: ${request.method} ${pathname}`,
  })
})

server.listen(PORT, '0.0.0.0', () => {
  console.log(`[fullstack-api] ${TABLE} -> http://127.0.0.1:${PORT}${COLLECTION_PATH}`)
})
"""


def generate_backend_bundle(contract: ArchitectContract) -> GeneratedBundle:
    """按契约 apiRoutes 生成后端服务与 README。"""
    routes = contract.api_routes or []
    table = extract_table_name(contract.database_ddl)
    columns = extract_columns(contract.database_ddl)
    base = collection_path(routes)
    detail = item_path(routes, base)
    entity = _entity_label(contract, table)
    seeds = _seed_rows(columns, table, entity)

    server_code = (
        _BACKEND_SERVER.replace("__API_PORT__", str(API_PORT))
        .replace("__TABLE__", table)
        .replace("__ENTITY__", entity)
        .replace("__COLLECTION_PATH__", base)
        .replace("__ITEM_PATH__", detail)
        .replace("__COLUMNS_JSON__", json.dumps(columns, ensure_ascii=False))
        .replace("__NEXT_ID__", str(max(4, len(seeds) + 1)))
        .replace("__SEED_ROWS__", json.dumps(seeds, ensure_ascii=False, indent=2))
    )

    route_lines = []
    for route in routes:
        method = str(route.method or "GET").upper()
        path = str(route.path or "")
        description = route.description or "契约路由"
        route_lines.append(f"- `{method} {path}` — {description}")
    readme = (
        f"# {entity} Backend\n\n"
        "由 BackendCoder 根据契约生成，运行于 Node 20，零第三方依赖。\n\n"
        f"- 表：`{table}`\n"
        f"- 集合路由：`{base}`\n"
        f"- 详情路由：`{detail}`\n"
        f"- 健康检查：`/api/health`\n\n"
        "## 契约路由\n\n"
        + ("\n".join(route_lines) if route_lines else "- 无显式路由，使用默认 CRUD")
        + "\n\n## 运行\n\n```bash\nnode server/index.mjs\n```\n"
    )

    files = {
        "server/index.mjs": server_code,
        "server/README.md": readme,
        "server/package.json": json.dumps(
            {
                "name": "vibe-fullstack-server",
                "private": True,
                "type": "module",
                "scripts": {"start": "node index.mjs"},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    }
    return GeneratedBundle(
        files=files,
        summary=f"生成 {len(routes) or 2} 条 REST 路由，集合路径 {base}",
        metadata={
            "table": table,
            "collectionPath": base,
            "itemPath": detail,
            "entity": entity,
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
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  async function load() {
    setStatus('loading')
    setError('')
    try {
      const response = await fetch(COLLECTION_PATH, {
        headers: { Accept: 'application/json' },
      })
      if (!response.ok) {
        throw new Error(`GET ${COLLECTION_PATH} -> HTTP ${response.status}`)
      }
      const payload = await response.json()
      setItems(normalizeItems(payload))
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
    try {
      const response = await fetch(COLLECTION_PATH, {
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
      const created = await response.json()
      setItems((previous) => [created, ...previous])
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
      <code style={{ fontSize: 10.5, color: '#64748b' }}>{COLLECTION_PATH}</code>

      {status === 'loading' && <span>正在请求后端…</span>}
      {status === 'error' && (
        <span data-fullstack-error style={{ color: '#f87171' }}>
          {error}
        </span>
      )}
      {status === 'ready' && (
        <span data-fullstack-ready style={{ color: '#34d399' }}>
          后端数据已加载
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

_DEV_SCRIPT = r"""// 由 FullstackGraph 生成：同时拉起前端 Vite 与 Node 后端。
// 容器 CMD 为 `npm run dev`，因此无需修改镜像启动方式。
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'

const children = []
let shuttingDown = false

function start(label, command, args) {
  const child = spawn(command, args, {
    stdio: 'inherit',
    env: process.env,
  })
  children.push(child)
  child.on('exit', (code, signal) => {
    console.log(`[vibe-dev] ${label} exited code=${code} signal=${signal}`)
    if (!shuttingDown && label === 'vite') {
      shutdown(code ?? 1)
    }
  })
  return child
}

function shutdown(code) {
  if (shuttingDown) return
  shuttingDown = true
  children.forEach((child) => {
    if (!child.killed) child.kill('SIGTERM')
  })
  setTimeout(() => process.exit(code), 300)
}

process.on('SIGTERM', () => shutdown(0))
process.on('SIGINT', () => shutdown(0))

if (existsSync('server/index.mjs')) {
  start('api', process.execPath, ['server/index.mjs'])
} else {
  console.log('[vibe-dev] 未找到 server/index.mjs，仅启动前端')
}
start('vite', 'npm', ['run', 'dev:vite'])
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
        "scripts/dev.mjs": _DEV_SCRIPT,
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


__all__ = [
    "API_PORT",
    "GeneratedBundle",
    "collection_path",
    "extract_columns",
    "extract_table_name",
    "generate_backend_bundle",
    "generate_dba_bundle",
    "generate_frontend_bundle",
    "item_path",
]
