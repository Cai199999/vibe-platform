/**
 * 存量 React 源码分析客户端。
 *
 * 只请求只读分析报告；不触发 CodeGen、沙盒或任何源码写入。
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
  return { code, message }
}

export async function analyzeExistingReact({ fileName, source }, token) {
  const response = await fetch('/api/v1/source-analysis/analyze', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token ?? ''}`,
    },
    body: JSON.stringify({ fileName, source }),
  })
  if (!response.ok) {
    const info = await readError(response)
    const error = new Error(`${info.code} · ${info.message || '源码分析失败'}`)
    error.code = info.code
    error.httpStatus = response.status
    throw error
  }
  return response.json()
}
