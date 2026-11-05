/**
 * 管理中台 API 客户端。
 *
 * <p>M3.1 使用 Mock 身份：Authorization 仍携带本地 Token，
 * {@code X-User-Role} 由 AuthContext 中的 Mock 角色提供；BFF 的
 * {@code @IsAdmin} 切面据此返回 200 / 403。</p>
 */

export class AdminApiError extends Error {
  constructor(message, { status, code, requestId } = {}) {
    super(message)
    this.name = 'AdminApiError'
    this.status = status
    this.code = code
    this.requestId = requestId
  }
}

async function readJson(response) {
  try {
    return await response.json()
  } catch {
    return null
  }
}

/**
 * GET /api/v1/admin/metrics
 */
export async function fetchAdminMetrics({ token, role, signal } = {}) {
  const response = await fetch('/api/v1/admin/metrics', {
    method: 'GET',
    headers: {
      Accept: 'application/json',
      Authorization: `Bearer ${token ?? ''}`,
      'X-User-Role': role ?? '',
    },
    signal,
  })

  const body = await readJson(response)
  if (!response.ok) {
    throw new AdminApiError(
      body?.message || `HTTP ${response.status} ${response.statusText || ''}`.trim(),
      {
        status: response.status,
        code: body?.code,
        requestId: body?.requestId,
      },
    )
  }
  return body
}
