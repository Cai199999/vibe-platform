import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  CollaborationClientError,
  fetchCollaborationRecord,
} from '../collaborationClient.js'

describe('collaborationClient', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('加载并规范化服务端协作记录', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        runId: 'run_1',
        inbox: [{ agent: 'DBACoder', summary: '表结构就绪' }],
        actions: [{ actor: 'Applier', target: 'server/index.js' }],
        fastPathGrants: [{ grantId: 'fp_1', taskId: 'task_1' }],
      }),
    })
    vi.stubGlobal('fetch', fetchMock)

    const result = await fetchCollaborationRecord('run_1')

    expect(result).toMatchObject({
      runId: 'run_1',
      inbox: [{ agent: 'DBACoder' }],
    })
    expect(result.actions).toHaveLength(1)
    expect(result.fastPathGrants).toHaveLength(1)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/architect/collaboration/runs/run_1')
    expect(init.headers.Accept).toBe('application/json')
  })

  it('返回错误信封时抛出带 code 的异常', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        json: async () => ({
          code: 'RUN_NOT_FOUND',
          userMessage: '找不到该运行记录',
        }),
      }),
    )
    await expect(fetchCollaborationRecord('missing')).rejects.toMatchObject({
      name: 'CollaborationClientError',
      code: 'RUN_NOT_FOUND',
      status: 404,
    })
    await expect(fetchCollaborationRecord('')).rejects.toBeInstanceOf(
      CollaborationClientError,
    )
  })
})
