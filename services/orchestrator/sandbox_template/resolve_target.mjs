#!/usr/bin/env node
/**
 * 目标解析器（Phase 1 · 确定性锚点）。
 *
 * 输入（spec.json）：
 *   {
 *     "workdir": "/app",
 *     "anchors": [{
 *       "anchorId": "anc_ann_1", "annotationId": "ann_1",
 *       "file": "src/App.jsx", "line": 15, "column": 7,
 *       "selector": "[data-vibe-target=\"x\"]", "component": "App",
 *       "instance": 1, "instanceCount": 1, "callSiteFile": ""
 *     }]
 *   }
 *
 * 输出（stdout JSON）：
 *   { "anchors": [ TargetAnchor + status/candidates ] }
 *
 * 解析优先级：
 *   1. 选择器精确命中；命中多个时用实例序号 / 行列消歧；
 *   2. 选择器失效时退回前端行列，取最内层 JSX 节点；
 *   3. 仍无法唯一定位 -> TARGET_AMBIGUOUS / TARGET_NOT_FOUND。
 *
 * 退出码：0 = 正常（逐锚点状态见 JSON）；2 = 脚本自身异常（调用方降级）。
 */
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'

function loadParser(workdir) {
  const candidates = [
    '/opt/vibe-ast/index.js',
    path.join(workdir || '/app', 'package.json'),
    '/app/package.json',
  ]
  let lastError = null
  for (const anchor of candidates) {
    try {
      const requireFrom = createRequire(anchor)
      return requireFrom('@babel/parser')
    } catch (error) {
      lastError = error
    }
  }
  throw new Error(`Babel parser 依赖缺失：${lastError?.message || lastError}`)
}

const SOURCE_MAP_CANDIDATES = [
  'vibe-source-map.json',
  '.vibe/vibe-source-map.json',
  'public/vibe-source-map.json',
]
const SOURCE_MAP_CACHE = new Map()

/**
 * 构建期 source map：oid -> 源码范围。
 * 解析器优先查这张表；查不到才回退 selector / file:line。
 */
function loadSourceMap(workdir) {
  const root = workdir || '/app'
  if (SOURCE_MAP_CACHE.has(root)) return SOURCE_MAP_CACHE.get(root)
  for (const relative of SOURCE_MAP_CANDIDATES) {
    const candidate = path.join(root, relative)
    try {
      const raw = fs.readFileSync(candidate, 'utf8')
      const parsed = JSON.parse(raw)
      if (parsed && parsed.nodes && typeof parsed.nodes === 'object') {
        SOURCE_MAP_CACHE.set(root, parsed)
        return parsed
      }
    } catch {
      // 继续尝试下一个位置。
    }
  }
  SOURCE_MAP_CACHE.set(root, null)
  return null
}

function sourceMapNodeForOid(workdir, oid) {
  if (!oid) return null
  const map = loadSourceMap(workdir)
  return map?.nodes?.[oid] || null
}

const JSX_TYPES = new Set(['JSXElement', 'JSXFragment'])
const FUNCTION_TYPES = new Set([
  'FunctionDeclaration',
  'FunctionExpression',
  'ArrowFunctionExpression',
  'ObjectMethod',
  'ClassMethod',
])

function walk(node, parent, visit) {
  if (!node || typeof node !== 'object') return
  if (Array.isArray(node)) {
    for (const item of node) walk(item, parent, visit)
    return
  }
  if (typeof node.type !== 'string') return
  visit(node, parent)
  for (const key of Object.keys(node)) {
    if (key === 'loc' || key === 'start' || key === 'end') continue
    walk(node[key], node, visit)
  }
}

function buildIndex(ast) {
  const parents = new Map()
  const jsxNodes = []
  const functions = []
  const exports = new Set()
  walk(ast, null, (node, parent) => {
    if (parent) parents.set(node, parent)
    if (JSX_TYPES.has(node.type)) jsxNodes.push(node)
    if (FUNCTION_TYPES.has(node.type)) functions.push(node)
    if (node.type === 'ExportDefaultDeclaration') {
      const decl = node.declaration
      if (decl?.type === 'FunctionDeclaration' && decl.id) {
        exports.add(decl.id.name)
      } else if (decl?.type === 'Identifier') {
        exports.add(decl.name)
      }
    }
    if (node.type === 'ExportNamedDeclaration' && node.declaration) {
      const decl = node.declaration
      if (decl.id?.name) exports.add(decl.id.name)
      if (decl.type === 'VariableDeclaration') {
        for (const item of decl.declarations || []) {
          if (item.id?.name) exports.add(item.id.name)
        }
      }
    }
  })
  return { parents, jsxNodes, functions, exports }
}

function jsxName(name) {
  if (!name) return ''
  if (name.type === 'JSXIdentifier') return name.name
  if (name.type === 'JSXMemberExpression') {
    return `${jsxName(name.object)}.${jsxName(name.property)}`
  }
  return ''
}

function readAttr(opening, attrName) {
  for (const attr of opening?.attributes || []) {
    if (attr.type !== 'JSXAttribute') continue
    if (attr.name?.name !== attrName) continue
    if (attr.value?.type === 'StringLiteral') return attr.value.value
    if (attr.value?.type === 'JSXExpressionContainer') {
      const expr = attr.value.expression
      if (expr?.type === 'StringLiteral') return expr.value
    }
  }
  return null
}

/** 解析常见选择器：data-* 属性 / id / class / 纯标签。 */
function parseSelector(selector) {
  const text = String(selector || '').trim()
  if (!text) return null
  let match = text.match(/^\[([\w:-]+)\s*=\s*["']([^"']+)["']\]$/)
  if (match) return { kind: 'attr', name: match[1], value: match[2] }
  match = text.match(/^\[([\w:-]+)\]$/)
  if (match) return { kind: 'hasAttr', name: match[1] }
  match = text.match(/^#([\w-]+)$/)
  if (match) return { kind: 'attr', name: 'id', value: match[1] }
  match = text.match(/^\.([\w-]+)$/)
  if (match) return { kind: 'class', value: match[1] }
  match = text.match(/^([A-Za-z][\w-]*)$/)
  if (match) return { kind: 'tag', value: match[1] }
  return null
}

function matchesSelector(node, selector) {
  if (!selector || node.type !== 'JSXElement') return false
  const opening = node.openingElement
  if (selector.kind === 'tag') {
    return jsxName(opening?.name) === selector.value
  }
  if (selector.kind === 'hasAttr') {
    return readAttr(opening, selector.name) !== null
  }
  if (selector.kind === 'attr') {
    return readAttr(opening, selector.name) === selector.value
  }
  if (selector.kind === 'class') {
    const classes = String(readAttr(opening, 'className') || '').split(/\s+/)
    return classes.includes(selector.value)
  }
  return false
}

function lineStarts(code) {
  const starts = [0]
  for (let index = 0; index < code.length; index += 1) {
    if (code[index] === '\n') starts.push(index + 1)
  }
  return starts
}

function offsetFor(line, column, starts, codeLength) {
  const lineNo = Number(line)
  if (!Number.isFinite(lineNo) || lineNo <= 0 || lineNo > starts.length) return -1
  const base = starts[lineNo - 1]
  // SDK 的 column 可能是 0 基或 1 基；两种都尝试，由调用方取交集。
  const col = Math.max(0, Number(column) || 1)
  return Math.min(codeLength, base + Math.max(0, col - 1))
}

function contains(node, offset) {
  return (
    Number.isFinite(node.start) &&
    Number.isFinite(node.end) &&
    node.start <= offset &&
    offset <= node.end
  )
}

/** 用户在多轮选择中确认的精确区间：直接按 start/end 命中节点。 */
function findExactNode(jsxNodes, range) {
  const start = Number(range?.startOffset)
  const end = Number(range?.endOffset)
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null
  return jsxNodes.find((node) => node.start === start && node.end === end) || null
}

function innermostAt(jsxNodes, offset) {
  let found = null
  for (const node of jsxNodes) {
    if (!contains(node, offset)) continue
    if (!found || node.start > found.start || (node.start === found.start && node.end < found.end)) {
      found = node
    }
  }
  return found
}

function componentOf(node, parents, fallback) {
  let current = parents.get(node)
  let candidate = ''
  while (current) {
    if (FUNCTION_TYPES.has(current.type)) {
      if (current.id?.name) candidate = current.id.name
      else if (current.parentKey === undefined) candidate = candidate || ''
      break
    }
    current = parents.get(current)
  }
  if (candidate) return candidate
  // 变量式组件：const App = () => (...)。向上找 VariableDeclarator。
  current = parents.get(node)
  while (current) {
    if (current.type === 'VariableDeclarator' && current.id?.name) {
      return current.id.name
    }
    current = parents.get(current)
  }
  return fallback || ''
}

function rendererFunctionOf(node, parents) {
  let current = parents.get(node)
  while (current) {
    if (FUNCTION_TYPES.has(current.type)) return current
    current = parents.get(current)
  }
  return null
}

function jsxElementName(node) {
  if (!node || node.type !== 'JSXElement') return ''
  return jsxName(node.openingElement?.name)
}

/**
 * Phase 2：为共享组件计算 callSite 锚点。
 *
 * Resolver 不允许修改组件定义文件，但“只在当前实例修改”需要一个可写的
 * 真实节点：调用点里的 `<Card ... />`。这里按组件名（支持 MemberExpression）
 * 在 callSite 文件中定位该调用节点，实例数量 > 1 时用 1-based 序号消歧。
 * 找不到就返回 null，前端决策只会提供“取消”，绝不猜一个近似节点。
 */
function findCallSiteAnchor(workdir, callSiteFile, component, instance, parser) {
  if (!callSiteFile || !component) return null
  let parsed
  try {
    parsed = parseFile(workdir, callSiteFile, parser)
  } catch {
    return null
  }
  const { code, ast } = parsed
  const index = buildIndex(ast)
  const shortName = String(component).split('.').pop()
  const matches = index.jsxNodes.filter((node) => {
    const name = jsxElementName(node)
    if (!name) return false
    return name === component || name.split('.').pop() === shortName
  })
  if (matches.length === 0) return null
  let node = null
  const position = Number(instance || 0)
  if (Number.isInteger(position) && position >= 1 && position <= matches.length) {
    node = matches[position - 1]
  } else if (matches.length === 1) {
    node = matches[0]
  }
  if (!node) return null
  return {
    status: 'RESOLVED',
    file: callSiteFile,
    range: rangeOf(node),
    nodeType: node.type,
    component: componentOf(node, index.parents, component),
    nodeSource: code.slice(node.start, node.end).slice(0, 4000),
    isShared: false,
    protected: false,
    sharedReason: '',
    componentFile: '',
    callSiteFile,
    allowedFiles: [callSiteFile],
    forbiddenRanges: [],
    allowedOperations: ['insert', 'delete', 'modify'],
    allowedInsertPositions: allowedInsertPositionsOf(node),
    decisionAction: 'modify_call_site',
  }
}

/**
 * 锚点节点支持的插入位置。
 *
 * 自闭合元素（<img /> / <Input />）没有 children，只能作为兄弟插入；
 * 成对元素 / Fragment 才可以 inside_*，避免把子节点插进不存在的
 * closing tag 里。
 */
function allowedInsertPositionsOf(node) {
  const beforeAfter = ['before', 'after']
  if (!node) return beforeAfter
  if (node.type === 'JSXFragment') {
    return [...beforeAfter, 'inside_start', 'inside_end', 'inside_index']
  }
  if (node.type === 'JSXElement') {
    if (node.openingElement?.selfClosing || !node.closingElement) {
      return beforeAfter
    }
    return [...beforeAfter, 'inside_start', 'inside_end', 'inside_index']
  }
  return beforeAfter
}

/** 参与“第几个子节点”计数的节点：纯空白 JSXText 不算。 */
function meaningfulChildren(node) {
  const children = Array.isArray(node?.children) ? node.children : []
  return children.filter(
    (child) => !(child.type === 'JSXText' && !String(child.value || '').trim()),
  )
}

/**
 * 把前端 placement 归一化成“相对父容器的精确子节点序号”。
 *
 * 前端 SDK 的 before / after 是相对“命中兄弟节点”的位置，但 Resolver
 * 的锚点是父容器。两者坐标系不同：直接照搬 before / after 会把新增
 * JSX 插到父容器**外面**。这里按 insertAnchor 的行列在父节点直接子节点
 * 中定位它，换算成 inside_index，保证最终插入仍在父容器内部。
 */
function childIndexFor(parentNode, insertAnchor) {
  if (!insertAnchor || typeof insertAnchor !== 'object') return null
  const children = meaningfulChildren(parentNode)
  const line = Number(insertAnchor.line)
  const column = Number(insertAnchor.column)
  if (Number.isFinite(line) && line > 0) {
    for (let index = 0; index < children.length; index += 1) {
      const start = children[index].loc?.start
      if (!start) continue
      if (start.line === line && start.column + 1 === column) return index
    }
  }
  const selector = parseSelector(insertAnchor.selector)
  if (selector) {
    for (let index = 0; index < children.length; index += 1) {
      if (matchesSelector(children[index], selector)) return index
    }
  }
  return null
}

function normalizePlacement(node, rawPlacement) {
  if (!rawPlacement || typeof rawPlacement !== 'object') return null
  const placement = { ...rawPlacement }
  // 自闭合元素只能作为兄弟插入，不能把 placement 改写成 inside_*。
  if (!allowedInsertPositionsOf(node).includes('inside_index')) {
    return placement
  }
  const children = meaningfulChildren(node)
  const position = String(placement.insertPosition || 'inside_end')
  const insertAnchor = placement.insertAnchor || null
  if (position === 'inside_start') {
    placement.insertPosition = 'inside_start'
    placement.siblingIndex = 0
    placement.siblingCount = children.length
    return placement
  }
  if (position === 'inside_end') {
    // 明确追加到末尾，避免前端 siblingIndex 被误当成“第几个子节点”。
    placement.insertPosition = 'inside_end'
    placement.siblingIndex = children.length
    placement.siblingCount = children.length
    return placement
  }
  const index = childIndexFor(node, insertAnchor)
  if (position === 'before') {
    placement.insertPosition = 'inside_index'
    placement.siblingIndex =
      index === null ? Math.max(0, Number(placement.siblingIndex) || 0) : index
  } else if (position === 'after') {
    placement.insertPosition = 'inside_index'
    placement.siblingIndex =
      index === null
        ? Math.max(0, Number(placement.siblingIndex) || 0)
        : index + 1
  } else if (position === 'inside_index') {
    // 显式 inside_index 已经是父容器的目标序号，保持原值。
    placement.siblingIndex = Math.max(0, Number(placement.siblingIndex) || 0)
  }
  placement.siblingCount = children.length
  return placement
}

function rangeOf(node) {
  return {
    startLine: node.loc?.start?.line || 0,
    startColumn: node.loc?.start?.column + 1 || 0,
    endLine: node.loc?.end?.line || 0,
    endColumn: node.loc?.end?.column + 1 || 0,
    startOffset: node.start,
    endOffset: node.end,
  }
}

function previewOf(code, node, limit = 160) {
  const text = code.slice(node.start, node.end).replace(/\s+/g, ' ').trim()
  return text.length > limit ? `${text.slice(0, limit)}…` : text
}

function sourceFiles(root, limit = 400) {
  const results = []
  const stack = [root]
  while (stack.length && results.length < limit) {
    const dir = stack.pop()
    let entries = []
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true })
    } catch {
      continue
    }
    for (const entry of entries) {
      if (entry.name === 'node_modules' || entry.name === '.git' || entry.name === 'dist') {
        continue
      }
      const full = path.join(dir, entry.name)
      if (entry.isDirectory()) stack.push(full)
      else if (/\.(jsx?|tsx?)$/.test(entry.name)) results.push(full)
    }
  }
  return results
}

/** 共享组件证据：其它文件是否 import 了该组件。 */
function importersOf(workdir, targetFile, component) {
  if (!component) return []
  const srcRoot = path.join(workdir, 'src')
  const root = fs.existsSync(srcRoot) ? srcRoot : workdir
  const base = path.basename(targetFile).replace(/\.(jsx?|tsx?)$/, '')
  const pattern = new RegExp(
    `import\\s+[^'";]*\\b${component.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b[^'";]*from\\s+['"]([^'"]+)['"]`,
    'g',
  )
  const hits = []
  for (const file of sourceFiles(root)) {
    const rel = path.relative(workdir, file).split(path.sep).join('/')
    if (rel === targetFile) continue
    let code = ''
    try {
      code = fs.readFileSync(file, 'utf8')
    } catch {
      continue
    }
    let match
    while ((match = pattern.exec(code)) !== null) {
      const source = match[1]
      const tail = source.split('/').pop() || ''
      if (tail === base || source.endsWith(`/${base}`)) {
        hits.push(rel)
        break
      }
    }
  }
  return hits
}

function parseFile(workdir, file, parser) {
  const full = path.join(workdir, file)
  const code = fs.readFileSync(full, 'utf8')
  const plugins = ['jsx']
  if (/\.(ts|tsx)$/.test(file)) plugins.push('typescript')
  const ast = parser.parse(code, {
    sourceType: 'unambiguous',
    plugins,
    errorRecovery: false,
    attachComment: false,
  })
  return { code, ast }
}

/** 节点是否位于数组 / 列表的 .map(...) 回调内部。 */
function isInsideMapCallback(node, parents) {
  let current = node
  while (current) {
    const parent = parents.get(current)
    if (!parent) break
    if (parent.type === 'CallExpression') {
      const callee = parent.callee
      if (
        callee?.type === 'MemberExpression' &&
        !callee.computed &&
        (callee.property?.name === 'map' || callee.property?.value === 'map')
      ) {
        return true
      }
      if (callee?.type === 'Identifier' && callee.name === 'map') {
        return true
      }
    }
    // 再检查一层：ArrowFunctionExpression -> CallExpression 的父链
    if (
      (parent.type === 'ArrowFunctionExpression' ||
        parent.type === 'FunctionExpression') &&
      parents.get(parent)?.type === 'CallExpression'
    ) {
      const call = parents.get(parent)
      const callee = call.callee
      if (callee?.type === 'MemberExpression' && callee.property?.name === 'map') {
        return true
      }
    }
    current = parent
  }
  return false
}

/** 是否为页面 / 组件文件中最外层的 JSX 根节点（没有 JSX 祖先）。 */
function isTopLevelJsxRoot(node, parents) {
  let current = node
  while (current) {
    const parent = parents.get(current)
    if (!parent) return true
    if (JSX_TYPES.has(parent.type)) return false
    current = parent
  }
  return true
}

function resolveOne(anchor, parser, workdir) {
  const oid = String(anchor.oid || '').trim()
  const oidNode = sourceMapNodeForOid(workdir, oid)
  // source map 是构建期事实：oid 命中时文件以地图为准，不被旧 file:line
  // 覆盖；地图缺失时才退回前端携带的 file。
  const file = String((oidNode && oidNode.file) || anchor.file || '').trim()
  const result = {
    anchorId: anchor.anchorId,
    annotationId: anchor.annotationId,
    oid,
    status: 'TARGET_NOT_FOUND',
    file,
    reason: '',
    candidates: [],
  }
  if (!file) {
    result.reason = oid
      ? `oid 未命中源码地图且缺少目标文件: ${oid}`
      : '标注缺少目标文件'
    return result
  }
  let parsed
  try {
    parsed = parseFile(workdir, file, parser)
  } catch (error) {
    result.reason = `目标文件不可解析: ${error.message}`
    return result
  }
  const { code, ast } = parsed
  const index = buildIndex(ast)
  // oid 是构建期唯一主键；没有显式 selector 时用 [data-oid="..."] 兜底，
  // 生成工程（无 start/end）也能通过 oid 精确命中。
  const selector = parseSelector(
    anchor.selector || (oid ? `[data-oid="${oid}"]` : ''),
  )
  const matches = selector
    ? index.jsxNodes.filter((node) => matchesSelector(node, selector))
    : []

  const starts = lineStarts(code)
  const offsets = []
  if (anchor.line) {
    for (const column of [anchor.column, Number(anchor.column || 0) + 1]) {
      const offset = offsetFor(anchor.line, column, starts, code.length)
      if (offset >= 0 && !offsets.includes(offset)) offsets.push(offset)
    }
  }
  const positionNode = offsets.length
    ? innermostAt(index.jsxNodes, offsets[0]) ||
      (offsets[1] !== undefined ? innermostAt(index.jsxNodes, offsets[1]) : null)
    : null

  const oidRange =
    oidNode && Number.isFinite(Number(oidNode.start))
      ? {
          startOffset: Number(oidNode.start),
          endOffset: Number(oidNode.end),
        }
      : anchor.sourceRange &&
          Number.isFinite(Number(anchor.sourceRange.startOffset))
        ? anchor.sourceRange
        : null
  let node = oidRange ? findExactNode(index.jsxNodes, oidRange) : null
  if (!node && anchor.selectedRange) {
    node = findExactNode(index.jsxNodes, anchor.selectedRange)
  }
  // oid 已命中地图且带精确区间，却找不到对应 AST：说明地图与源码
  // revision 不一致。此时必须 fail-closed，绝不能再退回 selector / 行列
  // 猜一个“看起来像”的节点。生成工程的 manifest 只有 nodeId、没有区间，
  // 允许继续走 [data-oid] / [data-node-id] selector。
  const oidHasRange = Boolean(
    oidNode && Number.isFinite(Number(oidNode.start)),
  )
  if (!node && oid && oidHasRange) {
    result.reason = `oid 源码区间未命中当前 AST，source map 可能已过期: ${oid}`
    return result
  }
  let ambiguityReason = ''
  // 用户已在上一次中断中选择候选时，优先采用服务端回填的精确区间。
  if (!node && matches.length === 1) {
    node = matches[0]
  } else if (!node && matches.length > 1) {
    const instance = Number(anchor.instance || 0)
    const instanceCount = Number(anchor.instanceCount || 0)
    if (
      Number.isInteger(instance) &&
      instance >= 1 &&
      instance <= matches.length &&
      instanceCount > 1
    ) {
      node = matches[instance - 1]
    } else if (positionNode && matches.includes(positionNode)) {
      node = positionNode
    } else {
      ambiguityReason = `选择器命中 ${matches.length} 个节点，且前端未提供唯一实例序号`
      result.candidates = matches.slice(0, 8).map((item, position) => ({
        candidateId: `${anchor.anchorId}_c${position + 1}`,
        file,
        range: rangeOf(item),
        nodeType: item.type,
        component: componentOf(item, index.parents, anchor.component),
        preview: previewOf(code, item),
      }))
    }
  } else if (!node && positionNode) {
    node = positionNode
  } else if (!node && !ambiguityReason) {
    result.reason = selector
      ? '选择器未命中任何 JSX 节点，行列也无法定位'
      : '缺少选择器与有效行列'
  }

  if (!node) {
    result.status = ambiguityReason ? 'TARGET_AMBIGUOUS' : 'TARGET_NOT_FOUND'
    result.reason = ambiguityReason || result.reason
    return result
  }

  const component = componentOf(node, index.parents, anchor.component)
  let callSiteFile = String(
    anchor.callSiteFile || anchor.callSite?.file || '',
  ).trim()
  const instanceCount = Number(anchor.instanceCount || 1)
  const importerHits = importersOf(workdir, file, component)
  // SDK 未提供 callSite 时，用 import 关系兜底：架构师生成的独立组件
  // 也应当能定位到调用它的页面文件，而不是被当成不可编辑的定义根。
  if (!callSiteFile && importerHits.length) {
    callSiteFile = importerHits[0]
  }
  const shared =
    instanceCount > 1 ||
    (Boolean(callSiteFile) && callSiteFile !== file) ||
    importerHits.length > 0
  const reasons = []
  if (instanceCount > 1) reasons.push(`同源码位置渲染 ${instanceCount} 次`)
  if (callSiteFile && callSiteFile !== file) reasons.push(`调用点在 ${callSiteFile}`)
  if (importerHits.length) {
    reasons.push(`被 ${importerHits.length} 个文件引用: ${importerHits.slice(0, 3).join(', ')}`)
  }

  const renderer = rendererFunctionOf(node, index.parents)
  const forbidden = []
  let componentFile = ''
  if (shared) {
    componentFile = file
    // 只有节点位于组件定义函数内部时，才把整段定义列为禁止修改区。
    if (renderer && renderer.start <= node.start && node.end <= renderer.end) {
      forbidden.push(rangeOf(renderer))
    }
  }
  const allowedFiles = []
  if (shared && callSiteFile && callSiteFile !== file) {
    // 共享组件的实例级修改只能发生在调用点文件，绝不写基础组件。
    allowedFiles.push(callSiteFile)
  } else {
    allowedFiles.push(file)
  }

  result.status = 'RESOLVED'
  result.nodeType = node.type
  result.component = component
  result.range = rangeOf(node)
  result.nodeSource = code.slice(node.start, node.end).slice(0, 4000)
  result.isShared = shared
  result.protected = shared
  result.sharedReason = reasons.join('；')
  result.instanceCount = instanceCount
  result.importerCount = importerHits.length
  result.sharedByInstance = instanceCount > 1
  result.sharedByImport = importerHits.length > 0
  result.componentFile = componentFile
  result.callSiteFile = callSiteFile
  result.allowedFiles = allowedFiles
  result.forbiddenRanges = forbidden
  result.allowedOperations = ['insert', 'delete', 'modify']
  result.allowedInsertPositions = allowedInsertPositionsOf(node)
  // 前端拖拽落位事实回传：坐标统一换算成“父容器内的精确子节点序号”，
  // Coder 据此选 inside_index / inside_end，并由 AST 工具按锚点插入，
  // 不能只依赖自然语言描述。
  result.placement = normalizePlacement(node, anchor.placement)
  // ---- DELETE_NODE 静态安全判定（只分析，不执行删除）----
  const inMap = isInsideMapCallback(node, index.parents)
  const isRouteRoot = isTopLevelJsxRoot(node, index.parents)
  const deleteReasons = [...reasons]
  if (inMap) deleteReasons.push('位于 map 循环内，删除会影响多个实例')
  if (isRouteRoot) deleteReasons.push('是页面顶层 JSX 根节点')
  result.inMap = inMap
  result.isRouteRoot = isRouteRoot
  result.deleteSafe = !shared && !inMap && !isRouteRoot
  result.deleteReasons = deleteReasons
  // 共享组件额外给出 callSite 锚点，供 Phase 2 决策“只在当前实例修改”。
  result.callSiteAnchor = null
  if (shared && callSiteFile && callSiteFile !== file) {
    const callSiteAnchor = findCallSiteAnchor(
      workdir,
      callSiteFile,
      component,
      anchor.instance,
      parser,
    )
    if (callSiteAnchor) {
      callSiteAnchor.anchorId = anchor.anchorId
      callSiteAnchor.annotationId = anchor.annotationId
      callSiteAnchor.sourceAnchorId = anchor.anchorId
      result.callSiteAnchor = callSiteAnchor
    }
  }
  return result
}

function main() {
  const specPath = process.argv[2]
  if (!specPath) throw new Error('缺少 spec.json 路径')
  const spec = JSON.parse(fs.readFileSync(specPath, 'utf8'))
  const workdir = spec.workdir || '/app'
  // parserRoot：依赖解析目录（默认 = workdir）。测试 / 独立诊断时项目
  // 目录不含 node_modules，可显式指向 sandbox_template。
  const parser = loadParser(spec.parserRoot || workdir)
  const anchors = []
  for (const anchor of spec.anchors || []) {
    try {
      anchors.push(resolveOne(anchor, parser, workdir))
    } catch (error) {
      anchors.push({
        anchorId: anchor.anchorId,
        annotationId: anchor.annotationId,
        status: 'TARGET_NOT_FOUND',
        file: anchor.file,
        reason: `解析异常: ${error.message}`,
        candidates: [],
      })
    }
  }
  process.stdout.write(JSON.stringify({ anchors }))
}

try {
  main()
} catch (error) {
  process.stderr.write(`resolver exception: ${error?.message || error}`)
  process.exit(2)
}
