#!/usr/bin/env node
/**
 * 存量源码 Direct MOVE_NODE 真实闭环 E2E。
 *
 * 验证：
 *   - Source Map 为同级 JSX 节点生成唯一 data-oid；
 *   - MOVE_NODE 使用 inside_index 的“移动后最终序号”语义，
 *     同父容器内移动不会出现“超过子节点范围”或假成功；
 *   - 写回后 HMR 预览顺序变化，BFF 快照同步为同一顺序；
 *   - 未被移动的兄弟节点内容保持不变；
 *   - 拖拽期间源节点 position:fixed + stub 占位，stub 不拦截命中；
 *   - 抓上半部向下拖仍按鼠标位置落位（MOVE_E2E_GRAB_RATIO=0.25）；
 *   - 尺寸面板发出 Fill / Fixed 消息契约，SET_SIZE 写入 Tailwind 尺寸类；
 *   - ADD_COMPONENT 接收圈选尺寸并写入初始宽高；
 *   - Alt 拖拽发出 free placement，MOVE_FREE 写入 absolute + 父容器 relative。
 *
 * 运行：
 *   NODE_PATH=/tmp/vibe-e2e/node_modules \
 *   MOVE_E2E_GRAB_RATIO=0.25 node scripts/direct_move_e2e.mjs
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
// 抓取点高度比例：0.5 = 正中；0.25 用于回归“抓上半部向下拖”时
// 源节点自身 rect 不得再参与中点比较。
const GRAB_RATIO = Number(process.env.MOVE_E2E_GRAB_RATIO || 0.5)
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
      const dragGeom = await previewPage.evaluate(
        ({ oid, ratio }) => {
          const node = document.querySelector(`[data-oid="${oid}"]`)
          const rect = node.getBoundingClientRect()
          const button = document.querySelector('button')
          const buttonRect = button.getBoundingClientRect()
          return {
            startX: rect.left + rect.width / 2,
            startY: rect.top + rect.height * ratio,
            endX: buttonRect.left + buttonRect.width / 2,
            endY: buttonRect.bottom + 4,
          }
        },
        { oid: paragraph.oid, ratio: GRAB_RATIO },
      )
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
      // Onlook 式拖拽：源节点脱离文档流、原位置有 stub 占位，
      // 且 stub 不能拦截 elementFromPoint。
      const dragShell = await previewPage.evaluate((oid) => {
        const node = document.querySelector(`[data-oid="${oid}"]`)
        const stub = document.querySelector('[data-vibe-drag-stub="true"]')
        return {
          stub: Boolean(stub),
          stubPointerEvents: stub
            ? getComputedStyle(stub).pointerEvents
            : '',
          sourcePosition: node ? getComputedStyle(node).position : '',
        }
      }, paragraph.oid)
      log('drag shell', JSON.stringify(dragShell))
      if (!dragShell.stub) throw new Error('drag stub should exist')
      if (dragShell.stubPointerEvents !== 'none') {
        throw new Error('drag stub must not intercept hits')
      }
      if (dragShell.sourcePosition !== 'fixed') {
        throw new Error('dragged node should be lifted out of flow')
      }
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
      const hintsAfterDrop = await previewPage.evaluate(() => ({
        hints: document.querySelectorAll('[data-vibe-drag-hint]').length,
        stubs: document.querySelectorAll('[data-vibe-drag-stub="true"]').length,
      }))
      if (hintsAfterDrop.hints !== 0) {
        throw new Error('drag hint should be cleared after drop')
      }
      if (hintsAfterDrop.stubs !== 0) {
        throw new Error('drag stub should be removed after drop')
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
      const sdkPayload = dragEnd?.payload || {}
      log('sdk drag payload', {
        token: sdkPayload.token,
        sourceOid: sdkPayload.source?.oid,
        placement: process.env.LIVE_DEBUG === '1'
          ? sdkPayload.placement
          : {
              parentOid: sdkPayload.placement?.parentOid,
              parentFile: sdkPayload.placement?.parentFile,
              insertPosition: sdkPayload.placement?.insertPosition,
              siblingIndex: sdkPayload.placement?.siblingIndex,
            },
      })
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

      // --- 任务三：Alt 自由定位的 SDK 消息契约 ---
      await previewPage.keyboard.down('Alt')
      const freeGeom = await previewPage.evaluate((oid) => {
        const node = document.querySelector(`[data-oid="${oid}"]`)
        const rect = node.getBoundingClientRect()
        return {
          startX: rect.left + rect.width / 2,
          startY: rect.top + rect.height / 2,
          endX: rect.left + rect.width / 2 + 90,
          endY: rect.top + rect.height / 2 + 40,
        }
      }, heading.oid)
      await previewPage.mouse.move(freeGeom.startX, freeGeom.startY)
      await previewPage.mouse.down()
      await previewPage.mouse.move(freeGeom.endX, freeGeom.endY, { steps: 10 })
      await previewPage.mouse.up()
      await previewPage.keyboard.up('Alt')
      const freeEnd = await previewPage
        .waitForFunction(
          () => {
            const events = (window.__vibeDragEvents || []).filter(
              (item) => item.type === 'drag-end',
            )
            return events.length >= 2 ? events[events.length - 1] : null
          },
          null,
          { timeout: 20000 },
        )
        .then((handle) => handle.jsonValue())
      const freePlacement = freeEnd?.payload?.placement || {}
      log('free drag payload', JSON.stringify({
        free: freeEnd?.payload?.free,
        mode: freePlacement.mode,
        parentOid: freePlacement.parentOid,
        left: freePlacement.freeLeft,
        top: freePlacement.freeTop,
        width: freePlacement.freeWidthPx,
      }))
      if (freeEnd?.payload?.free !== true) {
        throw new Error('alt drag should mark free=true')
      }
      if (freePlacement.mode !== 'free') {
        throw new Error('alt drag placement mode should be free')
      }
      if (freePlacement.parentOid !== main.oid) {
        throw new Error('alt drag parent should be main')
      }
      if (
        !Number.isFinite(freePlacement.freeLeft) ||
        !Number.isFinite(freePlacement.freeTop) ||
        !(Number(freePlacement.freeWidthPx) > 0)
      ) {
        throw new Error(
          `alt drag coordinates invalid: ${JSON.stringify(freePlacement)}`,
        )
      }

      // --- 任务二：尺寸面板 UI（Hug / Fill / Fixed）发出的消息契约 ---
      await previewPage.evaluate(() => {
        window.__vibeDragEvents = []
        window.postMessage(
          {
            channel: 'vibe-inspect',
            type: 'direct-mode',
            enabled: true,
            projectId: 'e2e',
          },
          '*',
        )
      })
      await previewPage.waitForTimeout(80)
      const sizeUi = await previewPage.evaluate(async (oid) => {
        const node = document.querySelector(`[data-oid="${oid}"]`)
        if (!node) return { ok: false, reason: 'node missing' }
        node.dispatchEvent(
          new MouseEvent('mouseover', { bubbles: true, cancelable: true }),
        )
        const waitFor = async (fn, timeout = 5000) => {
          const deadline = Date.now() + timeout
          while (Date.now() < deadline) {
            const value = fn()
            if (value) return value
            await new Promise((resolve) => setTimeout(resolve, 50))
          }
          return null
        }
        const visible = (el) => Boolean(el) && el.style.display !== 'none'
        const undo = await waitFor(() =>
          Array.from(
            document.querySelectorAll('[data-vibe-direct-ui]'),
          ).find((el) => visible(el) && String(el.textContent).includes('撤回')),
        )
        if (!undo) return { ok: false, reason: 'direct mode not armed' }
        const sizeButton = await waitFor(() =>
          Array.from(
            document.querySelectorAll('[data-vibe-direct-ui]'),
          ).find(
            (el) =>
              el.tagName === 'BUTTON' &&
              visible(el) &&
              String(el.title || '').includes('调整尺寸'),
          ),
        )
        if (!sizeButton) {
          return {
            ok: false,
            reason: 'size button not visible',
            nodeSource: node.getAttribute('data-source'),
            oid: node.getAttribute('data-oid'),
            uiCount: document.querySelectorAll('[data-vibe-direct-ui]').length,
          }
        }
        sizeButton.click()
        const panel = await waitFor(() => {
          const node = document.querySelector(
            '[data-vibe-size-panel="true"]',
          )
          return visible(node) ? node : null
        })
        if (!panel) return { ok: false, reason: 'size panel not visible' }
        panel
          .querySelector('[data-vibe-size-mode="fill"][data-axis="width"]')
          .click()
        panel
          .querySelector('[data-vibe-size-mode="fixed"][data-axis="height"]')
          .click()
        const input = panel.querySelector(
          '[data-vibe-size-value][data-axis="height"]',
        )
        input.value = '80'
        input.dispatchEvent(new Event('input', { bubbles: true }))
        panel.querySelector('[data-vibe-size-apply="true"]').click()
        const event = await waitFor(() =>
          (window.__vibeDragEvents || []).find(
            (item) => item.type === 'direct-size-update',
          ),
        )
        return {
          ok: Boolean(event),
          payload: event?.payload || null,
        }
      }, heading.oid)
      log('size ui payload', JSON.stringify({
        ok: sizeUi.ok,
        width: sizeUi.payload?.width,
        height: sizeUi.payload?.height,
        parentDisplay: sizeUi.payload?.parentLayout?.display,
        selector: sizeUi.payload?.source?.selector,
      }))
      if (!sizeUi.ok) {
        throw new Error(`size panel failed: ${JSON.stringify(sizeUi)}`)
      }
      if (sizeUi.payload?.width?.mode !== 'fill') {
        throw new Error('size panel width mode should be fill')
      }
      if (
        sizeUi.payload?.height?.mode !== 'fixed' ||
        Number(sizeUi.payload?.height?.value) !== 80
      ) {
        throw new Error('size panel height mode should be fixed 80')
      }
      if (sizeUi.payload?.source?.selector !== `[data-oid="${heading.oid}"]`) {
        throw new Error('size panel target selector mismatch')
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

    // --- 任务二：SET_SIZE 后端闭环（Fill 宽 + Fixed 高）---
    const sizeRevisionResponse = await fetch(
      `${BFF}/api/v1/direct-ops/revision?projectId=${projectId}` +
        `&sandboxId=${imported.sandboxId}`,
    )
    if (!sizeRevisionResponse.ok) {
      throw new Error(`size revision failed: ${await sizeRevisionResponse.text()}`)
    }
    const sizeRevision = (await sizeRevisionResponse.json()).revision
    const sizeActionId = `dact_size_${Date.now()}`
    const sizeResponse = await fetch(`${BFF}/api/v1/direct-ops/actions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        directActionId: sizeActionId,
        idempotencyKey: `${projectId}:${sizeActionId}`,
        projectId,
        sandboxId: imported.sandboxId,
        tenantId: 'tenant_local',
        baseRevision: sizeRevision,
        action: 'SET_SIZE',
        target: {
          anchorId: 'anc_size_e2e',
          oid: heading.oid,
          selector: `[data-oid="${heading.oid}"]`,
          file: 'src/App.jsx',
        },
        payload: {
          width: { mode: 'fill' },
          height: { mode: 'fixed', value: 80 },
          parentLayout: { display: 'block', flexDirection: 'row' },
        },
        options: { source: 'FLOATING_TOOLBAR', dryRun: false },
      }),
    })
    const sizeBody = await sizeResponse.json()
    if (sizeResponse.status !== 200 || sizeBody.status !== 'SUCCEEDED') {
      throw new Error(`direct size failed: ${JSON.stringify(sizeBody)}`)
    }
    let sizedSource = ''
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const response = await fetch(
        `${BFF}/api/v1/project-sources/${projectId}?tenantId=tenant_local`,
      )
      if (response.ok) {
        const body = await response.json()
        sizedSource = body.files?.['src/App.jsx'] || ''
        if (
          sizedSource.includes('w-full') &&
          sizedSource.includes('h-[80px]') &&
          sizedSource.includes('标题一')
        ) {
          break
        }
      }
      await new Promise((resolve) => setTimeout(resolve, 250))
    }
    if (
      !sizedSource.includes('w-full') ||
      !sizedSource.includes('h-[80px]')
    ) {
      throw new Error(`SET_SIZE source not persisted: ${sizedSource}`)
    }

    // --- 任务一：ADD_COMPONENT 接收圈选尺寸并写入初始宽高 ---
    const addRevisionResponse = await fetch(
      `${BFF}/api/v1/direct-ops/revision?projectId=${projectId}` +
        `&sandboxId=${imported.sandboxId}`,
    )
    if (!addRevisionResponse.ok) {
      throw new Error(`add revision failed: ${await addRevisionResponse.text()}`)
    }
    const addRevision = (await addRevisionResponse.json()).revision
    const addActionId = `dact_add_size_${Date.now()}`
    const addResponse = await fetch(`${BFF}/api/v1/direct-ops/actions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        directActionId: addActionId,
        idempotencyKey: `${projectId}:${addActionId}`,
        projectId,
        sandboxId: imported.sandboxId,
        tenantId: 'tenant_local',
        baseRevision: addRevision,
        action: 'ADD_COMPONENT',
        target: {
          anchorId: 'anc_add_size_e2e',
          oid: main.oid,
          selector: `[data-oid="${main.oid}"]`,
          file: 'src/App.jsx',
        },
        payload: {
          componentType: 'Container',
          size: { widthPx: 200, heightPx: 50 },
        },
        options: { source: 'FLOATING_TOOLBAR', dryRun: false },
      }),
    })
    const addBody = await addResponse.json()
    if (addResponse.status !== 200 || addBody.status !== 'SUCCEEDED') {
      throw new Error(`direct add size failed: ${JSON.stringify(addBody)}`)
    }
    let addedSource = ''
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const response = await fetch(
        `${BFF}/api/v1/project-sources/${projectId}?tenantId=tenant_local`,
      )
      if (response.ok) {
        const body = await response.json()
        addedSource = body.files?.['src/App.jsx'] || ''
        if (
          addedSource.includes('vibe-direct-container') &&
          addedSource.includes('w-[200px]') &&
          addedSource.includes('h-[50px]')
        ) {
          break
        }
      }
      await new Promise((resolve) => setTimeout(resolve, 250))
    }
    for (const marker of ['vibe-direct-container', 'w-[200px]', 'h-[50px]', 'min-h-[30px]']) {
      if (!addedSource.includes(marker)) {
        throw new Error(`ADD_COMPONENT size missing ${marker}: ${addedSource}`)
      }
    }

    // --- 任务三：MOVE_FREE 后端闭环（absolute + 父容器 relative）---
    const freeRevisionResponse = await fetch(
      `${BFF}/api/v1/direct-ops/revision?projectId=${projectId}` +
        `&sandboxId=${imported.sandboxId}`,
    )
    if (!freeRevisionResponse.ok) {
      throw new Error(`free revision failed: ${await freeRevisionResponse.text()}`)
    }
    const freeRevision = (await freeRevisionResponse.json()).revision
    const freeActionId = `dact_free_${Date.now()}`
    const freeResponse = await fetch(`${BFF}/api/v1/direct-ops/actions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        directActionId: freeActionId,
        idempotencyKey: `${projectId}:${freeActionId}`,
        projectId,
        sandboxId: imported.sandboxId,
        tenantId: 'tenant_local',
        baseRevision: freeRevision,
        action: 'MOVE_FREE',
        target: {
          anchorId: 'anc_free_e2e',
          oid: heading.oid,
          selector: `[data-oid="${heading.oid}"]`,
          file: 'src/App.jsx',
          placement: {
            parentSelector: `[data-oid="${main.oid}"]`,
            parentOid: main.oid,
            parentFile: 'src/App.jsx',
            mode: 'free',
            freeLeft: 120.5,
            freeTop: 40,
            freeWidthPx: 300,
            freeHeightPx: 60,
          },
        },
        payload: {},
        options: { source: 'FLOATING_TOOLBAR', dryRun: false },
      }),
    })
    const freeBody = await freeResponse.json()
    if (freeResponse.status !== 200 || freeBody.status !== 'SUCCEEDED') {
      throw new Error(`MOVE_FREE failed: ${JSON.stringify(freeBody)}`)
    }
    let freeSource = ''
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const response = await fetch(
        `${BFF}/api/v1/project-sources/${projectId}?tenantId=tenant_local`,
      )
      if (response.ok) {
        const body = await response.json()
        freeSource = body.files?.['src/App.jsx'] || ''
        if (
          freeSource.includes('position: "absolute"') &&
          freeSource.includes('position: "relative"') &&
          freeSource.includes('left: "120.5px"')
        ) {
          break
        }
      }
      await new Promise((resolve) => setTimeout(resolve, 250))
    }
    for (const marker of [
      'position: "absolute"',
      'position: "relative"',
      'left: "120.5px"',
      'top: "40px"',
      'width: "300px"',
      'height: "60px"',
      'transform: "none"',
    ]) {
      if (!freeSource.includes(marker)) {
        throw new Error(`MOVE_FREE source missing ${marker}: ${freeSource}`)
      }
    }

    // --- 离线缓冲：删除入队 -> 提交修改 -> 不重建 iframe，靠 HMR 生效 ---
    let liveFrame = null
    for (let attempt = 0; attempt < 40; attempt += 1) {
      liveFrame = page.frames().find((item) => item.url().includes(host))
      if (liveFrame) {
        const exists = await liveFrame
          .evaluate(() =>
            Boolean(document.querySelector('.vibe-direct-container')),
          )
          .catch(() => false)
        if (exists) break
      }
      await new Promise((resolve) => setTimeout(resolve, 250))
    }
    if (!liveFrame) throw new Error('workspace live frame not found')
    const containerVisible = await liveFrame
      .evaluate(() => Boolean(document.querySelector('.vibe-direct-container')))
      .catch(() => false)
    if (!containerVisible) {
      throw new Error('added container not visible in workspace frame')
    }
    await liveFrame.evaluate(() => {
      window.__vibeNoReloadMarker = 'buffer'
    })
    const deleteClicked = await liveFrame.evaluate(() => {
      const node = document.querySelector('.vibe-direct-container')
      if (!node) return false
      node.dispatchEvent(
        new MouseEvent('mouseover', { bubbles: true, cancelable: true }),
      )
      const deadline = Date.now() + 5000
      return new Promise((resolve) => {
        const timer = setInterval(() => {
          const button = [
            ...document.querySelectorAll('[data-vibe-direct-ui]'),
          ].find(
            (el) =>
              el.tagName === 'BUTTON' &&
              String(el.title || '').includes('删除组件') &&
              el.style.display !== 'none',
          )
          if (button) {
            clearInterval(timer)
            button.click()
            resolve(true)
            return
          }
          if (Date.now() > deadline) {
            clearInterval(timer)
            resolve(false)
          }
        }, 50)
      })
    })
    if (!deleteClicked) {
      throw new Error('delete button not clickable in workspace frame')
    }
    const draftBar = page.locator('[data-layout-draft-bar]')
    await draftBar.waitFor({ state: 'visible', timeout: 15000 })
    const draftText = await draftBar.innerText()
    if (!draftText.includes('未保存的画布变更')) {
      throw new Error(`draft bar text wrong: ${draftText}`)
    }
    const saveLabel = await draftBar.locator('.layout-draft-save').innerText()
    if (!saveLabel.includes('提交修改')) {
      throw new Error(`buffered delete save label wrong: ${saveLabel}`)
    }
    await draftBar.locator('.layout-draft-save').click()
    await draftBar.waitFor({ state: 'hidden', timeout: 60000 })
    let bufferSource = ''
    for (let attempt = 0; attempt < 80; attempt += 1) {
      const response = await fetch(
        `${BFF}/api/v1/project-sources/${projectId}?tenantId=tenant_local`,
      )
      if (response.ok) {
        const body = await response.json()
        bufferSource = body.files?.['src/App.jsx'] || ''
        if (!bufferSource.includes('vibe-direct-container')) break
      }
      await new Promise((resolve) => setTimeout(resolve, 250))
    }
    if (bufferSource.includes('vibe-direct-container')) {
      throw new Error(`buffered delete not persisted: ${bufferSource}`)
    }
    const noReloadMarker = await liveFrame
      .evaluate(() => window.__vibeNoReloadMarker || '')
      .catch(() => '')
    log('buffered delete', {
      persisted: true,
      noReload: noReloadMarker === 'buffer',
    })
    if (noReloadMarker !== 'buffer') {
      throw new Error('iframe was rebuilt after buffered delete (marker lost)')
    }

    const result = {
      projectId,
      mainOid: main.oid,
      paragraphOid: paragraph.oid,
      order: ['h1', 'button', 'p'],
      source,
      previewText: text,
      previewMs,
      sizeApplied: sizedSource.includes('h-[80px]'),
      addSized: addedSource.includes('w-[200px]'),
      freeApplied: freeSource.includes('position: "absolute"'),
      bufferDeleteNoReload: true,
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
