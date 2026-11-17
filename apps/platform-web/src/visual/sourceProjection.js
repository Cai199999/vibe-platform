/**
 * 存量 React 源码 → 只读虚拟投影。
 *
 * 这是轨道二（存量项目）的 MVP 适配层：
 *   - 输入是 analyze_existing_react.mjs 的只读分析报告；
 *   - 输出是 VisualNode 形状的投影，仅供画布渲染与 hover 检查；
 *   - 绝不写回源码、绝不写入 visualDocumentStore、绝不触发 CodeGen。
 *
 * 分层：分析报告（Node/Babel） → 本适配器（纯函数） → 只读画布。
 * 后续若接入 AST 写回，应在独立命令层实现，不能污染本模块。
 */

export const SOURCE_PROJECTION_SCHEMA_VERSION = '0.1-projection'
export const SOURCE_PROJECTION_TIP =
  '当前为只读分析视图。源码直改（AST）能力尚未开放'
export const SOURCE_PROJECTION_PRINCIPLE =
  '存量项目不会转换为虚拟树，源码始终是唯一事实源；后续编辑走 AST 直改。'
const MAX_INLINE_DEPTH = 6
const MAX_PROJECTION_NODES = 2000

const SECTION_TAGS = new Set([
  'section',
  'header',
  'footer',
  'main',
  'article',
  'nav',
  'aside',
])
const TEXT_TAGS = new Set([
  'h1',
  'h2',
  'h3',
  'h4',
  'h5',
  'h6',
  'p',
  'span',
  'label',
  'strong',
  'em',
  'small',
  'code',
  'pre',
  'figcaption',
  'blockquote',
  'li',
  'td',
  'th',
])
const CONTAINER_TAGS = new Set([
  'div',
  'ul',
  'ol',
  'thead',
  'tbody',
  'tr',
  'dl',
  'dd',
  'dt',
])
const VOID_INPUT_TAGS = new Set(['input', 'textarea', 'select'])

const SPACING = {
  0: '0px',
  1: '4px',
  2: '8px',
  3: '12px',
  4: '16px',
  5: '20px',
  6: '24px',
  8: '32px',
  10: '40px',
  12: '48px',
}

export class SourceProjectionError extends Error {
  constructor(message) {
    super(message)
    this.name = 'SourceProjectionError'
  }
}

function projectedType(node) {
  if (!node) return 'container'
  if (node.kind === 'text' || node.kind === 'expression') return 'text'
  if (node.kind === 'fragment') return 'container'
  if (node.kind === 'component') return 'component'
  const tag = String(node.tag || '').toLowerCase()
  if (tag === 'button') return 'button'
  if (VOID_INPUT_TAGS.has(tag)) return 'input'
  if (tag === 'img' || tag === 'svg') return 'image'
  if (tag === 'form') return 'form'
  if (tag === 'table') return 'table'
  if (tag === 'ul' || tag === 'ol') return 'list'
  if (SECTION_TAGS.has(tag)) return 'section'
  if (TEXT_TAGS.has(tag)) return 'text'
  if (CONTAINER_TAGS.has(tag)) return 'container'
  if (tag === 'a') return node.text ? 'text' : 'container'
  return 'container'
}

function spacingFromClass(token, prefix) {
  const match = new RegExp(`^${prefix}-(\\d+(?:\\.\\d+)?)$`).exec(token)
  if (!match) return ''
  return SPACING[match[1]] || ''
}

function inferLayout(node, classes) {
  const tokens = Array.isArray(classes) ? classes : []
  const has = (value) => tokens.includes(value)
  const layout = {
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
  if (has('flex') || tokens.some((item) => item === 'inline-flex')) {
    layout.display = has('inline-flex') ? 'inline-flex' : 'flex'
    layout.direction = has('flex-row') ? 'row' : 'column'
  }
  if (has('grid')) layout.display = 'grid'
  const gridCols = tokens
    .map((item) => /^grid-cols-(\d+)$/.exec(item))
    .find(Boolean)
  if (gridCols) layout.columns = Math.max(1, Math.min(12, Number(gridCols[1])))
  const gap = tokens.map((item) => spacingFromClass(item, 'gap')).find(Boolean)
  if (gap) layout.gap = gap
  const padding = tokens.map((item) => spacingFromClass(item, 'p')).find(Boolean)
  if (padding) layout.padding = padding
  if (tokens.includes('items-center')) layout.align = 'center'
  else if (tokens.includes('items-start')) layout.align = 'start'
  else if (tokens.includes('items-end')) layout.align = 'end'
  if (tokens.includes('justify-center')) layout.justify = 'center'
  else if (tokens.includes('justify-between')) layout.justify = 'between'
  else if (tokens.includes('justify-end')) layout.justify = 'end'
  if (tokens.includes('flex-wrap')) layout.wrap = true
  const maxWidth = tokens
    .map((item) => /^max-w-(\S+)$/.exec(item))
    .find(Boolean)
  if (maxWidth && !maxWidth[1].startsWith('[')) {
    layout.maxWidth = maxWidth[1] === 'full' ? '100%' : `${maxWidth[1]}`
  }
  if (!node || node.kind === 'text') layout.display = 'block'
  return layout
}

function nodeName(node, type) {
  if (node.kind === 'component') return node.tag
  if (node.kind === 'fragment') return 'Fragment'
  if (node.kind === 'text') {
    const text = String(node.text || '').trim()
    return text ? `文本 · ${text.slice(0, 24)}` : '文本'
  }
  if (node.kind === 'expression') return '动态表达式'
  return node.tag || type
}

function nodeProps(node, type) {
  const attributes = node.attributes || {}
  if (type === 'text') {
    return {
      text:
        node.kind === 'expression'
          ? `{${node.expression || '…'}}`
          : String(node.text || ''),
    }
  }
  if (type === 'button') {
    return {
      text:
        String(node.text || '').trim() ||
        String(attributes['aria-label'] || '') ||
        node.tag ||
        '按钮',
    }
  }
  if (type === 'input') {
    return {
      placeholder: String(attributes.placeholder || ''),
      type: String(attributes.type || 'text'),
    }
  }
  if (type === 'image') {
    return {
      src: String(attributes.src || ''),
      alt: String(attributes.alt || ''),
    }
  }
  return {}
}

function collectProjectionNodes(node, output = []) {
  if (!node) return output
  output.push(node)
  for (const child of node.children || []) collectProjectionNodes(child, output)
  return output
}

/**
 * 把分析报告转换为只读投影文档。
 * 任何字段缺失或报告未声明只读，都直接拒绝，避免悄悄进入写回链路。
 */
export function buildSourceProjection(analysis) {
  if (!analysis || typeof analysis !== 'object') {
    throw new SourceProjectionError('分析报告为空或格式不合法')
  }
  if (analysis.analysisOnly !== true || analysis.writable !== false) {
    throw new SourceProjectionError(
      '分析报告未声明 analysisOnly=true / writable=false',
    )
  }
  if (!analysis.root) {
    throw new SourceProjectionError('分析报告没有可渲染的入口组件')
  }

  const components = Array.isArray(analysis.components) ? analysis.components : []
  const componentByName = new Map(
    components.filter((item) => item?.name).map((item) => [item.name, item]),
  )
  const counter = { nodes: 0 }
  let sequence = 0
  const nextId = () => `sp_${String(++sequence).padStart(4, '0')}`

  function convert(node, context) {
    counter.nodes += 1
    if (counter.nodes > MAX_PROJECTION_NODES) {
      throw new SourceProjectionError(
        `投影节点超过上限 ${MAX_PROJECTION_NODES}，请缩小分析范围`,
      )
    }
    const type = projectedType(node)
    const classes = Array.isArray(node.classes) ? node.classes : []
    const tag = String(node.tag || type)
    const path = [...context.path, tag]
    const componentDefinition =
      node.kind === 'component' ? componentByName.get(node.tag) : null
    const canInline =
      Boolean(componentDefinition) &&
      !context.stack.includes(node.tag) &&
      context.depth < MAX_INLINE_DEPTH
    let children = []
    if (node.kind !== 'text' && node.kind !== 'expression') {
      if (canInline) {
        children = componentDefinition.roots.map((root) =>
          convert(root, {
            path,
            stack: [...context.stack, node.tag],
            depth: context.depth + 1,
          }),
        )
      } else {
        children = (node.children || []).map((child) =>
          convert(child, {
            path,
            stack: context.stack,
            depth: context.depth + 1,
          }),
        )
      }
    }

    return {
      id: nextId(),
      originNodeId: node.id || '',
      type,
      name: nodeName(node, type),
      componentRef: node.kind === 'component' ? node.tag : '',
      props: nodeProps(node, type),
      layout: inferLayout(node, classes),
      style: {
        tokenRefs: [],
        className: String(node.className || ''),
        tailwind: classes,
      },
      children,
      repeat: null,
      semantics: {
        role:
          node.kind === 'component'
            ? 'source-component'
            : node.kind === 'text'
              ? 'source-text'
              : 'source-element',
        purpose: String(node.className || node.text || node.tag || ''),
        importance: 'normal',
        businessEntity: '',
      },
      a11y: {
        label: String(node.attributes?.['aria-label'] || ''),
        description: '',
      },
      state: {
        defaultVisible: true,
        disabled: Boolean(node.attributes?.disabled),
      },
      codegen: {
        mode: 'primitive',
        componentName: node.kind === 'component' ? node.tag : '',
        extractComponent: false,
        imports: [],
      },
      provenance: { createdBy: 'user', revision: 1, changeId: '' },
      locked: true,
      projection: {
        analysisOnly: true,
        writable: false,
        kind: String(node.kind || 'element'),
        tag,
        className: String(node.className || ''),
        classes,
        attributes: node.attributes || {},
        source: node.source || null,
        path,
        inlined: canInline,
        cycle: Boolean(componentDefinition) && context.stack.includes(node.tag),
        external:
          node.kind === 'component' && !componentDefinition,
      },
    }
  }

  const entryName =
    analysis.entryComponent ||
    components.find((item) => item.exported === 'default')?.name ||
    ''
  const root = convert(analysis.root, {
    path: entryName ? [entryName] : [],
    stack: entryName ? [entryName] : [],
    depth: 0,
  })
  const projectedNodes = collectProjectionNodes(root)
  return {
    schemaVersion: SOURCE_PROJECTION_SCHEMA_VERSION,
    kind: 'SourceProjectionDocument',
    analysisOnly: true,
    writable: false,
    title: analysis.file || '存量 React 源码',
    entryComponent: entryName || null,
    root,
    componentHierarchies: Array.isArray(analysis.componentHierarchies)
      ? analysis.componentHierarchies
      : [],
    stats: {
      ...(analysis.stats || {}),
      projectionNodes: projectedNodes.length,
    },
    metadata: {
      sourceFile: analysis.file || '',
      analysisSchemaVersion: analysis.schemaVersion || '',
      generatedAt: new Date().toISOString(),
    },
    tip: SOURCE_PROJECTION_TIP,
    principle: SOURCE_PROJECTION_PRINCIPLE,
  }
}

export function flattenSourceProjection(root) {
  return collectProjectionNodes(root)
}

export function projectionHierarchyText(node) {
  const path = node?.projection?.path || []
  return path.length ? path.join(' › ') : ''
}

export function projectionClassName(node) {
  return node?.projection?.className || ''
}

export function projectionSourceText(node) {
  const source = node?.projection?.source
  if (!source) return ''
  return `${source.file}:${source.line}:${source.column}`
}
