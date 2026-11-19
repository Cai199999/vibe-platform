#!/usr/bin/env node
/**
 * 存量源码 Direct MOVE_NODE 真实闭环 E2E。
 *
 * 验证：
 *   - Source Map 为同级 JSX 节点生成唯一 data-oid；
 *   - MOVE_NODE 使用 inside_index 的“移动后最终序号”语义，
 *     同父容器内移动不会出现“超过子节点范围”或假成功；
 *   - 写回后 HMR 预览顺序变化，BFF 快照同步为同一顺序；
 *   - 未被移动的兄弟节点内容保持不变。
 *
 * 运行：
 *   NODE_PATH=/tmp/vibe-e2e/node_modules \
 *   node scripts/direct_move_e2e.mjs
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
const WORK = process.env.MOVE_E2E_WORK || '/tmp/vibe-direct-move-e2e'
const ZIP = path.join(WORK, 'project.zip')
const CHROME =
  process.env.CHROME_BIN ||
  path.join(
    os.homedir(),
    '.cache/ms-playwright/chromium-1254/chrome-linux64/chrome',
  )

const ORIGINAL = `export default function App() {
  return (
    <main>
      <h1>标题一</h1>
      <p>段落二</p>
      <button type="button">按钮三</button>
    </main>
  )
}
`

function log(...args) {
  console.log('[direct-move-e2e]', ...args)
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
}

async function waitForSourceMap(previewUrl) {
  let map = { nodes: {} }
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const response = await fetch(`${previewUrl}/vibe-source-map.json`)
      if (response.ok) map = await response.json()
    } catch {
      // Vite 尚未就绪
    }
    if (Object.keys(map.nodes || {}).length >= 4) return map
    await new Promise((resolve) => setTimeout(resolve, 500))
  }
  return map
}

function nodeByTag(map, tag) {
  return Object.values(map.nodes || {}).find(
    (node) => node.file === 'src/App.jsx' && node.tag === tag,
  )
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
    await demo.waitFor({ state: 'visible', timeout: 10000 }).catch(() => {})
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
        projectName: 'Direct 移动 E2E',
        entryFile: 'src/App.jsx',
        sourceName: 'App.jsx',
        files: { 'src/App.jsx': ORIGINAL },
      }),
    })
    if (!seed.ok) throw new Error(`seed failed: ${await seed.text()}`)

    const sourceMap = await waitForSourceMap(imported.previewUrl)
    const main = nodeByTag(sourceMap, 'main')
    const paragraph = nodeByTag(sourceMap, 'p')
    const button = nodeByTag(sourceMap, 'button')
    const heading = nodeByTag(sourceMap, 'h1')
    if (!main || !paragraph || !button || !heading) {
      throw new Error('source map nodes missing')
    }
    log('source map', {
      main: main.oid,
      heading: heading.oid,
      paragraph: paragraph.oid,
      button: button.oid,
      childOids: main.childOids,
      childCount: main.childCount,
    })
    if (
      !Array.isArray(main.childOids) ||
      main.childOids.join(',') !==
        [heading.oid, paragraph.oid, button.oid].join(',')
    ) {
      throw new Error(
        `childOids order mismatch: ${JSON.stringify(main.childOids)}`,
      )
    }

    // 直接打开预览页，向 SDK 自身发 drag-mode，验证真实手势落位。
    // 这一步不写代码，只验证 SDK 用 childOids 算出的 AST 序号。
    const previewPage = await context.newPage()
    try {
      await previewPage.goto(imported.previewUrl, {
        waitUntil: 'domcontentloaded',
      })
      await previewPage.waitForFunction(
        (oid) => Boolean(document.querySelector(`[data-oid="${oid}"]`)),
        paragraph.oid,
        { timeout: 60000 },
      )
      await previewPage.evaluate(() => {
        window.__vibeDragEvents = []
        window.addEventListener('message', (event) => {
          const data = event.data
          if (data && data.channel === 'vibe-inspect') {
            window.__vibeDragEvents.push(data)
          }
        })
        window.postMessage(
          { channel: 'vibe-inspect', type: 'drag-mode', enabled: true },
          '*',
        )
      })
      // 拖段落本身到末尾：块级节点必须移动自身，而不是上溯到 main。
      const dragGeom = await previewPage.evaluate((oid) => {
        const node = document.querySelector(`[data-oid="${oid}"]`)
        const rect = node.getBoundingClientRect()
        const button = document.querySelector('button')
        const buttonRect = button.getBoundingClientRect()
        return {
          startX: rect.left + rect.width / 2,
          startY: rect.top + rect.height / 2,
          endX: buttonRect.left + buttonRect.width / 2,
          endY: buttonRect.bottom + 4,
        }
      }, paragraph.oid)
      await previewPage.mouse.move(dragGeom.startX, dragGeom.startY)
      await previewPage.mouse.down()
      await previewPage.mouse.move(dragGeom.endX, dragGeom.endY, { steps: 12 })
      // 拖动中：候选父容器虚线框 + 插入线必须已经可见。
      await previewPage.waitForFunction(
        () => {
          const box = document.querySelector(
            '[data-vibe-drag-hint="container"]',
          )
          const line = document.querySelector('[data-vibe-drag-hint="line"]')
          return Boolean(
            box &&
              box.style.display === 'block' &&
              line &&
              line.style.display === 'block',
          )
        },
        null,
        { timeout: 5000 },
      )
      const hint = await previewPage.evaluate(
        ({ parentOid }) => {
          const toRect = (node) => node.getBoundingClientRect().toJSON()
          return {
            box: toRect(
              document.querySelector('[data-vibe-drag-hint="container"]'),
            ),
            line: toRect(document.querySelector('[data-vibe-drag-hint="line"]')),
            parent: toRect(document.querySelector(`[data-oid="${parentOid}"]`)),
            button: toRect(document.querySelector('button')),
          }
        },
        { parentOid: main.oid },
      )
      log('drag hint', JSON.stringify(hint))
      const near = (a, b) => Math.abs(a - b) <= 2
      if (
        !near(hint.box.left, hint.parent.left) ||
        !near(hint.box.top, hint.parent.top) ||
        !near(hint.box.width, hint.parent.width) ||
        !near(hint.box.height, hint.parent.height)
      ) {
        throw new Error(`drag hint parent box mismatch: ${JSON.stringify(hint)}`)
      }
      // 拖到末尾：插入线应落在 button 底边，宽度与 button 对齐。
      if (
        !near(hint.line.top, hint.button.bottom) ||
        !near(hint.line.width, hint.button.width)
      ) {
        throw new Error(`drag hint line mismatch: ${JSON.stringify(hint)}`)
      }
      await previewPage.mouse.up()
      const hintsAfterDrop = await previewPage.evaluate(
        () => document.querySelectorAll('[data-vibe-drag-hint]').length,
      )
      if (hintsAfterDrop !== 0) {
        throw new Error('drag hint should be cleared after drop')
      }
      const dragEnd = await previewPage
        .waitForFunction(
          () => {
            const events = window.__vibeDragEvents || []
            return events.find((item) => item.type === 'drag-end') || null
          },
          null,
          { timeout: 20000 },
        )
        .then((handle) => handle.jsonValue())
      const sdkPlacement = dragEnd?.payload?.placement || {}
      log('sdk drag payload', JSON.stringify(dragEnd?.payload || {}))
      if (
        sdkPlacement.parentOid !== main.oid ||
        sdkPlacement.parentFile !== 'src/App.jsx' ||
        sdkPlacement.insertPosition !== 'inside_index' ||
        sdkPlacement.siblingIndex !== 2
      ) {
        throw new Error(
          `sdk drag placement wrong: ${JSON.stringify(sdkPlacement)}`,
        )
      }
    } finally {
      await previewPage.close()
    }

    const revisionResponse = await fetch(
      `${BFF}/api/v1/direct-ops/revision?projectId=${projectId}` +
        `&sandboxId=${imported.sandboxId}`,
    )
    if (!revisionResponse.ok) {
      throw new Error(`revision failed: ${await revisionResponse.text()}`)
    }
    const revision = (await revisionResponse.json()).revision

    // 把 <p> 移到 <main> 的最后：移除后最终序号为 2。
    const directActionId = `dact_move_${Date.now()}`
    const started = Date.now()
    const directResponse = await fetch(`${BFF}/api/v1/direct-ops/actions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        directActionId,
        idempotencyKey: `${projectId}:${directActionId}`,
        projectId,
        sandboxId: imported.sandboxId,
        tenantId: 'tenant_local',
        baseRevision: revision,
        action: 'MOVE_NODE',
        target: {
          anchorId: 'anc_move_e2e',
          oid: paragraph.oid,
          placement: {
            parentSelector: `[data-oid="${main.oid}"]`,
            parentFile: 'src/App.jsx',
            insertPosition: 'inside_index',
            siblingIndex: 2,
          },
        },
        payload: {},
        options: { source: 'FLOATING_TOOLBAR', dryRun: false },
      }),
    })
    const directBody = await directResponse.json()
    if (directResponse.status !== 200 || directBody.status !== 'SUCCEEDED') {
      throw new Error(`direct move failed: ${JSON.stringify(directBody)}`)
    }
    log('direct move updated', {
      resolveMs: directBody.timings?.resolveMs,
      astEditMs: directBody.timings?.astEditMs,
      totalMs: directBody.timings?.totalMs,
      wallMs: Date.now() - started,
    })

    let source = ''
    let persisted = null
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const response = await fetch(
        `${BFF}/api/v1/project-sources/${projectId}?tenantId=tenant_local`,
      )
      if (response.ok) {
        persisted = await response.json()
        source = persisted.files?.['src/App.jsx'] || ''
        if (source.indexOf('按钮三') < source.indexOf('段落二')) break
      }
      await new Promise((resolve) => setTimeout(resolve, 250))
    }
    if (!persisted) throw new Error('snapshot failed')
    const positions = ['标题一', '按钮三', '段落二'].map((text) =>
      source.indexOf(text),
    )
    if (positions.some((value) => value < 0)) {
      throw new Error(`moved source lost content: ${source}`)
    }
    if (!(positions[0] < positions[1] && positions[1] < positions[2])) {
      throw new Error(`source order wrong: ${source}`)
    }

    // 预览顺序应与源码一致（HMR 完成后）。
    const host = new URL(imported.previewUrl).host
    let frame = null
    let text = ''
    const previewStarted = Date.now()
    for (let attempt = 0; attempt < 80; attempt += 1) {
      frame = page.frames().find((item) => item.url().includes(host))
      if (frame) {
        text = await frame.evaluate(() => document.body.innerText).catch(() => '')
        if (text.indexOf('按钮三') < text.indexOf('段落二')) break
      }
      await new Promise((resolve) => setTimeout(resolve, 250))
    }
    if (!frame) throw new Error('preview frame not found')
    if (!(text.indexOf('按钮三') < text.indexOf('段落二'))) {
      throw new Error(`preview order did not change: ${text}`)
    }
    const previewMs = Date.now() - previewStarted

    const result = {
      projectId,
      mainOid: main.oid,
      paragraphOid: paragraph.oid,
      order: ['h1', 'button', 'p'],
      source,
      previewText: text,
      previewMs,
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
