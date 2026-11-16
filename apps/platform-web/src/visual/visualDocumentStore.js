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
  insertVisualNodeIntoDocument,
  moveVisualNodeInDocument,
  updateVisualNodeInDocument,
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

function initialState() {
  const document = readStoredDocument()
  return {
    doc: document,
    selectedId: document.root?.id ?? null,
    history: [],
    future: [],
    lastError: '',
  }
}

export const useVisualDocumentStore = create((set, get) => {
  /**
   * 提交一次可撤销命令。history 保存命令前快照，future 在新命令时清空。
   */
  const commit = (nextDocument, selectedId = get().selectedId) => {
    if (!nextDocument || nextDocument === get().doc) return false
    const previous = get().doc
    set((state) => ({
      doc: nextDocument,
      selectedId:
        selectedId && findVisualNode(nextDocument.root, selectedId)
          ? selectedId
          : nextDocument.root?.id ?? null,
      history: [...state.history, previous].slice(-HISTORY_LIMIT),
      future: [],
      lastError: '',
    }))
    persistDocument(nextDocument)
    return true
  }

  return {
    ...initialState(),

    selectNode: (nodeId) => {
      set({ selectedId: nodeId || null })
    },

    addNode: (type, parentId = null, index = -1) => {
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
          type === 'page' ? next.root?.id ?? null : node.id
        return commit(next, selectedId)
      } catch (cause) {
        set({ lastError: String(cause?.message || cause) })
        return false
      }
    },

    duplicateNode: (nodeId) => {
      const state = get()
      const found = findVisualNode(state.doc.root, nodeId)
      if (!found) return false
      const next = duplicateVisualNodeInDocument(state.doc, nodeId)
      const copy = findVisualNode(next.root, found.parent?.id)
      return commit(next, copy?.node?.children?.[found.index + 1]?.id ?? nodeId)
    },

    deleteNode: (nodeId) => {
      const state = get()
      const next = deleteVisualNodeFromDocument(state.doc, nodeId)
      if (next === state.doc) return false
      return commit(next, null)
    },

    moveNode: (nodeId, parentId, index = -1) => {
      const state = get()
      const next = moveVisualNodeInDocument(state.doc, {
        nodeId,
        parentId,
        index,
      })
      return commit(next, nodeId)
    },

    updateNode: (nodeId, patch) => {
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
      const state = get()
      const found = findVisualNode(state.doc.root, nodeId)
      if (!found) return false
      return get().updateNode(nodeId, { locked: !found.node.locked })
    },

    setTitle: (title) => {
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
      const next = createEmptyVisualDocument({ title, withRoot })
      return commit(next, next.root?.id ?? null)
    },

    replaceDocument: (document, { resetHistory = true } = {}) => {
      if (resetHistory) {
        set({
          doc: document,
          selectedId: document?.root?.id ?? null,
          history: [],
          future: [],
          lastError: '',
        })
        persistDocument(document)
        return
      }
      return commit(document)
    },

    undo: () => {
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
      const state = get()
      if (!state.future.length) return false
      const next = state.future[0]
      set({
        doc: next,
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
