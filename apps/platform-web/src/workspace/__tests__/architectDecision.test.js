import { afterEach, describe, expect, it, vi } from 'vitest'

import { fetchArchitectDecision } from '../architectStream.js'

describe('fetchArchitectDecision', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('确认契约时携带用户调整后的 agentPolicy', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ status: 'CONFIRMED' }),
    })
    vi.stubGlobal('fetch', fetchMock)

    await fetchArchitectDecision(
      'run_1',
      'ctr_1',
      'CONFIRM',
      '',
      'L2_STANDARD',
      [],
      'token',
      {
        fastPathEdges: [
          ['DBACoder', 'BackendCoder', 'schema_ready', 'REBIND_ORM'],
        ],
      },
    )

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(
      '/api/v1/architect/runs/run_1/contracts/ctr_1/decision',
    )
    const body = JSON.parse(init.body)
    expect(body).toMatchObject({
      contractId: 'ctr_1',
      decision: 'CONFIRM',
      qualityTier: 'L2_STANDARD',
    })
    expect(body.agentPolicy.fastPathEdges).toHaveLength(1)
  })
})
