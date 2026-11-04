import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 浏览器只访问同源 /api，由 Vite 代理到 Java BFF：
//   - 宿主本地开发：http://127.0.0.1:8080
//   - docker compose：http://java-bff:8080
const bffTarget = process.env.VITE_BFF_PROXY_TARGET || 'http://127.0.0.1:8080'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: bffTarget,
        changeOrigin: true,
      },
    },
  },
})
