// 由 BackendCoder 根据《全栈实施契约》生成。
// Express + Sequelize：真实 REST 路由，Vite 通过 /api 同源代理访问。
// 生产环境由 DATABASE_URL 选择 MySQL / PostgreSQL；沙盒自动回退 SQLite。
import express from 'express'
import multer from 'multer'
import { mkdirSync } from 'node:fs'
import { extname } from 'node:path'
import { fileURLToPath } from 'node:url'


import {
  authenticate,
  authenticateOptional,
  loginHandler,
  meHandler,
  registerHandler,
  requireWriteRole,
} from './auth.js'

import {
  ORM_ENABLED,
  RUNTIME,
  STORAGE,
  createRecord,
  createRelatedRecord,
  createTableRecord,
  deleteRecord,
  deleteTableRecord,
  getRecord,
  getTableRecord,
  health,
  listRecords,
  listRelatedRecords,
  listTableRecords,
  updateRecord,
  updateTableRecord,
} from './db.js'

const PORT = Number(
  process.env.PORT || process.env.FULLSTACK_API_PORT || 3000,
)
const TABLE = 'facilities'
const ENTITY = '登录表单'
const COLLECTION_PATH = '/api/v1/facilities'
const ITEM_PATH = '/api/v1/facilities/:facility_id'
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

// 契约要求文件上传：磁盘存储 + 5MB 限制，文件落在 server/uploads。
const UPLOAD_DIR = fileURLToPath(new URL('./uploads/', import.meta.url))
mkdirSync(UPLOAD_DIR, { recursive: true })
const upload = multer({
  storage: multer.diskStorage({
    destination: (_request, _file, callback) => callback(null, UPLOAD_DIR),
    filename: (_request, file, callback) => {
      const suffix = extname(file.originalname || '').slice(0, 12)
      const stamp = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
      callback(null, `${stamp}${suffix}`)
    },
  }),
  limits: { fileSize: 5 * 1024 * 1024 },
})
app.use('/uploads', express.static(UPLOAD_DIR))



// JWT 全局门禁：/api 默认要求 Bearer token；登录 / 注册 / 健康检查除外。
const PUBLIC_AUTH_PATHS = ["/api/health", "/api/v1/health", "/api/v1/users/login", "/api/v1/users/register"]
app.use((request, response, next) => {
  const path = String(request.path || '')
  if (!path.startsWith('/api')) return next()
  if (
    PUBLIC_AUTH_PATHS.some(
      (item) => path === item || path.startsWith(item + '/'),
    )
  ) {
    return next()
  }
  return authenticate(request, response, () =>
    requireWriteRole(request, response, next),
  )
})


const listHandler = asyncHandler(async (request, response) => {
  const page = Math.max(1, Number(request.query.page || 1))
  const size = Math.min(100, Math.max(1, Number(request.query.size || 20)))
  const result = await listRecords({ page, size })
  response.json({ ...result, table: TABLE, storage: STORAGE })
})

const createHandler = asyncHandler(async (request, response) => {
  const body =
    request.body && typeof request.body === 'object' ? { ...request.body } : {}

  const record = await createRecord(body)
  response.status(201).json({
    ...record,
    ...(request.file
      ? {
          upload: {
            name: request.file.originalname,
            path: `/uploads/${request.file.filename}`,
            size: request.file.size,
          },
        }
      : {}),
    success: true,
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
app.post(COLLECTION_PATH, upload.single('file'), createHandler)
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

// ---- 鉴权路由 ----
app.post("/api/v1/users/login", loginHandler)
app.post("/api/v1/users/register", authenticateOptional, registerHandler)
app.get("/api/v1/users/me", authenticate, meHandler)

// ---- AI-Native 路由 ----


// ---- 主从表嵌套路由 ----
// 父资源下的子集合，例如 /api/v1/users/:id/resumes。
// 列表 / 详情响应本身也通过 Sequelize include 带回关联数组。
// 契约未声明主从表关联。

// ---- 多实体顶层集合 ----
// 契约中除核心表外的独立集合（例如 /api/v1/patrol-routes）。
// 主从表真实 CRUD：facilities -> facility_images
app.get("/api/v1/facilities/:facility_id/facility-images", asyncHandler(async (request, response) => {
  const page = Math.max(1, Number(request.query.page || 1))
  const size = Math.min(100, Math.max(1, Number(request.query.size || 20)))
  const result = await listRelatedRecords(
    request.params.facility_id, "images", { page, size }
  )
  if (!result) {
    return response
      .status(404)
      .json({ code: 'PARENT_NOT_FOUND', message: '父记录不存在' })
  }
  response.json({ ...result, relation: "images" })
}))

// 主从表真实 CRUD：facilities -> facility_images
app.post("/api/v1/facilities/:facility_id/facility-images", upload.single('file'), asyncHandler(async (request, response) => {
  const record = await createRelatedRecord(
    request.params.facility_id,
    "images",
    request.body && typeof request.body === 'object' ? request.body : {},
  )
  if (!record) {
    return response
      .status(404)
      .json({ code: 'PARENT_NOT_FOUND', message: '父记录不存在' })
  }
  response.status(201).json({ ...record, success: true })
}))

// 多实体 CRUD：patrol_routes
app.get("/api/v1/patrol-routes", asyncHandler(async (request, response) => {
  const page = Math.max(1, Number(request.query.page || 1))
  const size = Math.min(100, Math.max(1, Number(request.query.size || 20)))
  const result = await listTableRecords("patrol_routes", { page, size })
  response.json({ ...result, table: "patrol_routes" })
}))

// 多实体 CRUD：patrol_routes
app.post("/api/v1/patrol-routes", upload.single('file'), asyncHandler(async (request, response) => {
  const record = await createTableRecord(
    "patrol_routes",
    request.body && typeof request.body === 'object' ? request.body : {},
  )
  response.status(201).json({ ...record, success: true })
}))

// 契约健康检查：真实探测数据库，而不是固定 200。
app.get("/api/v1/health", asyncHandler(async (_request, response) => {
  try {
    response.json(await health())
  } catch (error) {
    response.status(503).json({
      status: 'degraded',
      message: String(error?.message || error),
    })
  }
}))


// ---- 契约业务路由 ----
// BackendCoder 为契约里的每个业务动作生成独立端点；通用 CRUD 只是基础能力，
// 绝不能替代抓取 / 改写 / 投递 / 对话等语义路由。
// 契约未声明额外业务路由。

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
