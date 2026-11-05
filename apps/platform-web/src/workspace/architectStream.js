/**
 * Architect SSE 客户端。
 *
 * 与局部 UI Run 的客户端结构一致，但使用独立命名空间与暂停 / 终态集合：
 *  - clarification.required / contract.ready 会结束当前连接；
 *  - 用户回答或决策后，通过 afterSeq 重放继续同一条时间轴。
 */

const ARCHITECT_TERMINAL_EVENTS = new Set([
  'architect.clarification.required',
  'architect.contract.ready',
  'architect.contract.confirmed',
  'architect.cancelled',
  'architect.failed',
])

const RECONNECT_LIMIT = 6

function abortError() {
  return new DOMException('Aborted', 'AbortError')
}

async function responseError(response) {
  let body = null
  try {
    body = await response.json()
  } catch {
    // 非 JSON 错误体
  }
  const detail = body?.detail
  if (typeof detail === 'string') return detail
  if (detail?.message) return `${detail.code || response.status} · ${detail.message}`
  if (body?.message) return `${body.code || response.status} · ${body.message}`
  return `HTTP ${response.status} ${response.statusText || ''}`.trim()
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
  afterSeq = 0,
  token,
  signal,
  onEvent,
}) {
  let lastSeq = Math.max(0, afterSeq)
  const resolvedRunId = runId || payload?.runId
  let terminal = false
  let mode = replay ? 'replay' : 'start'
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
        response = await fetch(
          `/api/v1/architect/runs/${encodeURIComponent(resolvedRunId)}/events/stream${query}`,
          {
            method: 'GET',
            headers: { ...headers, 'Last-Event-ID': String(lastSeq) },
            signal,
          },
        )
      }

      if (!response.ok) {
        const error = new Error(await responseError(response))
        error.httpStatus = response.status
        throw error
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
  token,
) {
  const response = await fetch(
    `/api/v1/architect/runs/${encodeURIComponent(runId)}/contracts/${encodeURIComponent(contractId)}/decision`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token ?? ''}`,
      },
      body: JSON.stringify({ contractId, decision, comment: comment || undefined }),
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

export async function fetchArchitectAnswers(
  runId,
  clarificationId,
  answers,
  comment,
  token,
) {
  const response = await fetch(
    `/api/v1/architect/runs/${encodeURIComponent(runId)}/clarifications/${encodeURIComponent(clarificationId)}/answers`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token ?? ''}`,
      },
      body: JSON.stringify({ answers, comment: comment || undefined }),
    },
  )
  if (!response.ok) {
    throw new Error(await responseError(response))
  }
  return response.json()
}
