/**
 * 项目 / 契约持久化客户端。
 *
 * 失败不阻断本地工作区：离线或 BFF 不可用时继续使用 localStorage，
 * 仅在下一次刷新时尝试重新拉取服务端项目。
 */

async function request(path, { method = 'GET', token, body, signal } = {}) {
  const response = await fetch(path, {
    method,
    headers: {
      Accept: 'application/json',
      ...(body ? { 'Content-Type': 'application/json' } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
    signal,
  })
  let payload = null
  try {
    payload = await response.json()
  } catch {
    // 非 JSON 错误体
  }
  if (!response.ok) {
    const error = new Error(
      payload?.message || `HTTP ${response.status} ${response.statusText || ''}`,
    )
    error.status = response.status
    error.code = payload?.code
    throw error
  }
  return payload
}

export async function fetchProjects({ token, signal } = {}) {
  const payload = await request('/api/v1/projects', { token, signal })
  return Array.isArray(payload?.projects) ? payload.projects : []
}

export async function createProjectRemote(
  project,
  { token, signal } = {},
) {
  const payload = await request('/api/v1/projects', {
    method: 'POST',
    token,
    signal,
    body: {
      id: project?.id,
      name: project?.name || '未命名项目',
      appType:
        project?.kind === 'fullstack' || project?.appType === 'FULLSTACK'
          ? 'FULLSTACK'
          : 'FRONTEND',
      sourceMode:
        project?.imported || project?.template?.startsWith('imported')
          ? 'source'
          : 'visual',
    },
  })
  return payload?.project ?? null
}

export async function updateProjectRemote(
  projectId,
  patch,
  { token, signal } = {},
) {
  const payload = await request(
    `/api/v1/projects/${encodeURIComponent(projectId)}`,
    { method: 'PATCH', token, signal, body: patch },
  )
  return payload?.project ?? null
}

export async function deleteProjectRemote(projectId, { token, signal } = {}) {
  return request(`/api/v1/projects/${encodeURIComponent(projectId)}`, {
    method: 'DELETE',
    token,
    signal,
  })
}

export async function saveProjectContract(
  projectId,
  contract,
  { token, signal, status = 'CONFIRMED' } = {},
) {
  const contractId =
    contract?.contractId || contract?.contract_id || contract?.id
  if (!projectId || !contractId) return null
  const payload = await request(
    `/api/v1/projects/${encodeURIComponent(projectId)}/contracts`,
    {
      method: 'POST',
      token,
      signal,
      body: {
        contractId,
        status,
        payload: contract,
      },
    },
  )
  return payload?.contract ?? null
}

export async function fetchProjectContracts(
  projectId,
  { token, signal } = {},
) {
  const payload = await request(
    `/api/v1/projects/${encodeURIComponent(projectId)}/contracts`,
    { token, signal },
  )
  return Array.isArray(payload?.contracts) ? payload.contracts : []
}
