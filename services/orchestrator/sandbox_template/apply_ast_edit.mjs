#!/usr/bin/env node
/**
 * 唯一写工具 apply_ast_edit 的 Babel 执行器（Phase 1）。
 *
 * 只做三件事：按解析锚点校验目标节点、做精确源码切片替换、验证结果 AST。
 * 任何越界、语法错误或删除后结构不等价都会返回结构化错误码；调用方
 * （Python Tool 层）负责把错误回传给 Coder 修正参数，禁止盲目重试。
 *
 * 输入（spec.json）：
 *   {
 *     "workdir": "/app",
 *     "file": "src/App.jsx",
 *     "operation": "insert" | "delete" | "modify",
 *     "anchorRange": {"startOffset": 100, "endOffset": 180},
 *     "targetRange": {"startOffset": 100, "endOffset": 180},
 *     "insertPosition":
 *       "after" | "before" | "inside_start" | "inside_end" | "inside_index",
 *     "insertIndex": 2,
 *     "jsxCode": "<button className=\"x\">保存</button>"
 *   }
 *
 * 输出（stdout JSON）：
 *   {"status":"OK","content":"<新文件内容>"}
 *   {"status":"ERROR","code":"...","message":"...","details":{...}}
 *
 * 退出码：0 = 执行完成（含业务错误）；2 = 脚本自身异常（fail-closed）。
 */
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'

function findNodeByRange(node, start, end, type) {
  if (!node || typeof node !== 'object') return null
  if (
    node.type === type &&
    Number(node.start) === Number(start) &&
    Number(node.end) === Number(end)
  ) {
    return node
  }
  for (const key of Object.keys(node)) {
    if (IGNORED_KEYS.has(key)) continue
    const value = node[key]
    if (Array.isArray(value)) {
      for (const item of value) {
        const found = findNodeByRange(item, start, end, type)
        if (found) return found
      }
    } else if (value && typeof value === 'object' && value.type) {
      const found = findNodeByRange(value, start, end, type)
      if (found) return found
    }
  }
  return null
}

const JSX_TYPES = new Set(['JSXElement', 'JSXFragment'])
const IGNORED_KEYS = new Set([
  'loc',
  'start',
  'end',
  'extra',
  'raw',
  'comments',
  'leadingComments',
  'trailingComments',
  'innerComments',
  'tokens',
  'errors',
])

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

function pluginsFor(file) {
  const plugins = ['jsx']
  if (/\.(ts|tsx)$/.test(file)) plugins.push('typescript')
  return plugins
}

function parseCode(code, file, parser) {
  return parser.parse(code, {
    sourceType: 'unambiguous',
    plugins: pluginsFor(file),
    errorRecovery: false,
    attachComment: false,
  })
}

function walk(node, parent, visit) {
  if (!node || typeof node !== 'object') return
  if (Array.isArray(node)) {
    for (const item of node) walk(item, parent, visit)
    return
  }
  if (typeof node.type !== 'string') return
  visit(node, parent)
  for (const key of Object.keys(node)) {
    if (IGNORED_KEYS.has(key)) continue
    walk(node[key], node, visit)
  }
}

function buildParents(ast) {
  const parents = new Map()
  walk(ast, null, (node, parent) => {
    if (parent) parents.set(node, parent)
  })
  return parents
}

function findExactNode(ast, range) {
  const start = Number(range?.startOffset)
  const end = Number(range?.endOffset)
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null
  let found = null
  walk(ast, null, (node) => {
    if (node.start === start && node.end === end) {
      if (!found || JSX_TYPES.has(node.type)) found = node
    }
  })
  return found
}

/** 去位置 / 注释后的结构哈希，用于删除前后的同构校验。 */
function normalize(value, key) {
  if (Array.isArray(value)) {
    // 过滤空白 JSXText：删除节点后，前后两段缩进文本会被合并成一个，
    // 若把空白文本当成结构节点，同构校验会误报 DELETE_SCOPE_MISMATCH。
    return value
      .map((item) => normalize(item, key))
      .filter((item) => item !== undefined && item !== '')
  }
  if (!value || typeof value !== 'object') return value
  if (IGNORED_KEYS.has(key)) return undefined
  if (value.type === 'JSXText') {
    return String(value.value || '')
      .replace(/\s+/g, ' ')
      .trim()
  }
  const out = {}
  for (const childKey of Object.keys(value).sort()) {
    if (IGNORED_KEYS.has(childKey)) continue
    const normalized = normalize(value[childKey], childKey)
    if (normalized !== undefined) out[childKey] = normalized
  }
  return out
}

function structuralHash(node) {
  return JSON.stringify(normalize(node, ''))
}

/**
 * 在深拷贝 AST 中移除目标节点，得到“预期剩余树”。
 * 只按 start/end 匹配，避免 JSON 深拷贝后对象引用不同。
 */
function pruneNode(ast, target) {
  const clone = JSON.parse(JSON.stringify(ast))
  let removed = false
  const visit = (node) => {
    if (!node || typeof node !== 'object' || removed) return
    if (Array.isArray(node)) {
      for (let index = 0; index < node.length; index += 1) {
        const child = node[index]
        if (
          child &&
          child.type &&
          child.start === target.start &&
          child.end === target.end &&
          JSX_TYPES.has(child.type)
        ) {
          node.splice(index, 1)
          removed = true
          return
        }
        visit(child)
      }
      return
    }
    for (const key of Object.keys(node)) {
      if (IGNORED_KEYS.has(key)) continue
      visit(node[key])
    }
  }
  visit(clone)
  return removed ? clone : null
}

function baseIndentAt(code, offset) {
  const lineStart = code.lastIndexOf('\n', Math.max(0, offset - 1)) + 1
  const line = code.slice(lineStart, offset)
  const match = line.match(/^[ \t]*/)
  return match ? match[0] : ''
}

/** 统一缩进：首行去掉自身缩进，后续行按最小公共缩进对齐到目标位置。 */
function reindent(snippet, indent) {
  const lines = String(snippet).replace(/\r\n/g, '\n').split('\n')
  const nonEmpty = lines.filter((line) => line.trim())
  let minimum = Infinity
  for (const line of nonEmpty) {
    const match = line.match(/^[ \t]*/)
    minimum = Math.min(minimum, match ? match[0].length : 0)
  }
  if (!Number.isFinite(minimum)) minimum = 0
  return lines
    .map((line, index) => {
      if (!line.trim()) return ''
      const stripped = line.slice(Math.min(minimum, line.length - line.trimStart().length))
      return index === 0 ? stripped.trimStart() : `${indent}${stripped}`
    })
    .join('\n')
}

function parseSnippet(jsxCode, file, parser) {
  const text = String(jsxCode || '').trim()
  if (!text) {
    const error = new Error('jsxCode 不能为空')
    error.code = 'INVALID_JSX_SNIPPET'
    throw error
  }
  let node
  try {
    node = parser.parseExpression(text, {
      plugins: pluginsFor(file),
      errorRecovery: false,
    })
  } catch (error) {
    error.code = 'INVALID_JSX_SNIPPET'
    throw error
  }
  // 允许 ( <div/> ) 包裹；Babel 会返回 JSXElement 本身。
  if (!JSX_TYPES.has(node.type)) {
    const error = new Error('jsxCode 必须是单个 JSX 元素或 Fragment')
    error.code = 'INVALID_JSX_SNIPPET'
    throw error
  }
  return node
}

/**
 * JSX 容器的开闭标签位置。
 *
 * inside_* 只对成对元素 / Fragment 有意义；自闭合元素没有子节点区，
 * 直接拒绝而不是生成非法 JSX。
 */
function jsxContainerParts(node) {
  if (!node) return null
  if (node.type === 'JSXFragment') {
    return {
      openEnd: node.openingFragment?.end ?? -1,
      closeStart: node.closingFragment?.start ?? -1,
      selfClosing: !node.openingFragment || !node.closingFragment,
    }
  }
  if (node.type === 'JSXElement') {
    return {
      openEnd: node.openingElement?.end ?? -1,
      closeStart: node.closingElement?.start ?? -1,
      selfClosing:
        !node.openingElement ||
        Boolean(node.openingElement.selfClosing) ||
        !node.closingElement,
    }
  }
  return null
}

/** 参与“第几个子节点”计数的节点：纯空白 JSXText 不算。 */
function meaningfulChildren(node) {
  const children = Array.isArray(node?.children) ? node.children : []
  return children.filter(
    (child) => !(child.type === 'JSXText' && !String(child.value || '').trim()),
  )
}

function countJsxNodes(ast) {
  let count = 0
  walk(ast, null, (node) => {
    if (JSX_TYPES.has(node.type)) count += 1
  })
  return count
}

/**
 * inside_* 插入的子节点缩进。
 *
 * 优先抄现有第一个子节点的行首缩进，保持项目原有排版；
 * 空容器 / 子节点与父节点同行时退化为“父缩进 + 两个空格”。
 */
function childIndentFor(code, anchor) {
  const parentIndent = baseIndentAt(code, anchor.start)
  const parentLine = anchor.loc?.start?.line || 0
  const children = meaningfulChildren(anchor)
  for (const child of children) {
    if ((child.loc?.start?.line || 0) > parentLine) {
      return baseIndentAt(code, child.start)
    }
  }
  return `${parentIndent}  `
}

/** 删除整行节点时连同行首缩进 / 换行一起移除，避免留下空白行。 */
function deleteSlice(code, node) {
  const lineStart = code.lastIndexOf('\n', Math.max(0, node.start - 1)) + 1
  const before = code.slice(lineStart, node.start)
  let lineEnd = code.indexOf('\n', node.end)
  if (lineEnd === -1) lineEnd = code.length
  const after = code.slice(node.end, lineEnd)
  if (!before.trim() && !after.trim()) {
    return { start: lineStart, end: lineEnd < code.length ? lineEnd + 1 : lineEnd }
  }
  return { start: node.start, end: node.end }
}

function fail(code, message, details = {}) {
  return { status: 'ERROR', code, message, details }
}

function main() {
  const specPath = process.argv[2]
  if (!specPath) throw new Error('缺少 spec.json 路径')
  const spec = JSON.parse(fs.readFileSync(specPath, 'utf8'))
  const workdir = spec.workdir || '/app'
  const file = String(spec.file || '')
  const operation = String(spec.operation || '').toLowerCase()
  const fullPath = path.join(workdir, file)
  // parserRoot：依赖解析目录（默认 = workdir）；测试环境中项目目录
  // 不含 node_modules，可显式指向 sandbox_template。
  const parser = loadParser(spec.parserRoot || workdir)

  let code = ''
  try {
    // 同一文件的多条编辑由 Graph 降序执行，后一条用前一条的结果作为
    // 输入（服务端注入，模型不可见），避免锚点 offset 因前序编辑失效。
    code =
      typeof spec.contentOverride === 'string'
        ? spec.contentOverride
        : fs.readFileSync(fullPath, 'utf8')
  } catch (error) {
    return fail('FILE_NOT_ALLOWED', `目标文件不可读: ${file}`, {
      error: error.message,
    })
  }

  let ast
  try {
    ast = parseCode(code, file, parser)
  } catch (error) {
    return fail('AST_EDIT_FAILED', `目标文件解析失败: ${error.message}`)
  }
  const parents = buildParents(ast)
  const anchor = findExactNode(ast, spec.anchorRange)
  if (!anchor || !JSX_TYPES.has(anchor.type)) {
    return fail('ANCHOR_RANGE_MISMATCH', '锚点区间已失效，请重新解析目标')
  }
  const parent = parents.get(anchor)
  if (!parent) {
    return fail('ANCHOR_RANGE_MISMATCH', '锚点节点缺少父节点上下文')
  }

  // ---- Direct Ops：UPDATE_TEXT ----
  // 只允许“唯一 JSXText 子节点”的叶子元素；混合子节点 / 表达式 /
  // 自闭合元素一律拒绝，交给 HITL 或 AI，不猜测。
  if (operation === 'update_text') {
    const target = findExactNode(ast, spec.targetRange || spec.anchorRange)
    if (!target || !JSX_TYPES.has(target.type)) {
      return fail('ANCHOR_RANGE_MISMATCH', '目标区间与 AST 节点不匹配')
    }
    if (target.start !== anchor.start || target.end !== anchor.end) {
      return fail(
        'RANGE_NOT_ALLOWED',
        '文本修改范围必须与解析锚点完全一致',
        { anchor: spec.anchorRange, target: spec.targetRange },
      )
    }
    const text = String(spec.text ?? '')
    if (!text.trim()) {
      return fail('INVALID_TEXT', '新文案不能为空')
    }
    if (/[<>{}\r\n]/.test(text)) {
      return fail('INVALID_TEXT', '新文案不能包含 < > { } 或换行')
    }
    const children = Array.isArray(target.children) ? target.children : []
    const meaningful = children.filter(
      (child) => !(child.type === 'JSXText' && !String(child.value || '').trim()),
    )
    if (meaningful.length !== 1 || meaningful[0].type !== 'JSXText') {
      return fail(
        'TEXT_TARGET_NOT_LEAF',
        '目标不是纯文本节点（包含子元素或表达式），请改用 AI 修改',
        { childCount: meaningful.length },
      )
    }
    const textNode = meaningful[0]
    const raw = code.slice(textNode.start, textNode.end)
    const leading = raw.match(/^\s*/)?.[0] || ''
    const trailing = raw.match(/\s*$/)?.[0] || ''
    const replacement = `${leading}${text.trim()}${trailing}`
    const next = `${code.slice(0, textNode.start)}${replacement}${code.slice(textNode.end)}`
    try {
      parseCode(next, file, parser)
    } catch (error) {
      return fail('INVALID_TEXT', `文本替换后文件无法解析: ${error.message}`)
    }
    return { status: 'OK', content: next }
  }

  // ---- Direct Ops：MOVE_NODE ----
  // 把 source 节点从原位置移除，再按 placement 插入 target 容器。
  // 只支持同一文件、容器内部移动；跨文件 / 跨路由交给 AI。
  if (operation === 'move_node') {
    const source = findExactNode(ast, spec.anchorRange)
    if (!source || !JSX_TYPES.has(source.type)) {
      return fail('ANCHOR_RANGE_MISMATCH', '待移动节点区间已失效')
    }
    const target = findExactNode(ast, spec.targetRange)
    if (!target || !JSX_TYPES.has(target.type)) {
      return fail('ANCHOR_RANGE_MISMATCH', '目标容器区间已失效')
    }
    // 禁止把节点移动到自身或自己的子树。
    if (target.start >= source.start && target.end <= source.end) {
      return fail('INVALID_MOVE_TARGET', '不能把组件移动到它自身内部')
    }
    const parts = jsxContainerParts(target)
    if (!parts || parts.selfClosing || parts.openEnd < 0 || parts.closeStart < 0) {
      return fail('INVALID_MOVE_TARGET', '目标不是可容纳子节点的 JSX 容器')
    }
    const position = String(spec.insertPosition || 'inside_end').toLowerCase()
    if (!['inside_start', 'inside_end', 'inside_index'].includes(position)) {
      return fail('INVALID_MOVE_TARGET', '移动只支持容器内部落位')
    }
    const children = meaningfulChildren(target)
    // placement.siblingIndex 语义：移动完成后目标容器中的最终序号。
    // 同一父容器内移动时，先从候选列表中剔除 source，再按最终序号落位。
    const sameParent = parents.get(source) === target
    const available = sameParent
      ? children.filter(
          (child) => !(child.start === source.start && child.end === source.end),
        )
      : children
    let index = available.length
    if (position === 'inside_index') {
      const rawIndex = Number(spec.insertIndex)
      if (!Number.isInteger(rawIndex) || rawIndex < 0 || rawIndex > available.length) {
        return fail('INVALID_MOVE_TARGET', 'insertIndex 超出子节点范围', {
          insertIndex: spec.insertIndex,
          childCount: available.length,
        })
      }
      index = rawIndex
    } else if (position === 'inside_start') {
      index = 0
    }
    const rawInsertOffset =
      position === 'inside_start'
        ? parts.openEnd
        : index < available.length
          ? available[index].start
          : parts.closeStart
    const slice = deleteSlice(code, source)
    const removedLength = slice.end - slice.start
    let insertOffset = rawInsertOffset
    if (slice.end <= rawInsertOffset) {
      insertOffset -= removedLength
    } else if (slice.start < rawInsertOffset && rawInsertOffset < slice.end) {
      return fail('INVALID_MOVE_TARGET', '落点位于被移动节点内部，已阻断')
    }
    const removedCode = `${code.slice(0, slice.start)}${code.slice(slice.end)}`
    if (insertOffset < 0 || insertOffset > removedCode.length) {
      return fail('INVALID_MOVE_TARGET', '落点偏移计算失败')
    }
    const parentIndent = baseIndentAt(code, target.start)
    const indent = childIndentFor(code, target)
    const snippet = reindent(code.slice(source.start, source.end), indent)
    // 移除落点前已有的缩进 / 换行，避免移动后出现空行或双倍缩进。
    const before = removedCode
      .slice(0, insertOffset)
      .replace(/[ \t]*\n?[ \t]*$/, '')
    const after = removedCode.slice(insertOffset)
    let next
    if (position === 'inside_start') {
      next = `${before}\n${indent}${snippet}${after}`
    } else if (index < available.length) {
      next = `${before}\n${indent}${snippet}\n${indent}${after}`
    } else {
      next = `${before}\n${indent}${snippet}\n${parentIndent}${after}`
    }
    let nextAst
    try {
      nextAst = parseCode(next, file, parser)
    } catch (error) {
      return fail('INVALID_MOVE_TARGET', `移动后文件无法解析: ${error.message}`)
    }
    if (countJsxNodes(nextAst) !== countJsxNodes(ast)) {
      return fail('MOVE_SCOPE_MISMATCH', '移动前后 JSX 节点数量不一致，已阻断')
    }
    return { status: 'OK', content: next }
  }

  // ---- Direct Ops：DELETE_DATA_ITEM ----
  // 确定性删除静态数组中的一项；动态数据在 ScopePolicy 已转 AI。
  if (operation === 'delete_array_item') {
    const range = spec.arrayRange || {}
    const startOffset = Number(range.startOffset)
    const endOffset = Number(range.endOffset)
    const array = findNodeByRange(
      ast,
      startOffset,
      endOffset,
      'ArrayExpression',
    )
    if (!array) {
      return fail('ANCHOR_RANGE_MISMATCH', '数据源数组区间已失效')
    }
    const elements = array.elements || []
    const index = Number(spec.itemIndex)
    if (!Number.isInteger(index) || index < 0 || index >= elements.length) {
      return fail('INVALID_DATA_ITEM', '数据项序号超出数组范围', {
        itemIndex: spec.itemIndex,
        count: elements.length,
      })
    }
    let removeStart
    let removeEnd
    if (elements.length === 1) {
      removeStart = elements[0].start
      removeEnd = elements[0].end
    } else if (index < elements.length - 1) {
      removeStart = elements[index].start
      removeEnd = elements[index + 1].start
    } else {
      removeStart = elements[index - 1].end
      removeEnd = elements[index].end
    }
    const next = `${code.slice(0, removeStart)}${code.slice(removeEnd)}`
    try {
      parseCode(next, file, parser)
    } catch (error) {
      return fail('INVALID_DATA_ITEM', `删除数据项后文件无法解析: ${error.message}`)
    }
    return {
      status: 'OK',
      content: next,
      removedItemIndex: index,
      itemCountBefore: elements.length,
      itemCountAfter: elements.length - 1,
    }
  }

  // ---- Direct Ops：UPDATE_PROPS ----
  // 只改调用点 JSX 的属名 / 字面量；不执行表达式，不触碰组件定义。
  if (operation === 'update_props') {
    const target = findExactNode(ast, spec.anchorRange)
    if (!target || target.type !== 'JSXElement' || !target.openingElement) {
      return fail('ANCHOR_RANGE_MISMATCH', 'Props 更新目标必须是 JSX 元素')
    }
    const props = spec.props
    if (!props || typeof props !== 'object' || Array.isArray(props)) {
      return fail('INVALID_PROPS', 'props 必须是对象')
    }
    const entries = Object.entries(props)
    if (entries.length === 0 || entries.length > 30) {
      return fail('INVALID_PROPS', '一次必须更新 1-30 个属性')
    }
    const updated = new Set()
    for (const [name, value] of entries) {
      if (!/^[A-Za-z_$][A-Za-z0-9_$:-]*$/.test(name) || name.length > 80) {
        return fail('INVALID_PROPS', `非法 prop 名称: ${name}`)
      }
      if (
        value !== null &&
        typeof value !== 'string' &&
        typeof value !== 'number' &&
        typeof value !== 'boolean'
      ) {
        return fail('INVALID_PROPS', `prop ${name} 只支持字符串/数字/布尔/null`)
      }
      if (typeof value === 'string' && /[<>{}\r\n]/.test(value)) {
        return fail('INVALID_PROPS', `prop ${name} 不能包含 < > { } 或换行`)
      }
      updated.add(name)
    }

    const opening = target.openingElement
    const attributes = []
    for (const attribute of opening.attributes || []) {
      if (
        attribute.type === 'JSXAttribute' &&
        updated.has(attribute.name?.name)
      ) {
        continue
      }
      attributes.push(code.slice(attribute.start, attribute.end))
    }
    for (const [name, value] of entries) {
      if (value === null) continue
      if (value === true) {
        attributes.push(name)
      } else if (typeof value === 'number') {
        attributes.push(`${name}={${value}}`)
      } else {
        const escaped = String(value)
          .replace(/&/g, '&amp;')
          .replace(/"/g, '&quot;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;')
        attributes.push(`${name}="${escaped}"`)
      }
    }
    const tagName = code.slice(opening.start, opening.name.end)
    const closing = opening.selfClosing ? ' />' : '>'
    const nextOpening =
      `${tagName}${attributes.length ? ` ${attributes.join(' ')}` : ''}` +
      closing
    const next = `${code.slice(0, opening.start)}${nextOpening}${code.slice(
      opening.end,
    )}`
    let nextAst
    try {
      nextAst = parseCode(next, file, parser)
    } catch (error) {
      return fail('INVALID_PROPS', `Props 更新后文件无法解析: ${error.message}`)
    }
    if (countJsxNodes(nextAst) !== countJsxNodes(ast)) {
      return fail('PROPS_SCOPE_MISMATCH', 'Props 更新前后 JSX 节点数量不一致')
    }
    return {
      status: 'OK',
      content: next,
      nodeCountBefore: countJsxNodes(ast),
      nodeCountAfter: countJsxNodes(nextAst),
    }
  }

  if (operation === 'modify' || operation === 'delete') {
    const target = findExactNode(ast, spec.targetRange || spec.anchorRange)
    if (!target || !JSX_TYPES.has(target.type)) {
      return fail('ANCHOR_RANGE_MISMATCH', '目标区间与 AST 节点不匹配')
    }
    if (target.start !== anchor.start || target.end !== anchor.end) {
      return fail(
        'RANGE_NOT_ALLOWED',
        '修改 / 删除范围必须与解析锚点完全一致',
        { anchor: spec.anchorRange, target: spec.targetRange },
      )
    }

    if (operation === 'modify') {
      let snippet
      try {
        snippet = parseSnippet(spec.jsxCode, file, parser)
      } catch (error) {
        return fail(error.code || 'INVALID_JSX_SNIPPET', error.message)
      }
      const indent = baseIndentAt(code, target.start)
      const replacement = reindent(String(spec.jsxCode), indent)
      const next = `${code.slice(0, target.start)}${replacement}${code.slice(target.end)}`
      try {
        parseCode(next, file, parser)
      } catch (error) {
        return fail('INVALID_JSX_SNIPPET', `替换后文件无法解析: ${error.message}`)
      }
      void snippet
      return { status: 'OK', content: next }
    }

    // delete：同构校验 = 原树删除目标子树后，剩余结构与新代码解析结果一致。
    const pruned = pruneNode(ast, target)
    if (!pruned) {
      return fail('DELETE_SCOPE_MISMATCH', '无法从 AST 中精确定位待删除子树')
    }
    const slice = deleteSlice(code, target)
    const next = `${code.slice(0, slice.start)}${code.slice(slice.end)}`
    let nextAst
    try {
      nextAst = parseCode(next, file, parser)
    } catch (error) {
      return fail(
        'DELETE_SCOPE_MISMATCH',
        `删除后代码无法解析，疑似误删父节点: ${error.message}`,
      )
    }
    if (structuralHash(nextAst.program) !== structuralHash(pruned.program)) {
      return fail(
        'DELETE_SCOPE_MISMATCH',
        '删除后 AST 与预期的“原树减目标子树”不一致，已阻断',
      )
    }
    return {
      status: 'OK',
      content: next,
      nodeCountBefore: countJsxNodes(ast),
      nodeCountAfter: countJsxNodes(nextAst),
      // 目标子树本身的 JSX 节点数（含根），用于精确校验删除量。
      targetNodeCount: countJsxNodes(target),
    }
  }

  if (operation === 'insert') {
    const position = String(spec.insertPosition || 'after').toLowerCase()
    if (
      ![
        'before',
        'after',
        'inside_start',
        'inside_end',
        'inside_index',
      ].includes(position)
    ) {
      return fail('INVALID_INSERTION_POINT', `不支持的插入位置: ${position}`)
    }
    try {
      parseSnippet(spec.jsxCode, file, parser)
    } catch (error) {
      return fail(error.code || 'INVALID_JSX_SNIPPET', error.message)
    }

    // ---- 兄弟插入（旧协议）----
    if (position === 'before' || position === 'after') {
      if (parent.type !== 'JSXElement' && parent.type !== 'JSXFragment') {
        return fail(
          'INVALID_INSERTION_POINT',
          '锚点父级不是 JSX 容器，无法安全插入兄弟节点',
        )
      }
      const offset = position === 'before' ? anchor.start : anchor.end
      // 插入的是兄弟节点：缩进必须与锚点同级，而不是再加一层。
      const indent = baseIndentAt(code, anchor.start)
      const snippet = reindent(String(spec.jsxCode), indent)
      const next = `${code.slice(0, offset)}\n${indent}${snippet}${code.slice(offset)}`
      let nextAst
      try {
        nextAst = parseCode(next, file, parser)
      } catch (error) {
        return fail('INVALID_INSERTION_POINT', `插入后文件无法解析: ${error.message}`)
      }
      // Graph 需要精确知道“插入点 / 插入长度”才能重投影同一文件后续
      // 编辑的锚点；只靠内容 diff 在相同前缀场景会误判。
      return {
        status: 'OK',
        content: next,
        nodeCountBefore: countJsxNodes(ast),
        nodeCountAfter: countJsxNodes(nextAst),
        insert: {
          offset,
          length: next.length - code.length,
          position,
          index: null,
        },
      }
    }

    // ---- 容器内部插入（暂存组件拖拽落位）----
    const parts = jsxContainerParts(anchor)
    if (!parts || parts.selfClosing || parts.openEnd < 0 || parts.closeStart < 0) {
      return fail(
        'INVALID_INSERTION_POINT',
        '锚点不是可容纳子节点的 JSX 容器（自闭合元素只能 before / after）',
      )
    }
    const indent = childIndentFor(code, anchor)
    const snippet = reindent(String(spec.jsxCode), indent)
    let next = ''
    // 插入点相对“编辑前内容”的偏移：Graph 用它重投影同文件后续锚点。
    let insertOffset = -1
    let insertIndex = null
    if (position === 'inside_start') {
      // 开标签之后：作为第一个子节点。
      insertOffset = parts.openEnd
      insertIndex = 0
      next = `${code.slice(0, insertOffset)}\n${indent}${snippet}${code.slice(insertOffset)}`
    } else {
      // inside_end / inside_index：在闭标签前插入。
      const children = meaningfulChildren(anchor)
      const rawIndex = spec.insertIndex
      const index =
        rawIndex === null || rawIndex === undefined || rawIndex === ''
          ? children.length
          : Number(rawIndex)
      if (!Number.isInteger(index) || index < 0 || index > children.length) {
        return fail('INVALID_INSERTION_POINT', 'insertIndex 超出子节点范围', {
          insertIndex: rawIndex,
          childCount: children.length,
        })
      }
      insertIndex = index
      if (index < children.length) {
        // 插在第 index 个子节点之前，并保留换行 / 缩进。
        insertOffset = children[index].start
        next = `${code.slice(0, insertOffset)}\n${indent}${snippet}\n${indent}${code.slice(insertOffset)}`
      } else {
        insertOffset = parts.closeStart
        const parentIndent = baseIndentAt(code, anchor.start)
        next = `${code.slice(0, insertOffset)}\n${indent}${snippet}\n${parentIndent}${code.slice(insertOffset)}`
      }
    }
    let nextAst
    try {
      nextAst = parseCode(next, file, parser)
    } catch (error) {
      return fail('INVALID_INSERTION_POINT', `插入后文件无法解析: ${error.message}`)
    }
    return {
      status: 'OK',
      content: next,
      nodeCountBefore: countJsxNodes(ast),
      nodeCountAfter: countJsxNodes(nextAst),
      insert: {
        offset: insertOffset,
        length: next.length - code.length,
        position,
        index: insertIndex,
      },
    }
  }

  return fail('INVALID_OPERATION', `不支持的操作类型: ${operation || '(空)'}`)
}

try {
  const result = main()
  process.stdout.write(JSON.stringify(result))
} catch (error) {
  process.stderr.write(`ast edit exception: ${error?.message || error}`)
  process.exit(2)
}
