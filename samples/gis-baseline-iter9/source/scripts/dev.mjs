// 由 FullstackGraph 生成：同时拉起前端 Vite 与 Express 后端。
// 容器 CMD 为 `npm run dev`，因此无需修改镜像启动方式。
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'

const children = []
let shuttingDown = false

function start(label, command, args) {
  const child = spawn(command, args, {
    stdio: 'inherit',
    env: process.env,
  })
  children.push(child)
  child.on('exit', (code, signal) => {
    console.log(`[vibe-dev] ${label} exited code=${code} signal=${signal}`)
    if (!shuttingDown && label === 'vite') {
      shutdown(code ?? 1)
    }
  })
  return child
}

function shutdown(code) {
  if (shuttingDown) return
  shuttingDown = true
  children.forEach((child) => {
    if (!child.killed) child.kill('SIGTERM')
  })
  setTimeout(() => process.exit(code), 300)
}

process.on('SIGTERM', () => shutdown(0))
process.on('SIGINT', () => shutdown(0))

const apiEntry = ['server/index.js', 'server/index.mjs'].find((path) =>
  existsSync(path),
)
if (apiEntry) {
  start('api', process.execPath, [apiEntry])
} else {
  console.log('[vibe-dev] 未找到 server/index.js，仅启动前端')
}
start('vite', 'npm', ['run', 'dev:vite'])
