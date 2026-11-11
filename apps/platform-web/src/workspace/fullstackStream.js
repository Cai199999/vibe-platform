/**
 * 全栈生成 SSE 客户端。
 *
 * 启动接口：POST /api/v1/runs/fullstack（BFF -> Python FullstackGraph）
 * 断线重放：GET  /api/v1/runs/{runId}/events/stream
 *
 * 与前端的 Architect / UI Run 客户端保持同样的 fetch + ReadableStream
 * 实现：原生 EventSource 不支持 POST body 与 Authorization。
 */

const TERMINAL_EVENTS = new Set(['fullstack.completed', 'fullstack.failed'])

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

/**
 * 启动全栈生成并消费 SSE。
 *
 * @param {object} options
 * @param {object} options.payload  POST /runs/fullstack 请求体
 * @param {string} [options.runId]  重连时使用
 * @param {boolean} [options.replay] 直接订阅已有 Run（Supervisor 派发专家时使用）
 * @param {number} [options.afterSeq]
 * @param {string} [options.token]
 * @param {AbortSignal} [options.signal]
 * @param {(event: object) => void} options.onEvent
 * @returns {Promise<number>} 最后收到的 seq
 */
export async function streamFullstack({
  payload,
  runId,
  replay = false,
  afterSeq = 0,
  token,
  signal,
  onEvent,
}) {
  let lastSeq = Math.max(0, afterSeq)
  let terminal = false
  // Supervisor 已经启动了 Fullstack Run，只需要按 runId 订阅；
  // 此时不能再次 POST /fullstack，否则会重复生成一套代码。
  let mode = replay ? 'replay' : 'start'
  let attempts = 0
  const resolvedRunId = runId || payload?.runId

  const headers = {
    Accept: 'text/event-stream',
    Authorization: `Bearer ${token ?? ''}`,
  }

  while (!signal?.aborted && !terminal) {
    try {
      let response
      if (mode === 'start') {
        response = await fetch('/api/v1/runs/fullstack', {
          method: 'POST',
          headers: { ...headers, 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
          signal,
        })
      } else {
        const query = lastSeq > 0 ? `?afterSeq=${lastSeq}` : ''
        response = await fetch(
          `/api/v1/runs/${encodeURIComponent(resolvedRunId)}/events/stream${query}`,
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
          if (TERMINAL_EVENTS.has(parsed.type)) terminal = true
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
      throw new Error('全栈生成事件流多次重连失败，请稍后重试')
    }
    mode = 'replay'
    await delay(Math.min(8000, 500 * 2 ** (attempts - 1)))
  }
  throw abortError()
}
