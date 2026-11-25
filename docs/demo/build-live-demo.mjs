#!/usr/bin/env node
/**
 * 用预制的丰富 VisualDocument 走完整业务审查与 CodeGen。
 *
 * 目的：演示“我已经在 V0 画布上把骨架补成真实 GIS 页面”，
 * 然后从冻结开始走 BACKEND_BINDING → 契约 → Visual CodeGen。
 */
import { createRequire } from 'node:module'
import os from 'node:os'
import path from 'node:path'
import fs from 'node:fs'

const require = createRequire(import.meta.url)
const { chromium } = require('/tmp/vibe-e2e/node_modules/playwright-core')

const WEB = process.env.WEB_URL || 'http://127.0.0.1:5173'
const CHROME =
  process.env.CHROME_BIN ||
  path.join(os.homedir(), '.cache/ms-playwright/chromium-1254/chrome-linux64/chrome')
const PROVIDER = process.env.DEMO_PROVIDER || 'auto'
const DOC_PATH =
  '/home/admin1/Workspace/vibe-platform/docs/demo/gis-visual-document.json'
const STATE_PATH =
  '/home/admin1/Workspace/vibe-platform/docs/demo/demo-storage.json'
const WORKSPACE_PATH =
  '/home/admin1/Workspace/vibe-platform/docs/demo/demo-workspace.json'

const GOAL_PROJECT_NAME = '测绘任务与成果管理平台'
const ENTITIES =
  'Project(id, name, region, manager, status, progress)；' +
  'Task(id, projectId, title, assignee, deadline, status)；' +
  'Deliverable(id, taskId, fileName, uploader, uploadedAt, qcStatus)；' +
  'QCRecord(id, deliverableId, reviewer, result, comment)'
const FLOW =
  '项目经理创建项目 -> 拆分并派发任务 -> 作业员上传影像成果 -> ' +
  '质检员审核并标记合格/返工 -> 项目总览更新进度'

const log = (...args) => console.log('[gis-rich]', ...args)

async function answerClarification(panel) {
  const form = panel.locator('.architect-clarification')
  await form.waitFor({ state: 'visible', timeout: 120000 })
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
      await area.fill(ENTITIES)
    } else if (label.includes('业务动作') || label.includes('交互')) {
      await area.fill(FLOW)
    } else {
      await area.fill(GOAL_PROJECT_NAME)
    }
  }
  const submit = form.getByRole('button', { name: '提交回答并继续' })
  if ((await submit.count()) === 0) {
    log('clarification form has no submit button; skip')
    return
  }
  await submit.click()
}

async function main() {
  const richDoc = JSON.parse(fs.readFileSync(DOC_PATH, 'utf8'))
  const browser = await chromium.launch({
    executablePath: CHROME,
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  })
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } })
  const page = await context.newPage()
  const pageErrors = []
  page.on('pageerror', (error) => pageErrors.push(String(error)))

  try {
    log('open workspace')
    await page.goto(`${WEB}/workspace`, { waitUntil: 'domcontentloaded' })
    const demo = page.getByRole('button', { name: '一键体验（推荐）' })
    await demo.waitFor({ state: 'visible', timeout: 15000 }).catch(() => {})
    if (await demo.isVisible().catch(() => false)) await demo.click()
    await page.locator('.project-switcher-button').waitFor({ timeout: 60000 })

    log('create blank project', { provider: PROVIDER })
    await page.locator('.tab-new').click()
    await page
      .locator('.new-project-dialog')
      .getByRole('button', { name: '从零开始' })
      .click()
    await page.locator('.workspace-tab').nth(1).waitFor({ timeout: 30000 })
    const active = page.locator('.tab-window.is-active')
    await active.locator('.model-picker select').selectOption(PROVIDER)

    const projectId = await page.evaluate(() => {
      const raw = window.localStorage.getItem('vibe.workspace.v1')
      if (!raw) return null
      const state = JSON.parse(raw)
      return state.activeTabId || null
    })
    if (!projectId) throw new Error('无法读取新建项目 ID')
    log('project id', projectId)

    // 给项目命名，并把预制的 VisualDocument 注入当前项目。
    await page.evaluate(
      ({ projectId, name, doc }) => {
        const key = 'vibe.workspace.v1'
        const state = JSON.parse(window.localStorage.getItem(key))
        state.projects = (state.projects || []).map((project) =>
          project.id === projectId ? { ...project, name } : project,
        )
        window.localStorage.setItem(key, JSON.stringify(state))
        const next = {
          ...doc,
          projectId,
          status: 'DRAFT',
          metadata: {
            ...(doc.metadata || {}),
            contentHash: '',
            createdAt: new Date().toISOString(),
            lastModifiedBy: 'user',
          },
        }
        window.localStorage.setItem('vibe.visualDocument.v1', JSON.stringify(next))
      },
      { projectId, name: GOAL_PROJECT_NAME, doc: richDoc },
    )

    log('open rich visual canvas')
    await page.goto(
      `${WEB}/visual-canvas?project=${encodeURIComponent(projectId)}&title=${encodeURIComponent(GOAL_PROJECT_NAME)}`,
      { waitUntil: 'domcontentloaded' },
    )
    await page.locator('.vc-node').first().waitFor({ state: 'visible', timeout: 60000 })
    const nodeCount = await page.locator('.vc-node').count()
    log('rich V0 node count', nodeCount)
    await page.waitForTimeout(1200)
    await page.screenshot({ path: '/tmp/gis-rich-v0.png', fullPage: false })

    log('freeze rich visual canvas')
    const freeze = page.getByRole('button', { name: /确认视觉，进入业务审查/ })
    await freeze.waitFor({ state: 'visible', timeout: 30000 })
    await freeze.click()
    await page.waitForURL(/\/workspace/, { timeout: 60000 })

    log('architect backend binding')
    const panel = page.locator('.architect-panel')
    await panel.waitFor({ state: 'visible', timeout: 60000 })
    await page.waitForTimeout(1200)
    await answerClarification(panel)

    log('wait contract confirmation')
    let contractReady = false
    for (let round = 0; round < 90; round += 1) {
      const contract = panel.locator('.contract-viewer').first()
      if (await contract.isVisible().catch(() => false)) {
        contractReady = true
        break
      }
      if ((await panel.locator('.architect-outcome.is-error').count()) > 0) {
        throw new Error(`Architect FAILED: ${(await panel.innerText()).slice(-800)}`)
      }
      const form = panel.locator('.architect-clarification')
      if (await form.isVisible().catch(() => false)) {
        log('answer backend-binding clarification', round + 1)
        await answerClarification(panel)
        await page.waitForTimeout(1500)
        continue
      }
      if (round > 0 && round % 10 === 0) {
        const status = await panel
          .locator('.architect-status')
          .allInnerTexts()
          .catch(() => [])
        log('contract wait', { round, status: status.slice(-2) })
      }
      await page.waitForTimeout(3000)
    }
    if (!contractReady) throw new Error('契约 5 分钟内未就绪')

    await panel
      .locator('.architect-status')
      .filter({ hasText: '等待确认' })
      .waitFor({ timeout: 60000 })
    const tier = panel.locator('[data-quality-tier] input[value="L2_STANDARD"]')
    if (await tier.count()) await tier.first().check({ force: true })
    await page.screenshot({ path: '/tmp/gis-rich-contract.png', fullPage: false })
    await panel.getByRole('button', { name: /确认契约，进入生成/ }).click()
    await panel
      .locator('.architect-status')
      .filter({ hasText: '已确认' })
      .waitFor({ timeout: 60000 })

    log('run visual codegen')
    const generate = panel.locator('[data-visual-codegen-generate]')
    await generate.waitFor({ state: 'visible', timeout: 30000 })
    await generate.click()
    await panel
      .locator('[data-visual-codegen-status="SUCCEEDED"]')
      .waitFor({ timeout: 480000 })

    log('canvas switched to live')
    await panel.getByRole('button', { name: '关闭架构师面板' }).click()
    const liveFrame = page.locator('.tab-window.is-active .canvas-live-frame')
    await liveFrame.waitFor({ state: 'visible', timeout: 60000 })
    const frame = page.frameLocator('.tab-window.is-active .canvas-live-frame')
    await frame.locator('body').waitFor({ state: 'visible', timeout: 60000 })
    const liveText = await frame.locator('body').innerText()
    if (!liveText.trim()) throw new Error('live 画布正文为空')
    await page.waitForTimeout(2000)
    await page.screenshot({ path: '/tmp/gis-rich-live.png', fullPage: false })

    log('reload workspace: state must survive')
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page
      .locator('.tab-window.is-active .canvas-live-frame')
      .waitFor({ state: 'visible', timeout: 90000 })
    await page.waitForTimeout(1500)
    await page.screenshot({ path: '/tmp/gis-rich-reload.png', fullPage: false })

    if (pageErrors.length) throw new Error(`页面错误: ${pageErrors.join(' | ')}`)
    log('PASS', {
      provider: PROVIDER,
      projectId,
      visualNodes: nodeCount,
      liveTextLength: liveText.trim().length,
    })
  } catch (error) {
    await page.screenshot({ path: '/tmp/gis-rich-fail.png', fullPage: false }).catch(() => {})
    console.error('[gis-rich] FAIL', error?.stack || error)
    process.exitCode = 1
  } finally {
    if (process.exitCode !== 1) {
      await context.storageState({ path: STATE_PATH }).catch((error) => {
        console.error('[gis-rich] storageState save failed', error)
      })
      const workspace = await page
        .evaluate(() => window.localStorage.getItem('vibe.workspace.v1'))
        .catch(() => null)
      if (workspace) fs.writeFileSync(WORKSPACE_PATH, workspace)
      log('demo storage saved', {
        statePath: STATE_PATH,
        workspaceBytes: workspace?.length ?? 0,
      })
    }
    await browser.close()
  }
}

main()
