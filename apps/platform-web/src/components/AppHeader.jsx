import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext.jsx'

const initials = (name = 'V') => name.trim().slice(0, 1).toUpperCase()

/**
 * 工作台顶栏：品牌、运行状态、用户头像与账户菜单。
 */
export default function AppHeader({ busy, onOpenAccount, onOpenSettings }) {
  const { user, isAdmin, logout } = useAuth()
  const navigate = useNavigate()
  const [menuOpen, setMenuOpen] = useState(false)
  const menuRef = useRef(null)

  useEffect(() => {
    if (!menuOpen) return undefined
    const handlePointer = (event) => {
      if (menuRef.current && !menuRef.current.contains(event.target)) {
        setMenuOpen(false)
      }
    }
    const handleKey = (event) => {
      if (event.key === 'Escape') setMenuOpen(false)
    }
    window.addEventListener('mousedown', handlePointer)
    window.addEventListener('keydown', handleKey)
    return () => {
      window.removeEventListener('mousedown', handlePointer)
      window.removeEventListener('keydown', handleKey)
    }
  }, [menuOpen])

  const handleLogout = () => {
    setMenuOpen(false)
    logout()
    navigate('/login', { replace: true })
  }

  return (
    <header className="topbar">
      <Link className="brand brand-link" to="/workspace">
        <span className="brand-mark">V</span>
        <div>
          <h1>Vibe Studio</h1>
        </div>
      </Link>

      <div className="topbar-meta">
        <span
          className={`live-dot ${busy ? 'is-busy' : ''}`}
          role="status"
          aria-label={busy ? '正在执行' : '就绪'}
          title={busy ? '正在执行' : '就绪'}
        >
          <i />
        </span>

        <div className="account-menu" ref={menuRef}>
          <button
            className="avatar-button"
            type="button"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((value) => !value)}
          >
            <span className="avatar">{initials(user?.name)}</span>
            <span className="account-name">{user?.name}</span>
            <span className="account-caret" aria-hidden="true">
              ▾
            </span>
          </button>

          {menuOpen && (
            <div className="account-dropdown" role="menu">
              <div className="account-dropdown-head">
                <strong>{user?.name}</strong>
                <span>{user?.email}</span>
              </div>
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setMenuOpen(false)
                  onOpenAccount()
                }}
              >
                账户
              </button>
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setMenuOpen(false)
                  onOpenSettings()
                }}
              >
                设置
              </button>
              {isAdmin && (
                <button
                  type="button"
                  role="menuitem"
                  className="is-admin"
                  onClick={() => {
                    setMenuOpen(false)
                    navigate('/admin')
                  }}
                >
                  <span className="admin-menu-glyph" aria-hidden="true">
                    ◆
                  </span>
                  管理中台（Admin Console）
                </button>
              )}
              <button type="button" role="menuitem" className="is-danger" onClick={handleLogout}>
                退出登录
              </button>
            </div>
          )}
        </div>
      </div>
    </header>
  )
}
