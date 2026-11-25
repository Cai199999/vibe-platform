#!/usr/bin/env node
/**
 * G4 上下文 E2E：多轮对话必须读取 project summary，不重复询问已确认决策。
 *
 * 场景：
 *   1. 首次目标明确写死“数据库 MySQL 8，不要再问数据库”；
 *   2. 多轮澄清 + 契约确认后，追加新需求；
 *   3. 修订契约必须仍保留 MySQL，且两轮都不得再问数据库。
 *
 * 运行：
 *   NODE_PATH=/tmp/vibe-e2e/node_modules node scripts/g4_context_e2e.mjs
 *   G4_PROVIDER=mock ...   # 快速自检（mock 不保证真实语义）
 *   G4_PROVIDER=auto ...   # 真实 DeepSeek（推荐用于验收）
 */
import { createRequire } from 'node:module'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const require = createRequire(import.meta.url)

function loadPlaywright() {
  try {
    return require('playwright-core')
  } catch {
    // ESM 不读取 NODE_PATH
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
      // continue
    }
  }
  throw new Error('缺少 playwright-core；请设置 NODE_PATH 或 PLAYWRIGHT_CORE_DIR')
}

const { chromium } = loadPlaywright()
const WEB = process.env.WEB_URL || 'http://127.0.0.1:5173'
const CHROME =
  process.env.CHROME_BIN ||
  path.join(
    os.homedir(),
    '.cache/ms-playwright/chromium-1254/chrome-linux64/chrome',
  )
const PROVIDER = process.env.G4_PROVIDER || 'auto'
const GOAL =
  '做一个测绘任务与成果管理平台：项目总览、任务看板、成果质检、影像上传；' +
  '数据库用 MySQL 8，不要再问数据库；作业员只能看自己任务，质检员看全项目。'
const FORBIDDEN =
  /数据库使用哪一种|数据库选型|数据库引擎|database\.engine|哪种数据库/i

const log = (...args) => console.log('[g4]', ...args)

async function answer(panel) {
  const form = panel.locator('.architect-clarification')
  await form.waitFor({ state: 'visible', timeout: 90000 })
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
  const areas = form.locator(
    'label.architect-question:not(.architect-comment) textarea',
  )
  const areaCount = await areas.count()
  for (let index = 0; index < areaCount; index += 1) {
    const area = areas.nth(index)
    const label = await area.locator('xpath=..').innerText()
    if (label.includes('数据实体')) {
      await area.fill(
        'Project(id,name);Task(id,projectId,title);Deliverable(id,taskId,fileName);QCRecord(id,deliverableId,result)',
      )
    } else if (label.includes('业务动作') || label.includes('交互')) {
      await area.fill('项目 -> 任务 -> 成果 -> 质检 -> 归档')
    } else {
      await area.fill(GOAL)
    }
  }
  const submit = form.getByRole('button', { name: '提交回答并继续' })
  if (await submit.count()) await submit.click()
}

async function captureQuestions(panel, bucket) {
  const form = panel.locator('.architect-clarification')
  if (!(await form.isVisible().catch(() => false))) return
  const text = await form.innerText()
  bucket.push(text)
  if (FORBIDDEN.test(text)) {
    throw new Error(`Architect 重复询问数据库: ${text.slice(0, 300)}`)
  }
}

async function driveV0(page, panel, questions) {
  for (let round = 0; round < 12; round += 1) {
    if (page.url().includes('/visual-canvas')) return
    const form = panel.locator('.architect-clarification')
    const appeared = await form
      .waitFor({ state: 'visible', timeout: 60000 })
      .then(() => true)
      .catch(() => false)
    if (appeared) {
      await captureQuestions(panel, questions)
      await answer(panel)
      await page.waitForTimeout(800)
      continue
    }
    if (
      (await panel.locator('.architect-outcome.is-error').count()) > 0
    ) {
      throw new Error(`V0 FAILED: ${(await panel.innerText()).slice(-600)}`)
    }
    await page.waitForTimeout(1000)
  }
  throw new Error('V0 超过 12 轮未完成')
}

async function waitForContract(page, panel, questions) {
  for (let round = 0; round < 120; round += 1) {
    if (
      await panel
        .locator('.contract-viewer')
        .first()
        .isVisible()
        .catch(() => false)
    ) {
      return panel.locator('.contract-viewer').first().innerText()
    }
    const form = panel.locator('.architect-clarification')
    if (await form.isVisible().catch(() => false)) {
      await captureQuestions(panel, questions)
      await answer(panel)
      await page.waitForTimeout(1000)
      continue
    }
    if ((await panel.locator('.architect-outcome.is-error').count()) > 0) {
      throw new Error(`契约 FAILED: ${(await panel.innerText()).slice(-600)}`)
    }
    await page.waitForTimeout(1500)
  }
  throw new Error('契约未在时限内就绪')
}

async function main() {
  const browser = await chromium.launch({
    executablePath: CHROME,
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  })
  const context = await browser.newContext({
    viewport: { width: 1600, height: 1000 },
  })
  const page = await context.newPage()
  const errors = []
  page.on('pageerror', (error) => errors.push(String(error)))
  const questions = []
  try {
    await page.goto(`${WEB}/workspace`, { waitUntil: 'domcontentloaded' })
    const demo = page.getByRole('button', { name: '一键体验（推荐）' })
    await demo.waitFor({ state: 'visible', timeout: 15000 }).catch(() => {})
    if (await demo.isVisible().catch(() => false)) await demo.click()
    await page.locator('.project-switcher-button').waitFor({ timeout: 60000 })

    await page.locator('.tab-new').click()
    await page
      .locator('.new-project-dialog')
      .getByRole('button', { name: '从零开始' })
      .click()
    await page.locator('.workspace-tab').nth(1).waitFor({ timeout: 30000 })
    const active = page.locator('.tab-window.is-active')
    await active.locator('.model-picker select').selectOption(PROVIDER)
    await active.locator('.canvas-start-button').click()

    const panel = page.locator('.architect-panel')
    await panel.waitFor({ state: 'visible', timeout: 30000 })
    await panel.locator('[data-architect-goal]').fill(GOAL)
    await panel
      .locator('[data-architect-quality-tier] input[value="L2_STANDARD"]')
      .check({ force: true })
    await panel.locator('[data-architect-start]').click()

    log('drive V0')
    await driveV0(page, panel, questions)
    await page
      .getByRole('button', { name: /确认视觉，进入业务审查/ })
      .click()
    await page.waitForURL(/\/workspace/, { timeout: 60000 })
    await panel.waitFor({ state: 'visible', timeout: 60000 })
    await page.waitForTimeout(800)

    log('first contract')
    const firstContract = await waitForContract(page, panel, questions)
    if (!/mysql/i.test(firstContract)) {
      throw new Error('首次契约未保留 MySQL')
    }
    console.log('[g4] first contract reuse hit:', /复用|REUSE|ADAPT|GeoAI|torchgeo/i.test(firstContract))
    await panel
      .locator('.architect-status')
      .filter({ hasText: '等待确认' })
      .waitFor({ timeout: 60000 })
    const tier = panel.locator('[data-quality-tier] input[value="L2_STANDARD"]')
    if (await tier.count()) await tier.first().check({ force: true })
    await panel.getByRole('button', { name: /确认契约，进入生成/ }).click()
    await panel
      .locator('.architect-status')
      .filter({ hasText: '已确认' })
      .waitFor({ timeout: 60000 })

    log('append requirement')
    const append = panel.locator('[data-contract-append]')
    await append.waitFor({ state: 'visible', timeout: 30000 })
    await append
      .locator('.architect-revision textarea')
      .fill('追加需求：再加一个成果导出按钮，其余决策保持不变。')
    await append.getByRole('button', { name: /追加需求并修订/ }).click()

    log('revised contract')
    const revised = await waitForContract(page, panel, questions)
    if (!/mysql/i.test(revised)) {
      throw new Error('修订契约丢失 MySQL')
    }
    if (FORBIDDEN.test(questions.join('\n'))) {
      throw new Error('多轮对话中重复询问数据库')
    }
    await page.screenshot({ path: '/tmp/g4-context.png', fullPage: false })
    log('PASS', {
      provider: PROVIDER,
      questionRounds: questions.length,
      mysqlInFirst: /mysql/i.test(firstContract),
      mysqlInRevised: /mysql/i.test(revised),
      pageErrors: errors.length,
    })
  } catch (error) {
    console.error('[g4] FAIL', error?.stack || error)
    await page.screenshot({ path: '/tmp/g4-context-fail.png' }).catch(() => {})
    process.exitCode = 1
  } finally {
    await browser.close()
  }
}

main()
