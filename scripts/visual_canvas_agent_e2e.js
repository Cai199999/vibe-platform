#!/usr/bin/env node
/**
 * 离线画布「AI 生成 + 冻结 + Architect 业务审查」真实 E2E。
 *
 * 前置：
 *   - platform-web (5173)、java-bff (8080)、python-ai (8100) 已启动；
 *   - python-ai 配置真实 DEEPSEEK_API_KEY；
 *   - 本机有 Playwright Chromium（可用 CHROME_BIN 覆盖）。
 *
 * 运行：
 *   node scripts/visual_canvas_agent_e2e.js
 *
 * 本脚本只驱动真实浏览器与真实接口，不 mock 网络。
 */

const { spawn } = require('node:child_process')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const WebSocket = require(path.join(
  __dirname,
  '../apps/platform-web/node_modules/ws',
))

const APP_BASE = process.env.APP_BASE || 'http://127.0.0.1:5173'
const DEBUG_PORT = Number(process.env.CDP_PORT || 9333)
const CHROME_BIN =
  process.env.CHROME_BIN ||
  path.join(
    os.homedir(),
    '.cache/ms-playwright/chromium-1254/chrome-linux64/chrome',
  )
const AGENT_TIMEOUT_MS = Number(process.env.VISUAL_AGENT_TIMEOUT_MS || 45000)
const PROJECT_ID = 'proj_visual_agent_e2e'
const TITLE = '离线画布 E2E'

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

function assert(condition, message) {
  if (!condition) throw new Error(message)
}

async function waitFor(fn, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const value = await fn()
    if (value) return value
    await sleep(250)
  }
  throw new Error(`等待超时: ${label}`)
}

async function connectCdp() {
  let target = null
  for (let index = 0; index < 40; index += 1) {
    try {
      const targets = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json`)).json()
      target = targets.find((item) => item.type === 'page')
      if (target) break
    } catch {
      // Chrome 尚未就绪
    }
    await sleep(250)
  }
  if (!target) throw new Error('无法连接 Chromium CDP')
  const ws = new WebSocket(target.webSocketDebuggerUrl, {
    maxPayload: 30 * 1024 * 1024,
  })
  let id = 0
  const pending = new Map()
  const requests = []
  ws.on('message', (raw) => {
    const message = JSON.parse(raw)
    if (message.id && pending.has(message.id)) {
      pending.get(message.id)(message)
      pending.delete(message.id)
    }
    if (message.method === 'Network.requestWillBeSent') {
      requests.push(message.params.request.url)
    }
  })
  await new Promise((resolve, reject) => {
    ws.on('open', resolve)
    ws.on('error', reject)
  })
  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const mid = ++id
      pending.set(mid, (message) =>
        message.error
          ? reject(new Error(JSON.stringify(message.error)))
          : resolve(message.result),
      )
      ws.send(JSON.stringify({ id: mid, method, params }))
    })
  const evaluate = async (expression) => {
    const result = await send('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    })
    if (result.exceptionDetails) {
      throw new Error(
        result.exceptionDetails.exception?.description ||
          result.exceptionDetails.text,
      )
    }
    return result.result.value
  }
  await send('Page.enable')
  await send('Runtime.enable')
  await send('Network.enable')
  await send('Emulation.setDeviceMetricsOverride', {
    width: 1440,
    height: 1000,
    deviceScaleFactor: 1,
    mobile: false,
  })
  return { send, evaluate, requests, close: () => ws.close() }
}

async function main() {
  if (!fs.existsSync(CHROME_BIN)) {
    throw new Error(
      `找不到 Chromium: ${CHROME_BIN}\n请设置 CHROME_BIN 或安装 Playwright Chromium`,
    )
  }
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'vibe-visual-e2e-'))
  const browser = spawn(
    CHROME_BIN,
    [
      '--headless=new',
      '--no-sandbox',
      '--disable-gpu',
      '--disable-dev-shm-usage',
      `--remote-debugging-port=${DEBUG_PORT}`,
      `--user-data-dir=${profile}`,
      'about:blank',
    ],
    { stdio: 'ignore', detached: true },
  )

  let cdp = null
  try {
    cdp = await connectCdp()
    const { send, evaluate, requests } = cdp
    const project = {
      id: PROJECT_ID,
      name: TITLE,
      template: 'vibe-react',
      templateVersion: 3,
      files: {},
      entryFile: 'src/App.jsx',
      bootstrap: 'ready',
      canvasMode: 'visual',
      generated: false,
      createdAt: new Date().toISOString(),
    }
    const seed = `
localStorage.setItem("vibe.auth.session", JSON.stringify({token:"vibe_mock_e2e", user:{id:"usr_e2e",name:"E2E",email:"e2e@vibe.local",tenantId:"tenant_local",role:"member"}, issuedAt:new Date().toISOString(), mode:"mock"}));
localStorage.setItem("vibe.workspace.v1", JSON.stringify({version:1, projects:[${JSON.stringify(project)}], openTabs:["${PROJECT_ID}"], activeTabId:"${PROJECT_ID}"}));
localStorage.setItem("vibe.llmProvider", "mock");
localStorage.removeItem("vibe.visualDocument.v1");
localStorage.removeItem("vibe.visual.sync.${PROJECT_ID}");
sessionStorage.removeItem("vibe.visual.review.${PROJECT_ID}");
sessionStorage.removeItem("vibe.visual.autoOpened.${PROJECT_ID}.ctr");
`
    const seedScript = await send('Page.addScriptToEvaluateOnNewDocument', {
      source: seed,
    })
    await send('Page.navigate', {
      url: `${APP_BASE}/visual-canvas?project=${PROJECT_ID}&title=${encodeURIComponent(TITLE)}`,
    })
    await waitFor(
      async () => evaluate(`!!document.querySelector('.vc-page')`),
      15000,
      '离线画布加载',
    )

    // 1. 添加空容器
    await evaluate(`(() => {
      const button = [...document.querySelectorAll('.vc-palette-item')]
        .find((item) => item.textContent.includes('容器'));
      button?.click();
      return Boolean(button);
    })()`)
    await waitFor(
      async () => evaluate(`!!document.querySelector('.vc-node-container.is-selected')`),
      5000,
      '添加容器',
    )

    // 2. ✨ AI 生成
    await evaluate(`(() => {
      const button = document.querySelector(
        '.vc-node-container.is-selected .vc-node-actions button[title="✨ AI 生成"]',
      );
      button?.click();
      return Boolean(button);
    })()`)
    await waitFor(
      async () => evaluate(`!!document.querySelector('#vc-magic-prompt')`),
      5000,
      'AI 生成弹窗',
    )
    const prompt = '生成一个包含用户名、密码输入框和登录按钮的表单'
    await evaluate(`(() => {
      const input = document.querySelector('#vc-magic-prompt');
      const setter = Object.getOwnPropertyDescriptor(
        HTMLTextAreaElement.prototype,
        'value',
      ).set;
      setter.call(input, ${JSON.stringify(prompt)});
      input.dispatchEvent(new Event('input', { bubbles: true }));
      return input.value;
    })()`)
    await evaluate(`(() => {
      const button = [...document.querySelectorAll('.vc-magic-actions button')]
        .find((item) => item.textContent.includes('生成并插入'));
      button?.click();
      return Boolean(button);
    })()`)
    await waitFor(
      async () =>
        evaluate(`document.querySelectorAll('.vc-node-input').length >= 2 &&
          document.querySelectorAll('.vc-node-button').length >= 1 &&
          !document.querySelector('#vc-magic-prompt')`),
      AGENT_TIMEOUT_MS,
      'VisualNode Agent 返回并插入画布',
    )
    const schemaText = await evaluate(
      `document.querySelector('.vc-validate')?.textContent || ''`,
    )
    assert(schemaText.includes('Schema 通过'), `Schema 校验未通过: ${schemaText}`)
    await sleep(600)
    const documentSnapshot = await evaluate(`(() => {
      const doc = JSON.parse(localStorage.getItem('vibe.visualDocument.v1') || 'null');
      return JSON.stringify({
        status: doc?.status,
        revision: doc?.revision,
        hasForm: JSON.stringify(doc).includes('"form"'),
        hasJsx: /<\\s*\\/?\\s*[A-Za-z][^>]*>/.test(JSON.stringify(doc)),
      });
    })()`)
    const snapshot = JSON.parse(documentSnapshot)
    assert(snapshot.hasForm, 'Agent 节点未写入 VisualDocument')
    assert(!snapshot.hasJsx, 'VisualDocument 中出现 JSX/HTML 标记')

    // 3. 冻结并进入业务审查
    await evaluate(`(() => {
      const button = [...document.querySelectorAll('button')]
        .find((item) => item.textContent.includes('确认视觉，进入业务审查'));
      button?.click();
      return Boolean(button);
    })()`)
    await waitFor(
      async () => evaluate(`location.pathname === '/workspace'`),
      15000,
      '冻结后跳转工作台',
    )
    await waitFor(
      async () =>
        evaluate(`document.body.innerText.includes('鉴权能力') &&
          !!document.querySelector('.architect-panel')`),
      20000,
      'Architect 语义审查问题',
    )
    await sleep(600)
    const frozen = await evaluate(`(() => {
      const doc = JSON.parse(localStorage.getItem('vibe.visualDocument.v1') || 'null');
      return JSON.stringify({
        status: doc?.status,
        hash: doc?.metadata?.contentHash || '',
        revision: doc?.revision,
      });
    })()`)
    const frozenDoc = JSON.parse(frozen)
    assert(frozenDoc.status === 'FROZEN', '文档未冻结')
    assert(/^sha256:[0-9a-f]{64}$/.test(frozenDoc.hash), 'contentHash 缺失')

    // 4. 重新打开画布：必须只读
    await send('Page.removeScriptToEvaluateOnNewDocument', {
      identifier: seedScript.identifier,
    })
    await send('Page.navigate', {
      url: `${APP_BASE}/visual-canvas?project=${PROJECT_ID}&title=${encodeURIComponent(TITLE)}`,
    })
    await waitFor(
      async () =>
        evaluate(`!!document.querySelector('.vc-frozen-badge')`),
      15000,
      '冻结画布重新加载',
    )
    const locked = await evaluate(`JSON.stringify({
      addDisabled: document.querySelector('.vc-palette-item')?.disabled,
      inspectorDisabled: document.querySelector('.vc-inspector-fieldset')?.disabled,
      forms: document.querySelectorAll('.vc-node-form').length,
    })`)
    const lockedState = JSON.parse(locked)
    assert(lockedState.addDisabled, '冻结后仍可添加组件')
    assert(lockedState.inspectorDisabled, '冻结后属性面板仍可编辑')
    assert(lockedState.forms >= 1, '冻结后生成的表单丢失')

    const forbidden = requests.filter(
      (url) =>
        url.includes('/api/v1/runs') ||
        url.includes('/api/v1/internal/sandbox') ||
        url.includes('/api/v1/direct-ops'),
    )
    assert(
      forbidden.length === 0,
      `出现真实源码/沙盒请求: ${forbidden.join(', ')}`,
    )

    console.log('PASS')
    console.log(
      JSON.stringify(
        {
          agentNodes: {
            inputs: await evaluate(
              `document.querySelectorAll('.vc-node-input').length`,
            ),
            buttons: await evaluate(
              `document.querySelectorAll('.vc-node-button').length`,
            ),
          },
          contentHash: frozenDoc.hash,
          documentRevision: frozenDoc.revision,
          architectReview: true,
          forbiddenRequests: forbidden,
        },
        null,
        2,
      ),
    )
  } finally {
    cdp?.close()
    try {
      process.kill(-browser.pid, 'SIGKILL')
    } catch {
      browser.kill('SIGKILL')
    }
    fs.rmSync(profile, { recursive: true, force: true })
  }
}

main().catch((error) => {
  console.error('E2E FAIL:', error.message)
  process.exit(1)
})
