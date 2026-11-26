/**
 * Architect SSE 客户端。
 *
 * 与局部 UI Run 的客户端结构一致，但使用独立命名空间与暂停 / 终态集合：
 *  - clarification.required / contract.ready 会结束当前连接；
 *  - 用户回答或决策后，通过 afterSeq 重放继续同一条时间轴。
 */

const ARCHITECT_TERMINAL_EVENTS = new Set([
  'architect.clarification.required',
  'architect.contract.conflict',
  'architect.contract.ready',
  'architect.visual_v0.ready',
  'architect.contract.confirmed',
  'architect.cancelled',
  'architect.failed',
])

const RECONNECT_LIMIT = 6

function abortError() {
  return new DOMException('Aborted', 'AbortError')
}

export async function responseError(response, { allowExpired = true } = {}) {
  let body = null
  try {
    body = await response.json()
  } catch {
    // 非 JSON 错误体
  }
  const detail = body?.detail
  const code =
    (detail && typeof detail === 'object' && detail.code) ||
    (typeof body?.code === 'string' ? body.code : null)
  let message
  if (typeof detail === 'string') message = detail
  else if (detail?.message) message = `${detail.code || response.status} · ${detail.message}`
  else if (body?.message) message = `${body.code || response.status} · ${body.message}`
  else message = `HTTP ${response.status} ${response.statusText || ''}`.trim()

  const knownExpired = code === 'ARCHITECT_RUN_NOT_FOUND' || code === 'RUN_NOT_FOUND'
  if (allowExpired && knownExpired) {
    message = '上一次架构会话已过期（AI Core 可能已重启），请重新发起架构分析。'
  }
  const error = new Error(message)
  error.code = code
  error.httpStatus = response.status
  // 启动新 Architect Run 的 404 不是“旧记录过期”，保留真实错误。
  error.runExpired =
    allowExpired && (knownExpired || response.status === 404)
  return error
}

function parseFrame(raw) {
  let id = null
  let event = 'message'
  const dataLines = []
  for (const line of raw.split(/\r?\n/)) {
    if (!line || line.startsWith(':')) continue
    const colon = line.indexOf(':')
    const field = colon >= 0 ? line.slice(0, colon) : line
    let value = colon >= 0 ? line.slice(colon + 1) : ''
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'id') id = value
    else if (field === 'event') event = value
    else if (field === 'data') dataLines.push(value)
  }
  if (dataLines.length === 0) return null
  return { id, event, data: dataLines.join('\n') }
}

async function consumeStream(response, { signal, onRawFrame }) {
  if (!response.body) {
    throw new Error('浏览器不支持流式响应')
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      if (signal?.aborted) throw abortError()
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let boundary = buffer.search(/\r?\n\r?\n/)
      while (boundary >= 0) {
        const raw = buffer.slice(0, boundary)
        const separatorLength = buffer.slice(boundary).startsWith('\r\n\r\n') ? 4 : 2
        buffer = buffer.slice(boundary + separatorLength)
        const frame = parseFrame(raw)
        if (frame) onRawFrame(frame)
        boundary = buffer.search(/\r?\n\r?\n/)
      }
    }
    buffer += decoder.decode()
    const frame = parseFrame(buffer)
    if (frame) onRawFrame(frame)
  } finally {
    reader.releaseLock()
  }
}

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

export async function streamArchitect({
  payload,
  runId,
  replay = false,
  attach = false,
  streamPath = null,
  afterSeq = 0,
  token,
  signal,
  onEvent,
}) {
  let lastSeq = Math.max(0, afterSeq)
  const resolvedRunId = runId || payload?.runId
  let terminal = false
  // attach：Run 已由 Supervisor 启动，前端只订阅，不重复 POST。
  let mode = attach ? 'attach' : replay ? 'replay' : 'start'
  let attempts = 0

  const headers = {
    Accept: 'text/event-stream',
    Authorization: `Bearer ${token ?? ''}`,
  }
  if (lastSeq > 0) headers['Last-Event-ID'] = String(lastSeq)

  while (!signal?.aborted && !terminal) {
    try {
      let response
      if (mode === 'start') {
        response = await fetch('/api/v1/architect/runs/stream', {
          method: 'POST',
          headers: { ...headers, 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
          signal,
        })
      } else {
        const query = lastSeq > 0 ? `?afterSeq=${lastSeq}` : ''
        const readPath =
          mode === 'attach' && streamPath
            ? streamPath
            : `/api/v1/architect/runs/${encodeURIComponent(resolvedRunId)}/events/stream`
        response = await fetch(
          `${readPath}${query}`,
          {
            method: 'GET',
            headers: { ...headers, 'Last-Event-ID': String(lastSeq) },
            signal,
          },
        )
      }

      if (!response.ok) {
        throw await responseError(response, {
          allowExpired: mode !== 'start',
        })
      }

      await consumeStream(response, {
        signal,
        onRawFrame: (frame) => {
          let parsed
          try {
            parsed = JSON.parse(frame.data)
          } catch {
            return
          }
          if (typeof parsed.seq === 'number') {
            lastSeq = Math.max(lastSeq, parsed.seq)
          }
          onEvent?.(parsed)
          if (ARCHITECT_TERMINAL_EVENTS.has(parsed.type)) terminal = true
        },
      })

      if (terminal) return lastSeq
    } catch (cause) {
      if (signal?.aborted || cause?.name === 'AbortError') {
        throw abortError()
      }
      if (cause?.httpStatus && cause.httpStatus < 500) {
        throw cause
      }
    }

    attempts += 1
    if (attempts > RECONNECT_LIMIT) {
      throw new Error('Architect 事件流多次重连失败，请稍后重试')
    }
    mode = 'replay'
    await delay(Math.min(8000, 500 * 2 ** (attempts - 1)))
  }
  throw abortError()
}

export async function fetchArchitectDecision(
  runId,
  contractId,
  decision,
  comment,
  qualityTier,
  knowledgeDocuments,
  token,
  agentPolicy,
) {
  const response = await fetch(
    `/api/v1/architect/runs/${encodeURIComponent(runId)}/contracts/${encodeURIComponent(contractId)}/decision`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token ?? ''}`,
      },
      body: JSON.stringify({
        contractId,
        decision,
        comment: comment || undefined,
        qualityTier: qualityTier || undefined,
        knowledgeDocuments:
          Array.isArray(knowledgeDocuments) && knowledgeDocuments.length
            ? knowledgeDocuments
            : undefined,
        agentPolicy: agentPolicy || undefined,
      }),
    },
  )
  if (!response.ok) {
    throw new Error(await responseError(response))
  }
  return response.json()
}

/**
 * 判断上传文档是长期复用还是一次性。
 *
 * 规则 + LLM 兜底由后端完成；前端最多等 15 秒，超时/失败都回退为
 * ASK，让用户在文档中心里手动决定，不阻塞追加需求。
 */
export async function classifyArchitectDocument({
  name,
  content,
  projectSummary = '',
  llmProvider = 'auto',
  token,
} = {}) {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), 15000)
  try {
    const response = await fetch('/api/v1/architect/documents/classify', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token ?? ''}`,
      },
      body: JSON.stringify({
        name,
        content,
        projectSummary,
        llmProvider,
      }),
      signal: controller.signal,
    })
    if (!response.ok) {
      throw new Error(await responseError(response))
    }
    return await response.json()
  } catch (cause) {
    return {
      decision: 'ASK',
      reuse: 'UNCERTAIN',
      reason: `语义判断暂不可用：${cause?.message || cause}`,
      confidence: 0,
      source: 'rules',
      signals: [],
    }
  } finally {
    window.clearTimeout(timer)
  }
}

/**
 * 澄清前的方案侦察：需求分析 + 联网/本地复用检索 + 自研方案。
 *
 * 只读接口，不创建 Run；用户在面板里选定方案后，再带着
 * solutionChoice 调 architect.start。检索失败时服务端会降级为
 * 自研方案，前端不应把该接口失败当成阻塞错误。
 */
export async function discoverArchitectSolutions({
  message,
  projectId = '',
  projectName = '',
  knowledgeDocuments = [],
  provider = 'auto',
  maxOptions = 3,
  token,
} = {}) {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), 30000)
  try {
    const response = await fetch('/api/v1/architect/solutions/discover', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token ?? ''}`,
      },
      body: JSON.stringify({
        message,
        projectId,
        projectName,
        knowledgeDocuments,
        provider,
        maxOptions,
      }),
      signal: controller.signal,
    })
    if (!response.ok) {
      throw new Error(await responseError(response))
    }
    return await response.json()
  } finally {
    window.clearTimeout(timer)
  }
}

/**
 * 契约命名冲突决策：只提交服务端下发的 optionId。
 *
 * 服务端会校验 optionId 是否属于当前冲突，客户端不能传入自造的
 * 表名或动作，避免绕过冲突选项直接改契约。
 */
export async function fetchArchitectConflictDecision(
  runId,
  conflictId,
  optionId,
  comment,
  token,
) {
  const response = await fetch(
    `/api/v1/architect/runs/${encodeURIComponent(runId)}/contract-conflicts/${encodeURIComponent(conflictId)}/decision`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token ?? ''}`,
      },
      body: JSON.stringify({
        optionId,
        comment: comment || undefined,
      }),
    },
  )
  if (!response.ok) {
    throw new Error(await responseError(response))
  }
  return response.json()
}

export async function fetchArchitectCancel(runId, reason, token) {
  const response = await fetch(
    `/api/v1/architect/runs/${encodeURIComponent(runId)}/cancel`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token ?? ''}`,
      },
      body: JSON.stringify({ reason }),
    },
  )
  if (!response.ok) {
    throw new Error(await responseError(response))
  }
  return response.json()
}

/**
 * 影子助手只读问答。
 *
 * 与 answers / decision 接口完全分离：服务端不会修改主状态机，
 * 因此这里也不需要 SSE，直接等待一次 JSON 响应即可。
 */
export async function fetchArchitectHelper(runId, question, history, token) {
  const response = await fetch(
    `/api/v1/architect/runs/${encodeURIComponent(runId)}/helper`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token ?? ''}`,
      },
      body: JSON.stringify({
        question,
        history: Array.isArray(history) ? history.slice(-12) : [],
        useKnowledgeBase: true,
      }),
    },
  )
  if (!response.ok) {
    throw new Error(await responseError(response))
  }
  return response.json()
}

export async function fetchArchitectAnswers(
  runId,
  clarificationId,
  answers,
  comment,
  token,
  adoptSuggestions = false,
) {
  const response = await fetch(
    `/api/v1/architect/runs/${encodeURIComponent(runId)}/clarifications/${encodeURIComponent(clarificationId)}/answers`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token ?? ''}`,
      },
      body: JSON.stringify({
        answers,
        comment: comment || undefined,
        adoptSuggestions: adoptSuggestions || undefined,
      }),
    },
  )
  if (!response.ok) {
    throw new Error(await responseError(response))
  }
  return response.json()
}
