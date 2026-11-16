import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { AuthProvider, useAuth } from './auth/AuthContext.jsx'
import { SettingsProvider } from './settings/SettingsContext.jsx'
import AdminForbidden from './components/AdminForbidden.jsx'
import LoginPage from './pages/LoginPage.jsx'
import AdminPage from './pages/AdminPage.jsx'
import VisualCanvasPage from './pages/VisualCanvasPage.jsx'
import WorkspacePage from './pages/WorkspacePage.jsx'

/**
 * 未登录访问工作台时重定向到 /login，并记录来源路径，登录后原路返回。
 */
function ProtectedRoute({ children }) {
  const { isAuthenticated } = useAuth()
  const location = useLocation()
  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location }} />
  }
  return children
}

/**
 * 管理端路由守卫：未登录先去登录；已登录但无管理员角色时渲染整页 403，
 * 不发起任何 /api/v1/admin 请求，避免数据在无权限时短暂闪现。
 */
function AdminRoute({ children }) {
  const { isAuthenticated, isAdmin } = useAuth()
  const location = useLocation()
  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location }} />
  }
  if (!isAdmin) {
    return <AdminForbidden />
  }
  return children
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <SettingsProvider>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route
              path="/workspace"
              element={
                <ProtectedRoute>
                  <WorkspacePage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/visual-canvas"
              element={
                <ProtectedRoute>
                  <VisualCanvasPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/admin"
              element={
                <AdminRoute>
                  <AdminPage />
                </AdminRoute>
              }
            />
            <Route path="/" element={<Navigate to="/workspace" replace />} />
            <Route path="*" element={<Navigate to="/workspace" replace />} />
          </Routes>
        </SettingsProvider>
      </AuthProvider>
    </BrowserRouter>
  )
}
