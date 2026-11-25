#!/usr/bin/env node
/**
 * 画布地基评测：新建一个 GIS 项目 → V0 → 冻结 → BACKEND_BINDING →
 * CodeGen → live 画布，然后在真实画布上跑：
 *   - 身份稳定（oid / node-id 唯一）；
 *   - 删除入缓冲 → 撤销；
 *   - 删除 → 提交修改 → 源码/预览一致；
 *   - 同父拖拽 → 提交修改 → 顺序与源码一致。
 *
 * 不导出代码包；评测对象是 live 画布与沙盒预览。
 *
 * 运行：
 *   NODE_PATH=/tmp/vibe-e2e/node_modules \
 *   CHROME_BIN=~/.cache/ms-playwright/chromium-1255/chrome-linux64/chrome \
 *   CANVAS_PROVIDER=deepseek node scripts/canvas_foundation_e2e.mjs
 */
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
const WEB = process.env.WEB_URL || 'http://127.0.0.1:5173'
const BFF = process.env.BFF_URL || 'http://127.0.0.1:8080'
const PY_AI = process.env.PY_AI_URL || 'http://127.0.0.1:8100'
const CHROME =
  process.env.CHROME_BIN ||
  path.join(
    os.homedir(),
    '.cache/ms-playwright/chromium-1254/chrome-linux64/chrome',
  )
const GOAL =
  process.env.CANVAS_GOAL ||
  '做一个 GIS 任务管理平台：地图总览、设施列表、巡检工单、登录；' +
    '要求 React + Vite + Node.js/Express + MySQL，可 Docker 部署。'
const PROVIDER = process.env.CANVAS_PROVIDER || 'auto'
const QUALITY_TIER = process.env.CANVAS_QUALITY_TIER || 'L2_STANDARD'
const ENTITY_VALUE =
  process.env.CANVAS_ENTITY_VALUE ||
  'Facility(id,name,type,status,lng,lat,address); ' +
    'InspectionTask(id,facilityId,assigneeId,title,status,dueDate); ' +
    'User(id,name,role)'
const FLOW_VALUE =
  process.env.CANVAS_FLOW_VALUE ||
  '登录 -> 地图总览 -> 筛选设施 -> 创建巡检工单 -> 更新状态'
const MIN_OIDS = Number(process.env.CANVAS_MIN_OIDS || 20)
const ACTIVE_BAR = '.tab-window.is-active [data-layout-draft-bar]'
const ACTIVE_SAVE = `${ACTIVE_BAR} .layout-draft-save`
const ACTIVE_UNDO = `${ACTIVE_BAR} .layout-draft-undo`

function log(...args) {
  console.log('[canvas-foundation-e2e]', ...args)
}

/** 默认只输出可定位字段；完整响应体仅在 LIVE_DEBUG=1 时打印。 */
function compactDirectResponse(response) {
  if (!response) return null
  let body = {}
  try {
    body = JSON.parse(response.body || '{}')
  } catch {
    body = {}
  }
  const summary = {
    httpStatus: response.status,
    action: body.action || null,
    result: body.status || null,
    changeId: body.changeId || null,
    errorCode: body.code || body.errorCode || null,
    error:
      body.diagnostic?.toolMessage ||
      body.userMessage ||
      body.message ||
      null,
    parentOid:
      body.resolvedTarget?.parent?.oid ||
      body.resolvedTarget?.identity?.parentOid ||
      null,
    commitHash: body.commitHash || null,
  }
  return process.env.LIVE_DEBUG === '1'
    ? { ...summary, body: response.body }
    : summary
}

function compactPlacement(placement) {
  if (!placement) return null
  const summary = {
    parentOid: placement.parentOid || '',
    parentFile: placement.parentFile || '',
    insertPosition: placement.insertPosition || '',
    siblingIndex: placement.siblingIndex,
  }
  return process.env.LIVE_DEBUG === '1'
    ? { ...summary, raw: placement }
    : summary
}

function fail(message) {
  throw new Error(message)
}

async function answerClarification(panel) {
  const form = panel.locator('.architect-clarification')
  await form.waitFor({ state: 'visible', timeout: 90000 })
  // 单选 / 多选取第一项，自由文本按问题语义填充。
  const groups = form.locator('fieldset.architect-question')
  const count = await groups.count()
  for (let index = 0; index < count; index += 1) {
    const group = groups.nth(index)
    const pills = group.locator('.choice-pills button')
    if ((await pills.count()) === 0) continue
    if ((await group.locator('.choice-pills button.is-selected').count()) === 0) {
      await pills.first().click()
    }
  }
  const areas = form.locator('label.architect-question:not(.architect-comment) textarea')
  const areaCount = await areas.count()
  for (let index = 0; index < areaCount; index += 1) {
    const area = areas.nth(index)
    const label = await area.locator('xpath=..').innerText()
    if (label.includes('数据实体')) {
      await area.fill(ENTITY_VALUE)
    } else if (label.includes('业务动作') || label.includes('交互')) {
      await area.fill(FLOW_VALUE)
    } else {
      await area.fill(GOAL)
    }
  }
  const submit = form.locator('button[type="submit"]')
  if ((await submit.count()) === 0) {
    log('clarification has no submit button; adopt suggestions')
    await form
      .getByRole('button', { name: /一键采用建议/ })
      .click()
    return
  }
  const submitText = await submit.innerText().catch(() => '')
  if (submitText.includes('提交中')) {
    // 同一表单的提交中状态：等它卸载，避免下一轮重复点击。
    await form.waitFor({ state: 'detached', timeout: 60000 }).catch(() => {})
    return
  }
  if (await submit.isDisabled().catch(() => false)) {
    log('clarification incomplete; adopt suggestions')
    await form
      .getByRole('button', { name: /一键采用建议/ })
      .click()
    return
  }
  await submit.click()
  await form.waitFor({ state: 'detached', timeout: 60000 }).catch(() => {})
}

async function driveV0(page, panel) {
  const deadline = Date.now() + 180000
  for (let round = 0; round < 10; round += 1) {
    if (page.url().includes('/visual-canvas')) return
    const outcome = panel.locator('.architect-outcome', {
      hasText: 'v0 虚拟原型已生成',
    })
    if (await outcome.count()) {
      // 契约已就绪，等待 WorkspacePage 自动跳转虚拟画布。
      await page.waitForURL(/\/visual-canvas/, { timeout: 60000 })
      return
    }
    if ((await panel.locator('.architect-outcome.is-error').count()) > 0) {
      fail(`V0 Architect FAILED: ${(await panel.innerText()).slice(-1200)}`)
    }
    // 刚重启容器后的首轮分析可能明显慢于常规，等待要覆盖冷启动。
    const form = panel.locator('.architect-clarification')
    const remaining = Math.max(5000, deadline - Date.now())
    const appeared = await form
      .waitFor({ state: 'visible', timeout: Math.min(60000, remaining) })
      .then(() => true)
      .catch(() => false)
    if (appeared) {
      log('answer V0 clarification round', round + 1)
      await answerClarification(panel)
      await page.waitForTimeout(1200)
      continue
    }
    if (page.url().includes('/visual-canvas')) return
  }
  const dump = await page.evaluate(() =>
    sessionStorage.getItem('vibe.architect.v1'),
  )
  fail(`V0 阶段超过 10 轮仍未产出虚拟画布: ${String(dump).slice(-1500)}`)
}

async function frameOf(locator) {
  const handle = await locator.elementHandle()
  if (!handle) fail('找不到 live iframe')
  const frame = await handle.contentFrame()
  if (!frame) fail('live iframe 尚未加载')
  return frame
}

async function tryDirectDelete(page, frame) {
  const target = await frame.evaluate(() => {
    const nodes = Array.from(document.querySelectorAll('[data-oid]'))
    const leaf = nodes.find((node) => {
      const rect = node.getBoundingClientRect()
      if (rect.width < 20 || rect.height < 10) return false
      if (node.getAttribute('data-source-kind') === 'container') return false
      // 优先编辑 CodeGen 的 VisualDocument 节点：页面切换器等 UI 是
      // map 模板，删除需要按作用域决策，不属于本用例的直连快路径。
      if (!node.hasAttribute('data-node-id')) return false
      return !node.querySelector('[data-oid]')
    })
    return leaf
      ? {
          oid: leaf.getAttribute('data-oid'),
          tag: leaf.tagName.toLowerCase(),
          text: (leaf.textContent || '').trim().slice(0, 30),
        }
      : null
  })
  if (!target) fail('live 画布没有可删除的源码节点')
  log('direct delete target', target)
  // Direct Ops 浮层只在 iframe 接管指针的浏览态出现。
  const interaction = page.locator('[data-canvas-interaction-toggle]')
  if (
    (await interaction.count()) > 0 &&
    (await interaction.getAttribute('aria-pressed')) !== 'true'
  ) {
    await interaction.click()
    await page.waitForTimeout(300)
  }
  const selector = `[data-oid="${target.oid}"]`
  await frame.hover(selector)
  await frame.waitForSelector('[data-vibe-direct-ui][title="删除组件"]', {
    state: 'visible',
    timeout: 5000,
  })
  await frame.click('[data-vibe-direct-ui][title="删除组件"]')
  const confirm = await frame.$('text=确认删除')
  if (confirm) {
    log('direct delete: confirm required')
    await confirm.click()
  }
  const response = await waitForDirectResponse()
  log('direct delete response', compactDirectResponse(response))
  const toast = await page
    .locator('.workspace-toast')
    .allInnerTexts()
    .catch(() => [])
  log('direct delete toast', toast)
  if (!response) fail('删除组件没有返回 Direct Ops 结果')
  if (response.status !== 200) {
    fail(`删除组件失败: HTTP ${response.status} ${response.body}`)
  }
  let body = null
  try {
    body = JSON.parse(response.body)
  } catch {
    // 保留原始日志
  }
  if (body?.action !== 'DELETE_NODE' || body?.status !== 'SUCCEEDED') {
    fail(`删除组件未成功: ${response.body}`)
  }
  // 等 HMR 把删除结果落到 iframe，再做下一次手势。
  await page.waitForTimeout(1500)
}

async function ensureDragArmed(page, active) {
  const toggle = active.locator('[data-canvas-drag-toggle]')
  if ((await toggle.count()) === 0) fail('live 画布缺少拖拽模式开关')
  // HMR 后 iframe 会重建，但父组件的 dragArmed 状态可能仍是 true，
  // 新 iframe 收不到 drag-mode=true。先 off 再 on，强制重发一次。
  if ((await toggle.getAttribute('aria-pressed')) === 'true') {
    await toggle.click()
    await page.waitForTimeout(250)
  }
  await toggle.click()
  await page.waitForTimeout(400)
}

async function trySameParentReorder(page, active) {
  await ensureDragArmed(page, active)
  const frame = await frameOf(active.locator('.canvas-live-frame'))
  const geometry = await frame.evaluate(() => {
    const containers = Array.from(
      document.querySelectorAll('[data-oid][data-source-kind="container"]'),
    ).filter((node) => node.hasAttribute('data-node-id'))
    const parent = containers.find(
      (node) =>
        Array.from(node.children || []).filter(
          (child) => child.hasAttribute?.('data-oid'),
        ).length >= 2,
    )
    if (!parent) return null
    const children = Array.from(parent.children || []).filter((child) =>
      child.hasAttribute?.('data-oid'),
    )
    const source = children[children.length - 1]
    const target = children[0]
    const sourceRect = source.getBoundingClientRect()
    const targetRect = target.getBoundingClientRect()
    return {
      parentOid: parent.getAttribute('data-oid'),
      source: {
        oid: source.getAttribute('data-oid'),
        x: sourceRect.left + sourceRect.width / 2,
        y: sourceRect.top + Math.min(24, sourceRect.height / 2),
      },
      target: {
        oid: target.getAttribute('data-oid'),
        x: targetRect.left + Math.min(24, targetRect.width / 2),
        y: targetRect.top + 4,
      },
    }
  })
  if (!geometry) fail('没有可测试同父容器重排的兄弟节点')
  log('same-parent reorder geometry', geometry)
  await page.evaluate(() => {
    window.__vibeDragEvents = []
    window.addEventListener('message', (event) => {
      const data = event.data
      if (data && data.channel === 'vibe-inspect') {
        window.__vibeDragEvents.push(data)
      }
    })
  })
  await frame.evaluate(({ source, target }) => {
    const sourceNode = document.querySelector(`[data-oid="${source.oid}"]`)
    if (!sourceNode) return
    const dispatch = (node, type, x, y) => {
      node.dispatchEvent(
        new PointerEvent(type, {
          bubbles: true,
          cancelable: true,
          pointerId: 1,
          pointerType: 'mouse',
          button: 0,
          buttons: type === 'pointerup' ? 0 : 1,
          clientX: x,
          clientY: y,
        }),
      )
    }
    dispatch(sourceNode, 'pointerdown', source.x, source.y)
    for (let step = 1; step <= 8; step += 1) {
      dispatch(
        document,
        'pointermove',
        source.x + ((target.x - source.x) * step) / 8,
        source.y + ((target.y - source.y) * step) / 8,
      )
    }
    dispatch(document, 'pointerup', target.x, target.y)
  }, geometry)
  const response = await waitForDirectResponse(20000)
  const dragEvents = await page.evaluate(() =>
    (window.__vibeDragEvents || [])
      .filter((item) => item.type === 'drag-end')
      .map((item) => item.payload?.placement),
  )
  log('same-parent reorder drag placement', dragEvents.map(compactPlacement))
  log('same-parent reorder response', {
    status: response?.status,
    action: response?.body ? JSON.parse(response.body).action : null,
    parent: response?.body
      ? JSON.parse(response.body).resolvedTarget?.parent
      : null,
    summary: response?.body
      ? JSON.parse(response.body).resolvedTarget?.identity?.file
      : null,
  })
  if (!response || response.status !== 200) {
    fail(`同父容器拖拽失败: ${response?.body || '无响应'}`)
  }
  let body = {}
  try {
    body = JSON.parse(response.body)
  } catch {
    // 保留原始日志
  }
  if (body.status !== 'SUCCEEDED' || body.action !== 'MOVE_NODE') {
    fail(`同父容器拖拽未成功: ${response.body}`)
  }
  const deadline = Date.now() + 15000
  let order = []
  while (Date.now() < deadline) {
    try {
      const live = await frameOf(active.locator('.canvas-live-frame'))
      order = await live.evaluate(({ parentOid }) => {
        const parent = document.querySelector(`[data-oid="${parentOid}"]`)
        if (!parent) return []
        return Array.from(parent.children || [])
          .filter((child) => child.hasAttribute?.('data-oid'))
          .map((child) => child.getAttribute('data-oid'))
      }, geometry)
    } catch {
      order = []
    }
    if (order[0] === geometry.source.oid) break
    await new Promise((resolve) => setTimeout(resolve, 300))
  }
  log('same-parent order after HMR', order)
  if (order[0] !== geometry.source.oid) {
    fail(
      `同父容器重排未按鼠标位置生效：期望首位 ${geometry.source.oid}，实际 ${order.join(',')}`,
    )
  }
}

async function tryDirectDrag(page, active) {
  await ensureDragArmed(page, active)
  // 删除成功后会触发 HMR / iframe 重载，必须重新获取 Frame。
  const frame = await frameOf(active.locator('.canvas-live-frame'))
  const geometry = await frame.evaluate(() => {
    const nodes = Array.from(document.querySelectorAll('[data-oid]'))
    const visible = nodes.filter((node) => {
      const rect = node.getBoundingClientRect()
      return rect.width > 40 && rect.height > 20
    })
    const containers = visible.filter(
      (node) => node.getAttribute('data-source-kind') === 'container',
    )
    const isRoot = (node) =>
      node === document.body ||
      ['MAIN', 'BODY'].includes(node.tagName) ||
      node.parentElement === document.body
    // 只选 VisualDocument 节点；页面切换器等模板 UI 不参与直连拖拽。
    const visualContainers = containers.filter(
      (node) => !isRoot(node) && node.hasAttribute('data-node-id'),
    )
    const source =
      visualContainers.find((node) =>
        (node.textContent || '').includes('SubmitForm'),
      ) || visualContainers[0]
    const target =
      visualContainers.find(
        (node) =>
          node !== source &&
          !node.contains(source) &&
          !source?.contains(node),
      ) || visualContainers.find((node) => node !== source)
    if (!source || !target) return null
    const rectOf = (node) => node.getBoundingClientRect()
    const sourceRect = rectOf(source)
    const targetRect = rectOf(target)
    return {
      source: {
        oid: source.getAttribute('data-oid'),
        tag: source.tagName.toLowerCase(),
        x: sourceRect.left + sourceRect.width / 2,
        y: sourceRect.top + 10,
      },
      target: {
        oid: target.getAttribute('data-oid'),
        tag: target.tagName.toLowerCase(),
        // 落在兄弟容器中央 → 嵌套；边缘落点会按同父排序处理。
        x: targetRect.left + targetRect.width / 2,
        y: targetRect.top + targetRect.height / 2,
      },
    }
  })
  if (!geometry) fail('live 画布没有可跨父容器拖拽的候选节点')
  log('direct drag geometry', JSON.stringify(geometry, null, 2))
  const tree = await frame.evaluate(() => {
    const lines = []
    const walk = (node, depth) => {
      if (node.nodeType !== 1) return
      const oid = node.getAttribute?.('data-oid')
      if (oid) {
        lines.push(
          `${'  '.repeat(depth)}${node.tagName.toLowerCase()} ${oid} ` +
            `kind=${node.getAttribute('data-source-kind')} ` +
            `text=${(node.textContent || '').trim().slice(0, 20)}`,
        )
      }
      Array.from(node.children || []).forEach((child) =>
        walk(child, depth + (oid ? 1 : 0)),
      )
    }
    walk(document.body, 0)
    return lines.join('\n')
  })
  log('live DOM tree\n' + tree)
  await page.evaluate(() => {
    window.__vibeDragEvents = []
    window.addEventListener('message', (event) => {
      const data = event.data
      if (data && data.channel === 'vibe-inspect') {
        window.__vibeDragEvents.push(data)
      }
    })
  })
  await frame.evaluate(({ source, target }) => {
    const sourceNode = document.querySelector(`[data-oid="${source.oid}"]`)
    const targetNode = document.querySelector(`[data-oid="${target.oid}"]`)
    if (!sourceNode || !targetNode) return
    const dispatch = (node, type, x, y) => {
      node.dispatchEvent(
        new PointerEvent(type, {
          bubbles: true,
          cancelable: true,
          pointerId: 1,
          pointerType: 'mouse',
          button: 0,
          buttons: type === 'pointerup' ? 0 : 1,
          clientX: x,
          clientY: y,
        }),
      )
    }
    dispatch(sourceNode, 'pointerdown', source.x, source.y)
    for (let step = 1; step <= 8; step += 1) {
      const x = source.x + ((target.x - source.x) * step) / 8
      const y = source.y + ((target.y - source.y) * step) / 8
      dispatch(document, 'pointermove', x, y)
    }
    dispatch(document, 'pointerup', target.x, target.y)
  }, geometry)
  await page.waitForTimeout(1200)
  const dragEvents = await page.evaluate(() =>
    (window.__vibeDragEvents || []).map((item) => ({
      type: item.type,
      placement: item.payload?.placement,
      sourceOid: item.payload?.source?.oid,
      rect: item.payload?.rect,
    })),
  )
  log(
    'drag events',
    dragEvents.slice(-6).map((item) => ({
      type: item.type,
      sourceOid: item.sourceOid,
      placement: compactPlacement(item.placement),
      rect: process.env.LIVE_DEBUG === '1' ? item.rect : undefined,
    })),
  )
  const response = await waitForDirectResponse(20000)
  log('direct drag response', compactDirectResponse(response))
  const toast = await page
    .locator('.workspace-toast')
    .allInnerTexts()
    .catch(() => [])
  log('direct drag toast', toast)
  const dragEnd = [...dragEvents].reverse().find((item) => item.type === 'drag-end')
  if (!dragEnd?.placement) {
    fail(`跨父容器拖拽没有算出落点: ${JSON.stringify(dragEnd)}`)
  }
  if (!response) fail('跨父容器拖拽没有返回 Direct Ops 结果')
  if (response.status !== 200) {
    fail(`跨父容器拖拽失败: HTTP ${response.status} ${response.body}`)
  }
  let body = null
  try {
    body = JSON.parse(response.body)
  } catch {
    // 保留原始日志
  }
  if (body?.action !== 'MOVE_NODE' || body?.status !== 'SUCCEEDED') {
    fail(`跨父容器拖拽未成功: ${response.body}`)
  }
  if (toast.some((text) => text.includes('没有找到可落位的父容器'))) {
    fail('跨父容器拖拽仍然提示“没有找到可落位的父容器”')
  }
  // 源码写回成功后必须等到 HMR 真正把节点换到新父容器；否则就是
  // 用户看到的“提示成功但视觉回退”。
  const afterParent = await waitForParentAfterHmr(
    active,
    geometry.source.oid,
    geometry.target.oid,
  )
  log('drag after HMR', {
    sourceOid: geometry.source.oid,
    expectedParent: geometry.target.oid,
    actualParent: afterParent,
  })
  if (afterParent !== geometry.target.oid) {
    await page.screenshot({
      path: '/tmp/vibe-drag-after-hmr.png',
      fullPage: true,
    })
    fail(
      `拖拽已写回源码但 iframe 未完成 HMR：实际父容器 ${afterParent}，期望 ${geometry.target.oid}`,
    )
  }
}

let lastDirectRequest = null

async function tryAgentComponentEdit(page, active) {
  const context = lastDirectRequest || {}
  const projectId = context.projectId || context.project_id
  const sandboxId = context.sandboxId || context.sandbox_id
  if (!projectId || !sandboxId) {
    fail('没有捕获到 Direct Ops 沙盒上下文，无法模拟前端工程师组件')
  }
  const agentSource = `export default function AgentCard() {
  return (
    <div data-agent-card style={{ margin: 16, padding: 16, border: '1px solid #cbd5e1', borderRadius: 12 }}>
      <h3>前端工程师组件</h3>
      <button type="button">组件按钮</button>
    </div>
  )
}
`
  const mainSource = `import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import AgentCard from './AgentCard.jsx'
import './visual.css'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
    <AgentCard />
  </React.StrictMode>,
)
`
  const patchResponse = await fetch(
    `${PY_AI}/api/v1/internal/sandbox/apply-patch`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        sandbox_id: sandboxId,
        files: {
          'src/AgentCard.jsx': agentSource,
          'src/main.jsx': mainSource,
        },
      }),
    },
  )
  if (!patchResponse.ok) {
    fail(`写入前端工程师组件失败: ${await patchResponse.text()}`)
  }

  const deadline = Date.now() + 20000
  let info = null
  while (Date.now() < deadline) {
    let frame = null
    try {
      frame = await frameOf(active.locator('.canvas-live-frame'))
    } catch {
      // HMR 重载中
    }
    if (frame) {
      info = await frame
        .evaluate(() => {
          const read = (node) => {
            if (!node) return null
            const oid = node.getAttribute('data-oid') || ''
            return {
              oid,
              nodeId: node.getAttribute('data-node-id') || '',
              file: node.getAttribute('data-source-file') || '',
              line: Number(node.getAttribute('data-source-line') || 0),
              column: Number(node.getAttribute('data-source-column') || 0),
              text: (node.textContent || '').trim().slice(0, 40),
            }
          }
          const host = document.querySelector('[data-agent-card]')
          return host
            ? { host: read(host), heading: read(host.querySelector('h3')) }
            : null
        })
        .catch(() => null)
      if (info?.heading?.oid) break
    }
    await new Promise((resolve) => setTimeout(resolve, 300))
  }
  if (!info?.heading?.oid) {
    fail('前端工程师新增组件未被源码插件标注（无 data-oid）')
  }
  if (info.heading.file !== 'src/AgentCard.jsx') {
    fail(`新增组件锚点文件错误: ${JSON.stringify(info.heading)}`)
  }
  log('agent component anchors', JSON.stringify(info, null, 2))

  const revisionResponse = await fetch(
    `${BFF}/api/v1/direct-ops/revision?projectId=${encodeURIComponent(
      projectId,
    )}&sandboxId=${encodeURIComponent(sandboxId)}`,
  )
  if (!revisionResponse.ok) {
    fail(`读取源码 revision 失败: ${await revisionResponse.text()}`)
  }
  const { revision } = await revisionResponse.json()
  const actionId = `dact_agent_edit_${Date.now()}`
  const actionResponse = await fetch(
    `${BFF}/api/v1/direct-ops/actions`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        directActionId: actionId,
        idempotencyKey: `${projectId}:${actionId}`,
        projectId,
        sandboxId,
        tenantId: 'tenant_local',
        baseRevision: revision,
        action: 'UPDATE_TEXT',
        target: {
          anchorId: 'anc_agent_component',
          oid: info.heading.oid,
          selector: `[data-oid="${info.heading.oid}"]`,
          file: info.heading.file,
          line: info.heading.line,
          column: info.heading.column,
        },
        payload: { text: '前端工程师组件-已编辑' },
        options: { source: 'FLOATING_TOOLBAR', dryRun: false },
      }),
    },
  )
  const actionBody = await actionResponse.json().catch(() => ({}))
  if (!actionResponse.ok || actionBody.status !== 'SUCCEEDED') {
    fail(`编辑前端工程师组件失败: ${JSON.stringify(actionBody)}`)
  }
  const textDeadline = Date.now() + 15000
  let text = ''
  while (Date.now() < textDeadline) {
    try {
      const frame = await frameOf(active.locator('.canvas-live-frame'))
      text = await frame
        .evaluate(
          () =>
            document
              .querySelector('[data-agent-card] h3')
              ?.textContent?.trim() || '',
        )
        .catch(() => '')
    } catch {
      text = ''
    }
    if (text.includes('已编辑')) break
    await new Promise((resolve) => setTimeout(resolve, 300))
  }
  if (!text.includes('已编辑')) {
    fail(`前端工程师组件编辑未通过 HMR 生效: ${text}`)
  }
  log('agent component edit', { file: info.heading.file, text })
}

async function tryGridReorder(page, active) {
  const context = lastDirectRequest || {}
  const projectId = context.projectId || context.project_id
  const sandboxId = context.sandboxId || context.sandbox_id
  const gridSource = `export default function GridCards() {
  return (
    <div
      data-grid-cards
      style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', gap: 12, margin: 16 }}
    >
      <div data-card="1" style={{ padding: 16, background: '#fff' }}>卡片一</div>
      <div data-card="2" style={{ padding: 16, background: '#fff' }}>卡片二</div>
      <div data-card="3" style={{ padding: 16, background: '#fff' }}>卡片三</div>
    </div>
  )
}
`
  const mainSource = `import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import AgentCard from './AgentCard.jsx'
import GridCards from './GridCards.jsx'
import './visual.css'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
    <AgentCard />
    <GridCards />
  </React.StrictMode>,
)
`
  const patchResponse = await fetch(
    `${PY_AI}/api/v1/internal/sandbox/apply-patch`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        sandbox_id: sandboxId,
        files: {
          'src/GridCards.jsx': gridSource,
          'src/main.jsx': mainSource,
        },
      }),
    },
  )
  if (!patchResponse.ok) {
    fail(`写入 grid 测试组件失败: ${await patchResponse.text()}`)
  }
  const deadline = Date.now() + 20000
  let geometry = null
  while (Date.now() < deadline) {
    let frame = null
    try {
      frame = await frameOf(active.locator('.canvas-live-frame'))
    } catch {
      // HMR 重载中
    }
    if (frame) {
      geometry = await frame
        .evaluate(() => {
          const grid = document.querySelector('[data-grid-cards]')
          if (!grid) return null
          const cards = Array.from(grid.querySelectorAll('[data-card]'))
          if (cards.length < 3) return null
          const rectOf = (node) => node.getBoundingClientRect()
          const source = cards[2]
          const target = cards[0]
          const sourceRect = rectOf(source)
          const targetRect = rectOf(target)
          return {
            gridOid: grid.getAttribute('data-oid') || '',
            source: {
              oid: source.getAttribute('data-oid') || '',
              x: sourceRect.left + sourceRect.width / 2,
              y: sourceRect.top + sourceRect.height / 2,
            },
            target: {
              oid: target.getAttribute('data-oid') || '',
              // 第一张卡片的左边缘：同一行内的“排序缝隙”。
              x: targetRect.left + 2,
              y: targetRect.top + targetRect.height / 2,
            },
          }
        })
        .catch(() => null)
      if (geometry?.source.oid && geometry?.target.oid) break
    }
    await new Promise((resolve) => setTimeout(resolve, 300))
  }
  if (!geometry?.source.oid) fail('grid 测试组件未被源码插件标注')
  log('grid reorder geometry', geometry)
  await ensureDragArmed(page, active)
  await page.evaluate(() => {
    window.__vibeDragEvents = []
    window.addEventListener('message', (event) => {
      const data = event.data
      if (data && data.channel === 'vibe-inspect') {
        window.__vibeDragEvents.push(data)
      }
    })
  })
  const frame = await frameOf(active.locator('.canvas-live-frame'))
  await frame.evaluate(({ source, target }) => {
    const sourceNode = document.querySelector(`[data-oid="${source.oid}"]`)
    if (!sourceNode) return
    const dispatch = (node, type, x, y) => {
      node.dispatchEvent(
        new PointerEvent(type, {
          bubbles: true,
          cancelable: true,
          pointerId: 1,
          pointerType: 'mouse',
          button: 0,
          buttons: type === 'pointerup' ? 0 : 1,
          clientX: x,
          clientY: y,
        }),
      )
    }
    dispatch(sourceNode, 'pointerdown', source.x, source.y)
    for (let step = 1; step <= 8; step += 1) {
      dispatch(
        document,
        'pointermove',
        source.x + ((target.x - source.x) * step) / 8,
        source.y + ((target.y - source.y) * step) / 8,
      )
    }
    dispatch(document, 'pointerup', target.x, target.y)
  }, geometry)
  const response = await waitForDirectResponse(20000)
  const gridPlacement = await page.evaluate(() =>
    (window.__vibeDragEvents || [])
      .filter((item) => item.type === 'drag-end')
      .map((item) => item.payload?.placement),
  )
  log('grid drag placement', gridPlacement.map(compactPlacement))
  if (!response || response.status !== 200) {
    let detail = {}
    try {
      detail = JSON.parse(response?.body || '{}')
    } catch {
      detail = {}
    }
    log('grid drag failure detail', {
      identity: detail.resolvedTarget?.identity,
      parent: detail.resolvedTarget?.parent,
    })
    fail(`grid 拖拽失败: ${response?.body || '无响应'}`)
  }
  let body = {}
  try {
    body = JSON.parse(response.body)
  } catch {
    // 保留原始日志
  }
  if (body.status !== 'SUCCEEDED' || body.action !== 'MOVE_NODE') {
    fail(`grid 拖拽未成功: ${response.body}`)
  }
  const orderDeadline = Date.now() + 15000
  let order = []
  while (Date.now() < orderDeadline) {
    try {
      const live = await frameOf(active.locator('.canvas-live-frame'))
      order = await live.evaluate(() => {
        const grid = document.querySelector('[data-grid-cards]')
        if (!grid) return []
        return Array.from(grid.querySelectorAll('[data-card]')).map((node) =>
          node.getAttribute('data-oid'),
        )
      })
    } catch {
      order = []
    }
    if (order[0] === geometry.source.oid) break
    await new Promise((resolve) => setTimeout(resolve, 300))
  }
  log('grid order after HMR', order)
  if (order[0] !== geometry.source.oid) {
    fail(
      `grid 内重排未按鼠标位置生效：期望首位 ${geometry.source.oid}，实际 ${order.join(',')}`,
    )
  }
}

async function tryRepeatReorder(page, active) {
  const context = lastDirectRequest || {}
  const sandboxId = context.sandboxId || context.sandbox_id
  const repeatSource = `const ITEMS = ['甲', '乙', '丙']

export default function RepeatList() {
  return (
    <ul
      data-repeat-demo
      style={{ display: 'flex', gap: 8, margin: 16, padding: 0, listStyle: 'none' }}
    >
      {ITEMS.map((label, index) => (
        <li
          key={label}
          data-repeat-template="oid_repeat_demo"
          data-repeat-index={index}
          style={{ padding: '8px 12px', background: '#fff', borderRadius: 4 }}
        >
          {label}
        </li>
      ))}
    </ul>
  )
}
`
  const mainSource = `import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import AgentCard from './AgentCard.jsx'
import GridCards from './GridCards.jsx'
import RepeatList from './RepeatList.jsx'
import './visual.css'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
    <AgentCard />
    <GridCards />
    <RepeatList />
  </React.StrictMode>,
)
`
  const patchResponse = await fetch(
    `${PY_AI}/api/v1/internal/sandbox/apply-patch`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        sandbox_id: sandboxId,
        files: {
          'src/RepeatList.jsx': repeatSource,
          'src/main.jsx': mainSource,
        },
      }),
    },
  )
  if (!patchResponse.ok) {
    fail(`写入 repeat 测试组件失败: ${await patchResponse.text()}`)
  }
  const deadline = Date.now() + 20000
  let geometry = null
  while (Date.now() < deadline) {
    let frame = null
    try {
      frame = await frameOf(active.locator('.canvas-live-frame'))
    } catch {
      // HMR 重载中
    }
    if (frame) {
      geometry = await frame
        .evaluate(() => {
          const list = document.querySelector('[data-repeat-demo]')
          if (!list) return null
          const items = Array.from(
            list.querySelectorAll('[data-repeat-template]'),
          )
          if (items.length < 3) return null
          const rectOf = (node) => node.getBoundingClientRect()
          const sourceRect = rectOf(items[2])
          const targetRect = rectOf(items[0])
          return {
            sourceText: (items[2].textContent || '').trim(),
            targetText: (items[0].textContent || '').trim(),
            source: {
              x: sourceRect.left + sourceRect.width / 2,
              y: sourceRect.top + sourceRect.height / 2,
            },
            target: {
              x: targetRect.left + 2,
              y: targetRect.top + targetRect.height / 2,
            },
          }
        })
        .catch(() => null)
      if (geometry) break
    }
    await new Promise((resolve) => setTimeout(resolve, 300))
  }
  if (!geometry) fail('repeat 测试组件未渲染')
  log('repeat reorder geometry', geometry)
  await ensureDragArmed(page, active)
  const frame = await frameOf(active.locator('.canvas-live-frame'))
  await frame.evaluate(({ source, target }) => {
    const list = document.querySelector('[data-repeat-demo]')
    const items = Array.from(list?.querySelectorAll('[data-repeat-template]') || [])
    const sourceNode = items[2]
    if (!sourceNode) return
    const dispatch = (node, type, x, y) => {
      node.dispatchEvent(
        new PointerEvent(type, {
          bubbles: true,
          cancelable: true,
          pointerId: 1,
          pointerType: 'mouse',
          button: 0,
          buttons: type === 'pointerup' ? 0 : 1,
          clientX: x,
          clientY: y,
        }),
      )
    }
    dispatch(sourceNode, 'pointerdown', source.x, source.y)
    for (let step = 1; step <= 8; step += 1) {
      dispatch(
        document,
        'pointermove',
        source.x + ((target.x - source.x) * step) / 8,
        source.y + ((target.y - source.y) * step) / 8,
      )
    }
    dispatch(document, 'pointerup', target.x, target.y)
  }, geometry)
  const response = await waitForDirectResponse(20000)
  if (!response || response.status !== 200) {
    fail(`repeat 拖拽失败: ${response?.body || '无响应'}`)
  }
  let body = {}
  try {
    body = JSON.parse(response.body)
  } catch {
    // 保留原始日志
  }
  if (body.status !== 'SUCCEEDED' || body.action !== 'MOVE_DATA_ITEM') {
    fail(`repeat 拖拽没有走 MOVE_DATA_ITEM: ${response.body}`)
  }
  const orderDeadline = Date.now() + 15000
  let labels = []
  while (Date.now() < orderDeadline) {
    try {
      const live = await frameOf(active.locator('.canvas-live-frame'))
      labels = await live.evaluate(() => {
        const list = document.querySelector('[data-repeat-demo]')
        return Array.from(
          list?.querySelectorAll('[data-repeat-template]') || [],
        ).map((node) => (node.textContent || '').trim())
      })
    } catch {
      labels = []
    }
    if (labels[0] === geometry.sourceText) break
    await new Promise((resolve) => setTimeout(resolve, 300))
  }
  log('repeat order after HMR', labels)
  if (labels[0] !== geometry.sourceText) {
    fail(
      `repeat 数据项重排未生效：期望首位 ${geometry.sourceText}，实际 ${labels.join(',')}`,
    )
  }
}

async function waitForParentAfterHmr(
  active,
  oid,
  expectedParent,
  timeout = 15000,
) {
  const deadline = Date.now() + timeout
  let last = ''
  while (Date.now() < deadline) {
    let frame = null
    try {
      frame = await frameOf(active.locator('.canvas-live-frame'))
    } catch {
      // HMR 可能正在整页重建 iframe，下一轮重新获取。
    }
    if (frame) {
      last = await frame
        .evaluate((sourceOid) => {
          const node = document.querySelector(`[data-oid="${sourceOid}"]`)
          if (!node) return 'MISSING'
          const parent = node.parentElement?.closest('[data-oid]')
          return parent?.getAttribute('data-oid') || ''
        }, oid)
        .catch(() => 'DETACHED')
      if (last === expectedParent) return last
    }
    await new Promise((resolve) => setTimeout(resolve, 200))
  }
  return last
}

let directResponses = []
function waitForDirectResponse(timeout = 12000) {
  const deadline = Date.now() + timeout
  return (async () => {
    while (Date.now() < deadline) {
      if (directResponses.length) return directResponses.shift()
      await new Promise((resolve) => setTimeout(resolve, 200))
    }
    return null
  })()
}

async function diagnoseLiveCanvas(page, active) {
  const frame = await frameOf(active.locator('.canvas-live-frame'))
  const stats = await frame.evaluate(async () => {
    const pick = (selector) => document.querySelectorAll(selector).length
    const nodes = Array.from(document.querySelectorAll('[data-oid]'))
    let map = null
    let mapError = ''
    try {
      const response = await fetch('/vibe-source-map.json', {
        cache: 'no-store',
      })
      map = await response.json()
    } catch (error) {
      mapError = String(error)
    }
    const panel = document.querySelector('[data-fullstack-panel]')
    let panelModule = ''
    try {
      const response = await fetch('/src/fullstack-api.jsx', {
        cache: 'no-store',
      })
      panelModule = await response.text()
    } catch (error) {
      panelModule = String(error)
    }
    return {
      mapFiles: Object.keys(map?.files || {}),
      panelModuleHasOid: panelModule.includes('data-oid'),
      panelModuleSnippet: panelModule.slice(0, 200),
      counts: {
        oid: nodes.length,
        nodeId: pick('[data-node-id]'),
        sourceFile: pick('[data-source-file]'),
        containers: pick('[data-source-kind="container"]'),
      },
      panel: panel
        ? {
            tag: panel.tagName.toLowerCase(),
            oid: panel.getAttribute('data-oid'),
            file: panel.getAttribute('data-source-file'),
            source: panel.getAttribute('data-source'),
            kind: panel.getAttribute('data-source-kind'),
            innerOids: panel.querySelectorAll('[data-oid]').length,
            innerElements: panel.querySelectorAll('*').length,
            outer: panel.outerHTML.slice(0, 300),
          }
        : null,
      sample: nodes.slice(0, 6).map((node) => ({
        tag: node.tagName.toLowerCase(),
        oid: node.getAttribute('data-oid'),
        nodeId: node.getAttribute('data-node-id'),
        file: node.getAttribute('data-source-file'),
        source: node.getAttribute('data-source'),
        kind: node.getAttribute('data-source-kind'),
        text: (node.textContent || '').trim().slice(0, 24),
      })),
      mapNodes: Object.keys(map?.nodes || {}).length,
      mapError,
      bodyBackground: getComputedStyle(document.body).backgroundColor,
      bodyHeight: document.body.scrollHeight,
    }
  })
  log('live canvas stats', JSON.stringify(stats, null, 2))
  const parentStats = await page.evaluate(() => ({
    visualNodes: document.querySelectorAll('.vc-node').length,
    liveFrames: document.querySelectorAll('.canvas-live-frame').length,
    stages: document.querySelectorAll('.canvas-stage').length,
    stageRects: Array.from(
      document.querySelectorAll('.canvas-stage, .vc-canvas, .visual-canvas'),
    ).map((node) => ({
      className: node.className,
      rect: node.getBoundingClientRect().toJSON(),
    })),
  }))
  log('parent canvas stats', JSON.stringify(parentStats, null, 2))
  await page.screenshot({
    path: '/tmp/vibe-live-canvas-diagnose.png',
    fullPage: true,
  })
  log('screenshot saved: /tmp/vibe-live-canvas-diagnose.png')
  return frame
}

async function runCanvasCase(results, name, fn) {
  const started = Date.now()
  try {
    await fn()
    results.push({ name, ok: true, durationMs: Date.now() - started })
    log('canvas case PASS', name)
  } catch (error) {
    const message = String(error?.message || error).slice(0, 800)
    results.push({
      name,
      ok: false,
      durationMs: Date.now() - started,
      error: message,
    })
    log('canvas case FAIL', name, message)
  }
}

function writeCanvasReport(report) {
  const outDir = path.join(ROOT, 'reports', 'canvas')
  fs.mkdirSync(outDir, { recursive: true })
  const stamp = new Date().toISOString().replace(/[:.]/g, '-')
  const jsonPath = path.join(outDir, `canvas-foundation-${stamp}.json`)
  const mdPath = path.join(outDir, `canvas-foundation-${stamp}.md`)
  fs.writeFileSync(jsonPath, JSON.stringify(report, null, 2), 'utf8')
  const lines = [
    '# 画布地基评测',
    '',
    `- 目标：${report.goal}`,
    `- Provider：${report.provider}`,
    `- 预览：${report.previewUrl}`,
    `- 结果：**${report.ok ? 'PASS' : 'PARTIAL'}**`,
    '',
    '| 用例 | 结果 | 耗时 | 错误 |',
    '|---|---|---:|---|',
    ...report.cases.map(
      (item) =>
        `| ${item.name} | ${item.ok ? '✅' : '❌'} | ${item.durationMs}ms | ${
          item.error || ''
        } |`,
    ),
    '',
    `页面错误：${report.pageErrors.length}`,
    '',
  ]
  fs.writeFileSync(mdPath, lines.join('\n'), 'utf8')
  if (process.env.CANVAS_BASELINE === '1') {
    fs.copyFileSync(jsonPath, path.join(outDir, 'baseline.json'))
  }
  return { jsonPath, mdPath }
}

async function waitUntil(check, timeout, label) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    if (await check()) return
    await new Promise((resolve) => setTimeout(resolve, 250))
  }
  throw new Error(`等待超时: ${label}`)
}

async function ensureInteraction(page) {
  const toggle = page.locator('[data-canvas-interaction-toggle]')
  if ((await toggle.count()) === 0) return
  if ((await toggle.getAttribute('aria-pressed')) !== 'true') {
    await toggle.click()
    await page.waitForTimeout(300)
  }
}

async function findLeaf(frame, exclude = new Set()) {
  return frame.evaluate((excluded) => {
    const nodes = Array.from(document.querySelectorAll('[data-oid]'))
    const leaf = nodes.find((node) => {
      const oid = node.getAttribute('data-oid')
      if (!oid || excluded.includes(oid)) return false
      const rect = node.getBoundingClientRect()
      if (rect.width < 20 || rect.height < 10) return false
      if (node.getAttribute('data-source-kind') === 'container') return false
      if (!node.hasAttribute('data-node-id')) return false
      return !node.querySelector('[data-oid]')
    })
    return leaf
      ? {
          oid: leaf.getAttribute('data-oid'),
          tag: leaf.tagName.toLowerCase(),
          text: (leaf.textContent || '').trim().slice(0, 40),
        }
      : null
  }, Array.from(exclude))
}

async function isNodeHidden(frame, oid) {
  return frame.evaluate((target) => {
    const node = document.querySelector(`[data-oid="${target}"]`)
    if (!node) return true
    const style = getComputedStyle(node)
    return (
      style.display === 'none' ||
      style.visibility === 'hidden' ||
      node.getClientRects().length === 0
    )
  }, oid)
}

async function waitBufferText(page, expected, timeout = 30000) {
  const save = page.locator(ACTIVE_SAVE)
  await save.waitFor({ state: 'visible', timeout })
  const deadline = Date.now() + timeout
  let text = ''
  while (Date.now() < deadline) {
    text = await save.innerText().catch(() => '')
    if (text.includes(expected)) return text
    await new Promise((resolve) => setTimeout(resolve, 250))
  }
  const bars = await page
    .locator(ACTIVE_BAR)
    .allInnerTexts()
    .catch(() => [])
  throw new Error(
    `缓冲按钮未出现「${expected}」，当前="${text}"，bars=${JSON.stringify(bars)}`,
  )
}

async function waitBarGone(page, timeout = 30000) {
  await waitUntil(
    async () => (await page.locator(ACTIVE_BAR).count()) === 0,
    timeout,
    '草稿条未清空',
  )
}

async function clickDelete(page, frame, oid) {
  const selector = `[data-oid="${oid}"]`
  await frame.hover(selector)
  await frame.waitForSelector('[data-vibe-direct-ui][title="删除组件"]', {
    state: 'visible',
    timeout: 8000,
  })
  await frame.click('[data-vibe-direct-ui][title="删除组件"]')
  const confirm = await frame.$('text=确认删除')
  if (confirm) await confirm.click()
  await page.waitForTimeout(400)
}

async function caseIdentity(page, active, phase) {
  const frame = await frameOf(active.locator('.canvas-live-frame'))
  const data = await frame.evaluate(() => ({
    oids: Array.from(document.querySelectorAll('[data-oid]'))
      .map((node) => node.getAttribute('data-oid'))
      .filter(Boolean),
    nodeIds: Array.from(document.querySelectorAll('[data-node-id]'))
      .map((node) => node.getAttribute('data-node-id'))
      .filter(Boolean),
  }))
  const uniqueOids = new Set(data.oids)
  const uniqueNodeIds = new Set(data.nodeIds)
  // 同一源码节点在页面中渲染多次（循环 / 多实例）时，raw data-oid 会重复；
  // 这是身份模型的正常形态，不作为失败，只记录为指标。
  const duplicateOids = data.oids.length - uniqueOids.size
  const duplicateNodeIds = data.nodeIds.length - uniqueNodeIds.size
  if (uniqueOids.size < MIN_OIDS) {
    throw new Error(`可定位 oid 过少: ${uniqueOids.size} < ${MIN_OIDS}`)
  }
  if (phase === 'baseline') {
    await page.evaluate(
      (payload) => {
        window.__canvasIdentityBaseline = payload
      },
      { oids: [...uniqueOids], nodeIds: [...uniqueNodeIds] },
    )
    log('identity baseline', {
      uniqueOids: uniqueOids.size,
      uniqueNodeIds: uniqueNodeIds.size,
      duplicateOids,
      duplicateNodeIds,
    })
    return
  }
  const baseline = await page.evaluate(
    () => window.__canvasIdentityBaseline || null,
  )
  if (!baseline) throw new Error('缺少 identity baseline')
  const baselineOids = new Set(baseline.oids)
  const baselineNodeIds = new Set(baseline.nodeIds)
  // data-node-id 才是 VisualDocument 的稳定身份；data-oid 还包含
  // inspect 插件为数据面板/第三方组件生成的源码锚点，允许新增。
  const oidDrift = [...uniqueOids].filter((oid) => !baselineOids.has(oid))
  const nodeIdDrift = [...uniqueNodeIds].filter(
    (nodeId) => !baselineNodeIds.has(nodeId),
  )
  if (nodeIdDrift.length) {
    throw new Error(
      `出现新 node-id（VisualDocument 身份漂移）: ${nodeIdDrift
        .slice(0, 5)
        .join(',')}`,
    )
  }
  log('identity after', {
    uniqueOids: uniqueOids.size,
    uniqueNodeIds: uniqueNodeIds.size,
    duplicateOids,
    oidDrift: oidDrift.length,
    nodeIdDrift: nodeIdDrift.length,
  })
}

async function caseSelect(page, active) {
  await ensureInteraction(page)
  const frame = await frameOf(active.locator('.canvas-live-frame'))
  const leaf = await findLeaf(frame)
  if (!leaf) throw new Error('没有可选中节点')
  await frame.hover(`[data-oid="${leaf.oid}"]`)
  await frame.waitForSelector('[data-vibe-direct-ui]', {
    state: 'visible',
    timeout: 8000,
  })
  log('select case', leaf)
}

async function caseDeleteUndo(page, active) {
  await ensureInteraction(page)
  const frame = await frameOf(active.locator('.canvas-live-frame'))
  const leaf = await findLeaf(frame)
  if (!leaf) throw new Error('没有可删除节点')
  await clickDelete(page, frame, leaf.oid)
  await waitBufferText(page, '提交修改', 20000)
  const afterDelete = await frameOf(active.locator('.canvas-live-frame'))
  if (!(await isNodeHidden(afterDelete, leaf.oid))) {
    throw new Error('删除后节点未乐观隐藏')
  }
  await page.locator(ACTIVE_UNDO).click()
  await waitUntil(async () => {
    const current = await frameOf(active.locator('.canvas-live-frame'))
    return !(await isNodeHidden(current, leaf.oid))
  }, 20000, '撤销后节点未恢复')
  await waitBarGone(page, 20000)
  log('delete-undo ok', leaf)
}

async function caseDeleteCommit(page, active) {
  await ensureInteraction(page)
  const frame = await frameOf(active.locator('.canvas-live-frame'))
  const leaf = await findLeaf(frame)
  if (!leaf) throw new Error('没有可提交删除的节点')
  await clickDelete(page, frame, leaf.oid)
  await waitBufferText(page, '提交修改', 20000)
  await page.locator(ACTIVE_SAVE).click()
  await waitUntil(async () => {
    const current = await frameOf(active.locator('.canvas-live-frame'))
    return isNodeHidden(current, leaf.oid)
  }, 30000, '提交修改后节点未删除')
  await waitBarGone(page, 30000)
  log('delete-commit ok', leaf)
}

async function caseMoveCommit(page, active) {
  await ensureDragArmed(page, active)
  // 删除提交可能还在同步；等草稿条清空，避免把移动操作和上一条提交混在一起。
  await waitBarGone(page, 10000).catch(() => {})
  const frame = await frameOf(active.locator('.canvas-live-frame'))
  const armed = await frame.evaluate(() =>
    document.documentElement.classList.contains('vibe-drag-enabled'),
  )
  log('move-commit armed', armed)
  if (!armed) {
    throw new Error('拖拽模式未 arm：iframe 没有收到 drag-mode')
  }
  await page.evaluate(() => {
    window.__vibeDragEvents = []
    window.addEventListener('message', (event) => {
      const data = event.data
      if (data && data.channel === 'vibe-inspect') {
        window.__vibeDragEvents.push(data)
      }
    })
  })
  const geometry = await frame.evaluate(() => {
    const containers = Array.from(
      document.querySelectorAll('[data-oid][data-source-kind="container"]'),
    ).filter((node) => node.hasAttribute('data-node-id'))
    const parent = containers.find(
      (node) =>
        Array.from(node.children || []).filter((child) =>
          child.hasAttribute?.('data-oid'),
        ).length >= 2,
    )
    if (!parent) return null
    const children = Array.from(parent.children || []).filter((child) =>
      child.hasAttribute?.('data-oid'),
    )
    const source = children[children.length - 1]
    const target = children[0]
    const sourceRect = source.getBoundingClientRect()
    const targetRect = target.getBoundingClientRect()
    return {
      parentOid: parent.getAttribute('data-oid'),
      source: {
        oid: source.getAttribute('data-oid'),
        x: sourceRect.left + sourceRect.width / 2,
        y: sourceRect.top + Math.min(24, sourceRect.height / 2),
      },
      target: {
        oid: target.getAttribute('data-oid'),
        x: targetRect.left + Math.min(24, targetRect.width / 2),
        y: targetRect.top + 4,
      },
    }
  })
  if (!geometry) throw new Error('没有可测试同父容器重排的兄弟节点')
  await frame.evaluate(({ source, target }) => {
    const sourceNode = document.querySelector(`[data-oid="${source.oid}"]`)
    if (!sourceNode) return
    const dispatch = (node, type, x, y) => {
      node.dispatchEvent(
        new PointerEvent(type, {
          bubbles: true,
          cancelable: true,
          pointerId: 1,
          pointerType: 'mouse',
          button: 0,
          buttons: type === 'pointerup' ? 0 : 1,
          clientX: x,
          clientY: y,
        }),
      )
    }
    dispatch(sourceNode, 'pointerdown', source.x, source.y)
    for (let step = 1; step <= 8; step += 1) {
      dispatch(
        document,
        'pointermove',
        source.x + ((target.x - source.x) * step) / 8,
        source.y + ((target.y - source.y) * step) / 8,
      )
    }
    dispatch(document, 'pointerup', target.x, target.y)
  }, geometry)
  await page.waitForTimeout(800)
  const dragEvents = await page.evaluate(() =>
    (window.__vibeDragEvents || [])
      .filter((item) => String(item.type || '').startsWith('drag-'))
      .map((item) => ({
        type: item.type,
        sourceOid: item.payload?.source?.oid,
        placement: item.payload?.placement
          ? {
              parentOid: item.payload.placement.parentOid,
              parentFile: item.payload.placement.parentFile,
              siblingIndex: item.payload.placement.siblingIndex,
              insertPosition: item.payload.placement.insertPosition,
            }
          : null,
      })),
  )
  const toast = await page
    .locator('.workspace-toast')
    .allInnerTexts()
    .catch(() => [])
  log('move-commit events', dragEvents)
  log('move-commit toast', toast)
  if (toast.some((text) => text.includes('没有找到可落位'))) {
    throw new Error(`拖拽没有找到落点: ${toast.join(' | ')}`)
  }
  if (!dragEvents.some((item) => item.type === 'drag-end')) {
    throw new Error(`拖拽没有产生 drag-end: ${JSON.stringify(dragEvents)}`)
  }
  await waitBufferText(page, '提交修改', 20000)
  await page.locator(ACTIVE_SAVE).click()
  let order = []
  await waitUntil(async () => {
    const current = await frameOf(active.locator('.canvas-live-frame'))
    order = await current.evaluate(({ parentOid }) => {
      const parent = document.querySelector(`[data-oid="${parentOid}"]`)
      if (!parent) return []
      return Array.from(parent.children || [])
        .filter((child) => child.hasAttribute?.('data-oid'))
        .map((child) => child.getAttribute('data-oid'))
    }, geometry)
    return order[0] === geometry.source.oid
  }, 30000, `拖拽提交后顺序未生效: ${order.join(',')}`)
  log('move-commit ok', { source: geometry.source.oid, order })
}

async function main() {
  const browser = await chromium.launch({
    executablePath: CHROME,
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  })
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } })
  const page = await context.newPage()
  const pageErrors = []
  directResponses = []
  page.on('pageerror', (error) => pageErrors.push(String(error)))
  page.on('request', (request) => {
    if (
      request.method() !== 'POST' ||
      !request.url().includes('/direct-ops/actions')
    ) {
      return
    }
    try {
      lastDirectRequest = JSON.parse(request.postData() || '{}')
    } catch {
      // 非 JSON 请求体忽略
    }
  })
  page.on('response', async (response) => {
    if (!response.url().includes('/direct-ops/actions')) return
    let body = ''
    try {
      body = await response.text()
    } catch {
      // 忽略无法读取的响应体
    }
    directResponses.push({
      url: response.url(),
      status: response.status(),
      body,
    })
  })

  try {
    log('open workspace')
    await page.goto(`${WEB}/workspace`, { waitUntil: 'domcontentloaded' })
    const demo = page.getByRole('button', { name: '一键体验（推荐）' })
    await demo.waitFor({ state: 'visible', timeout: 15000 }).catch(() => {})
    if (await demo.isVisible().catch(() => false)) await demo.click()
    await page.locator('.project-switcher-button').waitFor({ timeout: 60000 })

    log('create blank project + mock provider')
    await page.locator('.tab-new').click()
    // 新建项目强制分流：先选“从零开始”，不再直接创建标签。
    await page
      .locator('.new-project-dialog')
      .getByRole('button', { name: '从零开始' })
      .click()
    await page.locator('.workspace-tab').nth(1).waitFor({ timeout: 30000 })
    const active = page.locator('.tab-window.is-active')
    await active.locator('.model-picker select').selectOption(PROVIDER)
    const startButton = active.locator('.canvas-start-button')
    await startButton.waitFor({ state: 'visible', timeout: 60000 })

    log('start Architect conversation')
    await startButton.click()
    const panel = page.locator('.architect-panel')
    await panel.waitFor({ state: 'visible', timeout: 30000 })
    await panel.locator('[data-architect-goal]').fill(GOAL)
    await panel
      .locator(
        `[data-architect-quality-tier] input[value="${QUALITY_TIER}"]`,
      )
      .check({ force: true })
    await panel.locator('[data-architect-start]').click()

    log('solution discovery')
    const discovery = panel.locator('[data-architect-solution-discovery]')
    const discoveryVisible = await discovery
      .waitFor({ state: 'visible', timeout: 60000 })
      .then(() => true)
      .catch(() => false)
    if (discoveryVisible) {
      await panel
        .locator('[data-architect-solution-loading]')
        .waitFor({ state: 'detached', timeout: 90000 })
        .catch(() => {})
      const confirm = panel.locator('[data-architect-solution-confirm]')
      if (await confirm.count()) {
        await confirm.first().click()
      } else {
        await panel.locator('[data-architect-solution-skip]').first().click()
      }
      await page.waitForTimeout(500)
      log('solution selected')
    }

    log('drive V0 clarifications')
    await driveV0(page, panel)

    log('visual canvas: freeze')
    const nodes = page.locator('.vc-node')
    await nodes.first().waitFor({ state: 'visible', timeout: 60000 })
    const nodeCount = await nodes.count()
    if (nodeCount < 3) fail(`V0 虚拟树节点过少: ${nodeCount}`)
    const freeze = page.getByRole('button', { name: /确认视觉，进入业务审查/ })
    await freeze.waitFor({ state: 'visible', timeout: 30000 })
    await freeze.click()
    await page.waitForURL(/\/workspace/, { timeout: 60000 })

    log('backend binding review')
    await panel.waitFor({ state: 'visible', timeout: 30000 })
    // 冻结树缺失时新守卫会弹错误且不启动 Run；这里必须不能出现。
    await page.waitForTimeout(800)
    const bodyText = await page.locator('body').innerText()
    if (bodyText.includes('冻结界面已丢失')) {
      fail('BACKEND_BINDING 退化为无冻结树运行：命中了“冻结界面已丢失”守卫')
    }
    await answerClarification(panel)

    log('wait contract confirmation')
    let contractReady = false
    const contractDeadline = Date.now() + 300000
    let contractRound = 0
    while (Date.now() < contractDeadline) {
      contractRound += 1
      const contract = panel.locator('.contract-viewer').first()
      if (await contract.isVisible().catch(() => false)) {
        contractReady = true
        break
      }
      if ((await panel.locator('.architect-outcome.is-error').count()) > 0) {
        fail(`Architect FAILED: ${(await panel.innerText()).slice(-1200)}`)
      }
      const form = panel.locator('.architect-clarification')
      if (await form.count()) {
        log('answer backend-binding clarification', contractRound)
        await answerClarification(panel)
        await page.waitForTimeout(1500)
        continue
      }
      await page.waitForTimeout(2000)
    }
    if (!contractReady) {
      const dump = await page.evaluate(() =>
        sessionStorage.getItem('vibe.architect.v1'),
      )
      fail(`契约 5 分钟内未就绪: ${String(dump).slice(-2000)}`)
    }
    await panel
      .locator('.architect-status')
      .filter({ hasText: '等待确认' })
      .waitFor({ timeout: 60000 })

    // 质量分级：选 L2 标准模式后才能确认。
    const tier = panel.locator(
      `[data-quality-tier] input[value="${QUALITY_TIER}"]`,
    )
    if (await tier.count()) await tier.first().check({ force: true })
    await panel.getByRole('button', { name: /确认契约，进入生成/ }).click()
    await panel
      .locator('.architect-status')
      .filter({ hasText: '已确认' })
      .waitFor({ timeout: 60000 })

    log('assert visual contract path (regression)')
    const bootstraps = await panel.locator('[data-frontend-bootstrap-status]').count()
    if (bootstraps !== 0) {
      fail('契约确认后退回了旧版 FrontendBootstrap，而不是 Visual CodeGen')
    }
    if ((await panel.locator('[data-visual-codegen-status]').count()) === 0) {
      fail('契约缺少 visualDocument：面板未进入 Visual CodeGen 分支')
    }
    const confirmedText = await panel.innerText()
    if (confirmedText.includes('契约缺少')) {
      fail('出现“契约缺少 frontendRoutes/components”误导性错误')
    }

    log('run visual codegen')
    const generate = panel.locator('[data-visual-codegen-generate]')
    await generate.waitFor({ state: 'visible', timeout: 30000 })
    await generate.click()
    await panel
      .locator('[data-visual-codegen-status="SUCCEEDED"]')
      .waitFor({ timeout: 420000 })

    log('canvas switched to live')
    await panel.getByRole('button', { name: '关闭架构师面板' }).click()
    const liveFrame = active.locator('.canvas-live-frame')
    await liveFrame.waitFor({ state: 'visible', timeout: 60000 })
    const frame = page.frameLocator('.tab-window.is-active .canvas-live-frame')
    await frame.locator('body').waitFor({ state: 'visible', timeout: 60000 })
    const liveText = await frame.locator('body').innerText()
    if (!liveText.trim()) fail('live 画布正文为空')
    if ((await active.locator('.canvas-start-button').count()) !== 0) {
      fail('生成完成后画布仍停留在开始创建态')
    }
    const liveDocumentFrame = await frameOf(
      active.locator('.canvas-live-frame'),
    )
    const panelCount = await liveDocumentFrame.evaluate(
      () => document.querySelectorAll('[data-fullstack-panel]').length,
    )
    if (panelCount !== 0) {
      fail('视觉 CodeGen 仍注入了无源码锚点的 FullstackDataPanel')
    }
    if (process.env.LIVE_DIAGNOSE === '1') {
      await diagnoseLiveCanvas(page, active)
    }
    const canvasResults = []
    if (process.env.CANVAS_OPS !== '0') {
      await runCanvasCase(canvasResults, 'identity-baseline', () =>
        caseIdentity(page, active, 'baseline'),
      )
      await runCanvasCase(canvasResults, 'select', () =>
        caseSelect(page, active),
      )
      await runCanvasCase(canvasResults, 'delete-undo', () =>
        caseDeleteUndo(page, active),
      )
      await runCanvasCase(canvasResults, 'delete-commit', () =>
        caseDeleteCommit(page, active),
      )
      await runCanvasCase(canvasResults, 'move-commit', () =>
        caseMoveCommit(page, active),
      )
      await runCanvasCase(canvasResults, 'identity-after', () =>
        caseIdentity(page, active, 'after'),
      )
    }

    const previewUrl = liveDocumentFrame.url()
    log('reload workspace: state must survive')
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.locator('.tab-window.is-active .canvas-live-frame').waitFor({
      state: 'visible',
      timeout: 90000,
    })

    const report = {
      generatedAt: new Date().toISOString(),
      goal: GOAL,
      provider: PROVIDER,
      qualityTier: QUALITY_TIER,
      previewUrl,
      visualNodes: nodeCount,
      liveTextLength: liveText.trim().length,
      cases: canvasResults,
      pageErrors,
      ok:
        canvasResults.every((item) => item.ok) &&
        pageErrors.length === 0,
    }
    const reportPaths = writeCanvasReport(report)
    log('canvas report', { ...reportPaths, ok: report.ok })
    if (pageErrors.length) fail(`页面错误: ${pageErrors.join(' | ')}`)
    if (!report.ok) {
      fail(
        '画布地基用例失败: ' +
          canvasResults
            .filter((item) => !item.ok)
            .map((item) => `${item.name}: ${item.error}`)
            .join(' | '),
      )
    }
    log('PASS', {
      visualNodes: nodeCount,
      liveTextLength: liveText.trim().length,
      pageErrors,
      cases: canvasResults.map((item) => ({
        name: item.name,
        ok: item.ok,
        durationMs: item.durationMs,
      })),
    })
  } finally {
    await browser.close()
  }
}

main().catch((error) => {
  console.error('[visual-chain-e2e] FAIL', error?.stack || error)
  process.exit(1)
})
