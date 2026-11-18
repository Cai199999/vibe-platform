import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  loadProjectSource,
  ProjectSourceError,
  saveProjectSource,
} from '../projectSourceClient.js'

describe('项目源码持久化客户端', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('保存时 PUT 全量文件树并携带租户', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ projectId: 'proj_1', revision: 1 }),
    })
    vi.stubGlobal('fetch', fetchMock)

    const result = await saveProjectSource(
      {
        projectId: 'proj_1',
        tenantId: 'tenant_local',
        projectName: '存量项目',
        entryFile: 'src/App.jsx',
        sourceName: 'App.jsx',
        files: { 'src/App.jsx': 'export default 1' },
      },
      'token',
    )

    expect(result.revision).toBe(1)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/project-sources/proj_1')
    expect(init.method).toBe('PUT')
    expect(JSON.parse(init.body)).toMatchObject({
      tenantId: 'tenant_local',
      entryFile: 'src/App.jsx',
      files: { 'src/App.jsx': 'export default 1' },
    })
  })

  it('读取时按租户过滤并返回文件树', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        projectId: 'proj_1',
        entryFile: 'src/App.jsx',
        files: { 'src/App.jsx': 'export default 1' },
      }),
    })
    vi.stubGlobal('fetch', fetchMock)

    const result = await loadProjectSource('proj_1', {
      tenantId: 'tenant_local',
      token: 'token',
    })

    expect(result.files['src/App.jsx']).toBe('export default 1')
    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/v1/project-sources/proj_1?tenantId=tenant_local',
    )
  })

  it('把后端稳定错误码暴露给调用方', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 503,
        statusText: 'Service Unavailable',
        json: async () => ({
          code: 'PROJECT_SOURCE_STORAGE_UNAVAILABLE',
          message: '源码对象存储不可用',
          retryable: true,
        }),
      }),
    )

    const pending = loadProjectSource('proj_1', { tenantId: 'tenant_local' })
    await expect(pending).rejects.toBeInstanceOf(ProjectSourceError)
    await expect(pending).rejects.toMatchObject({
      name: 'ProjectSourceError',
      code: 'PROJECT_SOURCE_STORAGE_UNAVAILABLE',
      retryable: true,
    })
  })
})
