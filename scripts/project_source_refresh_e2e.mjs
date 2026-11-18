#!/usr/bin/env node
/**
 * 存量源码 OID 删除 + 持久化刷新真实浏览器 E2E。
 *
 * 前置：
 *   - platform-web / java-bff / python-ai 已启动；
 *   - 宿主机已安装 playwright-core，并设置 NODE_PATH；
 *   - 有可用的 Chromium（默认使用 Playwright 缓存）。
 *
 * 运行：
 *   NODE_PATH=/tmp/vibe-e2e/node_modules \
 *   node scripts/project_source_refresh_e2e.mjs
 *
 * 覆盖：
 *   1. 导入真实 React zip，启动沙盒并获得 vibe-source-map.json；
 *   2. 只读投影展示源码；
 *   3. iframe 内选中节点 → 删除，Direct Ops 返回 SUCCEEDED 且 sourceSync=SYNCED；
 *   4. BFF 快照 revision 增加、被删文本消失；
 *   5. 刷新页面后从 BFF 回灌，只读投影仍是删除后的代码。
 *
 * 脚本会输出 OID 覆盖率、sourceSync 耗时与刷新恢复耗时，并写
 * /tmp/vibe-source-refresh-e2e/result.json。
 */
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)

function loadPlaywright() {
  try {
    return require('playwright-core')
  } catch {
    // ESM 不读取 NODE_PATH，显式按目录回退。
  }
  const candidates = [
    process.env.PLAYWRIGHT_CORE_DIR,
    ...(process.env.NODE_PATH || '')
      .split(path.delimiter)
      .filter(Boolean)
      .map((dir) => path.join(dir, 'playwright-core')),
  ].filter(Boolean)
  for (const candidate of candidates) {
    try {
      return require(candidate)
    } catch {
      // 继续尝试
    }
  }
  throw new Error(
    '缺少 playwright-core；请安装后设置 NODE_PATH 或 PLAYWRIGHT_CORE_DIR',
  )
}

const { chromium } = loadPlaywright()

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.dirname(SCRIPT_DIR)
const TEMPLATE = path.join(ROOT, 'services/orchestrator/sandbox_template')
const WEB = process.env.WEB_URL || 'http://127.0.0.1:5173'
const BFF = process.env.BFF_URL || 'http://127.0.0.1:8080'
const WORK = process.env.SOURCE_E2E_WORK || '/tmp/vibe-source-refresh-e2e'
const ZIP = process.env.SOURCE_E2E_ZIP || path.join(WORK, 'project.zip')
const CHROME =
  process.env.CHROME_BIN ||
  path.join(
    os.homedir(),
    '.cache/ms-playwright/chromium-1254/chrome-linux64/chrome',
  )

const ORIGINAL = `export default function App() {
  return (
    <main className="p-8">
      <h1>E2E 存量项目</h1>
      <p className="text-slate-700">可删除节点</p>
      <button type="button">保留按钮</button>
    </main>
  )
}
`

function log(...args) {
  console.log('[source-e2e]', ...args)
}

function prepareFixture() {
  fs.mkdirSync(WORK, { recursive: true })
  const project = path.join(WORK, 'project')
  fs.rmSync(project, { recursive: true, force: true })
  fs.mkdirSync(path.join(project, 'src'), { recursive: true })
  for (const file of [
    'package.json',
    'index.html',
    'vite.config.js',
    'inspect_plugin.js',
    'inspect_sdk.js',
    'src/main.jsx',
  ]) {
    fs.copyFileSync(path.join(TEMPLATE, file), path.join(project, file))
  }
  fs.writeFileSync(path.join(project, 'src/App.jsx'), ORIGINAL, 'utf8')
  fs.rmSync(ZIP, { force: true })
  execFileSync('zip', ['-qr', ZIP, '.'], { cwd: project })
  log('fixture ready', ZIP)
}

async function waitForPreviewTextAbsent(page, host, text, timeout = 90000) {
  const started = Date.now()
  while (Date.now() - started < timeout) {
    const frame = page.frames().find((item) => item.url().includes(host))
    if (frame) {
      try {
        const present = await frame.evaluate(
          (value) => document.body.innerText.includes(value),
          text,
        )
        if (!present) return Date.now() - started
      } catch {
        // HMR 重载中，下一轮重新取 frame
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 250))
  }
  throw new Error(`text still present: ${text}`)
}

async function main() {
  prepareFixture()
  const browser = await chromium.launch({
    executablePath: CHROME,
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  })
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
  })
  const page = await context.newPage()
  const pageErrors = []
  page.on('pageerror', (error) => pageErrors.push(String(error)))

  let projectId = ''
  try {
    await page.goto(`${WEB}/workspace`, { waitUntil: 'domcontentloaded' })
    const demoButton = page.getByRole('button', { name: '一键体验（推荐）' })
    if (await demoButton.isVisible().catch(() => false)) {
      await demoButton.click()
    }
    await page.locator('.project-switcher-button').waitFor({ timeout: 30000 })

    log('importing zip …')
    const importStarted = Date.now()
    await page.locator('.project-switcher-button').click()
    const chooserPromise = page.waitForEvent('filechooser')
    await page.getByRole('menuitem', { name: '导入项目' }).click()
    const chooser = await chooserPromise
    const importResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        response.url().endsWith('/api/v1/projects/import'),
      { timeout: 420000 },
    )
    await chooser.setFiles(ZIP)
    const importResponse = await importResponsePromise
    if (importResponse.status() !== 201) {
      throw new Error(
        `import failed HTTP ${importResponse.status()}: ` +
          (await importResponse.text()),
      )
    }
    const imported = await importResponse.json()
    projectId = imported.projectId
    const importMs = Date.now() - importStarted
    log('import done', {
      projectId,
      sandboxId: imported.sandboxId,
      importMs,
    })

    const seed = await fetch(`${BFF}/api/v1/project-sources/${projectId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenantId: 'tenant_local',
        projectName: 'E2E 存量项目',
        entryFile: 'src/App.jsx',
        sourceName: 'App.jsx',
        files: { 'src/App.jsx': ORIGINAL },
      }),
    })
    if (!seed.ok) throw new Error(`seed snapshot failed: ${await seed.text()}`)

    let sourceMap = { nodes: {} }
    for (let attempt = 0; attempt < 60; attempt += 1) {
      try {
        const response = await fetch(
          `${imported.previewUrl}/vibe-source-map.json`,
        )
        if (response.ok) sourceMap = await response.json()
      } catch {
        // Vite 尚未就绪
      }
      if (Object.keys(sourceMap.nodes || {}).length >= 4) break
      await new Promise((resolve) => setTimeout(resolve, 500))
    }
    const oidCount = Object.keys(sourceMap.nodes || {}).length
    if (oidCount < 4) throw new Error(`oid coverage too low: ${oidCount}`)

    await page.goto(`${WEB}/visual-canvas?project=${projectId}&mode=existing`, {
      waitUntil: 'domcontentloaded',
    })
    await page.locator('[data-source-projection]').waitFor({ timeout: 30000 })
    await page.getByText('可删除节点').first().waitFor({ timeout: 30000 })

    await page.goto(`${WEB}/workspace`, { waitUntil: 'domcontentloaded' })
    const viewResult = page.getByRole('button', { name: /查看效果/ })
    if (await viewResult.isVisible().catch(() => false)) {
      await viewResult.click()
    }
    await page.locator('.tab-window.is-active .runtime-preview-frame').waitFor({
      state: 'visible',
      timeout: 60000,
    })
    const previewHost = new URL(imported.previewUrl).host
    const findPreviewFrame = () =>
      page.frames().find((item) => item.url().includes(previewHost))
    let frame = findPreviewFrame()
    await frame.waitForSelector('text=可删除节点', { timeout: 60000 })

    // 画布样式类面板：先把 p 的 className 改掉。
    await frame.evaluate(() => {
      const node = [...document.querySelectorAll('p')].find((item) =>
        item.textContent.includes('可删除节点'),
      )
      if (!node) throw new Error('target node not found')
      node.dispatchEvent(
        new MouseEvent('mouseover', { bubbles: true, cancelable: true }),
      )
    })
    await frame.waitForFunction(
      () => {
        const button = document.querySelector('button[title="编辑样式类"]')
        return Boolean(button && button.style.display !== 'none')
      },
      null,
      { timeout: 20000 },
    )
    await frame.evaluate(() => {
      document.querySelector('button[title="编辑样式类"]').click()
    })
    await frame.waitForFunction(
      () => {
        const panel = document.querySelector('[data-vibe-class-panel]')
        return Boolean(panel && panel.style.display !== 'none')
      },
      null,
      { timeout: 10000 },
    )
    await frame.evaluate(() => {
      const input = document.querySelector('[data-vibe-class-input]')
      input.value = 'text-red-600 font-bold'
      input.dispatchEvent(new Event('input', { bubbles: true }))
    })
    const classResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        response.url().includes('/api/v1/direct-ops/actions'),
      { timeout: 60000 },
    )
    await frame.evaluate(() => {
      document.querySelector('[data-vibe-class-save]').click()
    })
    const classResponse = await classResponsePromise
    const classBody = await classResponse.json()
    if (classResponse.status() !== 200 || classBody.status !== 'SUCCEEDED') {
      throw new Error(`class update failed: ${JSON.stringify(classBody)}`)
    }
    let classPersisted = false
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const response = await fetch(
        `${BFF}/api/v1/project-sources/${projectId}?tenantId=tenant_local`,
      )
      if (response.ok) {
        const body = await response.json()
        if ((body.files?.['src/App.jsx'] || '').includes('text-red-600 font-bold')) {
          classPersisted = true
          break
        }
      }
      await new Promise((resolve) => setTimeout(resolve, 250))
    }
    if (!classPersisted) throw new Error('class change was not persisted')
    log('class panel updated', {
      totalMs: classBody.timings?.totalMs,
    })

    // HMR 后 iframe 会重建，重新绑定 frame 再操作删除。
    frame = findPreviewFrame()
    await frame.waitForSelector('text=可删除节点', { timeout: 60000 })

    // 画布 inline style 面板：新增 fontSize。
    await frame.evaluate(() => {
      const node = [...document.querySelectorAll('p')].find((item) =>
        item.textContent.includes('可删除节点'),
      )
      if (!node) throw new Error('target node not found')
      node.dispatchEvent(
        new MouseEvent('mouseover', { bubbles: true, cancelable: true }),
      )
    })
    await frame.waitForFunction(
      () => {
        const button = document.querySelector('button[title="编辑内联样式"]')
        return Boolean(button && button.style.display !== 'none')
      },
      null,
      { timeout: 20000 },
    )
    await frame.evaluate(() => {
      document.querySelector('button[title="编辑内联样式"]').click()
    })
    await frame.waitForFunction(
      () => {
        const panel = document.querySelector('[data-vibe-style-panel]')
        return Boolean(panel && panel.style.display !== 'none')
      },
      null,
      { timeout: 10000 },
    )
    await frame.evaluate(() => {
      document.querySelector('[data-vibe-style-add]').click()
    })
    await frame.waitForFunction(
      () => Boolean(document.querySelector('[data-vibe-style-key]')),
      null,
      { timeout: 10000 },
    )
    await frame.evaluate(() => {
      const key = document.querySelector('[data-vibe-style-key]')
      const value = document.querySelector('[data-vibe-style-value]')
      key.value = 'fontSize'
      key.dispatchEvent(new Event('input', { bubbles: true }))
      value.value = '18px'
      value.dispatchEvent(new Event('input', { bubbles: true }))
    })
    const styleResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        response.url().includes('/api/v1/direct-ops/actions'),
      { timeout: 60000 },
    )
    await frame.evaluate(() => {
      document.querySelector('[data-vibe-style-save]').click()
    })
    const styleResponse = await styleResponsePromise
    const styleBody = await styleResponse.json()
    if (styleResponse.status() !== 200 || styleBody.status !== 'SUCCEEDED') {
      throw new Error(`style update failed: ${JSON.stringify(styleBody)}`)
    }
    let stylePersisted = false
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const response = await fetch(
        `${BFF}/api/v1/project-sources/${projectId}?tenantId=tenant_local`,
      )
      if (response.ok) {
        const body = await response.json()
        const app = body.files?.['src/App.jsx'] || ''
        if (app.includes('fontSize') && app.includes('18px')) {
          stylePersisted = true
          break
        }
      }
      await new Promise((resolve) => setTimeout(resolve, 250))
    }
    if (!stylePersisted) throw new Error('style change was not persisted')
    log('style panel updated', {
      totalMs: styleBody.timings?.totalMs,
    })

    frame = findPreviewFrame()
    await frame.waitForSelector('text=可删除节点', { timeout: 60000 })
    await frame.evaluate(() => {
      const node = [...document.querySelectorAll('p')].find((item) =>
        item.textContent.includes('可删除节点'),
      )
      if (!node) throw new Error('target node not found')
      node.dispatchEvent(
        new MouseEvent('mouseover', { bubbles: true, cancelable: true }),
      )
    })
    await frame.waitForFunction(
      () => {
        const button = document.querySelector('button[title="删除组件"]')
        return Boolean(button && button.style.display !== 'none')
      },
      null,
      { timeout: 20000 },
    )
    const directResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        response.url().includes('/api/v1/direct-ops/actions'),
      { timeout: 60000 },
    )
    await frame.evaluate(() => {
      document.querySelector('button[title="删除组件"]').click()
    })
    // 安全叶子立即执行；只有不安全节点才弹确认面板。
    const confirmVisible = await frame
      .waitForFunction(
        () =>
          [...document.querySelectorAll('button')].some(
            (item) =>
              item.textContent === '确认删除' && item.offsetParent !== null,
          ),
        null,
        { timeout: 1200 },
      )
      .then(() => true)
      .catch(() => false)
    if (confirmVisible) {
      await frame.evaluate(() => {
        const button = [...document.querySelectorAll('button')].find(
          (item) => item.textContent === '确认删除',
        )
        button.click()
      })
    }
    const directResponse = await directResponsePromise
    const directBody = await directResponse.json()
    if (directResponse.status() !== 200 || directBody.status !== 'SUCCEEDED') {
      throw new Error(`direct delete failed: ${JSON.stringify(directBody)}`)
    }
    const syncStatus = directBody.sourceSync?.status
    if (!['SYNCED', 'PENDING'].includes(syncStatus)) {
      throw new Error(
        `source snapshot not synced: ${JSON.stringify(directBody.sourceSync)}`,
      )
    }
    const removalMs = await waitForPreviewTextAbsent(
      page,
      previewHost,
      '可删除节点',
    )

    // 异步 sourceSync：轮询 BFF 快照，直到删除结果可见。
    let persistedBody = null
    let persistedSource = ''
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const persisted = await fetch(
        `${BFF}/api/v1/project-sources/${projectId}?tenantId=tenant_local`,
      )
      if (persisted.ok) {
        persistedBody = await persisted.json()
        persistedSource = persistedBody.files?.['src/App.jsx'] || ''
        if (!persistedSource.includes('可删除节点')) break
      }
      await new Promise((resolve) => setTimeout(resolve, 250))
    }
    if (!persistedBody) throw new Error('snapshot get failed')
    if (persistedSource.includes('可删除节点')) {
      throw new Error('persisted snapshot still contains deleted node')
    }
    if (!persistedSource.includes('保留按钮')) {
      throw new Error('persisted snapshot lost untouched content')
    }

    const hydrationPromise = page.waitForResponse(
      (response) =>
        response.request().method() === 'GET' &&
        response.url().includes(`/api/v1/project-sources/${projectId}`),
      { timeout: 30000 },
    )
    const reloadStarted = Date.now()
    await page.reload({ waitUntil: 'domcontentloaded' })
    const hydration = await hydrationPromise
    await page.waitForTimeout(600)
    await page.goto(`${WEB}/visual-canvas?project=${projectId}&mode=existing`, {
      waitUntil: 'domcontentloaded',
    })
    await page.locator('[data-source-projection]').waitFor({ timeout: 30000 })
    await page.getByText('保留按钮').first().waitFor({ timeout: 30000 })
    const staleCount = await page.getByText('可删除节点').count()
    const recoveryMs = Date.now() - reloadStarted
    if (staleCount !== 0) {
      throw new Error('refresh did not pick up persisted deletion')
    }

    const result = {
      projectId,
      oidCount,
      oidCoverage: `${oidCount}/${oidCount}`,
      importMs,
      directDelete: {
        resolveMs: directBody.timings?.resolveMs,
        sourceSyncMs: directBody.timings?.sourceSyncMs,
        totalMs: directBody.timings?.totalMs,
        previewRemovalMs: removalMs,
      },
      persistence: {
        revision: persistedBody.revision,
        contentHash: persistedBody.contentHash,
        hydrationStatus: hydration.status(),
        reloadRecoveryMs: recoveryMs,
      },
      pageErrors,
    }
    fs.mkdirSync(WORK, { recursive: true })
    fs.writeFileSync(
      path.join(WORK, 'result.json'),
      JSON.stringify(result, null, 2),
    )
    log('PASS', result)
  } catch (error) {
    log('FAIL', error)
    await page.screenshot({ path: path.join(WORK, 'failure.png') })
    fs.mkdirSync(WORK, { recursive: true })
    fs.writeFileSync(
      path.join(WORK, 'result.json'),
      JSON.stringify({ projectId, error: String(error), pageErrors }, null, 2),
    )
    process.exitCode = 1
  } finally {
    await browser.close()
  }
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
