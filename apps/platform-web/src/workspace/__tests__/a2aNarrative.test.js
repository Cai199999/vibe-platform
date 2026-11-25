import { describe, expect, it } from 'vitest'
import {
  A2A_ALLOWED,
  buildA2ANarrative,
  dedupeA2A,
  isAllowedA2A,
} from '../a2aNarrative.js'
import codeDefect from './fixtures/a2a/code_defect_resolved.json'
import contractDefect from './fixtures/a2a/contract_defect_revision.json'
import retryExhausted from './fixtures/a2a/retry_exhausted_escalated.json'
import replayDuplicate from './fixtures/a2a/replay_duplicate.json'

function payloads(fixture) {
  return fixture.events.map((event) => event.payload)
}

describe('a2aNarrative', () => {
  it('code_defect_resolved 还原问题叙事', () => {
    const narrative = buildA2ANarrative(payloads(codeDefect), {
      runTerminal: codeDefect.runTerminal,
    })
    expect(narrative.steps).toHaveLength(2)
    expect(narrative.loops).toBe(1)
    expect(narrative.terminalReason).toBe('RESOLVED')
    expect(narrative.steps[0]).toMatchObject({
      from: 'Applier',
      to: 'BackendCoder',
      kind: 'code_repair',
      action: 'REPAIR_CODE',
    })
  })

  it('contract_defect_revision 能还原修订链路', () => {
    const narrative = buildA2ANarrative(payloads(contractDefect), {
      runTerminal: contractDefect.runTerminal,
    })
    expect(narrative.steps).toHaveLength(3)
    expect(narrative.loops).toBe(1)
    expect(narrative.terminalReason).toBe('RESOLVED')
    expect(narrative.steps.map((step) => step.kind)).toEqual([
      'contract_revision',
      'contract_revision_started',
      'contract_confirmed',
    ])
  })

  it('retry_exhausted_escalated 能识别升级', () => {
    const narrative = buildA2ANarrative(payloads(retryExhausted), {
      runTerminal: retryExhausted.runTerminal,
    })
    expect(narrative.steps).toHaveLength(3)
    expect(narrative.loops).toBe(2)
    expect(narrative.terminalReason).toBe('ESCALATED')
  })

  it('重复 feedbackId 不重复追加', () => {
    const items = payloads(replayDuplicate)
    expect(dedupeA2A(items)).toHaveLength(1)
    const narrative = buildA2ANarrative(items, {
      runTerminal: replayDuplicate.runTerminal,
    })
    expect(narrative.steps).toHaveLength(1)
    expect(narrative.warnings).toContain('duplicate feedbackId a2a_replay_1')
  })

  it('矩阵外方向会被标记', () => {
    const bad = {
      feedbackId: 'a2a_bad',
      fromAgent: 'Applier',
      toAgent: 'Architect',
      kind: 'code_repair',
      requestedAction: 'REPAIR_CODE',
      summary: '不允许的方向',
    }
    expect(isAllowedA2A(bad)).toBe(false)
    const narrative = buildA2ANarrative([bad])
    expect(narrative.steps).toHaveLength(0)
    expect(narrative.warnings).toContain('out-of-matrix feedback a2a_bad')
  })

  it('允许矩阵至少覆盖真实运行时的核心方向', () => {
    expect(
      A2A_ALLOWED.has('Applier>BackendCoder>code_repair>REPAIR_CODE'),
    ).toBe(true)
    expect(
      A2A_ALLOWED.has(
        'Architect>BackendCoder>contract_confirmed>GENERATE_WITH_CONTRACT',
      ),
    ).toBe(true)
  })
})
