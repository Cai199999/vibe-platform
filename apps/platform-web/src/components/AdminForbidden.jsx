import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext.jsx'

/**
 * 非管理员直接访问 /admin 时的整页 403。
 *
 * <p>前端守卫在渲染任何管理数据之前拦截；即使绕过这里，BFF 的
 * {@code @IsAdmin} 切面仍会返回 403，形成双层防护。</p>
 */
export default function AdminForbidden() {
  const { logout } = useAuth()
  const navigate = useNavigate()

  // 已登录用户切换账号必须先清理会话，否则 /login 会因 isAuthenticated
  // 立刻把用户重定向回 /workspace。
  const switchAccount = () => {
    logout()
    navigate('/login', { replace: true })
  }

  return (
    <div className="admin-forbidden-shell">
      <div className="admin-forbidden-card" role="alert">
        <span className="admin-forbidden-code">403</span>
        <span className="eyebrow">Admin Console</span>
        <h1>没有管理中台权限</h1>
        <p>
          当前账号不是管理员。管理中台仅对 <code>platform_admin</code> 与{' '}
          <code>tenant_admin</code> 开放。
        </p>
        <div className="admin-forbidden-actions">
          <Link className="primary-button" to="/workspace">
            返回工作台
          </Link>
          <button className="ghost-button" type="button" onClick={switchAccount}>
            切换管理员账号
          </button>
        </div>
        <p className="admin-forbidden-hint">
          本地 Mock：使用 <code>admin@vibe.local</code> 登录，或在一键体验时勾选
          “以管理员身份登录”。
        </p>
      </div>
    </div>
  )
}
