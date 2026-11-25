#!/usr/bin/env node
/**
 * 浏览器侧韧性验证：后端崩溃时，DataList 是否能自动恢复。
 *
 * 场景：
 *   1. 打开 live 预览，确认 DataList 正常；
 *   2. 杀掉沙盒里的 Express；
 *   3. 立即 reload（用户重新打开页面），DataList 首次 fetch 会失败；
 *   4. dev.mjs 1s 自动重启 API，DataList 1s/2s 退避重试；
 *   5. 断言表格出现、无“加载失败”、无页面错误、健康恢复。
 *
 * 运行：
 *   PREVIEW_URL=http://127.0.0.1:39165 \
 *   SANDBOX_ID=sbx_11fe3f3061e941cb9020 \
 *   NODE_PATH=/tmp/vibe-e2e/node_modules \
 *   CHROME_BIN=$HOME/.cache/ms-playwright/chromium-1255/chrome-linux64/chrome \
 *   node scripts/gis_resilience_browser_e2e.mjs
 */
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.dirname(SCRIPT_DIR)

function loadPlaywright() {
  try {
    return require('playwright-core')
  } catch {
    // ESM 不读取 NODE_PATH，显式按目录回退。
  }
  for (const dir of (process.env.NODE_PATH || '')
    .split(path.delimiter)
    .filter(Boolean)) {
    try {
      return require(path.join(dir, 'playwright-core'))
    } catch {
      // 继续尝试
    }
  }
  throw new Error('缺少 playwright-core；请设置 NODE_PATH')
}

const { chromium } = loadPlaywright()
const PREVIEW = (process.env.PREVIEW_URL || 'http://127.0.0.1:39165').replace(
  /\/$/,
  '',
)
const SANDBOX_ID = process.env.SANDBOX_ID || 'sbx_11fe3f3061e941cb9020'
const CONTAINER = process.env.SANDBOX_CONTAINER || `vibe-sandbox-${SANDBOX_ID}`
const CHROME =
  process.env.CHROME_BIN ||
  path.join(
    os.homedir(),
    '.cache/ms-playwright/chromium-1255/chrome-linux64/chrome',
  )

function log(...args) {
  console.log('[gis-resilience-browser]', ...args)
}

async function healthStatus() {
  try {
    const response = await fetch(`${PREVIEW}/api/health`)
    return response.status
  } catch {
    return 0
  }
}

async function writeReport(report) {
  const outDir = path.join(ROOT, 'reports', 'production')
  fs.mkdirSync(outDir, { recursive: true })
  const stamp = new Date().toISOString().replace(/[:.]/g, '-')
  const jsonPath = path.join(outDir, `browser-resilience-${stamp}.json`)
  const mdPath = path.join(outDir, `browser-resilience-${stamp}.md`)
  fs.writeFileSync(jsonPath, JSON.stringify(report, null, 2), 'utf8')
  const lines = [
    '# 浏览器侧韧性验证',
    '',
    `- 预览：${PREVIEW}`,
    `- 结果：**${report.ok ? 'PASS' : 'FAIL'}**`,
    `- 初始表格行数：${report.initialRows}`,
    `- 故障后表格行数：${report.recoveredRows}`,
    `- 故障后出现“加载失败”：${report.errorVisible}`,
    `- 恢复耗时：${report.recoveryMs}ms`,
    `- 页面错误：${report.pageErrors.length}`,
    '',
  ]
  fs.writeFileSync(mdPath, lines.join('\n'), 'utf8')
  return { jsonPath, mdPath }
}

async function main() {
  const browser = await chromium.launch({
    executablePath: CHROME,
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  const pageErrors = []
  page.on('pageerror', (error) => pageErrors.push(String(error).slice(0, 300)))

  const report = {
    generatedAt: new Date().toISOString(),
    previewUrl: PREVIEW,
    sandboxId: SANDBOX_ID,
    container: CONTAINER,
    initialRows: 0,
    recoveredRows: 0,
    errorVisible: false,
    recoveryMs: -1,
    healthAfter: 0,
    pageErrors,
    ok: false,
  }

  try {
    log('initial load')
    await page.goto(PREVIEW, { waitUntil: 'domcontentloaded' })
    await page
      .locator('[data-testid="data-list"] table')
      .waitFor({ state: 'visible', timeout: 20000 })
    report.initialRows = await page
      .locator('[data-testid="data-list"] tbody tr')
      .count()
    log('initial rows', report.initialRows)

    log('kill backend', CONTAINER)
    execFileSync('docker', ['exec', CONTAINER, 'pkill', '-f', 'server/index.js'])
    const faultStarted = Date.now()

    log('reload during outage')
    await page.reload({ waitUntil: 'domcontentloaded' })
    const deadline = Date.now() + 20000
    while (Date.now() < deadline) {
      report.recoveredRows = await page
        .locator('[data-testid="data-list"] tbody tr')
        .count()
        .catch(() => 0)
      if (report.recoveredRows > 0) break
      await new Promise((resolve) => setTimeout(resolve, 200))
    }
    const bodyText = await page.locator('body').innerText().catch(() => '')
    report.errorVisible = bodyText.includes('加载失败')
    report.recoveryMs = Date.now() - faultStarted

    const healthDeadline = Date.now() + 15000
    while (Date.now() < healthDeadline) {
      report.healthAfter = await healthStatus()
      if (report.healthAfter === 200) break
      await new Promise((resolve) => setTimeout(resolve, 300))
    }

    report.ok =
      report.initialRows > 0 &&
      report.recoveredRows > 0 &&
      !report.errorVisible &&
      report.healthAfter === 200 &&
      pageErrors.length === 0
    log('report', report)
  } finally {
    const paths = await writeReport(report)
    log('written', paths)
    await browser.close()
  }

  if (!report.ok) process.exit(1)
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
