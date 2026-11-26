/**
 * 离线 VisualDocument 状态。
 *
 * 全部编辑命令只改本地 Zustand 树；拖拽过程中的鼠标移动不会触发
 * React 状态更新，只有落点确定后才提交一次命令。
 */

import { create } from 'zustand'
import {
  createEmptyVisualDocument,
  createVisualNode,
  deleteVisualNodeFromDocument,
  duplicateVisualNodeInDocument,
  findVisualNode,
  freezeVisualDocument,
  insertVisualNodeIntoDocument,
  insertVisualNodesIntoDocument,
  moveVisualNodeInDocument,
  switchVisualPage,
  updateVisualNodeInDocument,
  validateVisualDocument,
  withActivePageRoot,
  addVisualPage,
  removeVisualPage,
  activePageIdOf,
} from './visualDocument.js'

export const VISUAL_DOCUMENT_STORAGE_KEY = 'vibe.visualDocument.v1'
const HISTORY_LIMIT = 80
const PERSIST_DELAY_MS = 250

let persistTimer = 0

function readStoredDocument() {
  if (typeof window === 'undefined') return createEmptyVisualDocument()
  try {
    const raw = window.localStorage.getItem(VISUAL_DOCUMENT_STORAGE_KEY)
    if (!raw) return createEmptyVisualDocument()
    const parsed = JSON.parse(raw)
    if (!parsed?.docId || parsed.schemaVersion !== '1.0') {
      return createEmptyVisualDocument()
    }
    return parsed
  } catch {
    return createEmptyVisualDocument()
  }
}

function persistDocument(document) {
  if (typeof window === 'undefined') return
  window.clearTimeout(persistTimer)
  persistTimer = window.setTimeout(() => {
    try {
      window.localStorage.setItem(
        VISUAL_DOCUMENT_STORAGE_KEY,
        JSON.stringify(document),
      )
    } catch {
      // 隐私模式 / 配额不足时不阻塞编辑。
    }
  }, PERSIST_DELAY_MS)
}

export function flushVisualDocumentPersistence() {
  if (typeof window === 'undefined' || !persistTimer) return
  window.clearTimeout(persistTimer)
  persistTimer = 0
}

function selectedStillExists(document, selectedId) {
  return Boolean(selectedId && findVisualNode(document.root, selectedId))
}

/** 页面根不是可编辑节点；默认选中它的第一个子节点。 */
function firstSelectableChild(root) {
  return root?.children?.[0]?.id ?? null
}

function initialState() {
  const document = readStoredDocument()
  return {
    doc: withActivePageRoot(document, document.root),
    selectedId: firstSelectableChild(document.root),
    activePageId: activePageIdOf(document),
    history: [],
    future: [],
    lastError: '',
  }
}

export const useVisualDocumentStore = create((set, get) => {
  /**
   * 提交一次可撤销命令。history 保存命令前快照，future 在新命令时清空。
   */
  const editable = () => get().doc.status !== 'FROZEN'

  const commit = (nextDocument, selectedId = get().selectedId) => {
    if (!editable()) return false
    if (!nextDocument || nextDocument === get().doc) return false
    const previous = get().doc
    const synced = withActivePageRoot(nextDocument, nextDocument.root)
    set((state) => ({
      doc: synced,
      selectedId:
        selectedId &&
        selectedId !== synced.root?.id &&
        findVisualNode(synced.root, selectedId)
          ? selectedId
          : firstSelectableChild(synced.root),
      activePageId: activePageIdOf(synced),
      history: [...state.history, previous].slice(-HISTORY_LIMIT),
      future: [],
      lastError: '',
    }))
    persistDocument(synced)
    return true
  }

  return {
    ...initialState(),

    selectNode: (nodeId) => {
      const state = get()
      if (nodeId && nodeId === state.doc.root?.id) {
        set({ selectedId: firstSelectableChild(state.doc.root) })
        return
      }
      set({ selectedId: nodeId || null })
    },

    switchPage: (pageId) => {
      const state = get()
      const next = switchVisualPage(state.doc, pageId)
      if (next === state.doc) return false
      set({
        doc: next,
        activePageId: pageId,
        selectedId: firstSelectableChild(next.root),
        history: [],
        future: [],
        lastError: '',
      })
      persistDocument(next)
      return true
    },

    addPage: ({ title = '新页面', path = '' } = {}) => {
      if (!editable()) return false
      const state = get()
      const result = addVisualPage(state.doc, { title, path })
      return commit(result.document, firstSelectableChild(result.page.root))
    },

    deletePage: (pageId) => {
      if (!editable()) return false
      const state = get()
      const next = removeVisualPage(state.doc, pageId)
      if (!next) {
        set({ lastError: '至少保留一个页面，无法删除' })
        return false
      }
      return commit(next, firstSelectableChild(next.root))
    },

    addNode: (type, parentId = null, index = -1) => {
      if (!editable()) return false
      const state = get()
      const node = createVisualNode(type, {
        provenance: { createdBy: 'user', revision: 1, changeId: '' },
      })
      try {
        const next = insertVisualNodeIntoDocument(state.doc, {
          parentId:
            parentId ||
            state.doc.root?.id ||
            null,
          type,
          index,
          node,
        })
        const selectedId =
          type === 'page' ? firstSelectableChild(next.root) : node.id
        return commit(next, selectedId)
      } catch (cause) {
        set({ lastError: String(cause?.message || cause) })
        return false
      }
    },

    duplicateNode: (nodeId) => {
      if (!editable()) return false
      const state = get()
      const found = findVisualNode(state.doc.root, nodeId)
      if (!found) return false
      if (found.parent === null || found.node.type === 'page') return false
      const next = duplicateVisualNodeInDocument(state.doc, nodeId)
      const copy = findVisualNode(next.root, found.parent?.id)
      return commit(next, copy?.node?.children?.[found.index + 1]?.id ?? nodeId)
    },

    deleteNode: (nodeId) => {
      if (!editable()) return false
      const state = get()
      const next = deleteVisualNodeFromDocument(state.doc, nodeId)
      if (next === state.doc) return false
      return commit(next, null)
    },

    moveNode: (nodeId, parentId, index = -1) => {
      if (!editable()) return false
      const state = get()
      const next = moveVisualNodeInDocument(state.doc, {
        nodeId,
        parentId,
        index,
      })
      return commit(next, nodeId)
    },

    /** 局部 Agent 生成：一次命令插入整组节点，只产生一条历史。 */
    insertNodes: (parentId, nodes, index = -1) => {
      if (!editable()) return false
      const state = get()
      try {
        const result = insertVisualNodesIntoDocument(state.doc, {
          parentId: parentId || state.doc.root?.id || null,
          nodes,
          index,
          editor: 'agent',
        })
        return commit(result.document, result.insertedIds[0] || parentId)
      } catch (cause) {
        set({ lastError: String(cause?.message || cause) })
        return false
      }
    },

    /** 冻结后画布只读；contentHash 与 revision 同时固化。 */
    freezeDocument: async () => {
      const state = get()
      if (state.doc.status === 'FROZEN') return state.doc
      const report = validateVisualDocument(state.doc)
      if (!report.ok) {
        set({ lastError: `Schema 校验失败：${report.errors[0] || '未知错误'}` })
        return null
      }
      const frozen = await freezeVisualDocument(state.doc)
      set({
        doc: withActivePageRoot(frozen, frozen.root),
        selectedId: frozen.root?.id ?? null,
        activePageId: activePageIdOf(frozen),
        history: [],
        future: [],
        lastError: '',
      })
      persistDocument(withActivePageRoot(frozen, frozen.root))
      return frozen
    },

    updateNode: (nodeId, patch) => {
      if (!editable()) return false
      const state = get()
      const next = updateVisualNodeInDocument(state.doc, nodeId, patch)
      return commit(next, nodeId)
    },

    updateText: (nodeId, text) => {
      return get().updateNode(nodeId, { props: { text } })
    },

    updateLayout: (nodeId, layoutPatch) => {
      return get().updateNode(nodeId, { layout: layoutPatch })
    },

    renameNode: (nodeId, name) => {
      return get().updateNode(nodeId, { name })
    },

    toggleLock: (nodeId) => {
      if (!editable()) return false
      const state = get()
      const found = findVisualNode(state.doc.root, nodeId)
      if (!found) return false
      return get().updateNode(nodeId, { locked: !found.node.locked })
    },

    setTitle: (title) => {
      if (!editable()) return false
      const state = get()
      const next = {
        ...state.doc,
        title,
        revision: state.doc.revision + 1,
        metadata: { ...state.doc.metadata, lastModifiedBy: 'user' },
      }
      return commit(next)
    },

    resetDocument: ({ withRoot = true, title = '未命名页面' } = {}) => {
      if (!editable()) return false
      const next = createEmptyVisualDocument({ title, withRoot })
      return commit(next, next.root?.id ?? null)
    },

    replaceDocument: (document, { resetHistory = true } = {}) => {
      if (resetHistory) {
        const synced = withActivePageRoot(document, document?.root ?? null)
        set({
          doc: synced,
          selectedId: synced?.root?.id ?? null,
          activePageId: activePageIdOf(synced),
          history: [],
          future: [],
          lastError: '',
        })
        persistDocument(synced)
        return
      }
      return commit(document)
    },

    undo: () => {
      if (!editable()) return false
      const state = get()
      if (!state.history.length) return false
      const previous = state.history[state.history.length - 1]
      set({
        doc: previous,
        history: state.history.slice(0, -1),
        future: [state.doc, ...state.future].slice(0, HISTORY_LIMIT),
        selectedId: selectedStillExists(previous, state.selectedId)
          ? state.selectedId
          : previous.root?.id ?? null,
        lastError: '',
      })
      persistDocument(previous)
      return true
    },

    redo: () => {
      if (!editable()) return false
      const state = get()
      if (!state.future.length) return false
      const next = state.future[0]
      set({
        doc: next,
        activePageId: activePageIdOf(next),
        history: [...state.history, state.doc].slice(-HISTORY_LIMIT),
        future: state.future.slice(1),
        selectedId: selectedStillExists(next, state.selectedId)
          ? state.selectedId
          : next.root?.id ?? null,
        lastError: '',
      })
      persistDocument(next)
      return true
    },

    clearError: () => set({ lastError: '' }),
  }
})

export function selectVisualNode(state, nodeId) {
  return findVisualNode(state.doc.root, nodeId)?.node ?? null
}

/**
 * 读取当前项目已冻结的 VisualDocument。
 *
 * 工作台生成入口需要把浏览器里的冻结树回传给 CodeGen；这里同时检查
 * 内存 store 与 localStorage：store 可能尚未从另一个项目切换过来，
 * localStorage 也可能因防抖尚未落盘。两者都按 projectId 严格隔离。
 */
export function loadFrozenVisualDocument(projectId) {
  const candidates = [useVisualDocumentStore.getState()?.doc]
  if (typeof window !== 'undefined') {
    try {
      const raw = window.localStorage.getItem(VISUAL_DOCUMENT_STORAGE_KEY)
      if (raw) candidates.push(JSON.parse(raw))
    } catch {
      // 损坏 JSON / 隐私模式：只依赖内存 store。
    }
  }
  for (const document of candidates) {
    if (!document || document.status !== 'FROZEN') continue
    if (!document.metadata?.contentHash) continue
    if (projectId && document.projectId !== projectId) continue
    if (!projectId && document.projectId) continue
    return document
  }
  return null
}
