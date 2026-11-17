#!/usr/bin/env node
/**
 * CodeGen 页面 Playwright 冒烟。
 *
 * 前置：宿主机已安装 playwright-core，并设置 PREVIEW_URL。
 * 例：
 *   NODE_PATH=/tmp/visual-e2e/node_modules \
 *   PREVIEW_URL=http://127.0.0.1:51545 \
 *   node scripts/visual_codegen_playwright.js
 */
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { chromium } = require('playwright-core')

const PREVIEW_URL = process.env.PREVIEW_URL
if (!PREVIEW_URL) {
  console.error('PREVIEW_URL is required')
  process.exit(2)
}
const CHROME_BIN =
  process.env.CHROME_BIN ||
  path.join(
    os.homedir(),
    '.cache/ms-playwright/chromium-1254/chrome-linux64/chrome',
  )
const EXPECTED_IDS = (
  process.env.EXPECTED_NODE_IDS ||
  'nd_page0001,nd_title001,nd_list001,nd_item001,nd_input001,nd_btn0001'
)
  .split(',')
  .map((item) => item.trim())
  .filter(Boolean)
const EXPECTED_TEXT = (
  process.env.EXPECTED_TEXT || 'CodeGen E2E,示例任务,提交,请输入'
)
  .split(',')
  .map((item) => item.trim())
  .filter(Boolean)
const SCREENSHOT = path.join(
  __dirname,
  '..',
  'reports',
  'visual_codegen_e2e.png',
)

async function main() {
  const browser = await chromium.launch({
    executablePath: CHROME_BIN,
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  })
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  const pageErrors = []
  const consoleErrors = []
  page.on('pageerror', (error) => pageErrors.push(String(error)))
  page.on('console', (message) => {
    if (message.type() !== 'error') return
    const text = message.text()
    // Tailwind CDN 在本地预览会打印 production 警告，但不是页面错误。
    if (text.includes('cdn.tailwindcss.com')) return
    consoleErrors.push(text)
  })

  await page.goto(PREVIEW_URL, {
    waitUntil: 'networkidle',
    timeout: 45000,
  })
  await page.waitForSelector('[data-node-id="nd_page0001"]', {
    state: 'visible',
    timeout: 20000,
  })

  const found = await page.$$eval('[data-node-id]', (nodes) =>
    nodes.map((node) => ({
      id: node.getAttribute('data-node-id'),
      source: node.getAttribute('data-source'),
      text: node.textContent.trim(),
      visible: Boolean(node.offsetWidth || node.offsetHeight),
      className: node.className,
    })),
  )
  const ids = new Set(found.map((item) => item.id))
  const missing = EXPECTED_IDS.filter((id) => !ids.has(id))
  const invisible = found
    .filter((item) => EXPECTED_IDS.includes(item.id) && !item.visible)
    .map((item) => item.id)
  const badSource = found.filter(
    (item) => !item.source || !/^src\/App\.jsx:\d+$/.test(item.source),
  )
  const bodyText = await page.locator('body').innerText()
  const placeholders = await page.$$eval('input[placeholder]', (nodes) =>
    nodes.map((node) => node.getAttribute('placeholder') || ''),
  )
  const visibleText = [bodyText, ...placeholders].join('\n')
  const missingText = EXPECTED_TEXT.filter((text) => !visibleText.includes(text))
  const button = page.locator('[data-node-id="nd_btn0001"]')
  const buttonBackground = await button.evaluate(
    (node) => getComputedStyle(node).backgroundColor,
  )
  const buttonClasses = await button.getAttribute('class')

  fs.mkdirSync(path.dirname(SCREENSHOT), { recursive: true })
  await page.screenshot({ path: SCREENSHOT, fullPage: true })
  await browser.close()

  const failures = []
  if (missing.length) failures.push(`missing ids: ${missing.join(',')}`)
  if (invisible.length) failures.push(`invisible ids: ${invisible.join(',')}`)
  if (badSource.length) {
    failures.push(
      `bad data-source: ${badSource
        .map((item) => `${item.id}=${item.source}`)
        .join(',')}`,
    )
  }
  if (missingText.length) {
    failures.push(`missing text: ${missingText.join(',')}`)
  }
  if (pageErrors.length) failures.push(`page errors: ${pageErrors.join(' | ')}`)
  if (consoleErrors.length) {
    failures.push(`console errors: ${consoleErrors.join(' | ')}`)
  }
  if (
    !buttonBackground ||
    buttonBackground === 'rgba(0, 0, 0, 0)' ||
    buttonBackground === 'transparent'
  ) {
    failures.push(`Tailwind not applied to button: ${buttonBackground}`)
  }

  const result = {
    url: PREVIEW_URL,
    nodes: found.length,
    ids: [...ids],
    buttonBackground,
    buttonClasses,
    screenshot: SCREENSHOT,
    pageErrors,
    consoleErrors,
    failures,
  }
  console.log(JSON.stringify(result, null, 2))
  if (failures.length) {
    console.error('[playwright] FAILED')
    process.exit(1)
  }
  console.log('[playwright] PASS')
}

main().catch((error) => {
  console.error('[playwright] ERROR', error)
  process.exit(2)
})
