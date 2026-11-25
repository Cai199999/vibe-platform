#!/usr/bin/env node
/**
 * 打开预生成的 GIS 演示项目。
 *
 * 前置：
 *   - 平台已启动（docker compose up -d）
 *   - 已用 /tmp/vibe-gis-rich.mjs 生成并保存 docs/demo/demo-storage.json
 *   - 宿主机有 playwright-core（默认读取 /tmp/vibe-e2e/node_modules）
 *
 * 用法：
 *   node docs/demo/open-demo.mjs
 *   DEMO_STATE=demo-v0-storage.json node docs/demo/open-demo.mjs  # 从 V0 开始演示
 *   DEMO_STATE=demo-v0-storage.json DEMO_PATH='/visual-canvas?project=proj_xxx&title=...' \
 *     node docs/demo/open-demo.mjs                                # 直接打开 V0 画布
 *   DEMO_PREVIEW_URL='http://127.0.0.1:54201' node docs/demo/open-demo.mjs
 *                                                                  # 直接打开沙盒 live 预览
 *   DEMO_HEADLESS=1 node docs/demo/open-demo.mjs   # 仅用于自检/截图
 */
import { createRequire } from 'node:module'
import os from 'node:os'
import path from 'node:path'
import fs from 'node:fs'

const require = createRequire(import.meta.url)
const CANDIDATES = [
  process.env.PLAYWRIGHT_CORE_DIR,
  '/tmp/vibe-e2e/node_modules/playwright-core',
  path.join(process.cwd(), 'node_modules/playwright-core'),
].filter(Boolean)

let playwright = null
for (const candidate of CANDIDATES) {
  try {
    playwright = require(candidate)
    break
  } catch {
    // 尝试下一个
  }
}
if (!playwright) {
  console.error(
    '缺少 playwright-core。请先执行：\n' +
      '  npm install --prefix /tmp/vibe-e2e playwright-core\n' +
      '或设置 PLAYWRIGHT_CORE_DIR 指向包含 playwright-core 的目录。',
  )
  process.exit(2)
}

const STATE_PATH = path.join(
  path.dirname(new URL(import.meta.url).pathname),
  process.env.DEMO_STATE || 'demo-storage.json',
)
if (!fs.existsSync(STATE_PATH)) {
  console.error(`缺少 ${STATE_PATH}，请先生成演示项目。`)
  process.exit(2)
}

const CHROME =
  process.env.CHROME_BIN ||
  path.join(
    os.homedir(),
    '.cache/ms-playwright/chromium-1254/chrome-linux64/chrome',
  )
const WEB = process.env.WEB_URL || 'http://127.0.0.1:5173'
const DEMO_PATH = process.env.DEMO_PATH || '/workspace'
const PREVIEW_URL = process.env.DEMO_PREVIEW_URL || ''
const HEADLESS = process.env.DEMO_HEADLESS === '1'

const { chromium } = playwright
const browser = await chromium.launch({
  executablePath: CHROME,
  headless: HEADLESS,
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--window-size=1600,1000'],
})
const context = await browser.newContext({
  storageState: STATE_PATH,
  viewport: { width: 1600, height: 1000 },
})
const page = await context.newPage()
const target = PREVIEW_URL || `${WEB}${DEMO_PATH}`
await page.goto(target, { waitUntil: 'domcontentloaded' })
if (PREVIEW_URL) {
  await page.locator('body').waitFor({ state: 'visible', timeout: 30000 }).catch(() => {})
} else if (DEMO_PATH.startsWith('/visual-canvas')) {
  await page.locator('.vc-node').first().waitFor({ state: 'visible', timeout: 60000 })
} else {
  const liveFrame = page.locator('.tab-window.is-active .canvas-live-frame')
  const liveVisible = await liveFrame
    .waitFor({ state: 'visible', timeout: 15000 })
    .then(() => true)
    .catch(() => false)
  if (liveVisible) {
    const frame = page.frameLocator('.tab-window.is-active .canvas-live-frame')
    await frame
      .getByText('测绘任务与成果管理平台')
      .first()
      .waitFor({ timeout: 15000 })
      .catch(() => {})
    const liveText = await frame.locator('body').innerText().catch(() => '')
    console.log('live text length', liveText.trim().length)
    if (liveText.trim().length < 20) {
      console.log(
        '提示：workspace 未恢复 live iframe。可直接打开 DEMO_PREVIEW_URL（见 live-preview-url.txt）。',
      )
    }
  } else {
    console.log('workspace opened（clean / V0 state，无 live iframe）')
  }
}
await page.waitForTimeout(3000)
await page.screenshot({ path: '/tmp/demo-open.png', fullPage: false })
console.log('demo opened:', page.url())
console.log('screenshot: /tmp/demo-open.png')

if (HEADLESS) {
  await browser.close()
} else {
  console.log('按 Ctrl+C 关闭演示浏览器')
  await new Promise(() => {})
}
