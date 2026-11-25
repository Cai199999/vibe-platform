/**
 * 多服务生成产品入口客户端。
 *
 * 当前后端使用固定工单蓝图，只允许覆盖应用名；接口返回摘要与报告路径。
 */

export class GeneratedAppsError extends Error {
  constructor(message, { code = '', status = 0 } = {}) {
    super(message)
    this.name = 'GeneratedAppsError'
    this.code = code
    this.status = status
  }
}

async function readBody(response) {
  try {
    return await response.json()
  } catch {
    return null
  }
}

export async function generateMultiService({ name = 'ticket-copilot', token } = {}) {
  const response = await fetch('/api/v1/architect/multi-service/generate', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token ?? ''}`,
    },
    body: JSON.stringify({ name }),
  })
  const body = await readBody(response)
  if (!response.ok) {
    throw new GeneratedAppsError(
      body?.userMessage || body?.message || `多服务生成失败（HTTP ${response.status}）`,
      { code: body?.code || '', status: response.status },
    )
  }
  return body
}

export async function fetchMultiServiceRun(runId, token) {
  const id = typeof runId === 'string' ? runId.trim() : ''
  if (!id) {
    throw new GeneratedAppsError('缺少 runId', { code: 'RUN_ID_REQUIRED' })
  }
  const response = await fetch(
    `/api/v1/architect/multi-service/runs/${encodeURIComponent(id)}`,
    {
      headers: {
        Accept: 'application/json',
        Authorization: `Bearer ${token ?? ''}`,
      },
    },
  )
  const body = await readBody(response)
  if (!response.ok) {
    throw new GeneratedAppsError(
      body?.userMessage || body?.message || `读取生成结果失败（HTTP ${response.status}）`,
      { code: body?.code || '', status: response.status },
    )
  }
  return body
}
