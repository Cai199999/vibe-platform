// 由 FrontendCoder 生成：JWT 登录门禁 + 全局 Bearer 注入。
// 登录成功后 token 存 localStorage；所有 /api 请求自动带 Authorization。
import { useState } from 'react'

export const AUTH_TOKEN_KEY = 'vibe_auth_token'
const LOGIN_PATH = "/api/v1/users/login"

export function installAuthFetch() {
  if (typeof window === 'undefined' || window.__VIBE_AUTH_FETCH__) return
  window.__VIBE_AUTH_FETCH__ = true
  const original = window.fetch.bind(window)
  window.fetch = (input, init = {}) => {
    const url = typeof input === 'string' ? input : String(input?.url || '')
    const isApi = url.startsWith('/api/') || url.includes('/api/')
    let token = ''
    try {
      token = localStorage.getItem(AUTH_TOKEN_KEY) || ''
    } catch {
      token = ''
    }
    if (isApi && token) {
      const headers = new Headers(init.headers || {})
      if (!headers.has('Authorization')) {
        headers.set('Authorization', `Bearer ${token}`)
      }
      return original(input, { ...init, headers })
    }
    return original(input, init)
  }
}

export function AuthGate({ children }) {
  const [token, setToken] = useState(() => {
    try {
      return localStorage.getItem(AUTH_TOKEN_KEY) || ''
    } catch {
      return ''
    }
  })
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  if (token) return children

  const submit = async (event) => {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    setError('')
    try {
      const response = await fetch(LOGIN_PATH, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(payload.message || payload.code || `HTTP ${response.status}`)
      }
      const nextToken = payload.token || payload.data?.token
      if (!nextToken) throw new Error('登录响应缺少 token')
      localStorage.setItem(AUTH_TOKEN_KEY, nextToken)
      setToken(nextToken)
    } catch (cause) {
      setError(String(cause?.message || cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: '#f8fafc',
        padding: '24px',
        fontFamily: 'system-ui, -apple-system, sans-serif',
      }}
    >
      <form
        onSubmit={submit}
        style={{
          width: 'min(380px, 100%)',
          background: '#ffffff',
          border: '1px solid #e2e8f0',
          borderRadius: '16px',
          padding: '28px',
          boxShadow: '0 20px 45px rgba(15, 23, 42, 0.12)',
          display: 'flex',
          flexDirection: 'column',
          gap: '14px',
        }}
      >
        <h1 style={{ margin: 0, fontSize: '22px', color: '#0f172a' }}>登录</h1>
        <p style={{ margin: 0, color: '#64748b', fontSize: '14px' }}>
          登录后才能访问业务数据。
        </p>
        <input
          name="username"
          autoComplete="username"
          placeholder="用户名"
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          required
          style={inputStyle}
        />
        <input
          name="password"
          type="password"
          autoComplete="current-password"
          placeholder="口令"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          required
          style={inputStyle}
        />
        {error ? (
          <div style={{ color: '#b91c1c', fontSize: '13px' }}>{error}</div>
        ) : null}
        <button
          type="submit"
          disabled={busy}
          style={{
            border: 'none',
            borderRadius: '10px',
            padding: '11px 16px',
            background: busy ? '#93c5fd' : '#2563eb',
            color: '#ffffff',
            fontSize: '15px',
            cursor: busy ? 'wait' : 'pointer',
          }}
        >
          {busy ? '登录中…' : '登录'}
        </button>
        {import.meta.env.DEV ? (
          <small style={{ color: '#94a3b8' }}>
            首次登录：admin / admin123（可用 ADMIN_PASSWORD 修改）
          </small>
        ) : null}
      </form>
    </div>
  )
}

const inputStyle = {
  width: '100%',
  boxSizing: 'border-box',
  border: '1px solid #cbd5e1',
  borderRadius: '10px',
  padding: '10px 12px',
  fontSize: '14px',
  color: '#0f172a',
}
