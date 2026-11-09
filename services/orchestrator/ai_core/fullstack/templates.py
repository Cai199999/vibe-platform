"""全栈代码模板：Express + SQLite 后端、Vite 双服务启动脚本。

模板只保留 ``__PLACEHOLDER__``，由 ``coders.py`` 用契约中的表名、路由、
字段与种子数据做确定性替换。保持独立文件可以避免在 Python 字符串里
反复转义 JS 模板字面量。
"""

from __future__ import annotations

# ------------------------------------------------------------
# server/db.js：SQLite 持久化层
# ------------------------------------------------------------
SQLITE_DB_TEMPLATE = r"""// 由 BackendCoder 根据《全栈实施契约》生成。
// SQLite 持久化层：建表、种子数据与 CRUD，供 Express 路由复用。
// 使用 Node 22 内置 node:sqlite，无需编译原生扩展。
import { DatabaseSync } from 'node:sqlite'
import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const TABLE = '__TABLE__'
const ENTITY = '__ENTITY__'
const COLUMN_DEFS = __COLUMN_DEFS_JSON__
const ALL_COLUMNS = COLUMN_DEFS.map(([name]) => name)
const WRITABLE_COLUMNS = ALL_COLUMNS.filter(
  (name) => !['id', 'created_at', 'updated_at', 'deleted_at'].includes(name),
)
const SEED_ROWS = __SEED_ROWS_JSON__

// 数据库文件放在 /app/data，随容器文件系统持久化；重启容器后数据仍在。
const DATA_DIR = process.env.FULLSTACK_DATA_DIR || resolve(__dirname, '..', 'data')
mkdirSync(DATA_DIR, { recursive: true })
const DB_PATH = process.env.FULLSTACK_DB_PATH || resolve(DATA_DIR, 'fullstack.sqlite')

const db = new DatabaseSync(DB_PATH)
db.exec('PRAGMA busy_timeout = 5000;')

const columnDdl = COLUMN_DEFS.filter(([name]) => name !== 'id')
  .map(([name, type]) => `  "${name}" ${type}`)
  .join(',\n')
const SCHEMA_SQL = `CREATE TABLE IF NOT EXISTS "${TABLE}" (
  "id" INTEGER PRIMARY KEY AUTOINCREMENT${columnDdl ? ',\n' + columnDdl : ''}
);`

function all(sql, params = []) {
  return db.prepare(sql).all(...params)
}

function get(sql, params = []) {
  return db.prepare(sql).get(...params) ?? null
}

function run(sql, params = []) {
  const result = db.prepare(sql).run(...params)
  return {
    lastID: Number(result.lastInsertRowid),
    changes: Number(result.changes),
  }
}

function encode(value) {
  if (value === undefined || value === null) return null
  if (typeof value === 'object') return JSON.stringify(value)
  if (typeof value === 'boolean') return value ? 1 : 0
  return value
}

async function insertRow(input = {}) {
  const body = { ...input }
  if (body.name === undefined && body.title !== undefined) body.name = body.title
  const values = {}
  for (const column of WRITABLE_COLUMNS) {
    if (body[column] !== undefined) values[column] = encode(body[column])
  }
  if (ALL_COLUMNS.includes('tenant_id') && values.tenant_id === undefined) {
    values.tenant_id = 'tenant_local'
  }
  if (ALL_COLUMNS.includes('name') && values.name === undefined) {
    values.name = `新${ENTITY}`
  }
  if (ALL_COLUMNS.includes('status') && values.status === undefined) {
    values.status = 'ACTIVE'
  }
  const now = new Date().toISOString()
  if (ALL_COLUMNS.includes('created_at') && values.created_at === undefined) {
    values.created_at = now
  }
  if (ALL_COLUMNS.includes('updated_at') && values.updated_at === undefined) {
    values.updated_at = now
  }
  const columns = Object.keys(values)
  if (columns.length === 0) throw new Error('没有可写入的字段')
  const sql =
    `INSERT INTO "${TABLE}" (` +
    columns.map((column) => `"${column}"`).join(', ') +
    ') VALUES (' +
    columns.map(() => '?').join(', ') +
    ')'
  const result = await run(sql, columns.map((column) => values[column]))
  return getRecord(result.lastID)
}

export async function listRecords({ page = 1, size = 20 } = {}) {
  const totalRow = await get(`SELECT COUNT(*) AS total FROM "${TABLE}"`)
  const items = await all(
    `SELECT * FROM "${TABLE}" ORDER BY "id" DESC LIMIT ? OFFSET ?`,
    [size, (page - 1) * size],
  )
  return {
    items,
    total: Number(totalRow?.total ?? 0),
    page,
    size,
  }
}

export async function getRecord(id) {
  return get(`SELECT * FROM "${TABLE}" WHERE "id" = ?`, [Number(id)])
}

export async function createRecord(input = {}) {
  return insertRow(input)
}

export async function updateRecord(id, input = {}) {
  const values = {}
  for (const column of WRITABLE_COLUMNS) {
    if (input[column] !== undefined) values[column] = encode(input[column])
  }
  if (ALL_COLUMNS.includes('updated_at')) {
    values.updated_at = new Date().toISOString()
  }
  const columns = Object.keys(values)
  if (columns.length === 0) return getRecord(id)
  await run(
    `UPDATE "${TABLE}" SET ` +
      columns.map((column) => `"${column}" = ?`).join(', ') +
      ' WHERE "id" = ?',
    [...columns.map((column) => values[column]), Number(id)],
  )
  return getRecord(id)
}

export async function deleteRecord(id) {
  const result = await run(`DELETE FROM "${TABLE}" WHERE "id" = ?`, [Number(id)])
  return { deleted: result.changes > 0, id: Number(id) }
}

export async function health() {
  const totalRow = await get(`SELECT COUNT(*) AS total FROM "${TABLE}"`)
  return {
    status: 'ok',
    runtime: 'express-sqlite',
    storage: 'sqlite',
    table: TABLE,
    entity: ENTITY,
    records: Number(totalRow?.total ?? 0),
    dbPath: DB_PATH,
  }
}

async function seedIfEmpty() {
  const totalRow = await get(`SELECT COUNT(*) AS total FROM "${TABLE}"`)
  if (Number(totalRow?.total ?? 0) > 0) return
  for (const row of SEED_ROWS) {
    await insertRow(row)
  }
}

db.exec(SCHEMA_SQL)
await seedIfEmpty().catch((error) => {
  console.error('[fullstack-db] seed failed:', error)
})

export { DB_PATH, SCHEMA_SQL }
"""

# ------------------------------------------------------------
# server/index.js：Express REST 服务
# ------------------------------------------------------------
EXPRESS_SERVER_TEMPLATE = r"""// 由 BackendCoder 根据《全栈实施契约》生成。
// Express + SQLite：真实 REST 路由，Vite 通过 /api 同源代理访问。
import express from 'express'
import {
  createRecord,
  deleteRecord,
  getRecord,
  health,
  listRecords,
  updateRecord,
} from './db.js'

const PORT = Number(process.env.FULLSTACK_API_PORT || __API_PORT__)
const TABLE = '__TABLE__'
const ENTITY = '__ENTITY__'
const COLLECTION_PATH = '__COLLECTION_PATH__'
const ITEM_PATH = '__ITEM_PATH__'

const app = express()
app.disable('x-powered-by')
app.use(express.json({ limit: '1mb' }))
app.use((request, response, next) => {
  response.setHeader('Access-Control-Allow-Origin', '*')
  response.setHeader(
    'Access-Control-Allow-Headers',
    'Content-Type, Authorization, Idempotency-Key',
  )
  response.setHeader(
    'Access-Control-Allow-Methods',
    'GET,POST,PUT,PATCH,DELETE,OPTIONS',
  )
  if (request.method === 'OPTIONS') return response.status(204).end()
  next()
})

// Express 4 不会自动捕获 async 异常，统一转交给错误中间件。
const asyncHandler = (handler) => (request, response, next) =>
  Promise.resolve(handler(request, response, next)).catch(next)

const listHandler = asyncHandler(async (request, response) => {
  const page = Math.max(1, Number(request.query.page || 1))
  const size = Math.min(100, Math.max(1, Number(request.query.size || 20)))
  const result = await listRecords({ page, size })
  response.json({ ...result, table: TABLE, storage: 'sqlite' })
})

const createHandler = asyncHandler(async (request, response) => {
  const record = await createRecord(request.body || {})
  response.status(201).json({
    ...record,
    success: true,
    message: '提交成功，数据已写入数据库',
  })
})

app.get('/api/health', asyncHandler(async (_request, response) => {
  response.json(await health())
}))

app.get(COLLECTION_PATH, listHandler)
app.post(COLLECTION_PATH, createHandler)
// 通用数据别名：便于外部脚本和前端用同一张 SQLite 表做快速验证。
app.get('/api/data', listHandler)
app.post('/api/data', createHandler)

if (ITEM_PATH && ITEM_PATH !== COLLECTION_PATH) {
  app.get(ITEM_PATH, asyncHandler(async (request, response) => {
    const record = await getRecord(request.params.id)
    if (!record) {
      return response
        .status(404)
        .json({ code: 'RECORD_NOT_FOUND', message: '记录不存在' })
    }
    response.json(record)
  }))

  const updateHandler = asyncHandler(async (request, response) => {
    const record = await updateRecord(request.params.id, request.body || {})
    if (!record) {
      return response
        .status(404)
        .json({ code: 'RECORD_NOT_FOUND', message: '记录不存在' })
    }
    response.json(record)
  })
  app.put(ITEM_PATH, updateHandler)
  app.patch(ITEM_PATH, updateHandler)

  app.delete(ITEM_PATH, asyncHandler(async (request, response) => {
    response.json(await deleteRecord(request.params.id))
  }))
}

app.use((request, response) => {
  response.status(404).json({
    code: 'ROUTE_NOT_FOUND',
    message: `未实现的路由: ${request.method} ${request.path}`,
  })
})

app.use((error, _request, response, _next) => {
  console.error('[fullstack-api]', error)
  response.status(error.status || 500).json({
    code: 'INTERNAL_ERROR',
    message: String(error.message || error),
  })
})

app.listen(PORT, '0.0.0.0', () => {
  console.log(
    `[fullstack-api] ${TABLE} -> http://127.0.0.1:${PORT}${COLLECTION_PATH}`,
  )
})
"""

# ------------------------------------------------------------
# scripts/dev.mjs：同时启动 Vite 与 Express
# ------------------------------------------------------------
DEV_SCRIPT_TEMPLATE = r"""// 由 FullstackGraph 生成：同时拉起前端 Vite 与 Express + SQLite 后端。
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

const apiEntry = ['server/index.js', 'server/index.mjs'].find((path) =>
  existsSync(path),
)
if (apiEntry) {
  start('api', process.execPath, [apiEntry])
} else {
  console.log('[vibe-dev] 未找到 server/index.js，仅启动前端')
}
start('vite', 'npm', ['run', 'dev:vite'])
"""

__all__ = [
    "DEV_SCRIPT_TEMPLATE",
    "EXPRESS_SERVER_TEMPLATE",
    "SQLITE_DB_TEMPLATE",
]
