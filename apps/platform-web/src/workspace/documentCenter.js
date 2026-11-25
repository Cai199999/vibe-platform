/**
 * 本地文档中心。
 *
 * 只保存在当前浏览器 localStorage，按 projectId 隔离；不写后端、不进全局
 * 向量库。只有 active=true 的文档（最多 3 份）会随 Architect Run / 追加需求
 * 发送给后端做本次 RAG。
 */

const STORAGE_KEY = 'vibe.documentCenter.v1'
export const MAX_DOCUMENTS = 10
export const MAX_ACTIVE_DOCUMENTS = 3
export const MAX_DOCUMENT_BYTES = 200 * 1024
export const MAX_TOTAL_BYTES = 2 * 1024 * 1024

function newId() {
  try {
    return `doc_${crypto.randomUUID().replace(/-/g, '').slice(0, 16)}`
  } catch {
    return `doc_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`
  }
}

function readAll() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function writeAll(state) {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
    return true
  } catch {
    return false
  }
}

function normalizeDoc(doc) {
  if (!doc || typeof doc !== 'object') return null
  const name = String(doc.name || '').trim()
  const content = String(doc.content || '')
  if (!name || !content.trim()) return null
  return {
    id: String(doc.id || newId()),
    name: name.slice(0, 200),
    content,
    size: Number(doc.size) || content.length,
    createdAt: String(doc.createdAt || new Date().toISOString()),
    decision: ['STORE', 'DISCARD', 'ASK'].includes(doc.decision)
      ? doc.decision
      : 'STORE',
    reuse: ['FREQUENT', 'ONE_OFF', 'UNCERTAIN'].includes(doc.reuse)
      ? doc.reuse
      : 'UNCERTAIN',
    reason: String(doc.reason || '').slice(0, 300),
    source: doc.source === 'llm' ? 'llm' : 'rules',
    active: doc.active !== false,
  }
}

function projectDocs(state, projectId) {
  const items = state?.[projectId]
  if (!Array.isArray(items)) return []
  return items.map(normalizeDoc).filter(Boolean)
}

function totalBytes(docs) {
  return docs.reduce((sum, doc) => sum + String(doc.content || '').length, 0)
}

export function loadProjectDocuments(projectId) {
  if (!projectId) return []
  return projectDocs(readAll(), projectId)
}

export function documentCount(projectId) {
  return loadProjectDocuments(projectId).length
}

export function activeKnowledgeDocuments(projectId) {
  return loadProjectDocuments(projectId)
    .filter((doc) => doc.active)
    .slice(0, MAX_ACTIVE_DOCUMENTS)
    .map(({ name, content }) => ({ name, content }))
}

export function mergeKnowledgeDocuments(...groups) {
  const seen = new Set()
  const merged = []
  for (const group of groups) {
    for (const doc of group || []) {
      const name = String(doc?.name || '').trim()
      const content = String(doc?.content || '')
      if (!name || !content) continue
      const key = `${name}\u0000${content}`
      if (seen.has(key)) continue
      seen.add(key)
      merged.push({ name, content })
      if (merged.length >= MAX_ACTIVE_DOCUMENTS) return merged
    }
  }
  return merged
}

export function addProjectDocument(projectId, doc) {
  if (!projectId) return { ok: false, docs: [], error: '缺少项目 ID' }
  const normalized = normalizeDoc(doc)
  if (!normalized) return { ok: false, docs: [], error: '文档内容为空' }
  if (normalized.size > MAX_DOCUMENT_BYTES) {
    return { ok: false, docs: [], error: '单份文档不能超过 200KB' }
  }
  const state = readAll()
  const docs = projectDocs(state, projectId)
  if (docs.length >= MAX_DOCUMENTS) {
    return { ok: false, docs, error: `文档中心最多保存 ${MAX_DOCUMENTS} 份` }
  }
  if (totalBytes(docs) + normalized.size > MAX_TOTAL_BYTES) {
    return { ok: false, docs, error: '文档中心总大小超过 2MB，请先删除旧文档' }
  }
  const activeCount = docs.filter((item) => item.active).length
  const next = [
    ...docs,
    {
      ...normalized,
      active:
        normalized.active !== false &&
        activeCount < MAX_ACTIVE_DOCUMENTS,
    },
  ]
  const ok = writeAll({ ...state, [projectId]: next })
  return ok
    ? { ok: true, docs: next, error: '' }
    : { ok: false, docs, error: '浏览器存储空间不足' }
}

export function removeProjectDocument(projectId, documentId) {
  const state = readAll()
  const docs = projectDocs(state, projectId).filter(
    (doc) => doc.id !== documentId,
  )
  const ok = writeAll({ ...state, [projectId]: docs })
  return { ok, docs, error: ok ? '' : '浏览器存储空间不足' }
}

export function toggleProjectDocument(projectId, documentId, active) {
  const state = readAll()
  const docs = projectDocs(state, projectId)
  if (
    active &&
    docs.filter((doc) => doc.active && doc.id !== documentId).length >=
      MAX_ACTIVE_DOCUMENTS
  ) {
    return {
      ok: false,
      docs,
      error: `最多同时启用 ${MAX_ACTIVE_DOCUMENTS} 份文档`,
    }
  }
  const next = docs.map((doc) =>
    doc.id === documentId ? { ...doc, active: Boolean(active) } : doc,
  )
  const ok = writeAll({ ...state, [projectId]: next })
  return { ok, docs: next, error: ok ? '' : '浏览器存储空间不足' }
}
