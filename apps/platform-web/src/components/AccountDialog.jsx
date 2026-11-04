import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext.jsx'

const maskToken = (token = '') => {
  if (token.length <= 20) return token
  return `${token.slice(0, 14)}…${token.slice(-6)}`
}

const initials = (name = 'V') => name.trim().slice(0, 1).toUpperCase()

/**
 * 账户设置弹窗（M1：只读展示 Mock 会话与退出登录）。
 */
export default function AccountDialog({ open, onClose }) {
  const { session, user, logout } = useAuth()
  const navigate = useNavigate()

  useEffect(() => {
    if (!open) return undefined
    const handleKey = (event) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [open, onClose])

  if (!open || !user) return null

  const handleLogout = () => {
    logout()
    onClose()
    navigate('/login', { replace: true })
  }

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <div
        className="account-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="account-dialog-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="dialog-head">
          <div>
            <span className="eyebrow">Account</span>
            <h2 id="account-dialog-title">账户设置</h2>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭">
            ×
          </button>
        </div>

        <div className="account-identity">
          <span className="avatar avatar-lg">{initials(user.name)}</span>
          <div>
            <strong>{user.name}</strong>
            <p>{user.email}</p>
          </div>
          <span className="chip chip-muted">Mock Session</span>
        </div>

        <dl className="account-fields">
          <div>
            <dt>用户 ID</dt>
            <dd>{user.id}</dd>
          </div>
          <div>
            <dt>租户</dt>
            <dd>{user.tenantId}</dd>
          </div>
          <div>
            <dt>角色</dt>
            <dd>{user.role}</dd>
          </div>
          <div>
            <dt>签发时间</dt>
            <dd>{new Date(session.issuedAt).toLocaleString('zh-CN')}</dd>
          </div>
          <div className="account-token">
            <dt>Access Token</dt>
            <dd>{maskToken(session.token)}</dd>
          </div>
        </dl>

        <p className="account-note">
          Token 仅存储在浏览器 localStorage。后续接入真实认证时，将替换为 HttpOnly Cookie
          或短期令牌 + 刷新机制。
        </p>

        <div className="dialog-actions">
          <button className="ghost-button" type="button" onClick={onClose}>
            完成
          </button>
          <button className="danger-button" type="button" onClick={handleLogout}>
            退出登录
          </button>
        </div>
      </div>
    </div>
  )
}
