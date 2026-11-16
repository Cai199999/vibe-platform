/**
 * 离线画布局部 VisualNode Agent 客户端。
 *
 * 只请求结构化节点，不触发任何源码 / 沙盒接口。
 */

async function readError(response) {
  let body = null
  try {
    body = await response.json()
  } catch {
    // 非 JSON 错误体
  }
  const detail = body?.detail
  const code = detail?.code || body?.code || response.status
  const message =
    detail?.message || body?.userMessage || body?.message || response.statusText
  return { code, message, detail }
}

export async function generateVisualNodes(
  {
    projectId,
    documentId = '',
    nodeId = '',
    instruction,
    context,
    existingIds = [],
    maxNodes = 40,
  },
  token,
) {
  const response = await fetch('/api/v1/visual-agent/nodes', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token ?? ''}`,
    },
    body: JSON.stringify({
      projectId,
      documentId,
      nodeId,
      instruction,
      context,
      existingIds,
      maxNodes,
    }),
  })
  if (!response.ok) {
    const info = await readError(response)
    const error = new Error(`${info.code} · ${info.message || '生成失败'}`)
    error.code = info.code
    error.detail = info.detail
    error.httpStatus = response.status
    throw error
  }
  return response.json()
}
