/**
 * SSE 客户端：fetch + ReadableStream。
 *
 * 原生 EventSource 不支持 POST body 与 Authorization header，因此启动 Run
 * 必须使用 fetch。连接中断时记录 lastSeq，通过 GET 事件流接口重放补洞。
 */

const TERMINAL_EVENTS = new Set([
  'run.succeeded',
  'run.failed',
  'run.cancelled',
  'run.rejected',
  'clarification.required',
  // 审批暂停同样结束当前连接；决策后用 afterSeq 重放继续。
  'approval.required',
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

/**
 * 解析一个 SSE 帧。
 * 返回 null 表示心跳 / retry 帧，不需要 dispatch。
 */
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
      // SSE 帧以空行分隔；兼容 CRLF。
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

/**
 * 可中止的退避等待：AbortController 触发时立即清理定时器并 reject，
 * 避免浮层关闭后仍残留 setTimeout 并在旧请求链上继续重连。
 */
const delay = (ms, signal) =>
  new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortError())
      return
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    const onAbort = () => {
      clearTimeout(timer)
      reject(abortError())
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })

/**
 * 启动或恢复一个 Run 的事件流。
 *
 * @param {object} options
 * @param {object} [options.payload]   POST /runs/stream 的请求体；replay 模式可省略
 * @param {string} [options.runId]     replay 模式的 runId
 * @param {boolean} [options.replay]   true 时直接 GET 重放，不重复启动 Run
 * @param {number} [options.afterSeq]  从该 seq 之后开始
 * @param {string} [options.token]     Bearer token
 * @param {AbortSignal} [options.signal]
 * @param {(event: object) => void} options.onEvent
 * @returns {Promise<number>} 最后收到的 seq
 */
export async function streamRun({
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
  // attach：Run 已由 Supervisor 在服务端启动，前端只订阅其 SSE，绝不再 POST
  // 启动接口，避免重复创建沙盒。
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
        response = await fetch('/api/v1/runs/stream', {
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
            : `/api/v1/runs/${encodeURIComponent(resolvedRunId)}/events/stream`
        response = await fetch(
          `${readPath}${query}`,
          { method: 'GET', headers: { ...headers, 'Last-Event-ID': String(lastSeq) }, signal },
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
            // 未知帧忽略，客户端必须兼容新增字段 / 事件。
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
      // 连接正常结束但没收到终态：可能是网关超时，立即走重放补洞。
    } catch (cause) {
      if (signal?.aborted || cause?.name === 'AbortError') {
        throw abortError()
      }
      // 首次 POST 的 4xx（缺密钥 / 参数错误）直接抛给 UI，不重试。
      // 5xx / 网络错误则允许用同一 runId 重放，服务端可能已经启动。
      if (cause?.httpStatus && cause.httpStatus < 500) {
        throw cause
      }
    }

    attempts += 1
    if (attempts > RECONNECT_LIMIT) {
      throw new Error('事件流多次重连失败，请稍后重试')
    }
    mode = 'replay'
    // 500ms 起步，指数退避并封顶 8s；signal 中止时立即清理定时器。
    await delay(Math.min(8000, 500 * 2 ** (attempts - 1)), signal)
  }
  throw abortError()
}
