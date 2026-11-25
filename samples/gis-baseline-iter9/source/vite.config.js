import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { sourceInspectPlugin } from './inspect_plugin.js'

export default defineConfig({
  // 源码标记必须早于 React 编译：生成后新增的组件文件也会自动
  // 获得 data-oid / data-source，画布 Direct Ops 才能定位。
  plugins: [sourceInspectPlugin(), react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    // 同源 /api 请求转发到沙盒内 Node 后端；纯前端项目没有后端时
    // 不会主动请求 /api，代理配置本身无副作用。
    proxy: {
      '/api': {
        target: process.env.FULLSTACK_API_URL || 'http://127.0.0.1:3000',
        changeOrigin: true,
      },
    },
  },
})
