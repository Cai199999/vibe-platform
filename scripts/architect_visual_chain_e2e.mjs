#!/usr/bin/env node
/**
 * 新建项目两阶段全链路 E2E：Architect 对话 → Visual V0 → 冻结 →
 * BACKEND_BINDING 澄清 → 契约确认 → Visual CodeGen → live 画布。
 *
 * 重点回归：
 *   - 冻结树必须随 BACKEND_BINDING 传回服务端；契约确认后不得退回旧版
 *     “FrontendCoder 骨架初始化”，也不得出现“契约缺少 frontendRoutes /
 *     components”的误导性 toast；
 *   - BACKEND_BINDING 缺冻结树时服务端 422 fail-fast（入口级守卫的单测
 *     在 Python 侧，本脚本校验浏览器链路的正向流程）。
 *
 * 运行（需 web：5173、BFF：8080、python-ai：8100 已启动）：
 *   NODE_PATH=/tmp/vibe-e2e/node_modules node scripts/architect_visual_chain_e2e.mjs
 */
import { createRequire } from 'node:module'
import os from 'node:os'
import path from 'node:path'

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
const WEB = process.env.WEB_URL || 'http://127.0.0.1:5173'
const CHROME =
  process.env.CHROME_BIN ||
  path.join(
    os.homedir(),
    '.cache/ms-playwright/chromium-1254/chrome-linux64/chrome',
  )
const GOAL =
  '做一个订单管理后台：成员登录后查看订单列表，并能在表单里新建订单；' +
  '订单和成员数据需要持久化到 MySQL。'

function log(...args) {
  console.log('[visual-chain-e2e]', ...args)
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
      await area.fill(
        'Order(id, title, amount, memberId, createdAt)；Member(id, name)',
      )
    } else if (label.includes('业务动作') || label.includes('交互')) {
      await area.fill('成员登录 -> 查看订单列表 -> 新建订单并保存')
    } else {
      await area.fill(GOAL)
    }
  }
  await form.getByRole('button', { name: '提交回答并继续' }).click()
}

async function driveV0(page, panel) {
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
    const form = panel.locator('.architect-clarification')
    if (await form.count()) {
      log('answer V0 clarification round', round + 1)
      await answerClarification(panel)
      await page.waitForTimeout(1200)
      continue
    }
    await page.waitForTimeout(1500)
  }
  fail('V0 阶段超过 10 轮仍未产出虚拟画布')
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
  page.on('pageerror', (error) => pageErrors.push(String(error)))

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
    await active.locator('.model-picker select').selectOption('mock')
    const startButton = active.locator('.canvas-start-button')
    await startButton.waitFor({ state: 'visible', timeout: 60000 })

    log('start Architect conversation')
    await startButton.click()
    const panel = page.locator('.architect-panel')
    await panel.waitFor({ state: 'visible', timeout: 30000 })
    await panel.locator('[data-architect-goal]').fill(GOAL)
    await panel.locator('[data-architect-start]').click()

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
    for (let round = 0; round < 10; round += 1) {
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
        log('answer backend-binding clarification', round + 1)
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
      fail(`契约 2 分钟内未就绪: ${String(dump).slice(-2000)}`)
    }
    await panel
      .locator('.architect-status')
      .filter({ hasText: '等待确认' })
      .waitFor({ timeout: 60000 })

    // 质量分级：选 L2 标准模式后才能确认。
    const tier = panel.locator('[data-quality-tier] input[value="L2_STANDARD"]')
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

    log('reload workspace: state must survive')
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.locator('.tab-window.is-active .canvas-live-frame').waitFor({
      state: 'visible',
      timeout: 90000,
    })

    if (pageErrors.length) fail(`页面错误: ${pageErrors.join(' | ')}`)
    log('PASS', {
      visualNodes: nodeCount,
      liveTextLength: liveText.trim().length,
      pageErrors,
    })
  } finally {
    await browser.close()
  }
}

main().catch((error) => {
  console.error('[visual-chain-e2e] FAIL', error?.stack || error)
  process.exit(1)
})
