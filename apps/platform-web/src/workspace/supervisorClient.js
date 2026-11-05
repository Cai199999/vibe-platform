/**
 * Supervisor 统一调度客户端。
 *
 * 用户点击“发送 / 下一步”时先 route 获取 handoff 语义，再 dispatch 真正启动
 * 子图。子图 SSE 仍由各自客户端按 dispatch 返回的 streamPath 订阅。
 */

async function responseError(response) {
  let body = null
  try {
    body = await response.json()
  } catch {
    // 非 JSON 错误体
  }
  const detail = body?.detail
  const error = new Error(
    typeof detail === 'string'
      ? detail
      : detail?.message
        ? `${detail.code || response.status} · ${detail.message}`
        : `HTTP ${response.status} ${response.statusText || ''}`.trim(),
  )
  // 保留后端错误码，调用方可按 SUPERVISOR_* 精确分支。
  error.code = detail?.code ?? body?.code
  error.httpStatus = response.status
  return error
}

async function postJson(path, payload, token, signal) {
  const response = await fetch(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token ?? ''}`,
    },
    body: JSON.stringify(payload ?? {}),
    signal,
  })
  if (!response.ok) {
    throw await responseError(response)
  }
  return response.json()
}

export async function routeSupervisor(payload, token, signal) {
  return postJson('/api/v1/supervisor/route', payload, token, signal)
}

export async function dispatchSupervisor(payload, token, signal) {
  return postJson('/api/v1/supervisor/dispatch', payload, token, signal)
}

export async function releaseSupervisorGate(gateId, token, signal) {
  return postJson(
    `/api/v1/supervisor/gates/${encodeURIComponent(gateId)}/release`,
    {},
    token,
    signal,
  )
}

export async function fetchSupervisorAgents(token) {
  const response = await fetch('/api/v1/supervisor/agents', {
    headers: { Authorization: `Bearer ${token ?? ''}` },
  })
  if (!response.ok) {
    throw await responseError(response)
  }
  return response.json()
}

export async function fetchSupervisorGate(gateId, token) {
  const response = await fetch(
    `/api/v1/supervisor/gates/${encodeURIComponent(gateId)}`,
    {
      headers: { Authorization: `Bearer ${token ?? ''}` },
    },
  )
  if (!response.ok) {
    throw await responseError(response)
  }
  return response.json()
}
