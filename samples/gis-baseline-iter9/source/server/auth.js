// 由 BackendCoder 生成：JWT 登录 / 注册 / 当前用户 + Bearer 校验。
// 口令只存 Bcrypt 哈希；JWT_SECRET 必须由环境变量提供（生产缺失会告警）。
import jwt from 'jsonwebtoken'
import { hashPassword, verifyPassword } from './security.js'
import {
  countAuthUsers,
  createAuthUser,
  findAuthUserById,
  findAuthUserByUsername,
} from './db.js'

const ID_COLUMN = 'id'
const USERNAME_COLUMN = 'username'
const PASSWORD_COLUMN = 'password_hash'
const ROLE_COLUMN = 'role'
const JWT_SECRET = String(process.env.JWT_SECRET || 'vibe-dev-secret-change-me')
const ACCESS_TTL = String(process.env.JWT_ACCESS_TTL || '1h')
const REFRESH_TTL = String(process.env.JWT_REFRESH_TTL || '7d')
const ADMIN_USERNAME = String(process.env.ADMIN_USERNAME || 'admin')
const ADMIN_PASSWORD = String(process.env.ADMIN_PASSWORD || 'admin123')

if (process.env.NODE_ENV === 'production' && !process.env.JWT_SECRET) {
  console.warn('[auth] JWT_SECRET 未配置：生产环境请立即设置强随机密钥')
}

function publicUser(row) {
  if (!row) return null
  return {
    id: row[ID_COLUMN],
    username: row[USERNAME_COLUMN],
    role: ROLE_COLUMN ? row[ROLE_COLUMN] : 'viewer',
  }
}

function signToken(user, ttl) {
  return jwt.sign(
    {
      sub: String(user[ID_COLUMN]),
      username: user[USERNAME_COLUMN],
      role: ROLE_COLUMN ? user[ROLE_COLUMN] : 'viewer',
    },
    JWT_SECRET,
    { expiresIn: ttl },
  )
}

let adminPromise = null
async function ensureAdminUser() {
  if (adminPromise) return adminPromise
  adminPromise = (async () => {
    if ((await countAuthUsers()) > 0) return
    const passwordHash = await hashPassword(ADMIN_PASSWORD)
    await createAuthUser({
      [USERNAME_COLUMN]: ADMIN_USERNAME,
      [PASSWORD_COLUMN]: passwordHash,
      ...(ROLE_COLUMN ? { [ROLE_COLUMN]: 'admin' } : {}),
    })
  })().catch((error) => {
    adminPromise = null
    throw error
  })
  return adminPromise
}

export async function authenticate(request, response, next) {
  const header = String(request.headers.authorization || '')
  const token = header.startsWith('Bearer ') ? header.slice(7).trim() : ''
  if (!token) {
    return response
      .status(401)
      .json({ code: 'AUTH_REQUIRED', message: '请先登录' })
  }
  try {
    const payload = jwt.verify(token, JWT_SECRET)
    const user = await findAuthUserById(payload.sub)
    if (!user) {
      return response
        .status(401)
        .json({ code: 'AUTH_USER_NOT_FOUND', message: '登录状态已失效' })
    }
    request.authUser = user
    return next()
  } catch (error) {
    return response
      .status(401)
      .json({ code: 'AUTH_TOKEN_INVALID', message: '登录状态已失效' })
  }
}

// 注册接口本身是公开的（首个用户可自助成为 admin），但如果请求带了
// 合法 Bearer token，则识别当前用户，供“仅 admin 可创建非 viewer”校验。
export async function authenticateOptional(request, _response, next) {
  const header = String(request.headers.authorization || '')
  const token = header.startsWith('Bearer ') ? header.slice(7).trim() : ''
  if (!token) return next()
  try {
    const payload = jwt.verify(token, JWT_SECRET)
    const user = await findAuthUserById(payload.sub)
    if (user) request.authUser = user
  } catch (error) {
    // 公开接口忽略无效 token，保持匿名请求可继续。
  }
  return next()
}

// viewer 只读；admin / inspector 可写。角色列缺失时不启用该策略。
export function requireWriteRole(request, response, next) {
  if (['GET', 'HEAD', 'OPTIONS'].includes(String(request.method || '').toUpperCase())) {
    return next()
  }
  const current = ROLE_COLUMN
    ? String(request.authUser?.[ROLE_COLUMN] || 'viewer').toLowerCase()
    : 'viewer'
  if (current === 'viewer') {
    return response
      .status(403)
      .json({ code: 'AUTH_FORBIDDEN', message: 'viewer 角色只读' })
  }
  return next()
}

export function requireRole(...roles) {
  const allowed = roles.map((role) => String(role || '').toLowerCase())
  return (request, response, next) => {
    const current = ROLE_COLUMN
      ? String(request.authUser?.[ROLE_COLUMN] || 'viewer').toLowerCase()
      : 'viewer'
    if (allowed.length === 0 || allowed.includes(current)) return next()
    return response
      .status(403)
      .json({ code: 'AUTH_FORBIDDEN', message: '当前角色无权执行该操作' })
  }
}

export async function loginHandler(request, response) {
  const body =
    request.body && typeof request.body === 'object' ? request.body : {}
  const username = String(body.username || body[USERNAME_COLUMN] || '').trim()
  const password = String(body.password || body[PASSWORD_COLUMN] || '')
  if (!username || !password) {
    return response
      .status(400)
      .json({ code: 'AUTH_MISSING_CREDENTIALS', message: '用户名和口令必填' })
  }
  try {
    await ensureAdminUser()
  } catch (error) {
    // 首次种子失败不应返回 500 给登录页：明确告知管理员口令不合法。
    return response
      .status(500)
      .json({ code: 'AUTH_SEED_FAILED', message: String(error?.message || error) })
  }
  const user = await findAuthUserByUsername(username)
  const valid = user ? await verifyPassword(password, user[PASSWORD_COLUMN]) : false
  if (!valid) {
    return response
      .status(401)
      .json({ code: 'AUTH_INVALID_CREDENTIALS', message: '用户名或口令错误' })
  }
  return response.json({
    token: signToken(user, ACCESS_TTL),
    refreshToken: signToken(user, REFRESH_TTL),
    user: publicUser(user),
  })
}

export async function registerHandler(request, response) {
  const body =
    request.body && typeof request.body === 'object' ? request.body : {}
  const username = String(body.username || body[USERNAME_COLUMN] || '').trim()
  const password = String(body.password || body[PASSWORD_COLUMN] || '')
  if (!username || !password) {
    return response
      .status(400)
      .json({ code: 'AUTH_MISSING_CREDENTIALS', message: '用户名和口令必填' })
  }
  const existing = await findAuthUserByUsername(username)
  if (existing) {
    return response
      .status(409)
      .json({ code: 'AUTH_USER_EXISTS', message: '用户名已存在' })
  }
  const total = await countAuthUsers()
  const requestedRole = String(body.role || 'viewer')
  const role = total === 0 ? 'admin' : requestedRole
  if (total > 0) {
    if (!request.authUser) {
      return response
        .status(401)
        .json({ code: 'AUTH_REQUIRED', message: '请先登录' })
    }
    const currentRole = ROLE_COLUMN
      ? String(request.authUser[ROLE_COLUMN] || 'viewer')
      : 'viewer'
    if (role !== 'viewer' && currentRole !== 'admin') {
      return response.status(403).json({
        code: 'AUTH_FORBIDDEN',
        message: '只有管理员可以创建非 viewer 角色',
      })
    }
  }
  try {
    const passwordHash = await hashPassword(password)
    const user = await createAuthUser({
      [USERNAME_COLUMN]: username,
      [PASSWORD_COLUMN]: passwordHash,
      ...(ROLE_COLUMN ? { [ROLE_COLUMN]: role } : {}),
    })
    return response.status(201).json(publicUser(user))
  } catch (error) {
    const message = String(error?.message || error)
    const status = message === 'PASSWORD_LENGTH_INVALID' ? 400 : 500
    return response.status(status).json({
      code: status === 400 ? 'AUTH_PASSWORD_INVALID' : 'AUTH_CREATE_FAILED',
      message:
        status === 400 ? '口令长度必须为 8-128 个字符' : message,
    })
  }
}

export function meHandler(request, response) {
  return response.json(publicUser(request.authUser))
}
