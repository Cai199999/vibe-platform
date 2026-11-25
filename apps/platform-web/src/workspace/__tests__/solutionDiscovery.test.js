import { describe, expect, it } from 'vitest'
import {
  buildSolutionChoice,
  defaultSolutionChoice,
  solutionMeta,
  solutionSourceLabel,
} from '../solutionDiscovery.js'

describe('solutionDiscovery', () => {
  it('maps a REUSE option to the architect.start solutionChoice', () => {
    const choice = buildSolutionChoice({
      optionId: 'opt_reuse_opengeos_geoai',
      kind: 'REUSE',
      title: 'opengeos/geoai',
      sourceUrl: 'https://github.com/opengeos/geoai',
      license: 'MIT',
      summary: 'GIS 库',
      pros: ['可直接作为依赖'],
      cons: ['需要适配技术栈'],
    })
    expect(choice).toMatchObject({
      optionId: 'opt_reuse_opengeos_geoai',
      kind: 'REUSE',
      title: 'opengeos/geoai',
      sourceUrl: 'https://github.com/opengeos/geoai',
      license: 'MIT',
    })
    expect(choice.integrationPlan).toContain('依赖')
    expect(choice.notes).toEqual([])
  })

  it('returns null for an invalid option', () => {
    expect(buildSolutionChoice(null)).toBeNull()
    expect(buildSolutionChoice({ title: '缺少 optionId' })).toBeNull()
  })

  it('builds a stable BUILD fallback', () => {
    const choice = defaultSolutionChoice()
    expect(choice).toMatchObject({
      optionId: 'opt_build',
      kind: 'BUILD',
      title: '自研方案',
    })
    expect(choice.integrationPlan).toContain('从零实现')
  })

  it('formats card metadata without leaking undefined', () => {
    expect(
      solutionMeta({
        license: 'MIT',
        stars: 3479,
        language: 'Python',
        integrationCost: 'LOW',
      }),
    ).toBe('MIT · 3479 stars · Python · 集成成本：低')
    expect(solutionMeta({ integrationCost: 'HIGH' })).toBe('集成成本：高')
    expect(solutionMeta(null)).toBe('')
  })

  it('labels where an option came from', () => {
    expect(solutionSourceLabel({ source: 'github' })).toBe('GitHub 联网检索')
    expect(solutionSourceLabel({ source: 'local' })).toBe('本地复用目录')
    expect(solutionSourceLabel({ source: 'build' })).toBe('自研')
    expect(solutionSourceLabel(null)).toBe('')
  })
})
