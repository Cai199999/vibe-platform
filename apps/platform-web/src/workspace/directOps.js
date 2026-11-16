/**
 * Direct Ops 客户端：跳过 LLM 的确定性画布操作。
 *
 * Phase 0 只允许 dryRun 验证契约；Phase 1 仅 UPDATE_TEXT 真正落盘。
 * 所有请求都携带 baseRevision，服务端项目写锁 + revision 双重校验。
 */

async function responseError(response) {
  let body = null
  try {
    body = await response.json()
  } catch {
    // 非 JSON 错误体
  }
  const detail = body?.detail
  if (detail?.message) {
    return `${detail.code || response.status} · ${detail.message}`
  }
  if (body?.userMessage) {
    return `${body.code || response.status} · ${body.userMessage}`
  }
  if (body?.message) {
    return `${body.code || response.status} · ${body.message}`
  }
  return `HTTP ${response.status} ${response.statusText || ''}`.trim()
}

export async function fetchDirectRevision(projectId, sandboxId, token) {
  const query = new URLSearchParams({ projectId, sandboxId })
  const response = await fetch(`/api/v1/direct-ops/revision?${query}`, {
    headers: { Authorization: `Bearer ${token ?? ''}` },
  })
  if (!response.ok) {
    throw new Error(await responseError(response))
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
    const error = new Error(await responseError(response))
    error.httpStatus = response.status
    throw error
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
