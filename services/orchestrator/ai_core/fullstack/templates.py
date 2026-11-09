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
const ALL_COLUMNS = COLUMN_DEFS.map(([name]) => name)
const WRITABLE_COLUMNS = ALL_COLUMNS.filter(
  (name) => !['id', 'created_at', 'updated_at', 'deleted_at'].includes(name),
)
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
function buildValues(input = {}, { update = false } = {}) {
  const body = { ...input }
  if (body.name === undefined && body.title !== undefined) body.name = body.title
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

async function connectOrm() {
  // 动态 import：沙盒未配置 DATABASE_URL 时不会加载 sequelize，
  // 因而无需在预览镜像里预装生产依赖。
  const { Sequelize, DataTypes } = await import('sequelize')
  const dialect = STORAGE === 'postgresql' ? 'postgres' : 'mysql'
  const instance = new Sequelize(DATABASE_URL, {
    dialect,
    logging: false,
    dialectOptions:
      process.env.DB_SSL === 'true'
        ? { ssl: { require: true, rejectUnauthorized: false } }
        : {},
  })

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
  // 首次部署可通过 DB_AUTO_MIGRATE=true 自动建表；成熟项目应改用迁移脚本。
  if (process.env.DB_AUTO_MIGRATE === 'true') {
    await model.sync()
  }
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
    await instance.authenticate()
    const records = await model.count()
    return {
      status: 'ok',
      runtime: RUNTIME,
      storage: STORAGE,
      orm: 'sequelize',
      databaseUrlConfigured: true,
      table: TABLE,
      entity: ENTITY,
      records: Number(records),
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
    records: Number(total?.total ?? 0),
    dbPath: DB_PATH,
  }
}

/**
 * 显式迁移入口：生产环境可执行 `npm run db:migrate`。
 * SQLite 预览库在模块加载时已经自动建表。
 */
export async function migrate() {
  if (!ORM_ENABLED) {
    return {
      storage: 'sqlite',
      migrated: false,
      reason: 'DATABASE_URL 未配置，SQLite 预览库已自动建表',
    }
  }
  const { model } = await getOrm()
  await model.sync()
  return { storage: STORAGE, migrated: true, table: TABLE }
}

export { DB_PATH, ORM_ENABLED, RUNTIME, SCHEMA_SQL, STORAGE }
"""

# ------------------------------------------------------------
# server/index.js：Express REST 服务
# ------------------------------------------------------------
EXPRESS_SERVER_TEMPLATE = r"""// 由 BackendCoder 根据《全栈实施契约》生成。
// Express + Sequelize：真实 REST 路由，Vite 通过 /api 同源代理访问。
// 生产环境由 DATABASE_URL 选择 MySQL / PostgreSQL；沙盒自动回退 SQLite。
import express from 'express'
import {
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
const CORS_ORIGIN = process.env.CORS_ORIGIN || '*'

const app = express()
app.disable('x-powered-by')
app.use(express.json({ limit: '1mb' }))
app.use((request, response, next) => {
  response.setHeader('Access-Control-Allow-Origin', CORS_ORIGIN)
  if (CORS_ORIGIN !== '*') response.setHeader('Vary', 'Origin')
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
  response.json({ ...result, table: TABLE, storage: STORAGE })
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
# server/migrate.mjs：生产数据库迁移入口
# ------------------------------------------------------------
MIGRATE_SCRIPT_TEMPLATE = r"""// 由 DBACoder 生成：显式执行数据库迁移。
// 生产：npm run db:migrate（需要 DATABASE_URL）。
// 沙盒：DATABASE_URL 缺失时仅提示 SQLite 预览库已自动建表。
import { migrate } from './db.js'

const result = await migrate()
console.log('[fullstack-db] migrate', result)
process.exit(0)
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
# 由 InfraCoder 生成：后端 API 生产镜像。
# 构建上下文为项目根目录：docker build -t __SERVICE_NAME__-api .
FROM node:22-alpine

WORKDIR /app
ENV NODE_ENV=production

# 先复制依赖清单，命中 Docker 层缓存；生产依赖包含 Sequelize 与驱动。
COPY server/package.json ./package.json
RUN npm install --omit=dev --no-audit --no-fund \
    && chown -R node:node /app

COPY server/ ./
USER node

EXPOSE 3000
CMD ["node", "index.js"]
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
    envVars:
      - key: DATABASE_URL
        sync: false
      - key: DB_ENGINE
        value: __DB_ENGINE__
      # 首次部署自动建表；稳定后建议改为 false 并使用 npm run db:migrate。
      - key: DB_AUTO_MIGRATE
        value: "true"
      - key: CORS_ORIGIN
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
# ============================================================

# ---- 数据库（生产必填）----
# MySQL:      mysql://user:password@db-host:3306/database?charset=utf8mb4
# PostgreSQL: postgresql://user:password@db-host:5432/database
# 留空时后端回退到本地 SQLite，仅用于沙盒预览 / 本地测试。
DATABASE_URL=
DB_ENGINE=__DB_ENGINE__
# 首次部署设为 true 自动建表；稳定后建议关闭并使用 npm run db:migrate。
DB_AUTO_MIGRATE=false
DB_SSL=false
DB_SEED=false

# ---- 后端 API ----
PORT=3000
FULLSTACK_API_PORT=3000
FULLSTACK_DATA_DIR=./data
# 生产环境填写前端公网地址；* 仅用于本地联调。
CORS_ORIGIN=*

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
   - `CORS_ORIGIN`：前端公网地址（首次可先填 `*`）；
   - `VITE_API_BASE_URL`：后端 API 公网地址，例如
     `https://__SERVICE_NAME__-api.onrender.com`；
4. 应用 Blueprint：后端使用 Docker 镜像启动，前端构建为静态站；
5. 打开 `https://<后端域名>/api/health`，确认 `status=ok` 且
   `storage=__DB_ENGINE__`。

## 4. Vercel 部署前端（可选）

1. Vercel → **Add New → Project**，导入同一仓库；
2. Framework 选择 Vite，Build Command `npm run build`，Output `dist`；
3. 环境变量 `VITE_API_BASE_URL` 填写后端公网地址；
4. 部署完成后，把 Vercel 域名加入后端的 `CORS_ORIGIN`。

## 5. Railway / 其它 Docker 平台

```bash
docker build -t __SERVICE_NAME__-api .
docker run --rm -p 3000:3000 \
  -e DATABASE_URL='<你的连接串>' \
  -e DB_ENGINE='__DB_ENGINE__' \
  -e DB_AUTO_MIGRATE=true \
  -e CORS_ORIGIN='*' \
  __SERVICE_NAME__-api
```

平台会自动读取根目录 `Dockerfile`；也可以把镜像推送到任意容器 Registry。

## 6. 数据库迁移与验收

- 首次部署：`DB_AUTO_MIGRATE=true` 会调用 Sequelize `sync()` 建表；
- 稳定后：改为 `false`，在发布流程执行 `npm run db:migrate`；
- 验收：`GET /api/health`、`GET __COLLECTION_PATH__`、`POST __COLLECTION_PATH__`；
- 本地沙盒：未配置 `DATABASE_URL` 时自动使用 SQLite，便于先联调再上云。

> 安全提示：`DATABASE_URL`、账号密码与 `CORS_ORIGIN` 必须通过部署平台的
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
    "RENDER_YAML_TEMPLATE",
    "SEQUELIZE_DB_TEMPLATE",
]
