#!/usr/bin/env node
/**
 * 只准备“丰富 V0 画布”的浏览器状态，不冻结、不调模型。
 * 输出 docs/demo/demo-v0-storage.json，供演示时从 V0 开始现场冻结。
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
const DOC_PATH =
  '/home/admin1/Workspace/vibe-platform/docs/demo/gis-visual-document.json'
const STATE_PATH =
  '/home/admin1/Workspace/vibe-platform/docs/demo/demo-v0-storage.json'
const WORKSPACE_PATH =
  '/home/admin1/Workspace/vibe-platform/docs/demo/demo-v0-workspace.json'
const NAME = '测绘任务与成果管理平台'

const log = (...args) => console.log('[gis-v0]', ...args)

const richDoc = JSON.parse(fs.readFileSync(DOC_PATH, 'utf8'))
const browser = await chromium.launch({
  executablePath: CHROME,
  headless: true,
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
})
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } })
const page = await context.newPage()
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

  const projectId = await page.evaluate(() => {
    const raw = window.localStorage.getItem('vibe.workspace.v1')
    return raw ? JSON.parse(raw).activeTabId : null
  })
  if (!projectId) throw new Error('无法读取新建项目 ID')
  log('project id', projectId)

  await page.evaluate(
    ({ projectId, name, doc }) => {
      const key = 'vibe.workspace.v1'
      const state = JSON.parse(window.localStorage.getItem(key))
      state.projects = (state.projects || []).map((project) =>
        project.id === projectId ? { ...project, name } : project,
      )
      window.localStorage.setItem(key, JSON.stringify(state))
      window.localStorage.setItem(
        'vibe.visualDocument.v1',
        JSON.stringify({
          ...doc,
          projectId,
          status: 'DRAFT',
          metadata: {
            ...(doc.metadata || {}),
            contentHash: '',
            createdAt: new Date().toISOString(),
            lastModifiedBy: 'user',
          },
        }),
      )
    },
    { projectId, name: NAME, doc: richDoc },
  )

  await page.goto(
    `${WEB}/visual-canvas?project=${encodeURIComponent(projectId)}&title=${encodeURIComponent(NAME)}`,
    { waitUntil: 'domcontentloaded' },
  )
  await page.locator('.vc-node').first().waitFor({ state: 'visible', timeout: 60000 })
  await page.waitForTimeout(1500)
  await page.screenshot({ path: '/tmp/gis-v0-state.png', fullPage: false })
  const nodeCount = await page.locator('.vc-node').count()
  log('node count', nodeCount)

  await context.storageState({ path: STATE_PATH })
  const workspace = await page.evaluate(() =>
    window.localStorage.getItem('vibe.workspace.v1'),
  )
  if (workspace) fs.writeFileSync(WORKSPACE_PATH, workspace)
  log('saved', { STATE_PATH, projectId, nodeCount })
} catch (error) {
  await page.screenshot({ path: '/tmp/gis-v0-state-fail.png' }).catch(() => {})
  console.error('[gis-v0] FAIL', error?.stack || error)
  process.exitCode = 1
} finally {
  await browser.close()
}
