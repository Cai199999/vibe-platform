#!/usr/bin/env node
/**
 * VisualDocument CodeGen 编译门禁（Babel AST）。
 *
 * 输入（stdin JSON）：
 *   {
 *     "files": {"src/App.jsx": "..."},
 *     "expectedNodeIds": ["nd_...", "..."]
 *   }
 *
 * 输出（stdout JSON）：
 *   {
 *     "engine": "visual-codegen-babel",
 *     "ok": true,
 *     "imports": ["./visual.css"],
 *     "nodeIds": ["nd_..."],
 *     "duplicateNodeIds": [],
 *     "missingNodeIds": [],
 *     "unexpectedNodeIds": [],
 *     "errors": [{"file": "src/App.jsx", "message": "...", "line": 1}]
 *   }
 *
 * 门禁职责（全部基于 AST，禁止正则回退）：
 *   1. JSX / ES 语法必须可解析；
 *   2. import 白名单：只允许 react 与本地样式 / 本地模块；
 *   3. 每个 VisualNode 必须且只能出现一次 data-node-id；
 *   4. 禁止 dangerouslySetInnerHTML；
 *   5. 禁止 position:absolute/fixed 与绝对定位 Tailwind 类。
 *
 * 退出码：
 *   0 = 分析完成（ok 字段表示业务校验是否通过）；
 *   2 = 校验器自身不可用（Babel 未安装 / 输入不是合法 JSON）。
 */
import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url))
const ENGINE = 'visual-codegen-babel'

const FORBIDDEN_POSITION_TAILWIND = [
  'absolute',
  'fixed',
  'sticky',
  'inset-',
  'top-',
  'left-',
  'right-',
  'bottom-',
  'translate-',
]
const FORBIDDEN_STYLE_KEYS = new Set([
  'position',
  'inset',
  'top',
  'left',
  'right',
  'bottom',
  'transform',
  'translate',
])

function loadParser() {
  const candidates = [
    process.env.VIBE_AST_REQUIRE_ROOT,
    '/opt/vibe-ast-schema/index.js',
    '/app/sandbox_template/package.json',
    path.resolve(SCRIPT_DIR, '../../sandbox_template/package.json'),
    path.join(process.cwd(), 'package.json'),
  ].filter(Boolean)
  const failures = []
  for (const anchor of candidates) {
    try {
      const requireFrom = createRequire(anchor)
      return requireFrom('@babel/parser')
    } catch (error) {
      failures.push(`${anchor}: ${error.message}`)
    }
  }
  throw new Error(`无法加载 @babel/parser。尝试过:\n${failures.join('\n')}`)
}

function readStdin() {
  return new Promise((resolve, reject) => {
    let raw = ''
    process.stdin.setEncoding('utf8')
    process.stdin.on('data', (chunk) => {
      raw += chunk
    })
    process.stdin.on('end', () => resolve(raw))
    process.stdin.on('error', reject)
  })
}

function stringValue(node) {
  if (!node) return null
  if (node.type === 'StringLiteral') return node.value
  if (node.type === 'JSXExpressionContainer') {
    return stringValue(node.expression)
  }
  return null
}

function walk(node, visitor) {
  if (!node || typeof node !== 'object') return
  if (Array.isArray(node)) {
    node.forEach((item) => walk(item, visitor))
    return
  }
  if (typeof node.type === 'string') visitor(node)
  for (const [key, value] of Object.entries(node)) {
    if (key === 'loc' || key === 'start' || key === 'end') continue
    if (value && typeof value === 'object') walk(value, visitor)
  }
}

function importAllowed(source) {
  if (
    [
      'react',
      'react-dom',
      'react-dom/client',
      'react/jsx-runtime',
      'vite',
      '@vitejs/plugin-react',
    ].includes(source)
  ) {
    return true
  }
  // 只允许本地样式与本地模块；第三方包一律拒绝。
  return source.startsWith('./') || source.startsWith('../')
}

function checkFile(parser, file, source, expectedIds) {
  const errors = []
  const nodeIds = []
  const imports = []
  const fileErrors = (message, loc) =>
    errors.push({
      file,
      message,
      line: loc?.start?.line ?? 1,
    })

  let ast
  try {
    ast = parser.parse(source, {
      sourceType: 'module',
      plugins: ['jsx'],
      errorRecovery: false,
    })
  } catch (error) {
    fileErrors(`语法错误: ${error.message}`, error.loc)
    return { errors, nodeIds, imports }
  }

  walk(ast, (node) => {
    if (node.type === 'JSXOpeningElement') {
      const attributes = Array.isArray(node.attributes) ? node.attributes : []
      const nodeIdAttribute = attributes.find(
        (item) =>
          item.type === 'JSXAttribute' && item.name?.name === 'data-node-id',
      )
      if (!nodeIdAttribute) return
      const sourceAttribute = attributes.find(
        (item) =>
          item.type === 'JSXAttribute' && item.name?.name === 'data-source',
      )
      if (!sourceAttribute) {
        fileErrors('含 data-node-id 的元素缺少 data-source', node.loc)
        return
      }
      const sourceValue = stringValue(sourceAttribute.value)
      if (!sourceValue || !/^[^:\s]+:\d+$/.test(sourceValue)) {
        fileErrors(
          `data-source 必须是 "{filePath}:{line}"，当前为 ${sourceValue ?? '空'}`,
          sourceAttribute.loc,
        )
      }
      return
    }
    if (node.type === 'ImportDeclaration') {
      const sourceValue = String(node.source?.value || '')
      imports.push(sourceValue)
      if (!importAllowed(sourceValue)) {
        fileErrors(`import 不在白名单内: ${sourceValue}`, node.loc)
      }
      return
    }
    if (node.type === 'JSXAttribute') {
      const name = node.name?.name
      if (name === 'dangerouslySetInnerHTML') {
        fileErrors('禁止 dangerouslySetInnerHTML', node.loc)
        return
      }
      if (name === 'data-node-id') {
        const value = stringValue(node.value)
        if (!value) {
          fileErrors('data-node-id 必须是字符串字面量', node.loc)
        } else {
          nodeIds.push(value)
        }
        return
      }
      if (name === 'className') {
        const value = stringValue(node.value)
        if (!value) return
        for (const token of value.split(/\s+/).filter(Boolean)) {
          if (
            FORBIDDEN_POSITION_TAILWIND.some(
              (prefix) => token === prefix || token.startsWith(prefix),
            )
          ) {
            fileErrors(`className 含绝对定位类: ${token}`, node.loc)
          }
        }
        return
      }
      if (name === 'style') {
        const expression = node.value?.expression
        if (!expression || expression.type !== 'ObjectExpression') return
        for (const property of expression.properties) {
          if (property.type !== 'ObjectProperty') continue
          const keyNode = property.key
          const key = keyNode?.name ?? keyNode?.value
          if (!FORBIDDEN_STYLE_KEYS.has(String(key))) continue
          const value = stringValue(property.value)
          if (String(key) === 'position' && !['absolute', 'fixed'].includes(value)) {
            continue
          }
          fileErrors(`style 含绝对定位属性: ${key}`, property.loc)
        }
      }
    }
  })

  return { errors, nodeIds, imports }
}

async function main() {
  let payload
  try {
    payload = JSON.parse(await readStdin())
  } catch (error) {
    process.stderr.write(`输入不是合法 JSON: ${error.message}\n`)
    process.exit(2)
  }

  let parser
  try {
    parser = loadParser()
  } catch (error) {
    process.stderr.write(`${error.message}\n`)
    process.exit(2)
  }

  const files = payload?.files && typeof payload.files === 'object' ? payload.files : {}
  const expectedNodeIds = Array.isArray(payload?.expectedNodeIds)
    ? payload.expectedNodeIds.map(String)
    : []

  const errors = []
  const nodeIds = []
  const imports = []
  for (const [file, source] of Object.entries(files)) {
    if (typeof source !== 'string') continue
    const result = checkFile(parser, file, source, expectedNodeIds)
    errors.push(...result.errors)
    nodeIds.push(...result.nodeIds)
    imports.push(...result.imports)
  }

  const duplicateNodeIds = []
  const seen = new Set()
  for (const id of nodeIds) {
    if (seen.has(id) && !duplicateNodeIds.includes(id)) duplicateNodeIds.push(id)
    seen.add(id)
  }
  const expected = new Set(expectedNodeIds)
  const present = new Set(nodeIds)
  const missingNodeIds = [...expected].filter((id) => !present.has(id))
  const unexpectedNodeIds = [...present].filter((id) => !expected.has(id))
  if (duplicateNodeIds.length > 0) {
    errors.push({
      file: 'src/App.jsx',
      message: `data-node-id 重复: ${duplicateNodeIds.join(', ')}`,
      line: 1,
    })
  }
  if (missingNodeIds.length > 0) {
    errors.push({
      file: 'src/App.jsx',
      message: `缺少 data-node-id: ${missingNodeIds.join(', ')}`,
      line: 1,
    })
  }
  if (unexpectedNodeIds.length > 0) {
    errors.push({
      file: 'src/App.jsx',
      message: `出现未声明的 data-node-id: ${unexpectedNodeIds.join(', ')}`,
      line: 1,
    })
  }

  process.stdout.write(
    JSON.stringify({
      engine: ENGINE,
      ok: errors.length === 0,
      imports,
      nodeIds,
      duplicateNodeIds,
      missingNodeIds,
      unexpectedNodeIds,
      errors,
    }),
  )
}

main()
