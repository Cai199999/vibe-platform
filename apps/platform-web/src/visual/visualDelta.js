/**
 * VisualDocument v0 → current 增量包。
 *
 * 只用于 BACKEND_BINDING 阶段：Architect 不再加载整棵虚拟树，
 * 只审查用户新增 / 删除 / 移动 / 修改的节点及其交互、绑定缺口。
 */

export const VISUAL_V0_STORAGE_PREFIX = 'vibe.visual.v0.'

const NODE_FIELDS = [
  'type',
  'name',
  'componentRef',
  'props',
  'layout',
  'style',
  'semantics',
  'a11y',
  'state',
  'repeat',
  'codegen',
]

function stable(value) {
  if (Array.isArray(value)) return value.map(stable)
  if (value && typeof value === 'object') {
    return Object.keys(value)
      .sort()
      .reduce((result, key) => {
        result[key] = stable(value[key])
        return result
      }, {})
  }
  return value
}

function serialize(value) {
  return JSON.stringify(stable(value ?? null))
}

function flattenNodes(root) {
  const result = []
  const walk = (node, parentId, index, path) => {
    if (!node) return
    result.push({ node, parentId, index, path })
    ;(node.children || []).forEach((child, childIndex) =>
      walk(child, node.id, childIndex, [...path, node.id]),
    )
  }
  if (root) walk(root, null, 0, [])
  return result
}

function nodeSnapshot(node) {
  return {
    id: node.id,
    type: node.type,
    name: node.name || '',
    text: String(node.props?.text || ''),
    className: node.style?.className || '',
    tailwind: node.style?.tailwind || [],
    repeat: node.repeat || null,
    semantics: node.semantics || {},
  }
}

function collectionDiff(before, after, key = 'id') {
  const beforeMap = new Map((before || []).map((item) => [item[key], item]))
  const afterMap = new Map((after || []).map((item) => [item[key], item]))
  const added = []
  const removed = []
  const updated = []
  for (const [id, item] of afterMap) {
    if (!beforeMap.has(id)) {
      added.push(item)
    } else if (serialize(beforeMap.get(id)) !== serialize(item)) {
      updated.push(item)
    }
  }
  for (const [id, item] of beforeMap) {
    if (!afterMap.has(id)) removed.push(item)
  }
  return { added, removed, updated }
}

export function buildVisualDelta(baseline, current) {
  if (!baseline || !current) {
    return {
      from: null,
      to: null,
      added: [],
      removed: [],
      moved: [],
      updated: [],
      interactions: { added: [], removed: [], updated: [] },
      bindings: { added: [], removed: [], updated: [] },
      summary: '没有可比较的 v0 基线',
    }
  }
  const beforeNodes = flattenNodes(baseline.root)
  const afterNodes = flattenNodes(current.root)
  const beforeMap = new Map(beforeNodes.map((item) => [item.node.id, item]))
  const afterMap = new Map(afterNodes.map((item) => [item.node.id, item]))

  const added = []
  const removed = []
  const moved = []
  const updated = []
  for (const [id, item] of afterMap) {
    const previous = beforeMap.get(id)
    if (!previous) {
      added.push({
        ...nodeSnapshot(item.node),
        parentId: item.parentId,
        path: item.path,
      })
      continue
    }
    if (
      previous.parentId !== item.parentId ||
      previous.index !== item.index
    ) {
      moved.push({
        ...nodeSnapshot(item.node),
        fromParentId: previous.parentId,
        toParentId: item.parentId,
        fromIndex: previous.index,
        toIndex: item.index,
      })
    }
    const beforePayload = serialize({
      ...Object.fromEntries(
        NODE_FIELDS.map((field) => [field, previous.node[field]]),
      ),
      children: previous.node.children?.map((child) => child.id),
    })
    const afterPayload = serialize({
      ...Object.fromEntries(
        NODE_FIELDS.map((field) => [field, item.node[field]]),
      ),
      children: item.node.children?.map((child) => child.id),
    })
    if (beforePayload !== afterPayload) {
      updated.push({
        ...nodeSnapshot(item.node),
        before: nodeSnapshot(previous.node),
      })
    }
  }
  for (const [id, item] of beforeMap) {
    if (!afterMap.has(id)) {
      removed.push({
        ...nodeSnapshot(item.node),
        parentId: item.parentId,
        path: item.path,
      })
    }
  }

  const interactions = collectionDiff(
    baseline.interactions,
    current.interactions,
  )
  const bindings = collectionDiff(baseline.bindings, current.bindings)
  const newRepeats = added.filter((node) => node.repeat)
  const newInteractive = added.filter((node) =>
    ['button', 'input', 'form'].includes(node.type),
  )

  return {
    from: {
      docId: baseline.docId,
      revision: baseline.revision,
      contentHash: baseline.metadata?.contentHash || '',
    },
    to: {
      docId: current.docId,
      revision: current.revision,
      status: current.status,
    },
    added,
    removed,
    moved,
    updated,
    interactions,
    bindings,
    newRepeats,
    newInteractive,
    summary: [
      `新增 ${added.length} 个节点`,
      `删除 ${removed.length} 个节点`,
      `移动 ${moved.length} 个节点`,
      `修改 ${updated.length} 个节点`,
      `新增交互 ${interactions.added.length} 个`,
      `新增绑定 ${bindings.added.length} 个`,
    ].join('；'),
  }
}

function storageKey(projectId) {
  return `${VISUAL_V0_STORAGE_PREFIX}${projectId}`
}

export function storeVisualV0Baseline(projectId, document) {
  if (!projectId || !document || typeof window === 'undefined') return false
  try {
    window.localStorage.setItem(
      storageKey(projectId),
      JSON.stringify({
        storedAt: new Date().toISOString(),
        document,
      }),
    )
    return true
  } catch {
    return false
  }
}

export function loadVisualV0Baseline(projectId) {
  if (!projectId || typeof window === 'undefined') return null
  try {
    const raw = window.localStorage.getItem(storageKey(projectId))
    if (!raw) return null
    const parsed = JSON.parse(raw)
    return parsed?.document || null
  } catch {
    return null
  }
}

export function hasVisualV0Baseline(projectId) {
  return Boolean(loadVisualV0Baseline(projectId))
}

export function clearVisualV0Baseline(projectId) {
  if (!projectId || typeof window === 'undefined') return
  try {
    window.localStorage.removeItem(storageKey(projectId))
  } catch {
    // 忽略隐私模式 / 配额异常。
  }
}
