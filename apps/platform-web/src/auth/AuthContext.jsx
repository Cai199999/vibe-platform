import { createContext, useCallback, useContext, useMemo, useState } from 'react'

const STORAGE_KEY = 'vibe.auth.session'

/**
 * M3.1 Mock RBAC：
 *  - `admin@...` 或账号名恰为 admin 时视为平台管理员；
 *  - 登录页也可显式勾选“以管理员身份登录”；
 *  - 真实 JWT 接入后，这里改为读取 /auth/me 返回的 roles。
 */
const ADMIN_ROLES = new Set(['platform_admin', 'tenant_admin'])

export function isAdminUser(user) {
  return ADMIN_ROLES.has(user?.role)
}

const looksLikeAdmin = (account = '') => /^admin(@|$)/i.test(account.trim())

const newToken = () => {
  const random =
    typeof crypto !== 'undefined' && crypto.randomUUID
      ? crypto.randomUUID().replace(/-/g, '')
      : Math.random().toString(36).slice(2) + Date.now().toString(36)
  return `vibe_mock_${random}`
}

function readSession() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw)
    if (!parsed?.token || !parsed?.user?.name) return null
    return parsed
  } catch {
    return null
  }
}

function buildUser(account, demo, admin) {
  const isAdmin = Boolean(admin) || looksLikeAdmin(account)
  if (demo) {
    return {
      id: isAdmin ? 'usr_demo_admin' : 'usr_demo',
      name: isAdmin ? 'Vibe 管理员' : 'Vibe 体验官',
      email: isAdmin ? 'admin@vibe.local' : 'demo@vibe.local',
      tenantId: 'tenant_local',
      role: isAdmin ? 'platform_admin' : 'member',
    }
  }
  const email = account.trim()
  const name = email.includes('@') ? email.split('@')[0] : email
  return {
    id: `usr_${name.slice(0, 18)}`,
    name,
    email,
    tenantId: 'tenant_local',
    role: isAdmin ? 'platform_admin' : 'member',
  }
}

const AuthContext = createContext(null)

/**
 * Mock 鉴权：任意账号 + 密码（或一键体验）都会生成一个本地会话。
 * Token 只用于前端门禁与后续 BFF 请求头，M1 不做真实校验。
 */
export function AuthProvider({ children }) {
  const [session, setSession] = useState(readSession)

  const login = useCallback(({ account = '', password = '', demo = false, admin = false }) => {
    if (!demo) {
      if (!account.trim() || !password.trim()) {
        throw new Error('请输入账号和密码')
      }
    }
    const next = {
      token: newToken(),
      user: buildUser(account, demo, admin),
      issuedAt: new Date().toISOString(),
      mode: 'mock',
    }
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
    setSession(next)
    return next
  }, [])

  const logout = useCallback(() => {
    window.localStorage.removeItem(STORAGE_KEY)
    setSession(null)
  }, [])

  const value = useMemo(
    () => ({
      session,
      user: session?.user ?? null,
      isAuthenticated: Boolean(session),
      isAdmin: isAdminUser(session?.user),
      login,
      logout,
    }),
    [session, login, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const value = useContext(AuthContext)
  if (!value) {
    throw new Error('useAuth 必须在 AuthProvider 内使用')
  }
  return value
}
