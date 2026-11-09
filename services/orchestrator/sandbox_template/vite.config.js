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
  },
})
