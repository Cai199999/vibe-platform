import { createContext, useCallback, useContext, useMemo, useState } from 'react'

const STORAGE_KEY = 'vibe.auth.session'

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

function buildUser(account, demo) {
  if (demo) {
    return {
      id: 'usr_demo',
      name: 'Vibe 体验官',
      email: 'demo@vibe.local',
      tenantId: 'tenant_local',
      role: 'owner',
    }
  }
  const email = account.trim()
  const name = email.includes('@') ? email.split('@')[0] : email
  return {
    id: `usr_${name.slice(0, 18)}`,
    name,
    email,
    tenantId: 'tenant_local',
    role: 'owner',
  }
}

const AuthContext = createContext(null)

/**
 * Mock 鉴权：任意账号 + 密码（或一键体验）都会生成一个本地会话。
 * Token 只用于前端门禁与后续 BFF 请求头，M1 不做真实校验。
 */
export function AuthProvider({ children }) {
  const [session, setSession] = useState(readSession)

  const login = useCallback(({ account = '', password = '', demo = false }) => {
    if (!demo) {
      if (!account.trim() || !password.trim()) {
        throw new Error('请输入账号和密码')
      }
    }
    const next = {
      token: newToken(),
      user: buildUser(account, demo),
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
