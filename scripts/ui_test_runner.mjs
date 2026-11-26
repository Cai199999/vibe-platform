#!/usr/bin/env node
/**
 * 轻量 Playwright UI 验收服务。
 *
 * 运行：
 *   NODE_PATH=/path/to/node_modules \
 *   CHROME_BIN=/path/to/chrome \
 *   node scripts/ui_test_runner.mjs
 *
 * TestAgent 通过 POST /run 调用：
 *   { previewUrl, path, username, password, timeoutMs }
 *
 * 断言链：登录 → 打开表单 → 填写 → 提交 → 成功文案 → 回列表刷新后数据仍在。
 * 只返回结构化结果，不修改生成物。
 */
import http from 'node:http'
import { createRequire } from 'node:module'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { randomUUID } from 'node:crypto'

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
const CHROME =
  process.env.CHROME_BIN ||
  path.join(
    os.homedir(),
    '.cache/ms-playwright/chromium-1255/chrome-linux64/chrome',
  )
const PORT = Number(process.env.UI_RUNNER_PORT || 8765)

const SUCCESS_TEXT = /成功|已提交|已创建|已保存|保存成功|提交成功|创建成功/
const SUBMIT_TEXT = /提交|保存|创建|新增|发布|确定|Submit|Create|Save/i
const LOGIN_TEXT = /登录|登入|Login|Sign in/i

async function runScenario(payload) {
  const started = Date.now()
  const previewUrl = String(payload.previewUrl || '').replace(/\/$/, '')
  const formPath = String(payload.path || '/')
  const username = String(payload.username || 'admin')
  const password = String(payload.password || 'admin123')
  const timeoutMs = Math.max(5000, Number(payload.timeoutMs || 20000))
  const marker = `UI自动验收-${Date.now()}`
  const steps = []
  const evidence = { previewUrl, formPath, marker, steps, finalUrl: '' }

  if (!previewUrl) {
    return fail('UI_PREVIEW_MISSING', '缺少预览地址', evidence)
  }

  const browser = await chromium.launch({
    executablePath: CHROME,
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  })
  const context = await browser.newContext()
  const page = await context.newPage()
  page.setDefaultTimeout(Math.min(15000, timeoutMs))
  // Vite 冷启动首次编译可能超过 15s；导航单独放宽。
  page.setDefaultNavigationTimeout(60000)

  try {
    steps.push({ step: 'goto', url: previewUrl })
    await page.goto(previewUrl, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(1200)

    // 登录门禁：只有出现 password 输入框时才登录。
    const passwordInput = page.locator('input[type="password"]').first()
    if (await passwordInput.isVisible().catch(() => false)) {
      steps.push({ step: 'login-form' })
      const textInput = page
        .locator('input:not([type="password"]):not([type="hidden"])')
        .first()
      if (await textInput.isVisible().catch(() => false)) {
        await textInput.fill(username)
      }
      await passwordInput.fill(password)
      const loginButton = page.getByRole('button', { name: LOGIN_TEXT }).first()
      if (await loginButton.isVisible().catch(() => false)) {
        await loginButton.click()
      } else {
        await passwordInput.press('Enter')
      }
      await page.waitForTimeout(1800)
      steps.push({ step: 'login-submitted', url: page.url() })
    }

    if (formPath && formPath !== '/') {
      const target = `${previewUrl}${formPath.startsWith('/') ? '' : '/'}${formPath}`
      steps.push({ step: 'goto-form', url: target })
      await page.goto(target, { waitUntil: 'domcontentloaded' })
      await page.waitForTimeout(1200)
    }

    // 可见表单输入不足 2 个时，视为“列表/搜索页”，优先用 QuickCreate
    // （它与 DataList 绑定同一集合根）；已有完整创建表单时不动它。
    // QuickCreate 与 DataList 绑定同一集合根，字段名直接来自契约 DDL；
    // 有它时优先使用，避免误点搜索/筛选表单或字段名不匹配的旧表单。
    const quickCreate = page.locator('[data-quick-create] button').first()
    if (await quickCreate.isVisible().catch(() => false)) {
      steps.push({ step: 'open-create-modal' })
      await quickCreate.click()
      await page.waitForTimeout(600)
    } else {
      const visibleInputs = page.locator(
        'form input:not([type="password"]):not([type="hidden"]):not([type="submit"]):not([type="button"])',
      )
      if ((await visibleInputs.count().catch(() => 0)) < 2) {
        const createButton = page
          .getByRole('button', { name: /新增|创建|新建|Add|Create|New/i })
          .first()
        if (await createButton.isVisible().catch(() => false)) {
          steps.push({ step: 'open-create-modal' })
          await createButton.click()
          await page.waitForTimeout(600)
        }
      }
    }

    // 页面可能有筛选表单和创建表单：优先选输入框多、文案含“新建/创建”的表单。
    // 打开弹窗后只看弹窗内表单，避免被遮罩后的旧表单抢走点击。
    const modalForms = page.locator('[role="dialog"] form')
    const forms =
      (await modalForms.count().catch(() => 0)) > 0
        ? modalForms
        : page.locator('form')
    const formCount = await forms.count()
    let formIndex = 0
    let bestScore = -1
    for (let index = 0; index < formCount; index += 1) {
      const candidate = forms.nth(index)
      if (!(await candidate.isVisible().catch(() => false))) continue
      const inputs = await candidate
        .locator(
          'input:not([type="password"]):not([type="hidden"]):not([type="submit"]):not([type="button"])',
        )
        .count()
      const text = await candidate.innerText().catch(() => '')
      const score =
        inputs +
        (await candidate
          .getByRole('button', { name: SUBMIT_TEXT })
          .count()
          .catch(() => 0)) *
          2 +
        (/新建|创建|新增|提交|Create|Add/.test(text) ? 5 : 0)
      if (score > bestScore) {
        bestScore = score
        formIndex = index
      }
    }
    const form = forms.nth(formIndex)
    const textInputs = form.locator(
      'input:not([type="password"]):not([type="hidden"]):not([type="submit"]):not([type="button"])',
    )
    const inputCount = await textInputs.count()
    if (inputCount === 0) {
      return await fail(
        'UI_ELEMENT_NOT_FOUND',
        '页面上没有可填写的表单输入框',
        evidence,
      )
    }
    steps.push({ step: 'form-selected', index: formIndex, inputs: inputCount })
    for (let index = 0; index < Math.min(inputCount, 6); index += 1) {
      const input = textInputs.nth(index)
      const hint = [
        await input.getAttribute('placeholder').catch(() => ''),
        await input.getAttribute('name').catch(() => ''),
        await input.getAttribute('aria-label').catch(() => ''),
      ]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
      // 默认所有文本字段都填 marker：不同表单的可见列不同，保证至少
      // 一个持久化字段能在刷新后的列表里被断言到。
      let value = marker
      if (/status|状态/.test(hint)) value = 'todo'
      else if (/priority|优先级/.test(hint)) value = 'high'
      else if (/date|due|时间|日期/.test(hint)) value = '2026-12-31'
      await input.fill(value).catch(() => {})
    }

    let submit = form.getByRole('button', { name: SUBMIT_TEXT }).first()
    if (!(await submit.isVisible().catch(() => false))) {
      submit = page.getByRole('button', { name: SUBMIT_TEXT }).first()
    }
    if (!(await submit.isVisible().catch(() => false))) {
      return await fail(
        'UI_ELEMENT_NOT_FOUND',
        '页面上没有提交/保存按钮',
        evidence,
      )
    }
    steps.push({ step: 'click-submit' })
    await submit.click()
    await page.waitForTimeout(2000)
    evidence.finalUrl = page.url()
    const afterSubmit = (await page.locator('body').innerText().catch(() => '')).slice(
      0,
      2000,
    )
    steps.push({
      step: 'after-submit',
      hasMarker: afterSubmit.includes(marker),
      hasSuccessText: SUCCESS_TEXT.test(afterSubmit),
    })
    if (!afterSubmit.includes(marker) && !SUCCESS_TEXT.test(afterSubmit)) {
      return await fail(
        'UI_CLICK_NO_EFFECT',
        '点击提交后页面没有出现成功提示或新数据',
        evidence,
      )
    }

    // 回首页并刷新，验证数据真的持久化，而不是只在前端内存里。
    steps.push({ step: 'goto-list', url: previewUrl })
    await page.goto(previewUrl, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(1500)
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(1500)
    const afterReload = (
      await page.locator('body').innerText().catch(() => '')
    ).slice(0, 4000)
    steps.push({ step: 'after-reload', hasMarker: afterReload.includes(marker) })
    if (!afterReload.includes(marker)) {
      return await fail(
        'UI_DATA_NOT_PERSISTED',
        '刷新后页面看不到刚提交的数据',
        evidence,
      )
    }

    return {
      status: 'PASSED',
      message: 'UI 表单提交成功，刷新后数据仍在',
      evidence: { ...evidence, durationMs: Date.now() - started },
    }
  } catch (error) {
    return await fail(
      'UI_ASSERTION_FAILED',
      `UI 执行异常: ${error?.message || error}`,
      evidence,
    )
  } finally {
    await browser.close().catch(() => {})
  }

  async function fail(errorCode, message, data) {
    return {
      status: 'FAILED',
      errorCode,
      message,
      evidence: { ...data, durationMs: Date.now() - started },
    }
  }
}

const server = http.createServer(async (req, res) => {
  if (req.method === 'GET' && req.url === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ status: 'UP', runner: 'playwright' }))
    return
  }
  if (req.method !== 'POST' || req.url !== '/run') {
    res.writeHead(404, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ status: 'FAILED', errorCode: 'NOT_FOUND' }))
    return
  }
  let raw = ''
  for await (const chunk of req) raw += chunk
  let payload = {}
  try {
    payload = JSON.parse(raw || '{}')
  } catch {
    payload = {}
  }
  let result
  try {
    result = await runScenario(payload)
  } catch (error) {
    result = {
      status: 'FAILED',
      errorCode: 'UI_RUNNER_ERROR',
      message: String(error?.message || error),
      evidence: { requestId: randomUUID() },
    }
  }
  res.writeHead(200, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify(result))
})

server.listen(PORT, '0.0.0.0', () => {
  console.log(`[ui-runner] listening on :${PORT} chrome=${CHROME}`)
})
