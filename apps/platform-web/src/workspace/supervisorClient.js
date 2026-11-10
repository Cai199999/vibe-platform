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

async function deleteJson(path, token, signal) {
  const response = await fetch(path, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${token ?? ''}` },
    signal,
  })
  if (!response.ok) {
    throw await responseError(response)
  }
  // 204 或空响应体时返回 null，调用方无需重复判断。
  const text = await response.text()
  return text ? JSON.parse(text) : null
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

export async function createSupervisorAgent(payload, token, signal) {
  return postJson('/api/v1/supervisor/agents', payload, token, signal)
}

export async function deleteSupervisorAgent(agentId, token, signal) {
  return deleteJson(
    `/api/v1/supervisor/agents/${encodeURIComponent(agentId)}`,
    token,
    signal,
  )
}

export async function fetchAgentKnowledge(agentId, token, signal) {
  const response = await fetch(
    `/api/v1/supervisor/agents/${encodeURIComponent(agentId)}/knowledge`,
    {
      headers: { Authorization: `Bearer ${token ?? ''}` },
      signal,
    },
  )
  if (!response.ok) {
    throw await responseError(response)
  }
  return response.json()
}

/**
 * 上传 .md / .txt 到自定义 Agent 的知识库。
 *
 * 文件内容由浏览器读成 UTF-8 文本后以 JSON 提交：BFF 无需维护
 * multipart 转发链路，后端仍会做大小、扩展名与文件名安全校验。
 */
export async function uploadAgentKnowledge(agentId, file, token, signal) {
  if (!file) throw new Error('请选择 .md 或 .txt 知识文件')
  if (!/\.(md|txt)$/i.test(file.name)) {
    throw new Error('仅支持 .md / .txt 文件')
  }
  const content = await file.text()
  if (!content.trim()) throw new Error('知识文件内容为空')
  return postJson(
    `/api/v1/supervisor/agents/${encodeURIComponent(agentId)}/knowledge`,
    { fileName: file.name, content },
    token,
    signal,
  )
}

export async function deleteAgentKnowledge(agentId, fileName, token, signal) {
  return deleteJson(
    `/api/v1/supervisor/agents/${encodeURIComponent(agentId)}/knowledge/${encodeURIComponent(fileName)}`,
    token,
    signal,
  )
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
