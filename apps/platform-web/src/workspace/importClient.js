/**
 * 项目导入客户端。
 *
 * 浏览器 -> Java BFF（multipart）-> Python AI Core。
 * 不在前端解压 zip：所有安全校验与 npm install 都在服务端沙盒里完成。
 */

async function responseError(response) {
  let body = null
  try {
    body = await response.json()
  } catch {
    // 非 JSON 错误体，走状态码兜底。
  }
  const detail = body?.detail
  const message =
    typeof detail === 'string'
      ? detail
      : detail?.message || body?.message
        ? `${detail?.code || body?.code || response.status} · ${
            detail?.message || body?.message
          }`
        : `HTTP ${response.status} ${response.statusText || ''}`.trim()
  const error = new Error(message)
  error.code = detail?.code || body?.code
  error.httpStatus = response.status
  return error
}

/**
 * 上传 zip / 图片并启动导入沙盒。
 *
 * @param {object} options
 * @param {File} options.file        用户选择的文件
 * @param {string} options.projectId 前端预生成的 projectId（后续 Tab 与 Run 复用）
 * @param {string} [options.token]   登录态 Token
 */
export async function importProjectFile({ file, projectId, token }) {
  const form = new FormData()
  form.append('projectId', projectId)
  form.append('file', file, file.name)

  const response = await fetch('/api/v1/projects/import', {
    method: 'POST',
    // 不设置 Content-Type：浏览器会自动补 multipart boundary。
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: form,
  })
  if (!response.ok) {
    throw await responseError(response)
  }
  return response.json()
}
