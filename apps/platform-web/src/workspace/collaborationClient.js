/**
 * Architect 协作状态客户端。
 *
 * 只读接口：收件箱摘要、动作台账、快速通道授权。
 */

export class CollaborationClientError extends Error {
  constructor(message, { code = '', status = 0 } = {}) {
    super(message)
    this.name = 'CollaborationClientError'
    this.code = code
    this.status = status
  }
}

export async function fetchCollaborationRecord(runId) {
  const id = typeof runId === 'string' ? runId.trim() : ''
  if (!id) {
    throw new CollaborationClientError('缺少 runId', { code: 'RUN_ID_REQUIRED' })
  }
  const response = await fetch(
    `/api/v1/architect/collaboration/runs/${encodeURIComponent(id)}`,
    { headers: { Accept: 'application/json' } },
  )
  let body = null
  try {
    body = await response.json()
  } catch {
    body = null
  }
  if (!response.ok) {
    throw new CollaborationClientError(
      body?.userMessage || body?.message || `协作记录加载失败（HTTP ${response.status}）`,
      { code: body?.code || '', status: response.status },
    )
  }
  return {
    runId: body?.runId || id,
    inbox: Array.isArray(body?.inbox) ? body.inbox : [],
    actions: Array.isArray(body?.actions) ? body.actions : [],
    fastPathGrants: Array.isArray(body?.fastPathGrants)
      ? body.fastPathGrants
      : [],
  }
}
