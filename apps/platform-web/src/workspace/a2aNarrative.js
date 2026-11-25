/**
 * A2A 问题叙事的纯函数层。
 *
 * 只读取现有 a2a.feedback 事件，不改变运行状态；后续 UI 可以在
 * feature flag 打开时用它把“谁向谁提出了什么”渲染成时间线。
 */

export const A2A_ALLOWED = new Set([
  'Applier>DBACoder>schema_repair>REGENERATE_DB',
  'Applier>BackendCoder>code_repair>REPAIR_CODE',
  'BackendCoder>Architect>contract_revision>REVISE_CONTRACT',
  'BackendCoder>Architect>contract_review>REVIEW_CONTRACT',
  'ConsistencyVerifier>Architect>contract_revision>REVISE_CONTRACT',
  'ConsistencyVerifier>Architect>contract_review>REVIEW_CONTRACT',
  'ConsistencyVerifier>DBACoder>schema_repair>REGENERATE_ORM',
  'ConsistencyVerifier>BackendCoder>route_repair>REPAIR_BACKEND_JOIN',
  'ConsistencyVerifier>BackendCoder>route_repair>ADD_BACKEND_ROUTE',
  'DBACoder>BackendCoder>schema_ready>REBIND_ORM',
  'Architect>BackendCoder>contract_revision_started>WAIT_FOR_CONTRACT',
  'Architect>BackendCoder>contract_confirmed>GENERATE_WITH_CONTRACT',
])

export const A2A_REPAIR_KINDS = new Set([
  'schema_repair',
  'code_repair',
  'route_repair',
  'contract_revision',
])

export const A2A_ESCALATION_KINDS = new Set([
  'contract_revision',
  'contract_review',
])

export const A2A_TERMINAL_STATUSES = new Set([
  'RESOLVED',
  'ESCALATED',
  'FAILED',
])

function keyOf(item) {
  return [
    item?.fromAgent,
    item?.toAgent,
    item?.kind,
    item?.requestedAction,
  ].join('>')
}

export function isAllowedA2A(item) {
  return A2A_ALLOWED.has(keyOf(item))
}

export function dedupeA2A(items) {
  const seen = new Set()
  const next = []
  for (const item of Array.isArray(items) ? items : []) {
    if (!item || typeof item !== 'object') continue
    const id = item.feedbackId
    if (id && seen.has(id)) continue
    if (id) seen.add(id)
    next.push(item)
  }
  return next
}

export function buildA2ANarrative(items, { runTerminal = '' } = {}) {
  const warnings = []
  const seen = new Set()
  const steps = []

  for (const item of Array.isArray(items) ? items : []) {
    if (!item || typeof item !== 'object') continue
    const id = String(item.feedbackId || '')
    if (id && seen.has(id)) {
      warnings.push(`duplicate feedbackId ${id}`)
      continue
    }
    if (id) seen.add(id)
    if (!item.fromAgent || !item.toAgent || !item.kind || !item.requestedAction) {
      warnings.push(`incomplete feedback ${id || 'unknown'}`)
      continue
    }
    if (!isAllowedA2A(item)) {
      warnings.push(`out-of-matrix feedback ${id || 'unknown'}`)
      continue
    }
    steps.push({
      feedbackId: id,
      at: item.createdAt || '',
      from: item.fromAgent,
      to: item.toAgent,
      kind: item.kind,
      problem: item.summary || '',
      action: item.requestedAction,
      evidence: Array.isArray(item.evidence) ? item.evidence : [],
      status: String(item.payload?.status || ''),
    })
  }

  const loops = steps.filter((step) => A2A_REPAIR_KINDS.has(step.kind)).length
  const escalated = steps.some((step) => A2A_ESCALATION_KINDS.has(step.kind))
  const terminal = String(runTerminal || '').toUpperCase()
  let terminalReason = 'IN_PROGRESS'
  if (['SUCCEEDED', 'SUCCESS', 'COMPLETED'].includes(terminal)) {
    terminalReason = 'RESOLVED'
  } else if (['FAILED', 'CANCELLED'].includes(terminal)) {
    terminalReason = escalated ? 'ESCALATED' : 'FAILED'
  } else {
    const last = [...steps]
      .reverse()
      .find((step) => A2A_TERMINAL_STATUSES.has(step.status))
    terminalReason = last?.status || (escalated ? 'ESCALATED' : 'IN_PROGRESS')
  }

  return {
    steps,
    loops,
    terminalReason,
    warnings,
  }
}
