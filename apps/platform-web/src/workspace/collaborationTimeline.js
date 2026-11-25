/**
 * Agent 协作时间线：把后端事件归一成前端可渲染的小列表。
 *
 * 只消费事件，不改运行状态；与 a2aNarrative.js 的分工：
 * - a2aNarrative 描述“谁向谁提了什么请求”；
 * - 本模块描述“子 Agent 汇报 / 写操作 / 快速通道授权”。
 */

export const MAX_COLLABORATION_ITEMS = 100

function text(value, limit = 160) {
  const raw = typeof value === 'string' ? value.trim() : ''
  if (raw.length <= limit) return raw
  return `${raw.slice(0, limit - 3)}...`
}

function baseEntry(event, kind) {
  return {
    id: `${kind}:${event?.seq ?? 0}:${event?.type ?? ''}`,
    kind,
    seq: Number(event?.seq ?? 0),
    at: event?.ts ?? null,
  }
}

export function collaborationEntry(event) {
  const payload = event?.payload ?? {}
  switch (event?.type) {
    case 'agent.report':
      return {
        ...baseEntry(event, 'report'),
        agent: payload.agent ?? 'Agent',
        taskId: payload.taskId ?? '',
        status: payload.status ?? '',
        summary: text(payload.summary),
        deliveryMode: payload.deliveryMode ?? 'hub',
        originalTarget: payload.originalTarget ?? '',
      }
    case 'agent.action':
      return {
        ...baseEntry(event, 'action'),
        actor: payload.actor ?? 'Agent',
        tool: payload.tool ?? '',
        target: text(payload.target, 120),
        risk: payload.risk ?? 'low',
      }
    case 'agent.fastpath.granted':
      return {
        ...baseEntry(event, 'fastpath'),
        action: 'granted',
        taskId: payload.taskId ?? '',
        maxSteps: Number(payload.maxSteps ?? 0),
      }
    case 'agent.fastpath.revoked':
      return {
        ...baseEntry(event, 'fastpath'),
        action: 'revoked',
        taskId: payload.taskId ?? '',
        reason: text(payload.revokeReason, 80),
      }
    default:
      return null
  }
}

export function appendCollaborationEvent(items, event) {
  const current = Array.isArray(items) ? items : []
  const entry = collaborationEntry(event)
  if (!entry) return current
  return [...current, entry].slice(-MAX_COLLABORATION_ITEMS)
}

export function collaborationCounts(items) {
  const list = Array.isArray(items) ? items : []
  return {
    reports: list.filter((item) => item.kind === 'report').length,
    actions: list.filter((item) => item.kind === 'action').length,
    fastPath: list.filter((item) => item.kind === 'fastpath').length,
  }
}

export function collaborationFromSnapshot(snapshot) {
  const inbox = Array.isArray(snapshot?.inbox) ? snapshot.inbox : []
  const actions = Array.isArray(snapshot?.actions) ? snapshot.actions : []
  const grants = Array.isArray(snapshot?.fastPathGrants)
    ? snapshot.fastPathGrants
    : []

  const items = []
  inbox.forEach((item, index) => {
    items.push({
      id: `server:report:${item?.reportId || index}`,
      kind: 'report',
      seq: 0,
      agent: item?.agent ?? 'Agent',
      taskId: item?.taskId ?? '',
      status: item?.status ?? '',
      summary: text(item?.summary),
      deliveryMode: item?.deliveryMode ?? 'hub',
      originalTarget: item?.originalTarget ?? '',
    })
  })
  actions.forEach((item, index) => {
    items.push({
      id: `server:action:${item?.createdAt || index}:${item?.target || ''}`,
      kind: 'action',
      seq: 0,
      actor: item?.actor ?? 'Agent',
      tool: item?.tool ?? '',
      target: text(item?.target, 120),
      risk: item?.risk ?? 'low',
    })
  })
  grants.forEach((item, index) => {
    const revoked = Boolean(item?.revoked)
    items.push({
      id: `server:fastpath:${item?.grantId || index}`,
      kind: 'fastpath',
      seq: 0,
      action: revoked ? 'revoked' : 'granted',
      taskId: item?.taskId ?? '',
      reason: text(item?.revokeReason, 80),
    })
  })
  return items.slice(-MAX_COLLABORATION_ITEMS)
}

function collaborationKey(item) {
  if (item?.kind === 'report') {
    return `report:${item.agent}:${item.taskId}:${item.summary}`
  }
  if (item?.kind === 'action') {
    return `action:${item.actor}:${item.tool}:${item.target}`
  }
  if (item?.kind === 'fastpath') {
    return `fastpath:${item.action}:${item.taskId}:${item.reason || ''}`
  }
  return item?.id || ''
}

export function mergeCollaboration(base, extra) {
  const seen = new Set()
  const merged = []
  ;[...(Array.isArray(base) ? base : []), ...(Array.isArray(extra) ? extra : [])]
    .forEach((item) => {
      if (!item) return
      const key = collaborationKey(item)
      if (key && seen.has(key)) return
      if (key) seen.add(key)
      merged.push(item)
    })
  return merged.slice(-MAX_COLLABORATION_ITEMS)
}
