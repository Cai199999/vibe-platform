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
 *     "insertPosition": "after" | "before",
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
    return { status: 'OK', content: next }
  }

  if (operation === 'insert') {
    const position = String(spec.insertPosition || 'after').toLowerCase()
    if (!['before', 'after'].includes(position)) {
      return fail('INVALID_OPERATION', `不支持的插入位置: ${position}`)
    }
    if (parent.type !== 'JSXElement' && parent.type !== 'JSXFragment') {
      return fail(
        'INVALID_INSERTION_POINT',
        '锚点父级不是 JSX 容器，无法安全插入兄弟节点',
      )
    }
    try {
      parseSnippet(spec.jsxCode, file, parser)
    } catch (error) {
      return fail(error.code || 'INVALID_JSX_SNIPPET', error.message)
    }
    const offset = position === 'before' ? anchor.start : anchor.end
    // 插入的是兄弟节点：缩进必须与锚点同级，而不是再加一层。
    const indent = baseIndentAt(code, anchor.start)
    const snippet = reindent(String(spec.jsxCode), indent)
    const next = `${code.slice(0, offset)}\n${indent}${snippet}${code.slice(offset)}`
    try {
      parseCode(next, file, parser)
    } catch (error) {
      return fail('INVALID_INSERTION_POINT', `插入后文件无法解析: ${error.message}`)
    }
    return { status: 'OK', content: next }
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
