import { describe, expect, it } from 'vitest'

import {
  MAX_COLLABORATION_ITEMS,
  appendCollaborationEvent,
  collaborationCounts,
  collaborationFromSnapshot,
  mergeCollaboration,
} from '../collaborationTimeline.js'

describe('collaborationTimeline', () => {
  it('normalizes agent reports and keeps fast-path visibility', () => {
    const items = appendCollaborationEvent([], {
      type: 'agent.report',
      seq: 3,
      payload: {
        agent: 'DBACoder',
        taskId: 'task_1',
        status: 'done',
        summary: '表结构已就绪',
        deliveryMode: 'fast_path',
        originalTarget: 'BackendCoder',
      },
    })
    expect(items).toHaveLength(1)
    expect(items[0]).toMatchObject({
      kind: 'report',
      agent: 'DBACoder',
      deliveryMode: 'fast_path',
      originalTarget: 'BackendCoder',
    })
  })

  it('normalizes actions and fast-path grants', () => {
    let items = appendCollaborationEvent([], {
      type: 'agent.action',
      seq: 4,
      payload: {
        actor: 'Applier',
        tool: 'apply_patch',
        target: 'server/index.js',
      },
    })
    items = appendCollaborationEvent(items, {
      type: 'agent.fastpath.revoked',
      seq: 5,
      payload: { taskId: 'task_1', revokeReason: '出现重试' },
    })
    expect(items.map((item) => item.kind)).toEqual(['action', 'fastpath'])
    expect(items[1]).toMatchObject({ action: 'revoked', reason: '出现重试' })
  })

  it('ignores unrelated events and caps the list', () => {
    const ignored = appendCollaborationEvent([], {
      type: 'fullstack.node.started',
      seq: 1,
      payload: {},
    })
    expect(ignored).toEqual([])

    let items = []
    for (let index = 0; index < MAX_COLLABORATION_ITEMS + 5; index += 1) {
      items = appendCollaborationEvent(items, {
        type: 'agent.report',
        seq: index,
        payload: { agent: 'Agent', taskId: 'task', summary: `s${index}` },
      })
    }
    expect(items).toHaveLength(MAX_COLLABORATION_ITEMS)
    expect(collaborationCounts(items).reports).toBe(MAX_COLLABORATION_ITEMS)
  })

  it('把服务端快照转成时间线并与本地事件去重合并', () => {
    const server = collaborationFromSnapshot({
      inbox: [
        {
          reportId: 'rep_1',
          agent: 'DBACoder',
          taskId: 'task_1',
          summary: '表结构已就绪',
          deliveryMode: 'fast_path',
        },
      ],
      actions: [
        {
          actor: 'Applier',
          tool: 'apply_patch',
          target: 'server/index.js',
          createdAt: 'now',
        },
      ],
      fastPathGrants: [
        { grantId: 'fp_1', taskId: 'task_1', revoked: true, revokeReason: '重试' },
      ],
    })
    expect(server.map((item) => item.kind)).toEqual([
      'report',
      'action',
      'fastpath',
    ])
    expect(server[2]).toMatchObject({ action: 'revoked', reason: '重试' })

    const local = appendCollaborationEvent([], {
      type: 'agent.report',
      seq: 1,
      payload: {
        reportId: 'rep_1',
        agent: 'DBACoder',
        taskId: 'task_1',
        summary: '表结构已就绪',
      },
    })
    // 服务端 id 与本地事件 id 不同，但内容相同，按语义去重。
    expect(mergeCollaboration(local, server)).toHaveLength(3)
    expect(mergeCollaboration(server, server)).toHaveLength(3)
  })
})
