import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext.jsx'

const ROLE_LABELS = {
  platform_admin: '平台管理员',
  tenant_admin: '租户管理员',
  tenant_operator: '租户运营',
}

const initials = (name = 'A') => name.trim().slice(0, 1).toUpperCase()

/**
 * 管理中台顶栏：品牌返回工作台，右侧展示当前 Mock 角色与退出入口。
 */
export default function AdminHeader() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()

  const handleLogout = () => {
    logout()
    navigate('/login', { replace: true })
  }

  return (
    <header className="topbar admin-topbar">
      <Link className="brand brand-link" to="/workspace">
        <span className="brand-mark">V</span>
        <div>
          <h1>Vibe Studio</h1>
          <p>管理中台 · Admin Console</p>
        </div>
      </Link>

      <div className="topbar-meta admin-topbar-meta">
        <Link className="ghost-button admin-back-link" to="/workspace">
          <span aria-hidden="true">←</span> 返回工作台
        </Link>
        <div className="admin-identity" title={user?.email}>
          <span className="avatar">{initials(user?.name)}</span>
          <div>
            <strong>{user?.name}</strong>
            <span>{ROLE_LABELS[user?.role] ?? user?.role ?? '只读'}</span>
          </div>
        </div>
        <button className="ghost-button admin-logout" type="button" onClick={handleLogout}>
          退出
        </button>
      </div>
    </header>
  )
}
