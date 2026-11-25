import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  GeneratedAppsError,
  fetchMultiServiceRun,
  generateMultiService,
} from '../generatedAppsClient.js'

describe('generatedAppsClient', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('触发多服务生成并返回摘要', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        runId: 'ga_1',
        status: 'ok',
        artifacts: 30,
      }),
    })
    vi.stubGlobal('fetch', fetchMock)

    const result = await generateMultiService({ name: 'ticket-demo', token: 't' })

    expect(result).toMatchObject({ runId: 'ga_1', status: 'ok' })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/architect/multi-service/generate')
    expect(JSON.parse(init.body)).toEqual({ name: 'ticket-demo' })
  })

  it('读取失败时抛出带 code 的异常', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        json: async () => ({ code: 'NOT_FOUND', userMessage: '记录不存在' }),
      }),
    )
    await expect(fetchMultiServiceRun('missing')).rejects.toMatchObject({
      name: 'GeneratedAppsError',
      code: 'NOT_FOUND',
      status: 404,
    })
    await expect(fetchMultiServiceRun('')).rejects.toBeInstanceOf(
      GeneratedAppsError,
    )
  })
})
