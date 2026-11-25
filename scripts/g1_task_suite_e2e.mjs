#!/usr/bin/env node
/**
 * G1 真实任务集：自动跑 3 个典型任务并输出 N/3 成功率与耗时。
 *
 * 运行：
 *   NODE_PATH=/tmp/vibe-e2e/node_modules node scripts/g1_task_suite_e2e.mjs
 *
 * 默认使用 mock provider（确定性、快）；真实模型：
 *   G1_PROVIDER=auto NODE_PATH=... node scripts/g1_task_suite_e2e.mjs
 *
 * 产物：
 *   reports/g1/g1_<timestamp>.json
 *   reports/g1/g1_<timestamp>.md
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
const PROVIDER = process.env.G1_PROVIDER || 'mock'
const MODEL = process.env.G1_MODEL || 'auto'
const AI_BASE = (process.env.G1_AI_URL || 'http://127.0.0.1:8100').replace(
  /\/$/,
  '',
)
const RUN_TIMEOUT_MS = Number(process.env.G1_TASK_TIMEOUT_MS || 8 * 60 * 1000)
const RUN_STAMP = new Date()
  .toISOString()
  .replace(/[:.]/g, '-')
  .replace('Z', '')
const RUN_DIR = path.join(
  process.cwd(),
  'reports',
  'g1',
  'runs',
  `${RUN_STAMP}-${PROVIDER}`,
)
const COMPAT_STAMP = new Date().toISOString().replace(/[:.]/g, '-')

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true })
  return dir
}

function writeJson(file, payload) {
  ensureDir(path.dirname(file))
  fs.writeFileSync(file, JSON.stringify(payload, null, 2))
}

async function fetchJson(url) {
  try {
    const response = await fetch(url)
    if (!response.ok) return null
    return await response.json()
  } catch {
    return null
  }
}

const TASKS = [
  {
    id: 'todo',
    name: '个人 ToDo 任务流',
    goal:
      '做一个个人 ToDo 任务流：任务列表、创建任务、状态切换、按状态筛选；' +
      '核心实体 Task(id, title, status, priority, dueDate, createdAt)；' +
      '数据持久化到 MySQL。',
    entities:
      'Task(id, title, status, priority, dueDate, createdAt)',
    flow:
      '创建任务 -> 修改状态 -> 按状态筛选 -> 完成/归档',
  },
  {
    id: 'blog',
    name: '个人博客',
    goal:
      '做一个个人博客：文章列表、文章详情、新建文章、标签筛选；' +
      '核心实体 Post(id, title, content, tags, createdAt)；' +
      '数据持久化到 MySQL。',
    entities: 'Post(id, title, content, tags, createdAt)',
    flow: '新建文章 -> 编辑内容 -> 发布 -> 按标签浏览',
  },
  {
    id: 'crm',
    name: 'CRM 客户查询页',
    goal:
      '做一个 CRM 客户查询页：客户列表、关键词搜索、按状态筛选、客户详情；' +
      '核心实体 Customer(id, name, company, status, owner, createdAt)；' +
      '数据持久化到 MySQL。',
    entities: 'Customer(id, name, company, status, owner, createdAt)',
    flow: '搜索客户 -> 按状态筛选 -> 打开详情 -> 更新跟进状态',
  },
  {
    id: 'inventory',
    name: '库存管理',
    goal:
      '做一个库存管理：商品列表、入库/出库、低库存预警、库存流水；' +
      '核心实体 Product(id, name, sku, stock, threshold)、' +
      'StockMovement(id, productId, type, quantity, createdAt)；' +
      '数据持久化到 MySQL。',
    entities:
      'Product(id, name, sku, stock, threshold)；' +
      'StockMovement(id, productId, type, quantity, createdAt)',
    flow: '新增商品 -> 入库 -> 出库 -> 低库存预警 -> 查看流水',
  },
  {
    id: 'expense',
    name: '报销审批',
    goal:
      '做一个报销审批：报销单列表、提交报销、审批通过/驳回、按状态筛选；' +
      '核心实体 Expense(id, title, amount, applicant, status, createdAt)、' +
      'Approval(id, expenseId, reviewer, decision, comment)；' +
      '数据持久化到 MySQL。',
    entities:
      'Expense(id, title, amount, applicant, status, createdAt)；' +
      'Approval(id, expenseId, reviewer, decision, comment)',
    flow: '提交报销 -> 审批通过/驳回 -> 状态筛选 -> 查看审批记录',
  },
]

const ONLY_TASKS = String(process.env.G1_TASKS || '')
  .split(',')
  .map((item) => item.trim())
  .filter(Boolean)
const SELECTED_TASKS = (
  ONLY_TASKS.length
    ? TASKS.filter((task) => ONLY_TASKS.includes(task.id))
    : TASKS
).map((task) => ({
  requiresBackend: /MySQL|数据库|后端|API/i.test(task.goal),
  requiresDatabase: /MySQL|数据库/i.test(task.goal),
  ...task,
}))

const log = (...args) => console.log('[g1]', ...args)

async function answerClarification(panel, task) {
  const form = panel.locator('.architect-clarification')
  const appeared = await form
    .waitFor({ state: 'visible', timeout: 30000 })
    .then(() => true)
    .catch(() => false)
  if (!appeared) return false
  try {
    const groups = form.locator('fieldset.architect-question')
    const count = await groups.count()
    for (let index = 0; index < count; index += 1) {
      const group = groups.nth(index)
      const pills = group.locator('.choice-pills button')
      if ((await pills.count().catch(() => 0)) === 0) continue
      const selected = await group
        .locator('.choice-pills button.is-selected')
        .count()
        .catch(() => 0)
      if (selected === 0) {
        const pill = pills.first()
        if (await pill.isVisible().catch(() => false)) {
          await pill.click({ timeout: 5000 }).catch(() => {})
        }
      }
    }
    const areas = form.locator(
      'label.architect-question:not(.architect-comment) textarea',
    )
    const areaCount = await areas.count().catch(() => 0)
    for (let index = 0; index < areaCount; index += 1) {
      const area = areas.nth(index)
      const label = await area
        .locator('xpath=..')
        .innerText()
        .catch(() => '')
      if (label.includes('数据实体')) {
        await area.fill(task.entities).catch(() => {})
      } else if (label.includes('业务动作') || label.includes('交互')) {
        await area.fill(task.flow).catch(() => {})
      } else {
        await area.fill(task.goal).catch(() => {})
      }
    }
    const submit = form.getByRole('button', { name: '提交回答并继续' })
    if ((await submit.count().catch(() => 0)) === 0) return false
    await submit.click({ timeout: 10000 }).catch(() => {})
    return true
  } catch {
    // 表单可能已被契约 / 下一轮渲染替换，交给外层循环继续判断。
    return false
  }
}

async function driveV0(page, panel, task) {
  const deadline = Date.now() + RUN_TIMEOUT_MS
  for (let round = 0; round < 12; round += 1) {
    if (page.url().includes('/visual-canvas')) return
    const outcome = panel.locator('.architect-outcome', {
      hasText: 'v0 虚拟原型已生成',
    })
    if (await outcome.count()) {
      await page.waitForURL(/\/visual-canvas/, { timeout: 60000 })
      return
    }
    if ((await panel.locator('.architect-outcome.is-error').count()) > 0) {
      throw new Error(`V0 阶段失败: ${(await panel.innerText()).slice(-600)}`)
    }
    const form = panel.locator('.architect-clarification')
    const remaining = Math.max(5000, deadline - Date.now())
    const appeared = await form
      .waitFor({ state: 'visible', timeout: Math.min(60000, remaining) })
      .then(() => true)
      .catch(() => false)
    if (appeared) {
      log(task.id, 'answer V0 clarification', round + 1)
      await answerClarification(panel, task)
      await page.waitForTimeout(800)
      continue
    }
    if (page.url().includes('/visual-canvas')) return
  }
  throw new Error('V0 阶段超过 12 轮仍未产出虚拟画布')
}

async function runTask(browser, task) {
  const context = await browser.newContext({
    viewport: { width: 1600, height: 1000 },
  })
  const page = await context.newPage()
  const errors = []
  page.on('pageerror', (error) => errors.push(String(error)))
  const startedAt = Date.now()
  const taskDir = ensureDir(path.join(RUN_DIR, task.id))
  const record = {
    id: task.id,
    name: task.name,
    provider: PROVIDER,
    model: MODEL,
    requirements: {
      requiresBackend: Boolean(task.requiresBackend),
      requiresDatabase: Boolean(task.requiresDatabase),
    },
    contractSummary: null,
    status: 'FAILED',
    durationMs: 0,
    timeline: {
      startedAt: new Date(startedAt).toISOString(),
      v0ReadyMs: 0,
      contractReadyMs: 0,
      fullstackReadyMs: 0,
      firstUsableMs: 0,
      finishedMs: 0,
    },
    v0Nodes: 0,
    liveTextLength: 0,
    liveTextSample: '',
    previewUrl: '',
    architectRunId: '',
    fullstackRunId: '',
    pageErrors: 0,
    error: '',
    fullstackError: '',
    cost: {
      architect: null,
      fullstack: {
        available: false,
        note: 'FullstackGraph 尚未采集 token usage',
      },
    },
    acceptance: {
      checks: {},
      autoAccepted: false,
      humanReview: 'PENDING',
    },
    rollback: { exercised: false, note: '任务集未触发回滚' },
    artifacts: {
      dir: path.relative(process.cwd(), taskDir),
      screenshot: '',
      record: '',
      liveText: '',
      previewUrl: '',
    },
  }
  page.on('request', (request) => {
    try {
      const url = request.url()
      if (url.includes('/architect/runs/stream')) {
        const body = request.postDataJSON()
        if (body?.runId) record.architectRunId = String(body.runId)
      }
      if (url.includes('/runs/fullstack')) {
        const body = request.postDataJSON()
        if (body?.runId) record.fullstackRunId = String(body.runId)
      }
    } catch {
      // 非 JSON 请求忽略。
    }
  })
  try {
    log(task.id, 'open workspace')
    await page.goto(`${WEB}/workspace`, { waitUntil: 'domcontentloaded' })
    const demo = page.getByRole('button', { name: '一键体验（推荐）' })
    await demo.waitFor({ state: 'visible', timeout: 15000 }).catch(() => {})
    if (await demo.isVisible().catch(() => false)) await demo.click()
    await page.locator('.project-switcher-button').waitFor({ timeout: 60000 })

    log(task.id, 'create blank project')
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
    await panel.locator('[data-architect-goal]').fill(task.goal)
    await panel
      .locator('[data-architect-quality-tier] input[value="L2_STANDARD"]')
      .check({ force: true })
    await panel.locator('[data-architect-start]').click()

    log(task.id, 'select solution')
    const solutionList = panel.locator('[data-architect-solutions]')
    const solutionSkip = panel.locator('[data-architect-solution-skip]')
    await solutionList
      .or(solutionSkip)
      .first()
      .waitFor({ state: 'visible', timeout: 150000 })
    if (await solutionList.isVisible().catch(() => false)) {
      const option = panel.locator('[data-architect-solution-option]').first()
      await option.waitFor({ state: 'visible', timeout: 60000 })
      await option.click()
      await panel.locator('[data-architect-solution-confirm]').click()
    } else {
      await solutionSkip.click()
    }

    log(task.id, 'drive V0')
    await driveV0(page, panel, task)

    const nodes = page.locator('.vc-node')
    await nodes.first().waitFor({ state: 'visible', timeout: 60000 })
    record.v0Nodes = await nodes.count()
    record.timeline.v0ReadyMs = Date.now() - startedAt
    log(task.id, 'freeze', { v0Nodes: record.v0Nodes })
    await page
      .getByRole('button', { name: /确认视觉，进入业务审查/ })
      .click()
    await page.waitForURL(/\/workspace/, { timeout: 60000 })

    log(task.id, 'backend binding')
    await panel.waitFor({ state: 'visible', timeout: 60000 })
    await page.waitForTimeout(800)
    const bindingClarification = panel.locator('.architect-clarification')
    const bindingContract = panel.locator('.contract-viewer')
    await bindingClarification
      .or(bindingContract)
      .first()
      .waitFor({ state: 'visible', timeout: 90000 })
    const bindingContractVisible = await bindingContract
      .isVisible()
      .catch(() => false)
    if (
      !bindingContractVisible &&
      (await bindingClarification.isVisible().catch(() => false))
    ) {
      await answerClarification(panel, task)
    }

    let contractReady = false
    for (let round = 0; round < 90; round += 1) {
      if (
        await panel
          .locator('.contract-viewer')
          .first()
          .isVisible()
          .catch(() => false)
      ) {
        contractReady = true
        break
      }
      if ((await panel.locator('.architect-outcome.is-error').count()) > 0) {
        throw new Error(`契约阶段失败: ${(await panel.innerText()).slice(-600)}`)
      }
      const form = panel.locator('.architect-clarification')
      if (await form.isVisible().catch(() => false)) {
        await answerClarification(panel, task)
        await page.waitForTimeout(1000)
        continue
      }
      await page.waitForTimeout(2000)
    }
    if (!contractReady) throw new Error('契约未在时限内就绪')

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
    record.timeline.contractReadyMs = Date.now() - startedAt

    log(task.id, 'run CodeGen/fullstack')
    const generate = panel.locator('[data-visual-codegen-generate]')
    await generate.waitFor({ state: 'visible', timeout: 30000 })
    await generate.click()
    await panel
      .locator(
        '[data-visual-codegen-status="SUCCEEDED"], [data-visual-codegen-status="FAILED"]',
      )
      .first()
      .waitFor({ timeout: 480000 })
    const codegenStatus = await panel
      .locator('[data-visual-codegen-status]')
      .first()
      .getAttribute('data-visual-codegen-status')
    if (codegenStatus !== 'SUCCEEDED') {
      const panelText = await panel.innerText()
      record.fullstackError = panelText.slice(-900)
      throw new Error(`代码生成失败(${codegenStatus}): ${panelText.slice(-600)}`)
    }
    record.timeline.fullstackReadyMs = Date.now() - startedAt
    await panel.getByRole('button', { name: '关闭架构师面板' }).click()

    const liveFrame = active.locator('.canvas-live-frame')
    await liveFrame.waitFor({ state: 'visible', timeout: 60000 })
    const frame = page.frameLocator('.tab-window.is-active .canvas-live-frame')
    await frame.locator('body').waitFor({ state: 'visible', timeout: 60000 })
    const text = await frame.locator('body').innerText()
    record.liveTextLength = text.trim().length
    record.liveTextSample = text.trim().slice(0, 300)
    record.previewUrl = await active
      .locator('.canvas-live-frame')
      .getAttribute('src')
      .catch(() => '')
    record.timeline.firstUsableMs = Date.now() - startedAt
    if (record.liveTextLength < 20) throw new Error('live 画布正文为空')
    if (errors.length) throw new Error(`页面错误: ${errors.join(' | ')}`)

    record.status = 'PASSED'
  } catch (error) {
    record.error = String(error?.message || error).slice(0, 800)
  } finally {
    record.durationMs = Date.now() - startedAt
    record.timeline.finishedMs = record.durationMs
    record.pageErrors = errors.length
    const screenshotPath = path.join(taskDir, 'live.png')
    try {
      await page.screenshot({ path: screenshotPath, fullPage: false })
      record.artifacts.screenshot = path.relative(
        process.cwd(),
        screenshotPath,
      )
    } catch {
      // 失败任务可能没有可截图的页面。
    }
    if (record.architectRunId) {
      const snapshot = await fetchJson(
        `${AI_BASE}/api/v1/internal/architect/runs/${record.architectRunId}`,
      )
      if (snapshot?.tokenUsage) {
        record.cost.architect = snapshot.tokenUsage
      }
      if (snapshot) {
        const contract = snapshot.contract || {}
        const ddl = String(contract.databaseDdl || '')
        record.contractSummary = {
          appShape: String(snapshot.appShape || ''),
          qualityTier: String(
            snapshot.qualityTier || contract.qualityTier || '',
          ),
          apiRoutes: (contract.apiRoutes || []).length,
          tables: (ddl.match(/CREATE TABLE/gi) || []).length,
          components: (contract.components || []).length,
          frontendRoutes: (contract.frontendRoutes || []).length,
        }
      }
    }
    const summary = record.contractSummary || {}
    record.acceptance.checks = {
      v0Rendered: record.v0Nodes >= 1,
      contractReady: record.timeline.contractReadyMs > 0,
      backendGenerated: record.requirements.requiresBackend
        ? summary.appShape === 'full_stack_system'
        : true,
      databaseGenerated: record.requirements.requiresDatabase
        ? Number(summary.tables || 0) > 0
        : true,
      fullstackSucceeded: record.timeline.fullstackReadyMs > 0,
      liveUsable: record.liveTextLength >= 20,
      noPageErrors: errors.length === 0,
    }
    record.acceptance.autoAccepted =
      record.status === 'PASSED' &&
      Object.values(record.acceptance.checks).every(Boolean)
    const liveTextPath = path.join(taskDir, 'live-text.txt')
    fs.writeFileSync(liveTextPath, record.liveTextSample || '')
    record.artifacts.liveText = path.relative(process.cwd(), liveTextPath)
    const previewPath = path.join(taskDir, 'preview-url.txt')
    fs.writeFileSync(previewPath, record.previewUrl || '')
    record.artifacts.previewUrl = path.relative(process.cwd(), previewPath)
    const recordPath = path.join(taskDir, 'record.json')
    record.artifacts.record = path.relative(process.cwd(), recordPath)
    writeJson(recordPath, record)
    await context.close().catch(() => {})
    log(task.id, record.status, {
      durationMs: record.durationMs,
      firstUsableMs: record.timeline.firstUsableMs,
      v0Nodes: record.v0Nodes,
      liveTextLength: record.liveTextLength,
      autoAccepted: record.acceptance.autoAccepted,
      error: record.error || undefined,
    })
  }
  return record
}

function percentile(values, p) {
  if (!values.length) return 0
  const sorted = [...values].sort((a, b) => a - b)
  return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * p))]
}

function writeReport(records) {
  const outDir = ensureDir(path.join(process.cwd(), 'reports', 'g1'))
  ensureDir(RUN_DIR)
  const passed = records.filter((record) => record.status === 'PASSED')
  const durations = passed.map((record) => record.durationMs)
  const firstUsable = passed
    .map((record) => record.timeline?.firstUsableMs || 0)
    .filter((value) => value > 0)
  const autoAccepted = records.filter(
    (record) => record.acceptance?.autoAccepted,
  ).length
  const architectCosts = records
    .map((record) => Number(record.cost?.architect?.cost))
    .filter((value) => Number.isFinite(value))
  const summary = {
    generatedAt: new Date().toISOString(),
    provider: PROVIDER,
    model: MODEL,
    taskSetVersion: 'v2-5tasks',
    runDir: path.relative(process.cwd(), RUN_DIR),
    total: records.length,
    passed: passed.length,
    successRate: records.length ? passed.length / records.length : 0,
    autoAcceptRate: records.length ? autoAccepted / records.length : 0,
    humanAcceptance: 'PENDING',
    rollbackRate: 'NOT_EXERCISED',
    p50DurationMs: percentile(durations, 0.5),
    p95DurationMs: percentile(durations, 0.95),
    firstUsableP50Ms: percentile(firstUsable, 0.5),
    firstUsableP95Ms: percentile(firstUsable, 0.95),
    architectCostTotal:
      architectCosts.length > 0
        ? Number(architectCosts.reduce((sum, value) => sum + value, 0).toFixed(6))
        : null,
    architectCostAvg:
      architectCosts.length > 0
        ? Number(
            (
              architectCosts.reduce((sum, value) => sum + value, 0) /
              architectCosts.length
            ).toFixed(6),
          )
        : null,
    records,
  }
  const summaryJson = path.join(RUN_DIR, 'summary.json')
  writeJson(summaryJson, summary)
  const lines = [
    '# G1 任务集报告',
    '',
    `- 生成时间：${summary.generatedAt}`,
    `- Provider / Model：${summary.provider} / ${summary.model}`,
    `- 任务集：${summary.taskSetVersion}`,
    `- 成功率：**${summary.passed}/${summary.total}**`,
    `- 自动验收率：**${autoAccepted}/${summary.total}**（人工接受率待复核）`,
    `- 回滚率：未触发（任务集不执行回滚）`,
    `- P50 / P95 耗时：${(summary.p50DurationMs / 1000).toFixed(1)}s / ${(summary.p95DurationMs / 1000).toFixed(1)}s`,
    `- 首次可用 P50 / P95：${(summary.firstUsableP50Ms / 1000).toFixed(1)}s / ${(summary.firstUsableP95Ms / 1000).toFixed(1)}s`,
    `- Architect 成本合计 / 平均：${
      summary.architectCostTotal === null
        ? '未采集'
        : `${summary.architectCostTotal} / ${summary.architectCostAvg}`
    }`,
    `- 原始产物：\`${summary.runDir}\``,
    '',
    '| 任务 | 结果 | 形态 / 表 | 耗时 | 首次可用 | V0 节点 | live 文本 | 自动验收 | 成本 | 截图 | 错误 |',
    '|---|---|---|---:|---:|---:|---:|---|---:|---|---|',
    ...records.map(
      (record) =>
        `| ${record.name} | ${record.status} | ${record.contractSummary?.appShape || '-'} / ${record.contractSummary?.tables ?? '-'} | ${(record.durationMs / 1000).toFixed(1)}s | ${((record.timeline?.firstUsableMs || 0) / 1000).toFixed(1)}s | ${record.v0Nodes} | ${record.liveTextLength} | ${record.acceptance?.autoAccepted ? '✅' : '❌'} | ${record.cost?.architect?.cost ?? '-'} | ${record.artifacts?.screenshot ? `\`${record.artifacts.screenshot}\`` : '-'} | ${record.error ? record.error.slice(0, 120) : '-'} |`,
    ),
    '',
    '> 人工接受率、回滚率需要人工按截图 / 预览复核；本报告不伪造这两项。',
  ]
  const summaryMd = path.join(RUN_DIR, 'summary.md')
  fs.writeFileSync(summaryMd, lines.join('\n'))
  // 兼容既有 platform_eval 的 g1_glob：额外写一份顶层报告。
  const compatJson = path.join(outDir, `g1_${COMPAT_STAMP}.json`)
  const compatMd = path.join(outDir, `g1_${COMPAT_STAMP}.md`)
  fs.writeFileSync(compatJson, JSON.stringify(summary, null, 2))
  fs.writeFileSync(compatMd, lines.join('\n'))
  return {
    jsonPath: summaryJson,
    mdPath: summaryMd,
    compatJson,
    compatMd,
    summary,
  }
}

async function main() {
  const browser = await chromium.launch({
    executablePath: CHROME,
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  })
  const records = []
  try {
    for (const task of SELECTED_TASKS) {
      records.push(await runTask(browser, task))
    }
  } finally {
    await browser.close()
  }
  const report = writeReport(records)
  log('report', report.mdPath)
  log('compat', report.compatMd)
  log('summary', {
    passed: report.summary.passed,
    total: report.summary.total,
    successRate: report.summary.successRate,
    autoAcceptRate: report.summary.autoAcceptRate,
    p50ms: report.summary.p50DurationMs,
    p95ms: report.summary.p95DurationMs,
    firstUsableP50ms: report.summary.firstUsableP50Ms,
    architectCostTotal: report.summary.architectCostTotal,
  })
}

main().catch((error) => {
  console.error('[g1] FAIL', error?.stack || error)
  process.exit(1)
})
