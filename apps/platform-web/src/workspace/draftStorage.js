/**
 * 画布本地草稿的 IndexedDB 持久层。
 *
 * 设计目标：
 *  - 每个项目一条记录，主键固定为 `<tenantId>:<userId>:<projectId>`，
 *    多租户 / 多用户 / 多项目天然隔离；
 *  - 只存“尚未提交到 Git”的本地操作，不复制沙盒源码；
 *  - 写入串行化：拖拽会在极短时间内产生大量更新，避免并发事务互相覆盖；
 *  - 浏览器不支持 IndexedDB 时退化为进程内 Map，保证 UI 仍可用。
 */

const DB_NAME = 'vibe-platform-drafts'
const DB_VERSION = 1
const STORE_NAME = 'drafts'
const TOMBSTONE_PREFIX = 'vibe:draft:tombstone:'

let dbPromise = null
const memoryRecords = new Map()
// 所有写操作按调用顺序串行执行；调用方可用 draftWritesSettled() 等待落盘。
let writeChain = Promise.resolve()

const hasIndexedDb = () => typeof indexedDB !== 'undefined'

function openDatabase() {
  if (!hasIndexedDb()) return Promise.resolve(null)
  if (dbPromise) return dbPromise
  dbPromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION)
    request.onupgradeneeded = () => {
      const db = request.result
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: 'key' })
      }
    }
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  }).catch((cause) => {
    // 隐私模式 / 配额耗尽：降级到内存，不让草稿功能拖垮整个页面。
    console.warn('[draft-storage] IndexedDB 不可用，已降级为内存存储', cause)
    return null
  })
  return dbPromise
}

function runTransaction(mode, operation) {
  return openDatabase().then((db) => {
    if (!db) return operation(null)
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, mode)
      const store = tx.objectStore(STORE_NAME)
      let result
      try {
        result = operation(store)
      } catch (cause) {
        reject(cause)
        return
      }
      tx.oncomplete = () => resolve(result)
      tx.onerror = () => reject(tx.error)
      tx.onabort = () => reject(tx.error)
    })
  })
}

/** 把写任务追加到全局串行队列。取消失败也要继续后续写入。 */
function enqueueWrite(task) {
  writeChain = writeChain.then(task, task).catch((cause) => {
    console.warn('[draft-storage] 草稿写入失败', cause)
  })
  return writeChain
}

/** 测试 / 退出前等待所有 IndexedDB 写入完成。 */
export function draftWritesSettled() {
  return writeChain
}

export function draftKeyFor({ tenantId, userId, projectId }) {
  return `${tenantId}:${userId}:${projectId}`
}

export function draftScopePrefix({ tenantId, userId }) {
  return `${tenantId}:${userId}:`
}

export async function readDraftRecord(key) {
  if (memoryRecords.has(key)) return memoryRecords.get(key)
  const db = await openDatabase()
  if (!db) return null
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readonly')
    const request = tx.objectStore(STORE_NAME).get(key)
    request.onsuccess = () => resolve(request.result ?? null)
    request.onerror = () => reject(request.error)
  })
}

export function writeDraftRecord(record) {
  memoryRecords.set(record.key, record)
  return enqueueWrite(() =>
    runTransaction('readwrite', (store) => {
      if (store) store.put(record)
    }),
  )
}

export function deleteDraftRecord(key) {
  memoryRecords.delete(key)
  return enqueueWrite(() =>
    runTransaction('readwrite', (store) => {
      if (store) store.delete(key)
    }),
  )
}

export async function listDraftRecords() {
  const db = await openDatabase()
  if (!db) return [...memoryRecords.values()]
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readonly')
    const request = tx.objectStore(STORE_NAME).getAll()
    request.onsuccess = () => resolve(request.result ?? [])
    request.onerror = () => reject(request.error)
  })
}

export async function listDraftRecordsForScope(scope) {
  const prefix = draftScopePrefix(scope)
  const db = await openDatabase()
  if (!db) {
    return [...memoryRecords.values()].filter((record) =>
      record.key.startsWith(prefix),
    )
  }
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readonly')
    const range =
      typeof IDBKeyRange !== 'undefined'
        ? IDBKeyRange.bound(prefix, `${prefix}￿`)
        : null
    const request = range
      ? tx.objectStore(STORE_NAME).getAll(range)
      : tx.objectStore(STORE_NAME).getAll()
    request.onsuccess = () => {
      const records = request.result ?? []
      resolve(
        range
          ? records
          : records.filter((record) => record.key.startsWith(prefix)),
      )
    }
    request.onerror = () => reject(request.error)
  })
}

/**
 * 刷新 / 崩溃保护墓碑。
 *
 * `beforeunload` 里无法等待异步 IndexedDB 删除，因此在 `pagehide` 时同步
 * 写入 localStorage。下一次启动时先读墓碑再清理对应草稿，避免“用户已经
 * 确认放弃刷新，草稿却在下一次打开时复活”。
 */
export function writeDraftTombstone(scope, projectIds = []) {
  if (typeof localStorage === 'undefined') return
  try {
    localStorage.setItem(
      `${TOMBSTONE_PREFIX}${scope.tenantId}:${scope.userId}`,
      JSON.stringify({ projects: projectIds, at: Date.now() }),
    )
  } catch {
    // 隐私模式 / 配额不足：忽略，刷新仍能正常进行。
  }
}

export function readDraftTombstone(scope) {
  if (typeof localStorage === 'undefined') return null
  try {
    const raw = localStorage.getItem(
      `${TOMBSTONE_PREFIX}${scope.tenantId}:${scope.userId}`,
    )
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

export function clearDraftTombstone(scope) {
  if (typeof localStorage === 'undefined') return
  try {
    localStorage.removeItem(
      `${TOMBSTONE_PREFIX}${scope.tenantId}:${scope.userId}`,
    )
  } catch {
    // 忽略。
  }
}

export { TOMBSTONE_PREFIX }
