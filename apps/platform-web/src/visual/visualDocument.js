/**
 * VisualDocument：离线优先画布的唯一事实源。
 *
 * 这里的操作全部是纯函数，只改内存中的虚拟组件树，不触碰真实
 * React 源码、沙盒、Git 或任何后端接口。CodeGen 阶段才把冻结后的
 * 文档编译成源码。
 */

export const VISUAL_SCHEMA_VERSION = '1.0'
export const MAX_VISUAL_NODES = 5000
export const MAX_VISUAL_DEPTH = 20

export const NODE_TYPES = [
  'page',
  'section',
  'container',
  'grid',
  'stack',
  'text',
  'button',
  'input',
  'image',
  'list',
  'table',
  'form',
  'modal',
  'component',
]

export const CONTAINER_TYPES = new Set([
  'page',
  'section',
  'container',
  'grid',
  'stack',
  'list',
  'table',
  'form',
  'modal',
  'component',
])

export const REPEAT_TYPES = new Set(['list', 'table'])

export const NODE_LABELS = {
  page: '页面',
  section: '区块',
  container: '容器',
  grid: '网格',
  stack: '纵向栈',
  text: '文本',
  button: '按钮',
  input: '输入框',
  image: '图片',
  list: '列表',
  table: '表格',
  form: '表单',
  modal: '弹窗',
  component: '复合组件',
}

const FORBIDDEN_COORDINATE_KEYS = new Set([
  'x',
  'y',
  'absoluteX',
  'absoluteY',
  'absolute_x',
  'absolute_y',
])
const FORBIDDEN_PROP_KEYS = new Set([
  'dangerouslySetInnerHTML',
  'dangerously_set_inner_html',
  '__html',
])
const NODE_ID_PATTERN = /^nd_[A-Za-z0-9_-]{3,64}$/
const MARKUP_PATTERN = /<\s*\/?\s*[A-Za-z][^>]*>/

const DEFAULT_TOKENS = {
  colors: {
    primary: '#2563eb',
    primarySoft: '#dbeafe',
    surface: '#ffffff',
    canvas: '#f1f5f9',
    text: '#0f172a',
    muted: '#64748b',
    border: '#cbd5e1',
  },
  spacing: { xs: '4px', sm: '8px', md: '16px', lg: '24px', xl: '32px' },
  radius: { sm: '8px', card: '12px', pill: '999px' },
  typography: { body: '14px', title: '22px', caption: '12px' },
}

let fallbackSeq = 0

export function newVisualId(prefix = 'nd') {
  try {
    return `${prefix}_${crypto.randomUUID().replace(/-/g, '').slice(0, 20)}`
  } catch {
    fallbackSeq += 1
    return `${prefix}_${Date.now().toString(36)}${fallbackSeq}`
  }
}

function nowIso() {
  return new Date().toISOString()
}

function defaultLayout(type) {
  if (type === 'grid') {
    return {
      display: 'grid',
      direction: 'row',
      gap: '16px',
      padding: '16px',
      align: 'stretch',
      justify: 'start',
      wrap: true,
      columns: 3,
      minWidth: '',
      maxWidth: '',
    }
  }
  if (type === 'stack') {
    return {
      display: 'flex',
      direction: 'column',
      gap: '12px',
      padding: '0px',
      align: 'stretch',
      justify: 'start',
      wrap: false,
      columns: 1,
      minWidth: '',
      maxWidth: '',
    }
  }
  if (CONTAINER_TYPES.has(type)) {
    return {
      display: type === 'page' ? 'block' : 'flex',
      direction: 'column',
      gap: type === 'page' ? '0px' : '16px',
      padding: type === 'page' ? '32px' : '16px',
      align: 'stretch',
      justify: 'start',
      wrap: false,
      columns: 1,
      minWidth: '',
      maxWidth: type === 'page' ? '1180px' : '',
    }
  }
  return {
    display: 'block',
    direction: 'column',
    gap: '',
    padding: '',
    align: 'stretch',
    justify: 'start',
    wrap: false,
    columns: 1,
    minWidth: '',
    maxWidth: '',
  }
}

function defaultProps(type) {
  if (type === 'text') return { text: '在这里输入文本' }
  if (type === 'button') return { text: '按钮', variant: 'primary' }
  if (type === 'input') return { placeholder: '请输入内容' }
  if (type === 'image') {
    return {
      src: 'https://placehold.co/640x360/e2e8f0/475569?text=Image',
      alt: '示例图片',
    }
  }
  if (type === 'table') {
    return { columns: ['名称', '状态', '操作'] }
  }
  return {}
}

function defaultStyle(type) {
  if (type === 'page') {
    return { tokenRefs: [], className: '', tailwind: ['min-h-screen', 'bg-white'] }
  }
  if (type === 'section' || type === 'container') {
    return {
      tokenRefs: [],
      className: '',
      tailwind: ['rounded-xl', 'border', 'bg-white'],
    }
  }
  if (type === 'button') {
    return {
      tokenRefs: ['color.primary'],
      className: '',
      tailwind: ['rounded-lg', 'bg-blue-600', 'text-white'],
    }
  }
  return { tokenRefs: [], className: '', tailwind: [] }
}

/** 创建一个带完整默认值的 VisualNode。 */
export function createVisualNode(type, overrides = {}) {
  if (!NODE_TYPES.includes(type)) {
    throw new Error(`未知的 VisualNode 类型: ${type}`)
  }
  const base = {
    id: newVisualId(),
    type,
    name: NODE_LABELS[type] || type,
    componentRef: type === 'button' ? 'Button' : '',
    props: defaultProps(type),
    layout: defaultLayout(type),
    style: defaultStyle(type),
    children: [],
    repeat: null,
    semantics: {
      role: '',
      purpose: '',
      importance: 'normal',
      businessEntity: '',
    },
    a11y: { label: '', description: '' },
    state: { defaultVisible: true, disabled: false },
    codegen: {
      mode: 'primitive',
      componentName: '',
      extractComponent: false,
      imports: [],
    },
    provenance: {
      createdBy: 'user',
      revision: 1,
      changeId: '',
    },
    locked: false,
  }
  return {
    ...base,
    ...overrides,
    layout: { ...base.layout, ...(overrides.layout || {}) },
    style: { ...base.style, ...(overrides.style || {}) },
    props: { ...base.props, ...(overrides.props || {}) },
    semantics: { ...base.semantics, ...(overrides.semantics || {}) },
    a11y: { ...base.a11y, ...(overrides.a11y || {}) },
    state: { ...base.state, ...(overrides.state || {}) },
    codegen: { ...base.codegen, ...(overrides.codegen || {}) },
    provenance: { ...base.provenance, ...(overrides.provenance || {}) },
    children: Array.isArray(overrides.children) ? overrides.children : [],
  }
}

export function createEmptyVisualDocument({
  projectId = '',
  title = '未命名页面',
  withRoot = true,
} = {}) {
  const root = withRoot ? createVisualNode('page', { name: '页面' }) : null
  const pages = root
    ? [
        {
          pageId: newVisualId('pg'),
          path: '/',
          title,
          root,
        },
      ]
    : []
  return {
    schemaVersion: VISUAL_SCHEMA_VERSION,
    docId: newVisualId('vdoc'),
    projectId,
    revision: 0,
    status: 'DRAFT',
    title,
    designTokens: JSON.parse(JSON.stringify(DEFAULT_TOKENS)),
    pages,
    activePageId: pages[0]?.pageId || '',
    root,
    interactions: [],
    bindings: [],
    review: {
      frozenAt: null,
      questionRounds: 0,
      answers: [],
    },
    metadata: {
      createdBy: 'user',
      createdAt: nowIso(),
      lastModifiedBy: 'user',
      contentHash: '',
    },
  }
}

/** 返回文档的页面列表；旧单页文档自动归一为一页。 */
export function ensureVisualPages(document) {
  if (!document) return []
  if (Array.isArray(document.pages) && document.pages.length > 0) {
    return document.pages
  }
  if (!document.root) return []
  return [
    {
      pageId: document.activePageId || 'pg_default',
      path: '/',
      title: document.title || '页面',
      root: document.root,
    },
  ]
}

export function activePageIdOf(document) {
  const pages = ensureVisualPages(document)
  if (!pages.length) return ''
  if (
    document?.activePageId &&
    pages.some((page) => page.pageId === document.activePageId)
  ) {
    return document.activePageId
  }
  return pages[0].pageId
}

export function activePageRoot(document) {
  const pages = ensureVisualPages(document)
  const activeId = activePageIdOf(document)
  return pages.find((page) => page.pageId === activeId)?.root || null
}

/** 把 document.root 同步回当前页面，保持 pages 为权威事实源。 */
export function withActivePageRoot(document, root = document?.root) {
  if (!document) return document
  const pages = ensureVisualPages(document)
  if (!pages.length) return { ...document, root: root ?? null }
  const activeId = activePageIdOf(document)
  if (!root) {
    // 删除当前页根节点：移除该页面；若还有其它页面则切到第一页，
    // 否则得到合法空白画布（pages=[] / root=null）。
    const remaining = pages.filter((page) => page.pageId !== activeId)
    const nextActive = remaining[0] || null
    return {
      ...document,
      pages: remaining,
      activePageId: nextActive?.pageId || '',
      root: nextActive?.root || null,
    }
  }
  return {
    ...document,
    pages: pages.map((page) =>
      page.pageId === activeId ? { ...page, root } : page,
    ),
    activePageId: activeId,
    root,
  }
}

export function switchVisualPage(document, pageId) {
  if (!document) return document
  const pages = ensureVisualPages(document)
  const target = pages.find((page) => page.pageId === pageId)
  if (!target) return document
  const synced = withActivePageRoot(document, document.root)
  return {
    ...synced,
    activePageId: pageId,
    root: target.root,
  }
}

export function addVisualPage(
  document,
  { title = '新页面', path = '' } = {},
) {
  const pages = ensureVisualPages(document)
  const usedPaths = new Set(pages.map((page) => page.path))
  let routePath = path || `/page-${pages.length + 1}`
  if (!routePath.startsWith('/')) routePath = `/${routePath}`
  while (usedPaths.has(routePath)) {
    routePath = `${routePath}-${pages.length + 1}`
  }
  const root = createVisualNode('page', { name: title })
  root.props = { title }
  const page = {
    pageId: newVisualId('pg'),
    path: routePath,
    title,
    root,
  }
  const synced = withActivePageRoot(document, document.root)
  return {
    document: {
      ...synced,
      pages: [...ensureVisualPages(synced), page],
      activePageId: page.pageId,
      root,
    },
    page,
  }
}

export function cloneVisualNode(node, createdBy = 'user') {
  return {
    ...JSON.parse(JSON.stringify(node)),
    id: newVisualId(),
    children: (node.children || []).map((child) =>
      cloneVisualNode(child, createdBy),
    ),
    provenance: { createdBy, revision: 1, changeId: '' },
  }
}

export function findVisualNode(root, nodeId) {
  if (!root) return null
  const walk = (node, parent, index) => {
    if (node.id === nodeId) return { node, parent, index }
    for (let childIndex = 0; childIndex < node.children.length; childIndex += 1) {
      const found = walk(node.children[childIndex], node, childIndex)
      if (found) return found
    }
    return null
  }
  return walk(root, null, -1)
}

export function isVisualDescendant(ancestor, nodeId) {
  if (!ancestor || !nodeId) return false
  return ancestor.children.some(
    (child) => child.id === nodeId || isVisualDescendant(child, nodeId),
  )
}

export function replaceVisualNode(root, nodeId, updater) {
  if (!root) return root
  if (root.id === nodeId) return updater(root)
  let changed = false
  const children = root.children.map((child) => {
    const next = replaceVisualNode(child, nodeId, updater)
    if (next !== child) changed = true
    return next
  })
  return changed ? { ...root, children } : root
}

function removeVisualNode(root, nodeId) {
  if (!root) return { root, removed: null }
  if (root.id === nodeId) return { root: null, removed: root }
  for (let index = 0; index < root.children.length; index += 1) {
    const child = root.children[index]
    if (child.id === nodeId) {
      return {
        root: {
          ...root,
          children: root.children.filter((item) => item.id !== nodeId),
        },
        removed: child,
      }
    }
    const nested = removeVisualNode(child, nodeId)
    if (nested.removed) {
      const children = [...root.children]
      children[index] = nested.root
      return { root: { ...root, children }, removed: nested.removed }
    }
  }
  return { root, removed: null }
}

function insertVisualNode(root, parentId, node, index = -1) {
  if (!root) return root
  if (root.id === parentId) {
    const nextChildren = [...root.children]
    const at = index < 0 ? nextChildren.length : Math.min(index, nextChildren.length)
    nextChildren.splice(at, 0, node)
    return { ...root, children: nextChildren }
  }
  let changed = false
  const children = root.children.map((child) => {
    const next = insertVisualNode(child, parentId, node, index)
    if (next !== child) changed = true
    return next
  })
  return changed ? { ...root, children } : root
}

function touchDocument(document, editor = 'user') {
  const synced = withActivePageRoot(document, document.root)
  return {
    ...synced,
    revision: (document.revision || 0) + 1,
    status: document.status === 'FROZEN' ? 'DRAFT' : document.status,
    metadata: {
      ...document.metadata,
      lastModifiedBy: editor,
      contentHash: '',
    },
  }
}

/**
 * 插入节点。空画布首次添加非 page 节点时，会自动创建一个 page 根。
 * index < 0 表示追加到父容器末尾。
 */
export function insertVisualNodeIntoDocument(
  document,
  { parentId, type, index = -1, node, editor = 'user' },
) {
  if (!NODE_TYPES.includes(type)) throw new Error(`未知节点类型: ${type}`)
  let root = document.root
  if (!root) {
    if (type === 'page') {
      root = node || createVisualNode('page')
      return touchDocument({ ...document, root }, editor)
    }
    const page = createVisualNode('page', { name: '页面' })
    const child = node || createVisualNode(type)
    page.children = [child]
    return touchDocument({ ...document, root: page }, editor)
  }
  if (type === 'page') {
    throw new Error('页面根节点已存在；请直接编辑现有 page')
  }
  const target = findVisualNode(root, parentId || root.id)
  if (!target) throw new Error('目标父容器不存在')
  if (!CONTAINER_TYPES.has(target.node.type)) {
    throw new Error('目标节点不是容器，不能插入子节点')
  }
  if (target.node.locked) throw new Error('目标容器已锁定')
  const nextNode = node || createVisualNode(type)
  return touchDocument(
    {
      ...document,
      root: insertVisualNode(root, target.node.id, nextNode, index),
    },
    editor,
  )
}

/** 删除任意节点；删除根节点会得到合法空白画布。 */
export function deleteVisualNodeFromDocument(
  document,
  nodeId,
  editor = 'user',
) {
  const found = findVisualNode(document.root, nodeId)
  if (!found) return document
  // 页面根是隐式容器：不可删除；否则用户会把整页从 VisualDocument 中删掉。
  if (found.parent === null || found.node.type === 'page') return document
  if (found.node.locked) return document
  const { root, removed } = removeVisualNode(document.root, nodeId)
  if (!removed) return document
  return touchDocument({ ...document, root }, editor)
}

/**
 * 移动 / 换父容器。
 *
 * index 使用“移动前”的目标 children 下标；同父容器向后移动时会自动
 * 扣除被移除节点自身占用的位置。
 */
export function moveVisualNodeInDocument(
  document,
  { nodeId, parentId, index = -1, editor = 'user' },
) {
  const root = document.root
  const source = findVisualNode(root, nodeId)
  const target = findVisualNode(root, parentId)
  if (!source || !target) return document
  if (source.node.locked || target.node.locked) return document
  if (source.parent === null) return document
  if (target.node.id === nodeId || isVisualDescendant(source.node, target.node.id)) {
    return document
  }
  if (!CONTAINER_TYPES.has(target.node.type)) return document

  let insertIndex = index
  if (insertIndex < 0) insertIndex = target.node.children.length
  if (source.parent.id === target.node.id && source.index < insertIndex) {
    insertIndex -= 1
  }
  const { root: withoutSource } = removeVisualNode(root, nodeId)
  const nextRoot = insertVisualNode(withoutSource, target.node.id, source.node, insertIndex)
  if (nextRoot === withoutSource) return document
  return touchDocument({ ...document, root: nextRoot }, editor)
}

/** 更新节点；patch 为浅合并，嵌套字段请自行传入完整对象。 */
export function updateVisualNodeInDocument(
  document,
  nodeId,
  patch,
  editor = 'user',
) {
  const found = findVisualNode(document.root, nodeId)
  if (!found || found.node.locked) return document
  const nextPatch = typeof patch === 'function' ? patch(found.node) : patch
  if (!nextPatch) return document
  return touchDocument(
    {
      ...document,
      root: replaceVisualNode(document.root, nodeId, (node) => ({
        ...node,
        ...nextPatch,
        props: { ...node.props, ...(nextPatch.props || {}) },
        layout: { ...node.layout, ...(nextPatch.layout || {}) },
        style: { ...node.style, ...(nextPatch.style || {}) },
        semantics: { ...node.semantics, ...(nextPatch.semantics || {}) },
        a11y: { ...node.a11y, ...(nextPatch.a11y || {}) },
        state: { ...node.state, ...(nextPatch.state || {}) },
        codegen: { ...node.codegen, ...(nextPatch.codegen || {}) },
      })),
    },
    editor,
  )
}

export function duplicateVisualNodeInDocument(
  document,
  nodeId,
  editor = 'user',
) {
  const found = findVisualNode(document.root, nodeId)
  if (!found || !found.parent || found.node.locked) return document
  const copy = cloneVisualNode(found.node, editor)
  return insertVisualNodeIntoDocument(document, {
    parentId: found.parent.id,
    type: found.node.type,
    index: found.index + 1,
    node: copy,
    editor,
  })
}

export function validateVisualDocument(document) {
  const errors = []
  const warnings = []
  if (!document || typeof document !== 'object') {
    return {
      ok: false,
      errors: ['VisualDocument 必须是对象'],
      warnings,
      nodeCount: 0,
      maxDepth: 0,
    }
  }
  if (document.schemaVersion !== VISUAL_SCHEMA_VERSION) {
    errors.push(`schemaVersion 必须是 ${VISUAL_SCHEMA_VERSION}`)
  }
  if (!document.docId) errors.push('缺少 docId')
  if (!Number.isInteger(document.revision) || document.revision < 0) {
    errors.push('revision 必须是非负整数')
  }
  if (!Array.isArray(document.interactions)) {
    errors.push('interactions 必须是数组')
  }
  if (!Array.isArray(document.bindings)) {
    errors.push('bindings 必须是数组')
  }

  const pages = ensureVisualPages(document)
  const pageIds = new Set()
  const pagePaths = new Set()
  pages.forEach((page) => {
    if (!page?.pageId) {
      errors.push('页面缺少 pageId')
      return
    }
    if (pageIds.has(page.pageId)) errors.push(`重复 pageId: ${page.pageId}`)
    pageIds.add(page.pageId)
    const path = page.path || '/'
    if (pagePaths.has(path)) errors.push(`重复页面路径: ${path}`)
    pagePaths.add(path)
    if (!page.root || page.root.type !== 'page') {
      errors.push(`页面 ${page.pageId} 的 root 必须是 page 节点`)
    }
  })
  if (document.activePageId && !pageIds.has(document.activePageId)) {
    errors.push(`activePageId ${document.activePageId} 不在 pages 中`)
  }

  const nodeIds = new Set()
  const bindingIds = new Set()
  let nodeCount = 0
  let maxDepth = 0

  const walk = (node, depth) => {
    if (!node || typeof node !== 'object') {
      errors.push('节点必须是对象')
      return
    }
    nodeCount += 1
    maxDepth = Math.max(maxDepth, depth)
    if (nodeCount > MAX_VISUAL_NODES) {
      errors.push(`节点数超过上限 ${MAX_VISUAL_NODES}`)
      return
    }
    if (depth > MAX_VISUAL_DEPTH) {
      errors.push(`层级超过上限 ${MAX_VISUAL_DEPTH}`)
      return
    }
    if (!NODE_ID_PATTERN.test(String(node.id || ''))) {
      errors.push(`非法 nodeId: ${node.id}`)
    } else if (nodeIds.has(node.id)) {
      errors.push(`重复 nodeId: ${node.id}`)
    } else {
      nodeIds.add(node.id)
    }
    if (!NODE_TYPES.includes(node.type)) {
      errors.push(`未知节点类型: ${node.type}`)
    }
    if (!Array.isArray(node.children)) {
      errors.push(`节点 ${node.id} 的 children 必须是数组`)
      return
    }
    if (node.children.length && !CONTAINER_TYPES.has(node.type)) {
      errors.push(`节点 ${node.id} 类型 ${node.type} 不能包含子节点`)
    }
    const forbidden = Object.keys(node).filter((key) =>
      FORBIDDEN_COORDINATE_KEYS.has(key),
    )
    if (forbidden.length) {
      errors.push(`节点 ${node.id} 含像素坐标: ${forbidden.join(', ')}`)
    }
    const layoutForbidden = Object.keys(node.layout || {}).filter((key) =>
      FORBIDDEN_COORDINATE_KEYS.has(key),
    )
    if (layoutForbidden.length) {
      errors.push(
        `节点 ${node.id} 的 layout 含像素坐标: ${layoutForbidden.join(', ')}`,
      )
    }
    const badProps = Object.keys(node.props || {}).filter((key) =>
      FORBIDDEN_PROP_KEYS.has(key),
    )
    if (badProps.length) {
      errors.push(`节点 ${node.id} 含原始 HTML 注入: ${badProps.join(', ')}`)
    }
    Object.entries(node.props || {}).forEach(([key, value]) => {
      if (typeof value === 'string' && MARKUP_PATTERN.test(value)) {
        errors.push(`节点 ${node.id} 的 props.${key} 含 JSX/HTML 标记`)
      }
    })
    if (node.repeat && !REPEAT_TYPES.has(node.type)) {
      errors.push(`节点 ${node.id} 只有 list/table 支持 repeat`)
    }
    node.children.forEach((child) => walk(child, depth + 1))
  }

  pages.forEach((page) => {
    if (page?.root) walk(page.root, 1)
  })

  for (const binding of document.bindings || []) {
    if (!binding?.id) {
      errors.push('binding 缺少 id')
      continue
    }
    if (bindingIds.has(binding.id)) {
      errors.push(`重复 bindingId: ${binding.id}`)
    }
    bindingIds.add(binding.id)
    if (pages.length > 0 && !nodeIds.has(binding.nodeId)) {
      errors.push(`binding ${binding.id} 指向不存在的节点 ${binding.nodeId}`)
    }
  }
  const interactionIds = new Set()
  for (const interaction of document.interactions || []) {
    if (!interaction?.id) {
      errors.push('interaction 缺少 id')
      continue
    }
    if (interactionIds.has(interaction.id)) {
      errors.push(`重复 interactionId: ${interaction.id}`)
    }
    interactionIds.add(interaction.id)
    if (pages.length > 0 && !nodeIds.has(interaction.nodeId)) {
      errors.push(
        `interaction ${interaction.id} 指向不存在的节点 ${interaction.nodeId}`,
      )
    }
    if (interaction.bindingId && !bindingIds.has(interaction.bindingId)) {
      errors.push(
        `interaction ${interaction.id} 引用不存在的 binding ${interaction.bindingId}`,
      )
    }
    if (
      interaction.targetPageId &&
      pageIds.size > 0 &&
      !pageIds.has(interaction.targetPageId)
    ) {
      errors.push(
        `interaction ${interaction.id} 指向不存在的页面 ${interaction.targetPageId}`,
      )
    }
  }

  const visit = (node) => {
    if (!node) return
    if (node.repeat) {
      const source = node.repeat.source
      const matched = (document.bindings || []).some(
        (binding) => binding.id === source || binding.entity === source,
      )
      if (!matched) {
        const target = document.status === 'COMPILED' ? errors : warnings
        target.push(
          `节点 ${node.id} 的 repeat.source=${source} 尚未绑定` +
            (target === warnings ? '，将在业务审查阶段补齐' : ''),
        )
      }
    }
    node.children.forEach(visit)
  }
  pages.forEach((page) => {
    if (page?.root) visit(page.root)
  })

  if (document.status === 'FROZEN' && !document.metadata?.contentHash) {
    warnings.push('FROZEN 文档缺少 contentHash')
  }

  return {
    ok: errors.length === 0,
    errors,
    warnings,
    nodeCount,
    maxDepth,
  }
}

export function countVisualNodes(root) {
  if (!root) return 0
  return 1 + root.children.reduce((sum, child) => sum + countVisualNodes(child), 0)
}

/**
 * 从 Architect 契约中取出 VisualDocument，并补齐项目作用域。
 * 返回 null 表示旧契约没有虚拟树（走旧链路）。
 */
export function visualDocumentFromContract(
  contract,
  { projectId = '', title = '' } = {},
) {
  const visual = contract?.visualDocument
  if (!visual || typeof visual !== 'object') return null
  return {
    ...visual,
    projectId: visual.projectId || projectId,
    title: visual.title || title || '未命名页面',
  }
}

const GENERATED_NODE_TYPES = new Set(
  NODE_TYPES.filter((type) => type !== 'page'),
)
const MAX_AGENT_NODES = 80
const MAX_AGENT_DEPTH = 6

export function collectVisualNodeIds(root, target = new Set()) {
  if (!root) return target
  target.add(root.id)
  root.children.forEach((child) => collectVisualNodeIds(child, target))
  return target
}

/**
 * 规范化 Agent 返回的节点：
 *  - 重新分配不冲突的 nodeId；
 *  - 拒绝 page 根、非容器 children、过深/过多节点；
 *  - 保留后端校验过的结构，不在前端猜测业务。
 */
export function prepareAgentNodes(nodes, existingIds = new Set()) {
  if (!Array.isArray(nodes) || nodes.length === 0) {
    throw new Error('Agent 没有返回任何节点')
  }
  const used = new Set(existingIds)
  let count = 0

  const walk = (raw, depth) => {
    if (!raw || typeof raw !== 'object') {
      throw new Error('Agent 返回了非对象节点')
    }
    if (depth > MAX_AGENT_DEPTH) {
      throw new Error('Agent 返回的节点层级过深')
    }
    count += 1
    if (count > MAX_AGENT_NODES) {
      throw new Error('Agent 返回的节点数量过多')
    }
    if (!GENERATED_NODE_TYPES.has(raw.type)) {
      throw new Error(`Agent 返回了非法节点类型: ${raw.type}`)
    }
    if (raw.children != null && !Array.isArray(raw.children)) {
      throw new Error('Agent 节点的 children 必须是数组')
    }
    const children = raw.children || []
    if (children.length && !CONTAINER_TYPES.has(raw.type)) {
      throw new Error(`节点 ${raw.type} 不能包含 children`)
    }
    let id = String(raw.id || '')
    if (!NODE_ID_PATTERN.test(id) || used.has(id)) {
      id = newVisualId()
    }
    while (used.has(id)) id = newVisualId()
    used.add(id)
    return {
      ...JSON.parse(JSON.stringify(raw)),
      id,
      children: children.map((child) => walk(child, depth + 1)),
    }
  }

  return nodes.map((node) => walk(node, 1))
}

/**
 * 一次性插入一组 Agent 节点；只提交一次 revision 与撤销快照。
 */
export function insertVisualNodesIntoDocument(
  document,
  { parentId, nodes, index = -1, editor = 'agent' },
) {
  let root = document.root
  const target = root ? findVisualNode(root, parentId) : null
  if (!root) {
    root = createVisualNode('page')
  }
  if (!target && parentId && parentId !== root.id) {
    throw new Error('目标父容器不存在')
  }
  const targetNode = target?.node || root
  if (!CONTAINER_TYPES.has(targetNode.type)) {
    throw new Error('目标节点不是容器')
  }
  if (targetNode.locked) throw new Error('目标容器已锁定')
  const existingIds = collectVisualNodeIds(root)
  const prepared = prepareAgentNodes(nodes, existingIds)
  const start = index < 0 ? targetNode.children.length : index
  let nextRoot = root
  prepared.forEach((node, offset) => {
    nextRoot = insertVisualNode(
      nextRoot,
      targetNode.id,
      node,
      start + offset,
    )
  })
  return {
    document: touchDocument({ ...document, root: nextRoot }, editor),
    insertedIds: prepared.map((node) => node.id),
  }
}

function sortJson(value) {
  if (Array.isArray(value)) return value.map(sortJson)
  if (value && typeof value === 'object') {
    return Object.keys(value)
      .sort()
      .reduce((result, key) => {
        result[key] = sortJson(value[key])
        return result
      }, {})
  }
  return value
}

async function sha256Hex(text) {
  const subtle = globalThis.crypto?.subtle
  if (!subtle) {
    throw new Error('当前浏览器不支持 contentHash 计算（Web Crypto 不可用）')
  }
  const digest = await subtle.digest('SHA-256', new TextEncoder().encode(text))
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('')
}

/**
 * 冻结文档：锁定状态、写 contentHash。与 Python freeze_visual_document
 * 使用相同的字段排除与稳定 JSON 排序。
 */
export async function freezeVisualDocument(document) {
  const frozen = JSON.parse(JSON.stringify(document))
  frozen.status = 'FROZEN'
  frozen.review = {
    ...(frozen.review || {}),
    frozenAt: new Date().toISOString(),
    questionRounds: frozen.review?.questionRounds || 0,
    answers: frozen.review?.answers || [],
  }
  frozen.revision = (Number(frozen.revision) || 0) + 1
  const payload = JSON.parse(JSON.stringify(frozen))
  if (payload.metadata) delete payload.metadata.contentHash
  const hash = await sha256Hex(JSON.stringify(sortJson(payload)))
  frozen.metadata = {
    ...(frozen.metadata || {}),
    contentHash: `sha256:${hash}`,
  }
  return frozen
}
