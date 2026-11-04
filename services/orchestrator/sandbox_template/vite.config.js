import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 沙盒容器内固定监听 5173，宿主端口由 Docker 随机映射。
// HMR 客户端必须知道宿主端口，否则浏览器会尝试连接 5173 而失败。
const hmrClientPort = Number(process.env.VITE_HMR_CLIENT_PORT || 0)

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    hmr: hmrClientPort > 0 ? { clientPort: hmrClientPort } : true,
  },
})
