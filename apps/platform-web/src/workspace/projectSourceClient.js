/**
 * 项目源码持久化客户端。
 *
 * BFF 负责把文件树/元数据写入 MySQL，把文件正文写入 MinIO。前端在导入
 * 时先落库再建本地项目；重新打开项目时回灌，保证刷新后仍是存量模式。
 */

export class ProjectSourceError extends Error {
  constructor(message, { code = '', status = 0, retryable = false } = {}) {
    super(message)
    this.name = 'ProjectSourceError'
    this.code = code
    this.status = status
    this.retryable = retryable
  }
}

async function readError(response) {
  let body = null
  try {
    body = await response.json()
  } catch {
    // 非 JSON 错误体
  }
  const message =
    body?.userMessage ||
    body?.message ||
    body?.detail?.message ||
    response.statusText ||
    '源码持久化请求失败'
  return new ProjectSourceError(message, {
    code: body?.code || body?.detail?.code || String(response.status),
    status: response.status,
    retryable: Boolean(body?.retryable),
  })
}

export async function saveProjectSource(
  { projectId, tenantId, projectName, entryFile, sourceName, files },
  token,
  signal,
) {
  if (!projectId) {
    throw new ProjectSourceError('缺少 projectId，无法持久化源码')
  }
  const response = await fetch(
    `/api/v1/project-sources/${encodeURIComponent(projectId)}`,
    {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token ?? ''}`,
      },
      body: JSON.stringify({
        tenantId: tenantId || undefined,
        projectName: projectName || undefined,
        entryFile: entryFile || undefined,
        sourceName: sourceName || undefined,
        files,
      }),
      signal,
    },
  )
  if (!response.ok) {
    throw await readError(response)
  }
  return response.json()
}

export async function loadProjectSource(
  projectId,
  { tenantId = '', token = '', signal } = {},
) {
  if (!projectId) {
    throw new ProjectSourceError('缺少 projectId，无法读取源码')
  }
  const query = tenantId
    ? `?tenantId=${encodeURIComponent(tenantId)}`
    : ''
  const response = await fetch(
    `/api/v1/project-sources/${encodeURIComponent(projectId)}${query}`,
    {
      headers: { Authorization: `Bearer ${token ?? ''}` },
      signal,
    },
  )
  if (!response.ok) {
    throw await readError(response)
  }
  return response.json()
}
