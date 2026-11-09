import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import vue from '@vitejs/plugin-vue'
import { sourceInspectPlugin } from './inspect_plugin.js'

const hmrClientPort = Number(process.env.VITE_HMR_CLIENT_PORT || 0)

export default defineConfig({
  // sourceInspectPlugin 必须放在 react / vue 之前（enforce:pre），
  // 先把源码出处写进 JSX / SFC，再由官方插件编译。
  plugins: [sourceInspectPlugin(), react(), vue()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    hmr: hmrClientPort > 0 ? { clientPort: hmrClientPort } : true,
    // FullstackGraph：同源 /api 请求转发到沙盒内 Express + SQLite 后端（默认 3000）。
    // 旧沙盒没有该配置时，FrontendCoder 会在运行时补写并重启容器。
    proxy: {
      '/api': {
        target: process.env.FULLSTACK_API_URL || 'http://127.0.0.1:3000',
        changeOrigin: true,
      },
    },
  },
})
