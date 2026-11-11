"""全栈代码模板：Sequelize ORM 后端、Vite 双服务启动脚本与部署配置。

模板只保留 ``__PLACEHOLDER__``，由 ``coders.py`` 用契约中的表名、路由、
字段与种子数据做确定性替换。保持独立文件可以避免在 Python 字符串里
反复转义 JS 模板字面量。

数据库策略：

  - 生产：Sequelize ORM 通过 ``DATABASE_URL`` 连接 MySQL / PostgreSQL；
  - 沙盒预览：未配置 ``DATABASE_URL`` 时回退到 Node 22 内置
    ``node:sqlite``，不依赖外部数据库即可完成联调；
  - 两条路径导出同一组仓储函数，Express 路由与前端无需分支。
"""

from __future__ import annotations

# ------------------------------------------------------------
# server/db.js：Sequelize ORM + SQLite 预览回退
# ------------------------------------------------------------
SEQUELIZE_DB_TEMPLATE = r"""// 由 DBACoder 根据《全栈实施契约》生成。
// 生产路径：Sequelize ORM + DATABASE_URL（MySQL / PostgreSQL）。
// 沙盒预览：未配置 DATABASE_URL 时回退到 Node 22 内置 node:sqlite。
// 两条路径暴露同一组仓储函数，Express 路由与前端代码无需分支。
// node:sqlite 采用动态 import：生产配置 DATABASE_URL 时不会加载实验性模块。
import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const TABLE = '__TABLE__'
const ENTITY = '__ENTITY__'
const DB_ENGINE = (process.env.DB_ENGINE || '__DB_ENGINE__').toLowerCase()
const COLUMN_DEFS = __COLUMN_DEFS_JSON__
// DDL 约束元数据：NOT NULL / DEFAULT / UNIQUE / 主键。
// 只生成字段名不够——缺少 NOT NULL 列的默认值，生产 MySQL 会直接 500。
const COLUMN_META = __COLUMN_META_JSON__
const ALL_COLUMNS = COLUMN_DEFS.map(([name]) => name)
const WRITABLE_COLUMNS = ALL_COLUMNS.filter(
  (name) => !['id', 'created_at', 'updated_at', 'deleted_at'].includes(name),
)
// 写入时必须提供值的列：NOT NULL、无默认值、非主键/自增。
const REQUIRED_COLUMNS = Object.keys(COLUMN_META).filter((name) => {
  const meta = COLUMN_META[name] || {}
  return !meta.primary && !meta.autoIncrement && !meta.nullable && !meta.hasDefault
})
const UNIQUE_COLUMNS = Object.keys(COLUMN_META).filter(
  (name) => Boolean(COLUMN_META[name]?.unique),
)
// 前端表单统一使用 name 输入框；真正落库字段由 DDL 决定。
const DISPLAY_KEY = '__DISPLAY_KEY__'
const PAYLOAD_KEY = '__PAYLOAD_KEY__'
const SEED_ROWS = __SEED_ROWS_JSON__

// DATABASE_URL 是生产部署的唯一开关：存在时使用真实 ORM，缺失时走 SQLite。
const DATABASE_URL = (process.env.DATABASE_URL || '').trim()
const ORM_ENABLED = DATABASE_URL.length > 0
const STORAGE = ORM_ENABLED ? normalizeEngine(DB_ENGINE) : 'sqlite'
const RUNTIME = ORM_ENABLED
  ? `express-sequelize-${STORAGE}`
  : 'express-sqlite'

function normalizeEngine(value) {
  const engine = String(value || '').toLowerCase()
  if (engine === 'postgres' || engine === 'postgresql') return 'postgresql'
  if (engine === 'mysql' || engine === 'mariadb') return 'mysql'
  if (engine === 'sqlite') return 'sqlite'
  return engine || 'mysql'
}

// SQLite 数据库文件放在 /app/data，随容器文件系统持久化。
const DATA_DIR = process.env.FULLSTACK_DATA_DIR || resolve(__dirname, '..', 'data')
const DB_PATH = process.env.FULLSTACK_DB_PATH || resolve(DATA_DIR, 'fullstack.sqlite')

const columnDdl = COLUMN_DEFS.filter(([name]) => name !== 'id')
  .map(([name, type]) => `  "${name}" ${type}`)
  .join(',\n')
const SCHEMA_SQL = `CREATE TABLE IF NOT EXISTS "${TABLE}" (
  "id" INTEGER PRIMARY KEY AUTOINCREMENT${columnDdl ? ',\n' + columnDdl : ''}
);`

// ------------------------------------------------------------
// 公共写入值构造：创建 / 更新共用默认值策略
// ------------------------------------------------------------
// 为 NOT NULL 且无数据库默认值的列生成确定性默认值。
// 这是 Unknown column 之后的第二道生产 500 门禁：
// 例如 resumes.content 是 NOT NULL，旧模板只映射 name，必然写入失败。
function defaultForColumn(column) {
  const meta = COLUMN_META[column] || {}
  if (column === 'tenant_id') return 'tenant_local'
  if (UNIQUE_COLUMNS.includes(column)) {
    return `u-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  }
  if (/(^|_)id$/.test(column)) return 1
  if (/(amount|price|total|count|qty|size|score)$/i.test(column)) return 0
  if (/(^|_)(at|date)$/i.test(column) || /date|time/i.test(meta.type || '')) {
    return new Date().toISOString()
  }
  if (/json|payload|metadata/i.test(column)) return {}
  if (/status|state/i.test(column)) return 'ACTIVE'
  if (meta.sqliteType === 'INTEGER') return 1
  if (meta.sqliteType === 'REAL') return 0
  return `示例${ENTITY}`
}

function buildValues(input = {}, { update = false } = {}) {
  const body = { ...input }
  // 前端表单沿用 name/title；这里映射到 DDL 中真实的展示列。
  // 绝不把 name 当成固定列：resumes 的核心字段是 file_name。
  if (PAYLOAD_KEY && body[PAYLOAD_KEY] === undefined) {
    body[PAYLOAD_KEY] = body.name ?? body.title
  }
  const values = {}
  for (const column of WRITABLE_COLUMNS) {
    if (body[column] !== undefined) values[column] = body[column]
  }
  if (update) {
    if (ALL_COLUMNS.includes('updated_at')) {
      values.updated_at = new Date().toISOString()
    }
    return values
  }
  // NOT NULL 且无 DEFAULT 的列必须补齐，否则 MySQL 返回 500。
  for (const column of REQUIRED_COLUMNS) {
    if (values[column] === undefined) values[column] = defaultForColumn(column)
  }
  if (ALL_COLUMNS.includes('tenant_id') && values.tenant_id === undefined) {
    values.tenant_id = 'tenant_local'
  }
  return values
}

function encodeSqlite(value) {
  if (value === undefined || value === null) return null
  if (typeof value === 'object') return JSON.stringify(value)
  if (typeof value === 'boolean') return value ? 1 : 0
  return value
}

// ------------------------------------------------------------
// 生产路径：Sequelize ORM
// ------------------------------------------------------------
let ormPromise = null

function sequelizeType(name, sqliteType, DataTypes) {
  if (['created_at', 'updated_at', 'deleted_at'].includes(name)) {
    return DataTypes.DATE
  }
  if (['payload', 'metadata', 'data', 'json'].includes(name)) {
    return DataTypes.JSON
  }
  if (sqliteType === 'INTEGER') return DataTypes.INTEGER
  if (sqliteType === 'REAL') return DataTypes.FLOAT
  if (sqliteType === 'BLOB') return DataTypes.BLOB
  return DataTypes.TEXT
}

export async function createSequelize() {
  // 动态 import：沙盒未配置 DATABASE_URL 时不会加载 sequelize，
  // 因而无需在预览镜像里预装生产依赖。
  const { Sequelize } = await import('sequelize')
  const dialect = STORAGE === 'postgresql' ? 'postgres' : 'mysql'
  return new Sequelize(DATABASE_URL, {
    dialect,
    logging: false,
    // 云数据库（阿里云 RDS / Neon / Supabase）通常强制 TLS；
    // 是否启用必须由部署平台的环境变量决定，不能写死在代码里。
    dialectOptions:
      process.env.DB_SSL === 'true'
        ? { ssl: { require: true, rejectUnauthorized: false } }
        : {},
    pool: {
      max: Number(process.env.DB_POOL_MAX || 10),
      min: 0,
      acquire: 30000,
      idle: 10000,
    },
  })
}

async function connectOrm() {
  const { DataTypes } = await import('sequelize')
  const instance = await createSequelize()
  const attributes = {}
  for (const [name, sqliteType] of COLUMN_DEFS) {
    const isId = name === 'id'
    attributes[name] = {
      type: sequelizeType(name, sqliteType, DataTypes),
      allowNull: isId ? false : true,
      primaryKey: isId,
      autoIncrement: isId,
    }
  }
  const model = instance.define('Record', attributes, {
    tableName: TABLE,
    timestamps: false,
    freezeTableName: true,
  })

  await instance.authenticate()
  // 生产环境绝不调用 model.sync() 自动建表：表结构唯一来源是
  // server/migrations/*.sql，由 `npm run db:migrate` 显式执行。
  // 未迁移时健康检查返回 503，让部署平台在发布阶段就暴露问题。
  if (process.env.DB_SEED === 'true') {
    await seedOrm(model)
  }
  return { instance, model }
}

function getOrm() {
  if (!ORM_ENABLED) return null
  if (!ormPromise) ormPromise = connectOrm()
  return ormPromise
}

async function seedOrm(model) {
  const total = await model.count()
  if (Number(total) > 0) return
  for (const row of SEED_ROWS) {
    await model.create(buildValues(row))
  }
}

// ------------------------------------------------------------
// 本地预览路径：Node 22 内置 SQLite
// ------------------------------------------------------------
async function createSqliteState() {
  const { DatabaseSync } = await import('node:sqlite')
  mkdirSync(DATA_DIR, { recursive: true })
  const db = new DatabaseSync(DB_PATH)
  db.exec('PRAGMA busy_timeout = 5000;')
  db.exec(SCHEMA_SQL)
  const state = { db }
  await seedSqlite(state)
  return state
}

function getSqliteState() {
  // 单例 Promise：并发首个请求不会重复建库或重复灌种子数据。
  if (!sqliteStatePromise) sqliteStatePromise = createSqliteState()
  return sqliteStatePromise
}

function sqliteAll(state, sql, params = []) {
  return state.db.prepare(sql).all(...params)
}

function sqliteGet(state, sql, params = []) {
  return state.db.prepare(sql).get(...params) ?? null
}

function sqliteRun(state, sql, params = []) {
  const result = state.db.prepare(sql).run(...params)
  return {
    lastID: Number(result.lastInsertRowid),
    changes: Number(result.changes),
  }
}

function sqliteInsert(state, values) {
  const columns = Object.keys(values)
  if (columns.length === 0) throw new Error('没有可写入的字段')
  const sql =
    `INSERT INTO "${TABLE}" (` +
    columns.map((column) => `"${column}"`).join(', ') +
    ') VALUES (' +
    columns.map(() => '?').join(', ') +
    ')'
  const result = sqliteRun(
    state,
    sql,
    columns.map((column) => encodeSqlite(values[column])),
  )
  return sqliteGetRecord(state, result.lastID)
}

function sqliteGetRecord(state, id) {
  return sqliteGet(state, `SELECT * FROM "${TABLE}" WHERE "id" = ?`, [Number(id)])
}

function seedSqlite(state) {
  const total = sqliteGet(state, `SELECT COUNT(*) AS total FROM "${TABLE}"`)
  if (Number(total?.total ?? 0) > 0) return
  for (const row of SEED_ROWS) {
    sqliteInsert(state, buildValues(row))
  }
}

let sqliteStatePromise = null

// ------------------------------------------------------------
// 对外仓储接口：ORM 与 SQLite 行为一致
// ------------------------------------------------------------
export async function listRecords({ page = 1, size = 20 } = {}) {
  if (ORM_ENABLED) {
    const { model } = await getOrm()
    const { rows, count } = await model.findAndCountAll({
      limit: size,
      offset: (page - 1) * size,
      order: [['id', 'DESC']],
      raw: true,
    })
    return { items: rows, total: Number(count), page, size }
  }
  const state = await getSqliteState()
  const total = sqliteGet(
    state,
    `SELECT COUNT(*) AS total FROM "${TABLE}"`,
  )
  const items = sqliteAll(
    state,
    `SELECT * FROM "${TABLE}" ORDER BY "id" DESC LIMIT ? OFFSET ?`,
    [size, (page - 1) * size],
  )
  return {
    items,
    total: Number(total?.total ?? 0),
    page,
    size,
  }
}

export async function getRecord(id) {
  if (ORM_ENABLED) {
    const { model } = await getOrm()
    const row = await model.findByPk(Number(id), { raw: true })
    return row ?? null
  }
  return sqliteGetRecord(await getSqliteState(), id)
}

export async function createRecord(input = {}) {
  const values = buildValues(input)
  if (ORM_ENABLED) {
    const { model } = await getOrm()
    const row = await model.create(values)
    return row.get({ plain: true })
  }
  return sqliteInsert(await getSqliteState(), values)
}

export async function updateRecord(id, input = {}) {
  const values = buildValues(input, { update: true })
  if (ORM_ENABLED) {
    const { model } = await getOrm()
    const row = await model.findByPk(Number(id))
    if (!row) return null
    if (Object.keys(values).length > 0) await row.update(values)
    return row.get({ plain: true })
  }
  const state = await getSqliteState()
  const columns = Object.keys(values)
  if (columns.length === 0) return sqliteGetRecord(state, id)
  sqliteRun(
    state,
    `UPDATE "${TABLE}" SET ` +
      columns.map((column) => `"${column}" = ?`).join(', ') +
      ' WHERE "id" = ?',
    [
      ...columns.map((column) => encodeSqlite(values[column])),
      Number(id),
    ],
  )
  return sqliteGetRecord(state, id)
}

export async function deleteRecord(id) {
  if (ORM_ENABLED) {
    const { model } = await getOrm()
    const changes = await model.destroy({ where: { id: Number(id) } })
    return { deleted: changes > 0, id: Number(id) }
  }
  const result = sqliteRun(
    await getSqliteState(),
    `DELETE FROM "${TABLE}" WHERE "id" = ?`,
    [Number(id)],
  )
  return { deleted: result.changes > 0, id: Number(id) }
}

export async function health() {
  if (ORM_ENABLED) {
    const { instance, model } = await getOrm()
    // authenticate 验证连接与账号权限；失败时直接抛出，由 /api/health
    // 转为 HTTP 503，让负载均衡 / 部署平台停止摘流。
    await instance.authenticate()
    let tableReady = true
    let records = 0
    let reason = ''
    try {
      records = Number(await model.count())
    } catch (error) {
      // 连接正常但表不存在：通常是迁移未执行，返回 degraded 而不是 ok。
      tableReady = false
      reason = String(error?.message || error)
    }
    return {
      status: tableReady ? 'ok' : 'degraded',
      runtime: RUNTIME,
      storage: STORAGE,
      orm: 'sequelize',
      databaseUrlConfigured: true,
      table: TABLE,
      entity: ENTITY,
      tableReady,
      records,
      ...(reason ? { reason } : {}),
    }
  }
  const total = sqliteGet(
    await getSqliteState(),
    `SELECT COUNT(*) AS total FROM "${TABLE}"`,
  )
  return {
    status: 'ok',
    runtime: 'express-sqlite',
    storage: 'sqlite',
    orm: 'sqlite-fallback',
    databaseUrlConfigured: false,
    table: TABLE,
    entity: ENTITY,
    tableReady: true,
    records: Number(total?.total ?? 0),
    dbPath: DB_PATH,
  }
}

export {
  DATABASE_URL,
  DB_ENGINE,
  DB_PATH,
  ORM_ENABLED,
  RUNTIME,
  SCHEMA_SQL,
  STORAGE,
}
"""

# ------------------------------------------------------------
# server/index.js：Express REST 服务
# ------------------------------------------------------------
EXPRESS_SERVER_TEMPLATE = r"""// 由 BackendCoder 根据《全栈实施契约》生成。
// Express + Sequelize：真实 REST 路由，Vite 通过 /api 同源代理访问。
// 生产环境由 DATABASE_URL 选择 MySQL / PostgreSQL；沙盒自动回退 SQLite。
import express from 'express'
__UPLOAD_IMPORTS__
__AI_IMPORTS__
import {
  ORM_ENABLED,
  RUNTIME,
  STORAGE,
  createRecord,
  deleteRecord,
  getRecord,
  health,
  listRecords,
  updateRecord,
} from './db.js'

const PORT = Number(
  process.env.PORT || process.env.FULLSTACK_API_PORT || __API_PORT__,
)
const TABLE = '__TABLE__'
const ENTITY = '__ENTITY__'
const COLLECTION_PATH = '__COLLECTION_PATH__'
const ITEM_PATH = '__ITEM_PATH__'
const IS_PRODUCTION = process.env.NODE_ENV === 'production'
// CORS 白名单只从环境变量读取：支持逗号分隔的多个前端域名。
// 生产环境未配置时默认拒绝跨域（同源部署不受影响），绝不隐式放开 *。
const CORS_ORIGINS = String(
  process.env.CORS_ORIGINS || process.env.CORS_ORIGIN || '',
)
  .split(',')
  .map((item) => item.trim())
  .filter(Boolean)
const ALLOW_ANY_ORIGIN =
  CORS_ORIGINS.includes('*') || (!IS_PRODUCTION && CORS_ORIGINS.length === 0)

const app = express()
app.disable('x-powered-by')
// Render / 阿里云 SLB 会终止 TLS 并在 X-Forwarded-* 头里传真实协议与 IP。
app.set('trust proxy', 1)
app.use(express.json({ limit: '1mb' }))
app.use((request, response, next) => {
  const origin = String(request.headers.origin || '')
  const allowedOrigin = ALLOW_ANY_ORIGIN
    ? '*'
    : CORS_ORIGINS.includes(origin)
      ? origin
      : ''
  if (allowedOrigin) {
    response.setHeader('Access-Control-Allow-Origin', allowedOrigin)
    if (allowedOrigin !== '*') response.setHeader('Vary', 'Origin')
    response.setHeader(
      'Access-Control-Allow-Headers',
      'Content-Type, Authorization, Idempotency-Key',
    )
    response.setHeader(
      'Access-Control-Allow-Methods',
      'GET,POST,PUT,PATCH,DELETE,OPTIONS',
    )
  }
  if (request.method === 'OPTIONS') {
    // 未命中白名单的预检请求不返回 Allow-*，浏览器会按 CORS 规则拒绝。
    return response.status(allowedOrigin ? 204 : 403).end()
  }
  if (!IS_PRODUCTION && !allowedOrigin && origin) {
    console.warn(`[fullstack-api] CORS 拒绝来源: ${origin}`)
  }
  next()
})

// Express 4 不会自动捕获 async 异常，统一转交给错误中间件。
const asyncHandler = (handler) => (request, response, next) =>
  Promise.resolve(handler(request, response, next)).catch(next)

__UPLOAD_MIDDLEWARE__
__AI_MIDDLEWARE__

const listHandler = asyncHandler(async (request, response) => {
  const page = Math.max(1, Number(request.query.page || 1))
  const size = Math.min(100, Math.max(1, Number(request.query.size || 20)))
  const result = await listRecords({ page, size })
  response.json({ ...result, table: TABLE, storage: STORAGE })
})

const createHandler = asyncHandler(async (request, response) => {
  const body =
    request.body && typeof request.body === 'object' ? { ...request.body } : {}
__UPLOAD_BODY_PATCH__
  const record = await createRecord(body)
  response.status(201).json({
    ...record,
__UPLOAD_RESPONSE_FIELDS__    success: true,
    message: '提交成功，数据已写入数据库',
  })
})

// 健康检查必须真实探测数据库：连接失败或迁移未执行时返回 503。
// 负载均衡 / Render healthCheckPath 依据状态码决定是否接流。
app.get('/api/health', async (_request, response) => {
  try {
    const result = await health()
    response.status(result.status === 'ok' ? 200 : 503).json(result)
  } catch (error) {
    response.status(503).json({
      status: 'error',
      runtime: RUNTIME,
      storage: STORAGE,
      orm: ORM_ENABLED ? 'sequelize' : 'sqlite-fallback',
      databaseUrlConfigured: ORM_ENABLED,
      message: String(error?.message || error),
    })
  }
})

app.get(COLLECTION_PATH, listHandler)
app.post(COLLECTION_PATH, __COLLECTION_POST_MIDDLEWARE__createHandler)
// 通用数据别名：便于外部脚本和前端用同一张表做快速验证。
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

// ---- AI-Native 路由 ----
__AI_ROUTES__

// ---- 契约业务路由 ----
// BackendCoder 为契约里的每个业务动作生成独立端点；通用 CRUD 只是基础能力，
// 绝不能替代抓取 / 改写 / 投递 / 对话等语义路由。
__CONTRACT_BUSINESS_ROUTES__

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

const server = app.listen(PORT, '0.0.0.0', () => {
  console.log(
    `[fullstack-api] ${TABLE} storage=${STORAGE} -> ` +
      `http://127.0.0.1:${PORT}${COLLECTION_PATH}`,
  )
})

// 容器编排会发送 SIGTERM；先停止接收新请求，再退出进程。
const shutdown = (signal) => {
  console.log(`[fullstack-api] received ${signal}, shutting down`)
  server.close(() => process.exit(0))
}
process.on('SIGTERM', () => shutdown('SIGTERM'))
process.on('SIGINT', () => shutdown('SIGINT'))
"""

# ------------------------------------------------------------
# server/migrations.mjs：SQL 迁移执行器
# ------------------------------------------------------------
MIGRATION_RUNNER_TEMPLATE = r"""// 由 DBACoder 生成：按文件名顺序执行 server/migrations/*.sql。
// 生产环境禁止使用 Sequelize sync() 隐式建表；所有结构变更必须落成
// 可审计、可回放的 SQL 迁移，并记录在 _schema_migrations 表中。
import { createHash } from 'node:crypto'
import { readdir, readFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { ORM_ENABLED, STORAGE, createSequelize } from './db.js'

const __dirname = dirname(fileURLToPath(import.meta.url))
const MIGRATIONS_DIR = process.env.MIGRATIONS_DIR || join(__dirname, 'migrations')
const MIGRATION_TABLE = '_schema_migrations'

const TRACKING_DDL =
  STORAGE === 'postgresql'
    ? `CREATE TABLE IF NOT EXISTS ${MIGRATION_TABLE} (
        id BIGSERIAL PRIMARY KEY,
        filename VARCHAR(255) NOT NULL UNIQUE,
        checksum VARCHAR(64) NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
      )`
    : `CREATE TABLE IF NOT EXISTS ${MIGRATION_TABLE} (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        filename VARCHAR(255) NOT NULL UNIQUE,
        checksum VARCHAR(64) NOT NULL,
        applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
      ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`

// 逐字符切分 SQL，避免把字符串 / 注释里的分号误当语句结束。
function splitSqlStatements(sql) {
  const statements = []
  let current = ''
  let quote = ''
  let lineComment = false
  let blockComment = false
  for (let index = 0; index < sql.length; index += 1) {
    const char = sql[index]
    const next = sql[index + 1]
    if (lineComment) {
      if (char === '\n') lineComment = false
      current += char
      continue
    }
    if (blockComment) {
      current += char
      if (char === '*' && next === '/') {
        current += next
        index += 1
        blockComment = false
      }
      continue
    }
    if (!quote && char === '-' && next === '-') {
      lineComment = true
      current += char + next
      index += 1
      continue
    }
    if (!quote && char === '/' && next === '*') {
      blockComment = true
      current += char + next
      index += 1
      continue
    }
    if (quote) {
      current += char
      if (char === quote) {
        if (sql[index + 1] === quote) {
          current += sql[index + 1]
          index += 1
        } else {
          quote = ''
        }
      } else if (char === '\\') {
        current += next || ''
        index += 1
      }
      continue
    }
    if (char === "'" || char === '"' || char === '`') {
      quote = char
      current += char
      continue
    }
    if (char === ';') {
      if (current.trim()) statements.push(current.trim())
      current = ''
      continue
    }
    current += char
  }
  if (current.trim()) statements.push(current.trim())
  return statements
}

function checksumOf(sql) {
  return createHash('sha256').update(sql, 'utf8').digest('hex')
}

export async function runMigrations() {
  if (!ORM_ENABLED) {
    return {
      storage: 'sqlite',
      skipped: true,
      applied: [],
      reason: 'DATABASE_URL 未配置，生产迁移中止；SQLite 预览库不参与迁移',
    }
  }
  const sequelize = await createSequelize()
  const appliedNow = []
  const skipped = []
  try {
    await sequelize.authenticate()
    await sequelize.query(TRACKING_DDL)
    const files = (await readdir(MIGRATIONS_DIR))
      .filter((name) => name.endsWith('.sql'))
      .sort()
    if (files.length === 0) {
      throw new Error(`未找到迁移文件: ${MIGRATIONS_DIR}`)
    }
    const [rows] = await sequelize.query(
      `SELECT filename, checksum FROM ${MIGRATION_TABLE}`,
    )
    const applied = new Map(
      (rows || []).map((row) => [String(row.filename), String(row.checksum)]),
    )
    for (const filename of files) {
      const sql = await readFile(join(MIGRATIONS_DIR, filename), 'utf8')
      const checksum = checksumOf(sql)
      if (applied.has(filename)) {
        if (applied.get(filename) !== checksum) {
          throw new Error(
            `迁移 ${filename} 已执行但内容被修改；` +
              '生产环境禁止改写历史迁移，请新增下一版迁移文件',
          )
        }
        skipped.push(filename)
        continue
      }
      const statements = splitSqlStatements(sql)
      if (statements.length === 0) {
        throw new Error(`迁移文件为空或无法解析: ${filename}`)
      }
      await sequelize.transaction(async (transaction) => {
        for (const statement of statements) {
          await sequelize.query(statement, { transaction })
        }
        await sequelize.query(
          `INSERT INTO ${MIGRATION_TABLE} (filename, checksum) VALUES (?, ?)`,
          { transaction, replacements: [filename, checksum] },
        )
      })
      appliedNow.push(filename)
      console.log(`[fullstack-db] applied ${filename}`)
    }
    return {
      storage: STORAGE,
      skipped: false,
      applied: appliedNow,
      alreadyApplied: skipped,
    }
  } finally {
    await sequelize.close()
  }
}

export { splitSqlStatements }
"""

# ------------------------------------------------------------
# server/migrate.mjs：生产数据库迁移入口
# ------------------------------------------------------------
MIGRATE_SCRIPT_TEMPLATE = r"""// 由 DBACoder 生成：显式执行数据库迁移。
// 生产：node server/migrate.mjs（需要 DATABASE_URL）。
// 迁移失败立刻以非 0 退出码终止发布，绝不让服务带着旧表结构上线。
import { runMigrations } from './migrations.mjs'

try {
  const result = await runMigrations()
  if (result.skipped) {
    console.error('[fullstack-db] 迁移中止：', result.reason)
    process.exit(1)
  }
  console.log('[fullstack-db] migrate', result)
  process.exit(0)
} catch (error) {
  console.error('[fullstack-db] 迁移失败：', error)
  process.exit(1)
}
"""

# ------------------------------------------------------------
# scripts/dev.mjs：同时启动 Vite 与 Express
# ------------------------------------------------------------
DEV_SCRIPT_TEMPLATE = r"""// 由 FullstackGraph 生成：同时拉起前端 Vite 与 Express 后端。
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

# ------------------------------------------------------------
# Infrastructure as Code：Dockerfile / Render Blueprint / env
# ------------------------------------------------------------
BACKEND_DOCKERFILE_TEMPLATE = r"""# syntax=docker/dockerfile:1
# 由 InfraCoder 生成：后端 API 生产镜像（多阶段构建）。
# 构建上下文为项目根目录：docker build -t __SERVICE_NAME__-api .
#
# 阶段 1 只负责安装生产依赖；阶段 2 仅复制 node_modules 与源码，
# 不携带 npm 缓存、构建工具和 devDependencies，镜像体积更小、攻击面更低。

FROM node:22-alpine AS deps
WORKDIR /app
# 先复制依赖清单，命中 Docker 层缓存；生产依赖包含 Sequelize 与驱动。
COPY server/package.json ./server/package.json
RUN cd server && npm install --omit=dev --no-audit --no-fund

FROM node:22-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    PORT=3000
# 保留 server/ 目录层级：db.js 默认把 SQLite 放在 /app/data，
# 与沙盒目录结构一致，未配置 DATABASE_URL 时也能直接写库。
COPY --from=deps --chown=node:node /app/server/node_modules ./server/node_modules
COPY --chown=node:node server/ ./server
# COPY 以 root 写入时必须显式 --chown 或在切换用户前再次移交属主；
# 否则 node 用户无法创建 server/uploads（multer 落盘）与 data/（SQLite），
# 上传会直接 500 EACCES。
RUN mkdir -p /app/server/uploads /app/data \
    && chown -R node:node /app
USER node

EXPOSE 3000
# 生产发布流程：先 node server/migrate.mjs，再启动本镜像。
CMD ["node", "server/index.js"]
"""

DOCKERIGNORE_TEMPLATE = r"""# 构建镜像时排除本地依赖、运行期数据与敏感配置。
node_modules
dist
data
.git
.env
.env.*
!.env.example
*.sqlite
*.sqlite3
*.db
*.log
docs
reports
"""

RENDER_YAML_TEMPLATE = r"""# 由 InfraCoder 生成：Render Blueprint（后端 Docker + 前端静态站）。
# 使用方法：把项目推到 Git 仓库，在 Render 选择 New > Blueprint，
# 指向本文件；随后填写 DATABASE_URL、CORS_ORIGIN 与 VITE_API_BASE_URL。
services:
  - type: web
    name: __SERVICE_NAME__-api
    runtime: docker
    dockerfilePath: ./Dockerfile
    healthCheckPath: /api/health
    # 发布前显式执行 SQL 迁移；迁移失败会阻断本次部署。
    preDeployCommand: node server/migrate.mjs
    envVars:
      - key: NODE_ENV
        value: production
      - key: DATABASE_URL
        sync: false
      - key: DB_ENGINE
        value: __DB_ENGINE__
      - key: DB_SSL
        sync: false
      - key: CORS_ORIGINS
        sync: false
  - type: web
    name: __SERVICE_NAME__-web
    runtime: static
    buildCommand: npm install --no-audit --no-fund && npm run build
    staticPublishPath: dist
    envVars:
      - key: VITE_API_BASE_URL
        sync: false
"""

ENV_EXAMPLE_TEMPLATE = r"""# ============================================================
# __PROJECT_NAME__ · 生产部署环境变量
# 复制为 .env 后按需填写；真实 .env 不得提交到 Git。
# 所有连接串 / 端口 / CORS 白名单都只从这里读取，代码中不得硬编码。
# ============================================================

# ---- 运行环境 ----
NODE_ENV=production

# ---- 数据库（生产必填）----
# MySQL:      mysql://user:password@db-host:3306/database?charset=utf8mb4
# PostgreSQL: postgresql://user:password@db-host:5432/database?sslmode=require
# 留空时后端回退到本地 SQLite，仅用于沙盒预览 / 本地测试。
DATABASE_URL=
DB_ENGINE=__DB_ENGINE__
# 云数据库通常要求 TLS；本地 Docker MySQL 保持 false。
DB_SSL=false
DB_POOL_MAX=10
DB_SEED=false
# 迁移目录默认 server/migrations；一般无需修改。
MIGRATIONS_DIR=
# 迁移必须显式执行：node server/migrate.mjs
# 生产不会通过 Sequelize sync() 自动建表。

# ---- 后端 API ----
PORT=3000
FULLSTACK_API_PORT=3000
FULLSTACK_DATA_DIR=./data
# CORS 白名单：逗号分隔的完整 Origin，例如
# CORS_ORIGINS=https://app.example.com,https://admin.example.com
# 生产留空表示只允许同源访问；* 仅用于本地联调。
CORS_ORIGINS=

# ---- 前端构建（Vite）----
# 生产环境填写后端公网地址；留空则使用同源 /api 代理（沙盒默认）。
VITE_API_BASE_URL=
"""

DEPLOY_GUIDE_TEMPLATE = r"""# __PROJECT_NAME__ · 部署指引

本目录由 Vibe Platform 的 InfraCoder 自动生成，已包含：

| 文件 | 用途 |
|---|---|
| `Dockerfile` | 后端 API 生产镜像（Express + Sequelize） |
| `.dockerignore` | 隔离本地依赖、运行期数据与密钥 |
| `render.yaml` | Render Blueprint：后端 Docker + 前端静态站 |
| `.env.example` | 数据库地址、端口、CORS 等环境变量模板 |
| `DEPLOY.md` | 本部署指引 |

目标数据库：**__DB_ENGINE_LABEL__**（ORM: Sequelize，连接串由 `DATABASE_URL` 注入）。

## 1. 导出代码包

在工作区点击「导出」，或使用平台提供的归档下载；导出包不包含
`node_modules`、`dist`、`.env` 与本地 SQLite 数据文件。

## 2. 准备云数据库

1. 在云厂商（阿里云 RDS / 腾讯云 / Neon / Supabase / Railway 等）创建
   __DB_ENGINE_LABEL__ 实例；
2. 创建业务库与账号，拿到连接串；
3. 生产连接串写入部署平台的 `DATABASE_URL`，不要写进代码或 Git。

## 3. Render 一键部署（推荐）

1. 将代码推到 GitHub / GitLab；
2. Render → **New → Blueprint**，选择仓库与 `render.yaml`；
3. 填写：
   - `DATABASE_URL`：第 2 步的数据库连接串；
   - `CORS_ORIGINS`：前端公网地址，逗号分隔多个域名（生产禁止 `*`）；
   - `DB_SSL`：云数据库通常填 `true`；
   - `VITE_API_BASE_URL`：后端 API 公网地址，例如
     `https://__SERVICE_NAME__-api.onrender.com`；
4. 应用 Blueprint：Render 会先执行 `node server/migrate.mjs`，
   迁移成功后才启动 Docker 镜像，最后构建前端静态站；
5. 打开 `https://<后端域名>/api/health`，确认 HTTP 200、`status=ok`、
   `tableReady=true` 且 `storage=__DB_ENGINE__`。

## 4. Vercel 部署前端（可选）

1. Vercel → **Add New → Project**，导入同一仓库；
2. Framework 选择 Vite，Build Command `npm run build`，Output `dist`；
3. 环境变量 `VITE_API_BASE_URL` 填写后端公网地址；
4. 部署完成后，把 Vercel 域名加入后端的 `CORS_ORIGINS`。

## 5. Railway / 阿里云 / 其它 Docker 平台

```bash
# 1) 构建生产镜像（多阶段构建，不包含 devDependencies）
docker build -t __SERVICE_NAME__-api .

# 2) 先执行 SQL 迁移；失败会以非 0 退出，禁止继续发布
docker run --rm --env-file .env \
  __SERVICE_NAME__-api node server/migrate.mjs

# 3) 迁移成功后再启动服务
docker run --rm -p 3000:3000 --env-file .env \
  __SERVICE_NAME__-api
```

平台会自动读取根目录 `Dockerfile`；也可以把镜像推送到任意容器 Registry。
生产环境必须把迁移作为独立发布步骤，不能依赖应用启动时自动建表。

## 6. 数据库迁移与验收（生产必读）

- 迁移文件：`server/migrations/001_init.sql`（随契约 DDL 生成）；
- 执行入口：`node server/migrate.mjs` / `npm run db:migrate`；
- 执行顺序：按文件名升序；已执行文件记录在 `_schema_migrations`，
  重复执行不会重复建表；改动历史文件会因 checksum 不一致直接报错；
- 禁止在生产调用 Sequelize `sync()`：应用启动只连接数据库，不建表；
- 新增结构变更：新增 `002_xxx.sql`，不要修改 `001_init.sql`；
- 验收：
  1. `GET /api/health` 返回 HTTP 200、`status=ok`、`tableReady=true`；
  2. `GET __COLLECTION_PATH__` 能读取列表；
  3. `POST __COLLECTION_PATH__` 能写入并再次查询到记录；
- 本地沙盒：未配置 `DATABASE_URL` 时自动使用 SQLite，便于先联调再上云。

> 安全提示：`DATABASE_URL`、账号密码与 `CORS_ORIGINS` 必须通过部署平台的
> 环境变量注入；不要把真实密钥写入仓库、镜像或前端构建产物。
"""

__all__ = [
    "BACKEND_DOCKERFILE_TEMPLATE",
    "DEPLOY_GUIDE_TEMPLATE",
    "DEV_SCRIPT_TEMPLATE",
    "DOCKERIGNORE_TEMPLATE",
    "ENV_EXAMPLE_TEMPLATE",
    "EXPRESS_SERVER_TEMPLATE",
    "MIGRATE_SCRIPT_TEMPLATE",
    "MIGRATION_RUNNER_TEMPLATE",
    "RENDER_YAML_TEMPLATE",
    "SEQUELIZE_DB_TEMPLATE",
]
