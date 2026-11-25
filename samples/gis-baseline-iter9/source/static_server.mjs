// 极简静态服务器：供“导入纯 HTML 项目”时使用。
//
// 为什么不依赖 npm 包：
//   - 沙盒镜像构建期不联网安装额外依赖；
//   - 用户上传的 HTML 项目通常没有 package.json，也没有构建步骤；
//   - 只需要安全地把目录内容以 5173 端口暴露给预览 iframe。
//
// 安全约束：
//   - 解析后路径必须位于 root 内，拒绝 ../ 与绝对路径；
//   - 只读 GET / HEAD；
//   - 未知路由回退 index.html，兼容前端 history 路由。
import { createReadStream, existsSync, statSync } from 'node:fs'
import { extname, join, normalize, resolve } from 'node:path'
import { createServer } from 'node:http'

const root = resolve(process.argv[2] || '/app')
const port = Number(process.argv[3] || 5173)

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.htm': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.ico': 'image/x-icon',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.txt': 'text/plain; charset=utf-8',
}

function safePath(urlPath) {
  const decoded = decodeURIComponent(urlPath.split('?')[0].split('#')[0])
  const normalized = normalize(decoded).replace(/^(\.\.[/\\])+/, '')
  const target = resolve(join(root, normalized))
  if (target !== root && !target.startsWith(`${root}/`)) return null
  return target
}

function sendFile(response, filePath, status = 200) {
  response.writeHead(status, {
    'Content-Type': MIME[extname(filePath).toLowerCase()] || 'application/octet-stream',
    'Cache-Control': 'no-store',
  })
  createReadStream(filePath).pipe(response)
}

createServer((request, response) => {
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    response.writeHead(405, { Allow: 'GET, HEAD' })
    response.end('Method Not Allowed')
    return
  }

  let target = safePath(request.url || '/')
  if (!target) {
    response.writeHead(400)
    response.end('Bad Request')
    return
  }
  if (existsSync(target) && statSync(target).isDirectory()) {
    target = join(target, 'index.html')
  }
  if (!existsSync(target) || !statSync(target).isFile()) {
    const fallback = join(root, 'index.html')
    if (existsSync(fallback)) {
      sendFile(response, fallback)
      return
    }
    response.writeHead(404)
    response.end('Not Found')
    return
  }
  sendFile(response, target)
}).listen(port, '0.0.0.0', () => {
  console.log(`vibe static server: ${root} -> 0.0.0.0:${port}`)
})
