import { describe, expect, it } from 'vitest'
import { EMPTY_ARCHITECT, reduceArchitect } from '../useArchitect.js'

const conflictPayload = {
  conflictId: 'ccf_ctr_1',
  runId: 'arc_1',
  contractId: 'ctr_1',
  kind: 'contract_naming_conflict',
  status: 'PENDING',
  message: 'API 集合 /api/v1/orders 对应的业务表 orders 不在 DDL 中',
  apiCollection: '/api/v1/orders',
  resource: 'orders',
  missingTable: 'orders',
  suggestedTable: 'resumes',
  options: [
    { optionId: 'map_to_table', action: 'MAP_TO_TABLE', label: '映射' },
    { optionId: 'create_table', action: 'CREATE_TABLE', label: '新建表' },
    { optionId: 'cancel', action: 'CANCEL', label: '取消' },
  ],
}

describe('architect 契约冲突决策状态', () => {
  it('冲突事件进入 AWAITING_DECISION 并保存可选方案', () => {
    const next = reduceArchitect(
      { ...EMPTY_ARCHITECT, runId: 'arc_1', status: 'REPAIRING_CONTRACT' },
      { type: 'architect.contract.conflict', seq: 12, payload: conflictPayload },
    )

    expect(next.status).toBe('AWAITING_DECISION')
    expect(next.conflict).toEqual(conflictPayload)
    expect(next.contract).toBeNull()
    expect(next.clarification).toBeNull()
    expect(next.messages.at(-1).kind).toBe('conflict')
  })

  it('提交决策后恢复 ANALYZING 并清空冲突面板', () => {
    const state = {
      ...EMPTY_ARCHITECT,
      runId: 'arc_1',
      status: 'AWAITING_DECISION',
      conflict: conflictPayload,
      conflictSubmitting: true,
    }

    const next = reduceArchitect(state, {
      type: 'architect.contract.conflict.answered',
      seq: 13,
      payload: {
        conflictId: 'ccf_ctr_1',
        optionId: 'create_table',
        action: 'CREATE_TABLE',
      },
    })

    expect(next.status).toBe('ANALYZING')
    expect(next.conflict).toBeNull()
    expect(next.conflictSubmitting).toBe(false)
    expect(next.submitting).toBe(true)
  })

  it('契约 ready 会清理残留的冲突状态', () => {
    const state = {
      ...EMPTY_ARCHITECT,
      runId: 'arc_1',
      status: 'AWAITING_DECISION',
      conflict: conflictPayload,
    }

    const next = reduceArchitect(state, {
      type: 'architect.contract.ready',
      seq: 20,
      payload: { contract: { contractId: 'ctr_1', status: 'AWAITING_CONFIRMATION' } },
    })

    expect(next.status).toBe('AWAITING_CONTRACT_CONFIRMATION')
    expect(next.conflict).toBeNull()
  })
})
