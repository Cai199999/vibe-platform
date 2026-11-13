import 'fake-indexeddb/auto'
import { beforeEach, describe, expect, it } from 'vitest'
import {
  SAVE_CLEARED_TOAST,
  SAVE_DURING_EDIT_TOAST,
  draftKeyFor,
  resolveSaveToast,
  useDraftStore,
} from '../draftStore.js'
import {
  draftWritesSettled,
  writeDraftTombstone,
} from '../draftStorage.js'

let sequence = 0
const makeScope = (overrides = {}) => {
  sequence += 1
  return {
    tenantId: 'tenant_test',
    userId: `user_${Date.now()}_${sequence}`,
    projectId: `proj_${sequence}`,
    ...overrides,
  }
}

const layoutOp = (selector, dx) => ({
  selector,
  dx,
  dy: 0,
  next: { x: dx, y: 0 },
  base: { x: 0, y: 0 },
  token: `token_${selector}`,
})

const additionOp = (annotationId, intent) => ({
  annotationId,
  id: annotationId,
  intent,
  staged: true,
})

beforeEach(async () => {
  await draftWritesSettled()
  useDraftStore.setState({ records: {}, loaded: {} })
})

describe('画布草稿生命周期 / revision barrier', () => {
  it('保存期间并发操作必须保留，且只清理 barrier 之前的内容', async () => {
    const scope = makeScope()
    await useDraftStore.getState().hydrateProjectDraft(scope)

    useDraftStore.getState().syncDraft(scope, {
      layoutOps: [layoutOp('.hero', 12)],
      additions: [],
    })

    // 用户点击【保存快照】的瞬间：当前最大 seq = 1。
    const ticket = useDraftStore.getState().beginSave(scope)
    expect(ticket.barrier).toBe(1)

    // 保存请求还在途，用户继续拖拽 / 修改 / 新增组件。
    useDraftStore.getState().syncDraft(scope, {
      layoutOps: [layoutOp('.hero', 28), layoutOp('.footer', 8)],
      additions: [additionOp('ann_during_save', '新增一个订阅按钮')],
    })
    const duringSave = useDraftStore.getState().records[draftKeyFor(scope)]
    const seqSnapshot = duringSave.ops.map((op) => ({
      id: op.id,
      seq: op.seq,
    }))

    // 后端 Git checkpoint 成功：只能删除 seq <= 1 的操作。
    const committed = await useDraftStore
      .getState()
      .commitSave(scope, { barrier: ticket.barrier })
    const toast = resolveSaveToast(committed)
    const afterSave = useDraftStore.getState().records[draftKeyFor(scope)]

    console.log('[并发保存] barrier =', ticket.barrier)
    console.log('[并发保存] 保存期间操作 =', JSON.stringify(seqSnapshot))
    console.log(
      '[并发保存] commit 后保留 =',
      JSON.stringify(
        afterSave.ops.map((op) => ({ id: op.id, seq: op.seq })),
      ),
    )
    console.log('[并发保存] Toast =', toast.text)

    expect(committed.remaining).toBe(3)
    expect(afterSave.ops.map((op) => op.id)).toEqual([
      'layout:.hero',
      'layout:.footer',
      'addition:ann_during_save',
    ])
    expect(toast.kind).toBe('info')
    expect(toast.text).toBe(SAVE_DURING_EDIT_TOAST)
    expect(afterSave.status).toBe('dirty')
    expect(afterSave.epoch).toBe(0)
  })

  it('没有并发操作时清空草稿、epoch 递增、seq 归零', async () => {
    const scope = makeScope()
    await useDraftStore.getState().hydrateProjectDraft(scope)
    useDraftStore.getState().syncDraft(scope, {
      layoutOps: [layoutOp('.card', 5)],
      additions: [],
    })

    const ticket = useDraftStore.getState().beginSave(scope)
    const committed = await useDraftStore
      .getState()
      .commitSave(scope, { barrier: ticket.barrier })
    const toast = resolveSaveToast(committed)
    const record = useDraftStore.getState().records[draftKeyFor(scope)]

    console.log('[保存完成] remaining =', committed.remaining)
    console.log('[保存完成] epoch =', record.epoch, 'seq =', record.seq)
    console.log('[保存完成] Toast =', toast.text)

    expect(committed.cleared).toBe(true)
    expect(record.ops).toHaveLength(0)
    expect(record.epoch).toBe(1)
    expect(record.seq).toBe(0)
    expect(toast.kind).toBe('success')
    expect(toast.text).toBe(SAVE_CLEARED_TOAST)
  })

  it('草稿按租户:用户:项目隔离，并可跨 Store 重建从 IndexedDB 恢复', async () => {
    const scopeA = makeScope({ projectId: 'project_a' })
    const scopeB = makeScope({ projectId: 'project_b' })
    await useDraftStore.getState().hydrateProjectDraft(scopeA)
    await useDraftStore.getState().hydrateProjectDraft(scopeB)

    useDraftStore.getState().syncDraft(scopeA, {
      layoutOps: [layoutOp('.only-a', 16)],
      additions: [additionOp('ann_a', '只属于 A 的新增组件')],
    })
    useDraftStore.getState().syncDraft(scopeB, {
      layoutOps: [layoutOp('.only-b', 3)],
      additions: [],
    })
    await draftWritesSettled()

    // 模拟刷新：内存清空，只剩 IndexedDB。
    useDraftStore.setState({ records: {}, loaded: {} })
    const restoredA = await useDraftStore
      .getState()
      .hydrateProjectDraft(scopeA)
    const restoredB = await useDraftStore
      .getState()
      .hydrateProjectDraft(scopeB)

    console.log(
      '[隔离恢复] A =',
      JSON.stringify(restoredA?.ops.map((op) => op.id)),
    )
    console.log(
      '[隔离恢复] B =',
      JSON.stringify(restoredB?.ops.map((op) => op.id)),
    )

    expect(restoredA.ops.map((op) => op.id)).toEqual([
      'layout:.only-a',
      'addition:ann_a',
    ])
    expect(restoredB.ops.map((op) => op.id)).toEqual(['layout:.only-b'])
    expect(restoredA.key).toBe(draftKeyFor(scopeA))
    expect(restoredB.key).toBe(draftKeyFor(scopeB))
  })

  it('刷新确认后的同步墓碑会在下次启动清理对应草稿', async () => {
    const scope = makeScope({ projectId: 'project_tombstone' })
    await useDraftStore.getState().hydrateProjectDraft(scope)
    useDraftStore.getState().syncDraft(scope, {
      layoutOps: [layoutOp('.discard-me', 20)],
      additions: [],
    })
    await draftWritesSettled()
    writeDraftTombstone(scope, [scope.projectId])

    useDraftStore.setState({ records: {}, loaded: {} })
    // 换一个等价 scope 对象，模拟新页面启动。
    const restored = await useDraftStore.getState().hydrateProjectDraft(scope)

    console.log('[墓碑清理] restored ops =', restored?.ops?.length ?? 0)
    expect(restored).toBeNull()
  })
})
