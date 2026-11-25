// 由 BackendCoder 生成：Web 安全编码基线（Skill: web.best_practices）。
// 密码只使用 Bcrypt 哈希；所有输入按白名单与长度上限收敛，
// 禁止把用户输入直接拼接进 SQL 或文件路径。
import bcrypt from 'bcryptjs'

const BCRYPT_ROUNDS = 12

export async function hashPassword(password) {
  const value = String(password || '')
  if (value.length < 8 || value.length > 128) {
    throw new Error('PASSWORD_LENGTH_INVALID')
  }
  return bcrypt.hash(value, BCRYPT_ROUNDS)
}

export async function verifyPassword(password, passwordHash) {
  if (!passwordHash) return false
  return bcrypt.compare(String(password || ''), String(passwordHash))
}

export function sanitizeString(value, maxLength = 8000) {
  return String(value ?? '').trim().slice(0, maxLength)
}

export function pickAllowed(body, allowedFields) {
  const source = body && typeof body === 'object' ? body : {}
  const result = {}
  for (const field of allowedFields) {
    if (Object.prototype.hasOwnProperty.call(source, field)) {
      result[field] = source[field]
    }
  }
  return result
}

export function safeSortField(value, allowedFields, fallback) {
  const field = String(value || '')
  return allowedFields.includes(field) ? field : fallback
}
