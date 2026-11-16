/**
 * Direct Ops 客户端：跳过 LLM 的确定性画布操作。
 *
 * Phase 0 只允许 dryRun 验证契约；Phase 1 仅 UPDATE_TEXT 真正落盘。
 * 所有请求都携带 baseRevision，服务端项目写锁 + revision 双重校验。
 */

async function readApiError(response) {
  let body = null
  try {
    body = await response.json()
  } catch {
    // 非 JSON 错误体
  }
  const detail = body?.detail
  const code = detail?.code || body?.code || String(response.status)
  const message =
    detail?.message || body?.userMessage || body?.message || response.statusText
  let extra = ''
  if (detail && typeof detail === 'object') {
    const parts = Object.entries(detail)
      .filter(([key]) => key !== 'code' && key !== 'message')
      .map(([key, value]) => {
        const text =
          value && typeof value === 'object' ? JSON.stringify(value) : String(value)
        return `${key}=${text}`
      })
    extra = parts.join('; ').slice(0, 500)
  }
  return {
    code,
    detail,
    message: `${code} · ${message || '请求失败'}${extra ? ` · ${extra}` : ''}`,
  }
}

async function throwApiError(response) {
  const info = await readApiError(response)
  const error = new Error(info.message)
  error.code = info.code
  error.detail = info.detail
  error.httpStatus = response.status
  throw error
}

export async function fetchDirectRevision(projectId, sandboxId, token) {
  const query = new URLSearchParams({ projectId, sandboxId })
  const response = await fetch(`/api/v1/direct-ops/revision?${query}`, {
    headers: { Authorization: `Bearer ${token ?? ''}` },
  })
  if (!response.ok) {
    await throwApiError(response)
  }
  return response.json()
}

export async function submitDirectAction(payload, token) {
  const response = await fetch('/api/v1/direct-ops/actions', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token ?? ''}`,
    },
    body: JSON.stringify(payload),
  })
  if (!response.ok) {
    await throwApiError(response)
  }
  return response.json()
}

export async function analyzeDirectDelete(payload, token) {
  const response = await fetch('/api/v1/direct-ops/analyze-delete', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token ?? ''}`,
    },
    body: JSON.stringify(payload),
  })
  if (!response.ok) {
    await throwApiError(response)
  }
  return response.json()
}

let seq = 0

export function nextDirectActionId() {
  seq += 1
  try {
    return `dact_${crypto.randomUUID().replace(/-/g, '').slice(0, 20)}`
  } catch {
    return `dact_${Date.now().toString(36)}${seq}`
  }
}
