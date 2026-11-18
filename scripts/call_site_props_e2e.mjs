#!/usr/bin/env node
/**
 * 跨文件组件调用点 Props 修改 E2E。
 *
 * 验证：
 *   - App.jsx 的 <UserCard title="..." /> 生成 callSiteOid；
 *   - Source Map 能把 UserCard 定义根关联到 App.jsx 调用点；
 *   - UPDATE_PROPS 精准修改 App.jsx 的调用点，不触碰 UserCard.jsx；
 *   - sourceSync 把 App.jsx / UserCard.jsx 最新内容写回 BFF 快照。
 *
 * 运行：
 *   NODE_PATH=/tmp/vibe-e2e/node_modules \
 *   node scripts/call_site_props_e2e.mjs
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
  throw new Error('缺少 playwright-core；请设置 NODE_PATH')
}

const { chromium } = loadPlaywright()
const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.dirname(SCRIPT_DIR)
const TEMPLATE = path.join(ROOT, 'services/orchestrator/sandbox_template')
const WEB = process.env.WEB_URL || 'http://127.0.0.1:5173'
const BFF = process.env.BFF_URL || 'http://127.0.0.1:8080'
const WORK =
  process.env.CALL_SITE_E2E_WORK || '/tmp/vibe-call-site-e2e'
const ZIP = path.join(WORK, 'project.zip')
const CHROME =
  process.env.CHROME_BIN ||
  path.join(
    os.homedir(),
    '.cache/ms-playwright/chromium-1254/chrome-linux64/chrome',
  )

const APP_SOURCE = `import UserCard from './UserCard'

export default function App() {
  return (
    <main>
      <h1>跨文件调用点</h1>
      <UserCard title="原始标题" />
    </main>
  )
}
`
const CARD_SOURCE = `export default function UserCard({ title }) {
  return (
    <section>
      <h2>{title}</h2>
      <p>UserCard 内部</p>
    </section>
  )
}
`

function log(...args) {
  console.log('[call-site-e2e]', ...args)
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
  fs.writeFileSync(path.join(project, 'src/App.jsx'), APP_SOURCE, 'utf8')
  fs.writeFileSync(path.join(project, 'src/UserCard.jsx'), CARD_SOURCE, 'utf8')
  fs.rmSync(ZIP, { force: true })
  execFileSync('zip', ['-qr', ZIP, '.'], { cwd: project })
}

async function waitForSourceMap(previewUrl) {
  let map = { nodes: {}, callSites: {} }
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const response = await fetch(`${previewUrl}/vibe-source-map.json`)
      if (response.ok) map = await response.json()
    } catch {
      // Vite 尚未就绪
    }
    if (Object.keys(map.callSites || {}).length > 0) return map
    await new Promise((resolve) => setTimeout(resolve, 500))
  }
  return map
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
    const demo = page.getByRole('button', { name: '一键体验（推荐）' })
    if (await demo.isVisible().catch(() => false)) await demo.click()
    await page.locator('.project-switcher-button').waitFor({ timeout: 30000 })

    await page.locator('.project-switcher-button').click()
    const chooserPromise = page.waitForEvent('filechooser')
    await page.getByRole('menuitem', { name: '导入项目' }).click()
    const chooser = await chooserPromise
    const importPromise = page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        response.url().endsWith('/api/v1/projects/import'),
      { timeout: 420000 },
    )
    await chooser.setFiles(ZIP)
    const importResponse = await importPromise
    if (importResponse.status() !== 201) {
      throw new Error(`import failed: ${await importResponse.text()}`)
    }
    const imported = await importResponse.json()
    projectId = imported.projectId
    log('imported', { projectId, sandboxId: imported.sandboxId })

    const seed = await fetch(`${BFF}/api/v1/project-sources/${projectId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenantId: 'tenant_local',
        projectName: '跨文件调用点',
        entryFile: 'src/App.jsx',
        sourceName: 'project.zip',
        files: {
          'src/App.jsx': APP_SOURCE,
          'src/UserCard.jsx': CARD_SOURCE,
        },
      }),
    })
    if (!seed.ok) throw new Error(`seed failed: ${await seed.text()}`)

    const sourceMap = await waitForSourceMap(imported.previewUrl)
    const callSites = Object.values(sourceMap.callSites || {})
    const callSite = callSites.find(
      (item) => item.file === 'src/App.jsx' && item.component === 'UserCard',
    )
    if (!callSite) throw new Error('callSite for UserCard not found')
    if (callSite.definitionFile !== 'src/UserCard.jsx') {
      throw new Error(
        `definition mapping wrong: ${JSON.stringify(callSite)}`,
      )
    }
    const definition = sourceMap.nodes?.[callSite.definitionOid]
    if (!definition || definition.callSites?.length !== 1) {
      throw new Error('definition root missing callSites back-reference')
    }
    log('call site index', {
      callSiteOid: callSite.oid,
      callSiteFile: callSite.file,
      definitionFile: callSite.definitionFile,
      props: callSite.props,
    })

    const revisionResponse = await fetch(
      `${BFF}/api/v1/direct-ops/revision?projectId=${projectId}` +
        `&sandboxId=${imported.sandboxId}`,
    )
    if (!revisionResponse.ok) {
      throw new Error(`revision failed: ${await revisionResponse.text()}`)
    }
    const revision = (await revisionResponse.json()).revision

    const directActionId = `dact_props_${Date.now()}`
    const directStarted = Date.now()
    const directResponse = await fetch(
      `${BFF}/api/v1/direct-ops/actions`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          directActionId,
          idempotencyKey: `${projectId}:${directActionId}`,
          projectId,
          sandboxId: imported.sandboxId,
          tenantId: 'tenant_local',
          baseRevision: revision,
          action: 'UPDATE_PROPS',
          target: {
            anchorId: 'anc_call_site_e2e',
            callSiteOid: callSite.oid,
          },
          payload: { props: { title: '来自调用点的新标题' } },
          options: { source: 'FLOATING_TOOLBAR', dryRun: false },
        }),
      },
    )
    const directBody = await directResponse.json()
    if (directResponse.status !== 200 || directBody.status !== 'SUCCEEDED') {
      throw new Error(`direct props update failed: ${JSON.stringify(directBody)}`)
    }
    const syncStatus = directBody.sourceSync?.status
    if (!['SYNCED', 'PENDING'].includes(syncStatus)) {
      throw new Error(`source sync failed: ${JSON.stringify(directBody.sourceSync)}`)
    }
    log('direct props updated', {
      resolveMs: directBody.timings?.resolveMs,
      astEditMs: directBody.timings?.astEditMs,
      sourceSyncMs: directBody.timings?.sourceSyncMs,
      totalMs: directBody.timings?.totalMs,
      wallMs: Date.now() - directStarted,
    })

    let snapshot = null
    let app = ''
    let card = ''
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const snapshotResponse = await fetch(
        `${BFF}/api/v1/project-sources/${projectId}?tenantId=tenant_local`,
      )
      if (snapshotResponse.ok) {
        snapshot = await snapshotResponse.json()
        app = snapshot.files['src/App.jsx'] || ''
        card = snapshot.files['src/UserCard.jsx'] || ''
        if (app.includes('来自调用点的新标题')) break
      }
      await new Promise((resolve) => setTimeout(resolve, 250))
    }
    if (!snapshot) throw new Error('snapshot failed')
    if (!app.includes('title="来自调用点的新标题"')) {
      throw new Error('App.jsx call site props were not updated')
    }
    if (card !== CARD_SOURCE) {
      throw new Error('UserCard.jsx definition was modified unexpectedly')
    }
    if (!app.includes("<UserCard title=\"来自调用点的新标题\" />")) {
      throw new Error('App.jsx call site JSX is not the expected exact node')
    }

    // 预览应由 HMR 展示新标题，同时保留 UserCard 内部实现。
    await page.goto(`${WEB}/workspace`, { waitUntil: 'domcontentloaded' })
    const viewResult = page.getByRole('button', { name: /查看效果/ })
    if (await viewResult.isVisible().catch(() => false)) await viewResult.click()
    const host = new URL(imported.previewUrl).host
    let frame = null
    for (let attempt = 0; attempt < 60; attempt += 1) {
      frame = page.frames().find((item) => item.url().includes(host))
      if (frame) {
        const text = await frame.evaluate(() => document.body.innerText).catch(() => '')
        if (text.includes('来自调用点的新标题')) break
      }
      await new Promise((resolve) => setTimeout(resolve, 500))
    }
    if (!frame) throw new Error('preview frame not found')
    const previewText = await frame.evaluate(() => document.body.innerText)
    if (!previewText.includes('来自调用点的新标题')) {
      throw new Error(`preview did not update: ${previewText}`)
    }
    if (!previewText.includes('UserCard 内部')) {
      throw new Error('UserCard definition content disappeared')
    }

    const result = {
      projectId,
      callSiteOid: callSite.oid,
      callSiteFile: callSite.file,
      definitionFile: callSite.definitionFile,
      appSource: app,
      cardUnchanged: card === CARD_SOURCE,
      directTimings: directBody.timings,
      pageErrors,
    }
    fs.writeFileSync(
      path.join(WORK, 'result.json'),
      JSON.stringify(result, null, 2),
    )
    log('PASS', result)
  } catch (error) {
    log('FAIL', error)
    await page.screenshot({ path: path.join(WORK, 'failure.png') })
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
