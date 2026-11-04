import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { AuthProvider, useAuth } from './auth/AuthContext.jsx'
import LoginPage from './pages/LoginPage.jsx'
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

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
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
          <Route path="/" element={<Navigate to="/workspace" replace />} />
          <Route path="*" element={<Navigate to="/workspace" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  )
}
