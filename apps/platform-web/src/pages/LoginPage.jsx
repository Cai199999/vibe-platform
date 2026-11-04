import { useState } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext.jsx'

/**
 * 极简 Mock 登录页：
 *  - 账号 + 密码登录（任意非空值均通过）；
 *  - 一键体验：直接生成演示会话；
 *  - 会话写入 localStorage，由 AuthProvider 统一管理。
 */
export default function LoginPage() {
  const { isAuthenticated, login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [account, setAccount] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')

  const redirectTo = location.state?.from?.pathname || '/workspace'

  if (isAuthenticated) {
    return <Navigate to={redirectTo} replace />
  }

  const enter = (options) => {
    setError('')
    try {
      login(options)
      navigate(redirectTo, { replace: true })
    } catch (cause) {
      setError(cause.message)
    }
  }

  const handleSubmit = (event) => {
    event.preventDefault()
    enter({ account, password })
  }

  return (
    <div className="auth-shell">
      <section className="auth-aside">
        <div className="auth-brand">
          <span className="brand-mark">V</span>
          <div>
            <h1>Vibe Studio</h1>
            <p>画布标注 · 多项目工作台</p>
          </div>
        </div>

        <div className="auth-pitch">
          <span className="eyebrow">Visual Vibe Coding</span>
          <h2>
            圈出你想改的地方，
            <br />
            Agent 负责其余一切。
          </h2>
          <p>
            在画布上直接圈选组件，输入一句话，剩下的规划、编码、沙盒验证与自我修复全部自动完成。
          </p>
        </div>

        <ul className="auth-points">
          <li>
            <strong>01</strong>
            <span>画布内浮层输入，不打断心流</span>
          </li>
          <li>
            <strong>02</strong>
            <span>隔离沙盒实时预览，代码 Diff 可审计</span>
          </li>
          <li>
            <strong>03</strong>
            <span>多项目工作区，随时切换与回滚</span>
          </li>
        </ul>
      </section>

      <section className="auth-main">
        <form className="auth-card" onSubmit={handleSubmit}>
          <span className="eyebrow">Sign in</span>
          <h2>进入工作台</h2>
          <p className="auth-sub">使用任意账号密码即可体验。</p>

          <label className="auth-field" htmlFor="login-account">
            <span>账号</span>
            <input
              id="login-account"
              type="text"
              autoComplete="username"
              value={account}
              placeholder="you@example.com"
              onChange={(event) => setAccount(event.target.value)}
            />
          </label>

          <label className="auth-field" htmlFor="login-password">
            <span>密码</span>
            <input
              id="login-password"
              type="password"
              autoComplete="current-password"
              value={password}
              placeholder="任意密码"
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>

          {error && <div className="auth-error">{error}</div>}

          <button className="primary-button auth-submit" type="submit">
            登录并进入工作台
            <span aria-hidden="true">→</span>
          </button>

          <div className="auth-divider">
            <span>或</span>
          </div>

          <button
            className="ghost-button auth-demo"
            type="button"
            onClick={() => enter({ demo: true })}
          >
            一键体验（推荐）
          </button>

          <p className="auth-hint">M1 阶段仅做前端门禁，真实鉴权与 RBAC 在后续版本接入。</p>
        </form>
      </section>
    </div>
  )
}
