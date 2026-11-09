/**
 * 项目快照客户端。
 *
 * 快照完全复用沙盒内的 Git 仓库：commit = 存档，reset --hard = 回滚。
 * 浏览器只调用 BFF 的 /api/v1/sandboxes/{sandboxId}/snapshots 系列接口，
 * 不直接访问 Python internal API。
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
        : body?.message
          ? `${body.code || response.status} · ${body.message}`
          : `HTTP ${response.status} ${response.statusText || ''}`.trim(),
  )
  error.code = detail?.code ?? body?.code
  error.httpStatus = response.status
  return error
}

async function request(path, { token, method = 'GET', body, signal } = {}) {
  const headers = token ? { Authorization: `Bearer ${token}` } : {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  })
  if (!response.ok) throw await responseError(response)
  return response.json()
}

const snapshotsPath = (sandboxId) =>
  `/api/v1/sandboxes/${encodeURIComponent(sandboxId)}/snapshots`

/** 创建快照，返回 { sandboxId, commitHash, shortHash, message, committedAt }。 */
export function createSnapshot(sandboxId, message, token, signal) {
  return request(snapshotsPath(sandboxId), {
    token,
    method: 'POST',
    body: { message },
    signal,
  })
}

/** 按时间倒序列出快照。 */
export function listSnapshots(sandboxId, token, signal) {
  return request(snapshotsPath(sandboxId), { token, signal })
}

/** 硬回滚到指定快照。 */
export function restoreSnapshot(sandboxId, commitHash, token, signal) {
  return request(
    `${snapshotsPath(sandboxId)}/${encodeURIComponent(commitHash)}/restore`,
    { token, method: 'POST', signal },
  )
}

/** 读取快照 Diff；默认 baseline 展示该版本相对初始代码的累积改动。 */
export function fetchSnapshotDiff(sandboxId, commitHash, token, signal, mode = 'baseline') {
  return request(
    `${snapshotsPath(sandboxId)}/${encodeURIComponent(commitHash)}/diff?mode=${encodeURIComponent(mode)}`,
    { token, signal },
  )
}
