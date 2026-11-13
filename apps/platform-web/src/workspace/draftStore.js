/**
 * 画布草稿 Store（Zustand）。
 *
 * 关键语义：
 *  1. 草稿按 `<tenantId>:<userId>:<projectId>` 隔离，互不串项目；
 *  2. 每条操作拥有单调递增的 seq，保存时记录 revision barrier；
 *  3. 保存成功后只删除 `seq <= barrier` 的操作，保存期间的新操作必须保留；
 *  4. 草稿清空后 epoch + 1、seq 归零，下一轮项目从干净的修订号开始。
 *
 * 这里刻意不把草稿塞进 run.result：Run 是执行态，草稿是本地编辑态。
 * 两者生命周期不同，混在一起会导致一次失败的 Run 覆盖用户尚未保存的意图。
 */
import { create } from 'zustand'
import {
  clearDraftTombstone,
  deleteDraftRecord,
  draftKeyFor,
  draftScopePrefix,
  draftWritesSettled,
  listDraftRecordsForScope,
  readDraftRecord,
  readDraftTombstone,
  writeDraftRecord,
} from './draftStorage.js'

export const SAVE_DURING_EDIT_TOAST = '已保存进度，保存期间的新修改已保留'
export const SAVE_CLEARED_TOAST = '已保存到项目，本地草稿已清空'

const EMPTY_RECORD = Object.freeze({
  key: '',
  tenantId: '',
  userId: '',
  projectId: '',
  epoch: 0,
  seq: 0,
  status: 'clean',
  ops: [],
  savingBarrier: null,
  updatedAt: 0,
})

const cloneSerializable = (value) => {
  try {
    return JSON.parse(JSON.stringify(value))
  } catch {
    return value
  }
}

const payloadFingerprint = (value) => {
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

const makeRecord = (scope) => ({
  ...EMPTY_RECORD,
  key: draftKeyFor(scope),
  tenantId: scope.tenantId,
  userId: scope.userId,
  projectId: scope.projectId,
  updatedAt: Date.now(),
})

const normalizeRecord = (record, scope) => {
  if (!record) return null
  return {
    ...EMPTY_RECORD,
    ...record,
    key: record.key || draftKeyFor(scope),
    tenantId: record.tenantId || scope.tenantId,
    userId: record.userId || scope.userId,
    projectId: record.projectId || scope.projectId,
    ops: Array.isArray(record.ops) ? record.ops : [],
  }
}

const toOperation = (kind, id, payload) => ({
  id: `${kind}:${id}`,
  kind,
  payload: cloneSerializable(payload),
})

/**
 * 把 ref 中的当前草稿镜像到 Store。
 *
 * 以 selector / annotationId 作为稳定身份：
 *  - 内容没变：保留原 seq，避免每次 publish 都制造假操作；
 *  - 内容变化：分配新 seq，保存屏障才能识别“保存期间的新修改”；
 *  - 已被撤销：从记录中移除。
 */
function mergeOps(existingOps, incomingOps, startSeq) {
  const existing = new Map(existingOps.map((op) => [op.id, op]))
  const next = []
  let seq = startSeq
  let changed = false

  incomingOps.forEach((incoming) => {
    const previous = existing.get(incoming.id)
    const same =
      previous &&
      payloadFingerprint(previous.payload) ===
        payloadFingerprint(incoming.payload)
    if (same) {
      next.push(previous)
      existing.delete(incoming.id)
      return
    }
    seq += 1
    next.push({ ...incoming, seq, updatedAt: Date.now() })
    existing.delete(incoming.id)
    changed = true
  })

  // 其余都是已被撤销 / 已提交的操作。
  if (existing.size > 0) changed = true
  if (next.length !== existingOps.length) changed = true
  return { ops: next, seq, changed }
}

/** 墓碑清理只做一次，避免每个项目 hydration 都重复扫描 localStorage。 */
const tombstonePromises = new Map()

async function ensureTombstoneConsumed(scope) {
  const prefix = draftScopePrefix(scope)
  if (tombstonePromises.has(prefix)) return tombstonePromises.get(prefix)
  const task = (async () => {
    const tombstone = readDraftTombstone(scope)
    if (!tombstone) return
    const projects = Array.isArray(tombstone.projects) ? tombstone.projects : []
    const keys = projects
      .filter(Boolean)
      .map((projectId) => draftKeyFor({ ...scope, projectId }))
    if (keys.length === 0) {
      // 没有项目列表时保守清空当前用户全部草稿。
      const records = await listDraftRecordsForScope(scope)
      records.forEach((record) => keys.push(record.key))
    }
    keys.forEach((key) => {
      void deleteDraftRecord(key)
    })
    await draftWritesSettled()
    clearDraftTombstone(scope)
  })().catch((cause) => {
    console.warn('[draft-store] 墓碑清理失败', cause)
  })
  tombstonePromises.set(prefix, task)
  // 清理完成后释放缓存：同一页面生命周期内可能再次出现墓碑（例如测试、
  // 或用户取消刷新后再次刷新），不能把第一次“无墓碑”的结果永久缓存。
  void task.finally(() => {
    if (tombstonePromises.get(prefix) === task) {
      tombstonePromises.delete(prefix)
    }
  })
  return task
}

export const useDraftStore = create((set, get) => ({
  // key -> DraftRecord
  records: {},
  // key -> true：已经完成 IndexedDB 读取；在此之前的 publish 不得写库，
  // 否则空 ref 会把上一次会话的草稿覆盖掉。
  loaded: {},

  async hydrateProjectDraft(scope) {
    await ensureTombstoneConsumed(scope)
    const key = draftKeyFor(scope)
    if (get().loaded[key]) return get().records[key] ?? null
    const stored = await readDraftRecord(key)
    const record = normalizeRecord(stored, scope)
    set((state) => ({
      records: record
        ? { ...state.records, [key]: record }
        : state.records,
      loaded: { ...state.loaded, [key]: true },
    }))
    return record
  },

  markProjectLoaded(scope) {
    const key = draftKeyFor(scope)
    if (get().loaded[key]) return
    set((state) => ({ loaded: { ...state.loaded, [key]: true } }))
  },

  syncDraft(scope, { layoutOps = [], additions = [] } = {}) {
    const key = draftKeyFor(scope)
    // 尚未读取 IndexedDB 时禁止写入，防止覆盖旧草稿。
    if (!get().loaded[key]) return get().records[key] ?? null

    const incoming = [
      ...layoutOps
        .filter((item) => item?.selector)
        .map((item) => toOperation('layout', item.selector, item)),
      ...additions
        .filter((item) => item?.annotationId || item?.id)
        .map((item) =>
          toOperation('addition', item.annotationId || item.id, item),
        ),
    ]
    const existing = get().records[key] || makeRecord(scope)
    const merged = mergeOps(existing.ops, incoming, existing.seq)
    if (!merged.changed) return existing

    const next = normalizeRecord(
      {
        ...existing,
        ops: merged.ops,
        seq: merged.seq,
        status:
          merged.ops.length === 0
            ? 'clean'
            : existing.status === 'saving'
              ? 'saving'
              : 'dirty',
        updatedAt: Date.now(),
      },
      scope,
    )
    set((state) => ({ records: { ...state.records, [key]: next } }))
    void writeDraftRecord(next)
    return next
  },

  /** 点击【保存快照】的瞬间记录屏障；后续新操作一定拥有更大的 seq。 */
  beginSave(scope) {
    const key = draftKeyFor(scope)
    const existing = get().records[key] || makeRecord(scope)
    const next = normalizeRecord(
      {
        ...existing,
        status: existing.ops.length ? 'saving' : existing.status,
        savingBarrier: existing.seq,
        updatedAt: Date.now(),
      },
      scope,
    )
    set((state) => ({ records: { ...state.records, [key]: next } }))
    void writeDraftRecord(next)
    return {
      key,
      epoch: next.epoch,
      barrier: next.savingBarrier,
      total: next.ops.length,
    }
  },

  cancelSave(scope) {
    const key = draftKeyFor(scope)
    const existing = get().records[key]
    if (!existing) return null
    const next = normalizeRecord(
      {
        ...existing,
        status: existing.ops.length ? 'dirty' : 'clean',
        savingBarrier: null,
        updatedAt: Date.now(),
      },
      scope,
    )
    set((state) => ({ records: { ...state.records, [key]: next } }))
    void writeDraftRecord(next)
    return next
  },

  /**
   * 保存成功：只删除 barrier 及其之前的操作。
   *
   * 返回 remaining 用于 Toast 分支：
   *  - remaining > 0：保留保存期间产生的新操作；
   *  - remaining = 0：彻底清空，epoch + 1 并把 seq 归零。
   */
  async commitSave(scope, { barrier } = {}) {
    const key = draftKeyFor(scope)
    const existing = get().records[key]
    if (!existing) {
      return { cleared: true, remaining: 0, epoch: 0, barrier: barrier ?? 0 }
    }
    const limit = Number.isFinite(barrier)
      ? barrier
      : (existing.savingBarrier ?? existing.seq)
    const remainingOps = existing.ops.filter((op) => op.seq > limit)
    const cleared = remainingOps.length === 0
    const next = normalizeRecord(
      {
        ...existing,
        ops: remainingOps,
        seq: cleared ? 0 : existing.seq,
        epoch: cleared ? existing.epoch + 1 : existing.epoch,
        status: cleared ? 'clean' : 'dirty',
        savingBarrier: null,
        updatedAt: Date.now(),
      },
      scope,
    )
    set((state) => ({ records: { ...state.records, [key]: next } }))
    await writeDraftRecord(next)
    return {
      cleared,
      remaining: remainingOps.length,
      epoch: next.epoch,
      barrier: limit,
    }
  },

  async purgeProjectDraft(scope) {
    const key = draftKeyFor(scope)
    set((state) => {
      const records = { ...state.records }
      delete records[key]
      return { records }
    })
    await deleteDraftRecord(key)
  },

  async purgeUserDrafts(scope) {
    const prefix = draftScopePrefix(scope)
    const records = await listDraftRecordsForScope(scope)
    set((state) => {
      const next = {}
      Object.entries(state.records).forEach(([key, record]) => {
        if (!key.startsWith(prefix)) next[key] = record
      })
      return { records: next }
    })
    await Promise.all(records.map((record) => deleteDraftRecord(record.key)))
  },

  hasDraftForProject(scope) {
    const record = get().records[draftKeyFor(scope)]
    return Boolean(record?.ops?.length)
  },
}))

/**
 * 退出登录前检查：必须同时看内存 Store 与 IndexedDB。
 *
 * 用户可能停留在管理后台，此时 WorkspacePage 没有挂载、Store 里没有
 * 当前项目的记录；只查内存会漏掉已落盘的草稿。
 */
export async function findUnsavedDraftRecords(scope) {
  await ensureTombstoneConsumed(scope)
  await draftWritesSettled()
  const prefix = draftScopePrefix(scope)
  const records = new Map()
  Object.entries(useDraftStore.getState().records).forEach(([key, record]) => {
    if (key.startsWith(prefix)) records.set(key, record)
  })
  const stored = await listDraftRecordsForScope(scope)
  stored.forEach((record) => {
    // IDB 中的记录通常更新；以它为准确值，避免内存残留旧数据。
    if (record?.ops?.length) records.set(record.key, record)
  })
  return [...records.values()].filter((record) => record.ops?.length > 0)
}

export async function purgeDraftsForScope(scope) {
  await useDraftStore.getState().purgeUserDrafts(scope)
}

/** 纯函数：保存成功后该弹哪条 Toast。 */
export function resolveSaveToast({ remaining }) {
  return remaining > 0
    ? { kind: 'info', text: SAVE_DURING_EDIT_TOAST }
    : { kind: 'success', text: SAVE_CLEARED_TOAST }
}

export { EMPTY_RECORD }
export { draftKeyFor }
