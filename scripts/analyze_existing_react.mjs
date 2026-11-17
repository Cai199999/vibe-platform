#!/usr/bin/env node
/**
 * 存量 React 源码逆向解析（轨道二 · 底层验证）。
 *
 * 只做“分析视图”，绝不写回源码、绝不生成/覆盖 VisualDocument：
 *   React 源码 → Babel AST → 组件树 JSON（层级 / 标签 / 文本 / className）
 *
 * 用法：
 *   node scripts/analyze_existing_react.mjs --file src/App.jsx
 *   cat src/App.jsx | node scripts/analyze_existing_react.mjs
 *   node scripts/analyze_existing_react.mjs --self-test
 *
 * 输出：stdout 为 JSON；--self-test 额外在 stderr 打印断言结果。
 * 退出码：0 成功；1 解析/断言失败；2 参数或 Babel 环境不可用。
 */
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url))
const ANALYSIS_SCHEMA_VERSION = '0.1-analysis'

function loadBabel() {
  const candidates = [
    process.env.REACT_ANALYZE_REQUIRE_ROOT,
    '/opt/vibe-ast-schema/index.js',
    '/app/sandbox_template/package.json',
    path.resolve(
      SCRIPT_DIR,
      '../services/orchestrator/sandbox_template/package.json',
    ),
    path.join(process.cwd(), 'package.json'),
  ].filter(Boolean)
  const failures = []
  for (const anchor of candidates) {
    try {
      const requireFrom = createRequire(anchor)
      const parser = requireFrom('@babel/parser')
      const traverseModule = requireFrom('@babel/traverse')
      const traverse = traverseModule.default || traverseModule
      if (typeof parser.parse !== 'function' || typeof traverse !== 'function') {
        throw new Error('@babel/parser 或 @babel/traverse 导出异常')
      }
      return { parser, traverse, anchor }
    } catch (error) {
      failures.push(`${anchor}: ${error.message}`)
    }
  }
  throw new Error(`无法加载 Babel 依赖。尝试过:\n${failures.join('\n')}`)
}

function parseSource(parser, file, source) {
  const plugins = ['jsx']
  if (/\.tsx?$/i.test(file)) plugins.push('typescript')
  return parser.parse(source, {
    sourceType: 'module',
    plugins,
    errorRecovery: false,
    sourceFilename: file,
  })
}

function sourceLoc(file, node) {
  if (!node?.loc) return null
  return {
    file,
    line: node.loc.start.line,
    column: node.loc.start.column + 1,
    endLine: node.loc.end.line,
    endColumn: node.loc.end.column + 1,
  }
}

function codeOf(source, node) {
  if (!node || typeof node.start !== 'number' || typeof node.end !== 'number') {
    return ''
  }
  return source.slice(node.start, node.end).replace(/\s+/g, ' ').trim()
}

function jsxName(nameNode) {
  if (!nameNode) return ''
  if (nameNode.type === 'JSXIdentifier') return nameNode.name
  if (nameNode.type === 'JSXMemberExpression') {
    return `${jsxName(nameNode.object)}.${jsxName(nameNode.property)}`
  }
  if (nameNode.type === 'JSXNamespacedName') {
    return `${nameNode.namespace.name}:${nameNode.name.name}`
  }
  return codeOf('', nameNode)
}

function isComponentTag(tag) {
  const first = String(tag || '').split('.')[0]
  return /^[A-Z]/.test(first)
}

function staticStringFromExpression(expression) {
  if (!expression) return ''
  if (expression.type === 'StringLiteral') return expression.value
  if (expression.type === 'TemplateLiteral' && expression.expressions.length === 0) {
    return expression.quasis.map((item) => item.value.cooked ?? '').join('')
  }
  return ''
}

function collectStringLiterals(node, output = []) {
  if (!node || typeof node !== 'object') return output
  if (Array.isArray(node)) {
    node.forEach((item) => collectStringLiterals(item, output))
    return output
  }
  if (node.type === 'StringLiteral') {
    if (node.value) output.push(node.value)
    return output
  }
  if (node.type === 'TemplateLiteral') {
    for (const quasi of node.quasis) {
      if (quasi.value?.cooked) output.push(quasi.value.cooked)
    }
  }
  for (const [key, value] of Object.entries(node)) {
    if (key === 'loc' || key === 'start' || key === 'end') continue
    if (value && typeof value === 'object') collectStringLiterals(value, output)
  }
  return output
}

function extractClassName(jsxAttribute, source) {
  if (!jsxAttribute) return { className: '', classes: [], expression: '' }
  const valueNode = jsxAttribute.value
  if (!valueNode) return { className: '', classes: [], expression: '' }
  if (valueNode.type === 'StringLiteral') {
    const className = valueNode.value.trim()
    return {
      className,
      classes: className.split(/\s+/).filter(Boolean),
      expression: '',
    }
  }
  if (valueNode.type === 'JSXExpressionContainer') {
    const expression = valueNode.expression
    const staticValue = staticStringFromExpression(expression)
    if (staticValue.trim()) {
      const className = staticValue.trim()
      return {
        className,
        classes: className.split(/\s+/).filter(Boolean),
        expression: '',
      }
    }
    const literals = [...new Set(collectStringLiterals(expression))]
    return {
      className: literals.join(' ').trim(),
      classes: literals.flatMap((item) => item.split(/\s+/)).filter(Boolean),
      expression: codeOf(source, expression),
    }
  }
  return {
    className: '',
    classes: [],
    expression: codeOf(source, valueNode),
  }
}

function extractAttributes(opening, source) {
  const attributes = {}
  for (const item of opening.attributes || []) {
    if (item.type !== 'JSXAttribute') continue
    const name =
      item.name?.type === 'JSXNamespacedName'
        ? `${item.name.namespace.name}:${item.name.name.name}`
        : item.name?.name
    if (!name || name === 'className') continue
    if (!item.value) {
      attributes[name] = true
      continue
    }
    if (item.value.type === 'StringLiteral') {
      attributes[name] = item.value.value
      continue
    }
    if (item.value.type === 'JSXExpressionContainer') {
      const expression = item.value.expression
      if (expression?.type === 'StringLiteral') {
        attributes[name] = expression.value
      } else if (expression?.type === 'NumericLiteral') {
        attributes[name] = expression.value
      } else if (expression?.type === 'BooleanLiteral') {
        attributes[name] = expression.value
      } else {
        attributes[name] = { expression: codeOf(source, expression) }
      }
    }
  }
  return attributes
}

function createNodeFactory() {
  let sequence = 0
  return (prefix = 'an') => `${prefix}_${String(++sequence).padStart(4, '0')}`
}

function createJsxConverter({ file, source, nextId }) {
  function convert(node) {
    if (!node) return []
    if (node.type === 'JSXText') {
      const text = node.value.replace(/\s+/g, ' ').trim()
      if (!text) return []
      return [
        {
          id: nextId('tx'),
          kind: 'text',
          tag: '#text',
          text,
          source: sourceLoc(file, node),
          children: [],
        },
      ]
    }
    if (node.type === 'JSXExpressionContainer') {
      const expression = node.expression
      if (!expression || expression.type === 'JSXEmptyExpression') return []
      const staticText = staticStringFromExpression(expression)
      if (staticText.trim()) {
        return [
          {
            id: nextId('tx'),
            kind: 'text',
            tag: '#text',
            text: staticText.trim(),
            source: sourceLoc(file, node),
            children: [],
          },
        ]
      }
      if (expression.type === 'JSXElement' || expression.type === 'JSXFragment') {
        return convert(expression)
      }
      if (expression.type === 'ConditionalExpression') {
        return [
          ...convertExpressionBranch(expression.consequent),
          ...convertExpressionBranch(expression.alternate),
        ]
      }
      return [
        {
          id: nextId('ex'),
          kind: 'expression',
          tag: '#expression',
          expression: codeOf(source, expression),
          source: sourceLoc(file, node),
          children: [],
        },
      ]
    }
    if (node.type === 'JSXFragment') {
      const children = (node.children || []).flatMap(convert)
      return [
        {
          id: nextId('fr'),
          kind: 'fragment',
          tag: '#fragment',
          source: sourceLoc(file, node),
          children,
        },
      ]
    }
    if (node.type !== 'JSXElement') return []

    const opening = node.openingElement
    const tag = jsxName(opening?.name)
    const kind = isComponentTag(tag) ? 'component' : 'element'
    const classNameAttribute = (opening?.attributes || []).find(
      (item) => item.type === 'JSXAttribute' && item.name?.name === 'className',
    )
    const className = extractClassName(classNameAttribute, source)
    const children = (node.children || []).flatMap(convert)
    return [
      {
        id: nextId(kind === 'component' ? 'cp' : 'el'),
        kind,
        tag,
        className: className.className,
        classes: className.classes,
        classNameExpression: className.expression,
        attributes: extractAttributes(opening, source),
        source: sourceLoc(file, node),
        selfClosing: Boolean(opening?.selfClosing),
        children,
      },
    ]
  }

  function convertExpressionBranch(expression) {
    if (!expression) return []
    if (expression.type === 'JSXElement' || expression.type === 'JSXFragment') {
      return convert(expression)
    }
    if (expression.type === 'ParenthesizedExpression') {
      return convertExpressionBranch(expression.expression)
    }
    return [
      {
        id: nextId('ex'),
        kind: 'expression',
        tag: '#expression',
        expression: codeOf(source, expression),
        source: sourceLoc(file, expression),
        children: [],
      },
    ]
  }

  return convert
}

function collectJsxFromExpression(expression, output = []) {
  if (!expression) return output
  if (
    expression.type === 'JSXElement' ||
    expression.type === 'JSXFragment' ||
    expression.type === 'JSXText'
  ) {
    output.push(expression)
    return output
  }
  if (expression.type === 'ParenthesizedExpression') {
    return collectJsxFromExpression(expression.expression, output)
  }
  if (expression.type === 'ConditionalExpression') {
    collectJsxFromExpression(expression.consequent, output)
    collectJsxFromExpression(expression.alternate, output)
    return output
  }
  if (expression.type === 'LogicalExpression') {
    collectJsxFromExpression(expression.left, output)
    collectJsxFromExpression(expression.right, output)
    return output
  }
  return output
}

function findReturnedJsx(traverse, fnPath) {
  const roots = []
  const fnNode = fnPath.node
  if (!fnNode.body) return roots
  if (fnNode.body.type !== 'BlockStatement') {
    return collectJsxFromExpression(fnNode.body, roots)
  }
  fnPath.traverse({
    Function(innerPath) {
      if (innerPath.node !== fnNode) innerPath.skip()
    },
    ReturnStatement(returnPath) {
      collectJsxFromExpression(returnPath.node.argument, roots)
    },
  })
  return roots
}

function functionName(path_) {
  const node = path_.node
  if (node.type === 'FunctionDeclaration') return node.id?.name || ''
  if (
    (node.type === 'ArrowFunctionExpression' ||
      node.type === 'FunctionExpression') &&
    path_.parentPath?.isVariableDeclarator?.()
  ) {
    return path_.parentPath.node.id?.name || ''
  }
  return ''
}

function exportKind(path_) {
  let current = path_
  while (current) {
    if (current.isExportDefaultDeclaration?.()) return 'default'
    if (current.isExportNamedDeclaration?.()) return 'named'
    current = current.parentPath
  }
  return ''
}

function collectComponentTree(ast, { file, source, parser, traverse }) {
  const nextId = createNodeFactory()
  const convert = createJsxConverter({ file, source, nextId })
  const components = []
  const seenFunctions = new Set()

  function record(fnPath, name, exported) {
    if (!fnPath?.node || seenFunctions.has(fnPath.node)) return
    seenFunctions.add(fnPath.node)
    const roots = findReturnedJsx(traverse, fnPath)
    if (!roots.length) return
    const trees = roots.map((node) => convert(node)[0]).filter(Boolean)
    if (!trees.length) return
    components.push({
      name: name || '<anonymous>',
      exported,
      source: sourceLoc(file, fnPath.node),
      roots: trees,
      root: trees[0],
    })
  }

  traverse(ast, {
    FunctionDeclaration(fnPath) {
      record(fnPath, functionName(fnPath), exportKind(fnPath))
    },
    VariableDeclarator(declPath) {
      const init = declPath.get('init')
      if (!init?.node) return
      if (
        init.node.type !== 'ArrowFunctionExpression' &&
        init.node.type !== 'FunctionExpression'
      ) {
        return
      }
      record(init, declPath.node.id?.name || '', exportKind(declPath))
    },
    ExportDefaultDeclaration(exportPath) {
      const declaration = exportPath.get('declaration')
      if (
        declaration?.node?.type === 'FunctionDeclaration' ||
        declaration?.node?.type === 'FunctionExpression'
      ) {
        record(declaration, functionName(declaration) || 'default', 'default')
      }
    },
  })

  return { components, nextId }
}

function walkTree(node, visitor, depth = 0) {
  if (!node) return
  visitor(node, depth)
  for (const child of node.children || []) walkTree(child, visitor, depth + 1)
}

function collectUsedComponents(component) {
  const used = new Set()
  for (const root of component.roots) {
    walkTree(root, (node) => {
      if (node.kind === 'component' && node.tag) used.add(node.tag)
    })
  }
  return [...used]
}

function buildHierarchies(components) {
  const byName = new Map(components.map((item) => [item.name, item]))
  const usedByOther = new Set()
  for (const component of components) {
    for (const used of collectUsedComponents(component)) {
      if (byName.has(used)) usedByOther.add(used)
    }
  }
  const paths = new Set()

  function walk(name, trail, seen, depth) {
    if (depth > 12 || !byName.has(name)) return
    const component = byName.get(name)
    const children = collectUsedComponents(component).filter((item) =>
      byName.has(item),
    )
    if (!children.length) {
      paths.add(trail.join(' -> '))
      return
    }
    for (const child of children) {
      if (seen.has(child)) {
        paths.add([...trail, `${child} (cycle)`].join(' -> '))
        continue
      }
      walk(child, [...trail, child], new Set([...seen, child]), depth + 1)
    }
  }

  const roots = components
    .map((item) => item.name)
    .filter((name) => !usedByOther.has(name))
  for (const root of roots) walk(root, [root], new Set([root]), 1)
  // 没有明确根节点（互相引用 / 全部被使用）时，从每个组件各走一遍。
  if (!paths.size) {
    for (const component of components) {
      walk(component.name, [component.name], new Set([component.name]), 1)
    }
  }
  return [...paths]
}

function analyze(source, { file = 'src/App.jsx' } = {}) {
  const { parser, traverse, anchor } = loadBabel()
  const ast = parseSource(parser, file, source)
  const { components } = collectComponentTree(ast, {
    file,
    source,
    parser,
    traverse,
  })
  const stats = { components: components.length, elements: 0, texts: 0, maxDepth: 0 }
  for (const component of components) {
    for (const root of component.roots) {
      walkTree(root, (node, depth) => {
        stats.maxDepth = Math.max(stats.maxDepth, depth + 1)
        if (node.kind === 'element') stats.elements += 1
        if (node.kind === 'text') stats.texts += 1
      })
    }
  }
  const entry =
    components.find((item) => item.exported === 'default') ||
    components.find((item) => item.name === 'App') ||
    components[components.length - 1] ||
    null
  return {
    schemaVersion: ANALYSIS_SCHEMA_VERSION,
    kind: 'ReactSourceAnalysis',
    // 与 VisualDocument 形似但语义不同：只读分析视图，绝不写回源码。
    analysisOnly: true,
    writable: false,
    file,
    framework: 'react',
    parser: { engine: 'babel', requireRoot: anchor },
    entryComponent: entry?.name || null,
    root: entry?.root || null,
    components,
    componentHierarchies: buildHierarchies(components),
    stats,
    warnings: components.length
      ? []
      : ['未找到包含 JSX 返回值的 React 函数组件'],
  }
}

const SELF_TEST_SOURCE = `import React from 'react'

export function Text({ children }) {
  return <p className="m-0 text-sm text-slate-700">{children}</p>
}

export function Header() {
  return (
    <header className="flex items-center justify-between border-b px-4 py-3">
      <Text>卡片标题</Text>
      <button className="rounded bg-blue-600 px-3 py-1 text-white">操作</button>
    </header>
  )
}

export default function Card() {
  return (
    <div className="rounded-xl border bg-white p-4 shadow">
      <Header />
      <section className="mt-3 grid grid-cols-2 gap-2">
        <Text>第一列</Text>
        <Text>第二列</Text>
      </section>
    </div>
  )
}
`

function readInput(argv) {
  const fileIndex = argv.indexOf('--file')
  if (fileIndex >= 0 && argv[fileIndex + 1]) {
    const file = argv[fileIndex + 1]
    return { file, source: fs.readFileSync(file, 'utf8') }
  }
  const stdinNameIndex = argv.indexOf('--stdin-name')
  const stdinName =
    stdinNameIndex >= 0 && argv[stdinNameIndex + 1]
      ? argv[stdinNameIndex + 1]
      : ''
  const positional = argv.find(
    (item) =>
      !item.startsWith('-') &&
      item !== process.argv[0] &&
      item !== process.argv[1] &&
      item !== stdinName,
  )
  if (positional) {
    return { file: positional, source: fs.readFileSync(positional, 'utf8') }
  }
  if (process.stdin.isTTY) {
    throw new Error(
      '请使用 --file <path>、--stdin-name <name> + stdin 或位置参数传入 React 源码',
    )
  }
  return {
    file: stdinName || 'stdin.jsx',
    source: fs.readFileSync(0, 'utf8'),
  }
}

function selfTest() {
  const report = analyze(SELF_TEST_SOURCE, { file: 'self-test/Card.jsx' })
  const expected = 'Card -> Header -> Text'
  const matched = report.componentHierarchies.includes(expected)
  const card = report.components.find((item) => item.name === 'Card')
  const header = card?.roots?.[0]?.children?.find(
    (item) => item.kind === 'component' && item.tag === 'Header',
  )
  const cardClasses = card?.roots?.[0]?.classes || []
  const textNode = header?.children?.[0]
  const assertions = [
    {
      name: 'component hierarchy contains Card -> Header -> Text',
      ok: matched,
      actual: report.componentHierarchies,
    },
    {
      name: 'entry component is default-exported Card',
      ok: report.entryComponent === 'Card' && report.root?.tag === 'div',
      actual: { entryComponent: report.entryComponent, root: report.root?.tag },
    },
    {
      name: 'Card root div className parsed',
      ok: cardClasses.includes('rounded-xl') && cardClasses.includes('bg-white'),
      actual: cardClasses,
    },
    {
      name: 'Header nested inside Card root',
      ok: header?.tag === 'Header',
      actual: header?.tag,
    },
  ]
  process.stdout.write(JSON.stringify(report, null, 2) + '\n')
  for (const assertion of assertions) {
    if (!assertion.ok) {
      console.error(
        `[analyze] FAIL: ${assertion.name} | actual=${JSON.stringify(
          assertion.actual,
        )}`,
      )
    } else {
      console.error(`[analyze] PASS: ${assertion.name}`)
    }
  }
  return assertions.every((item) => item.ok) ? 0 : 1
}

function main() {
  const argv = process.argv.slice(2)
  if (argv.includes('--help') || argv.includes('-h')) {
    console.log(
      'Usage: node scripts/analyze_existing_react.mjs --file <path> | --stdin-name <name> | --self-test',
    )
    return 0
  }
  if (argv.includes('--self-test')) {
    return selfTest()
  }
  let input
  try {
    input = readInput(process.argv)
  } catch (error) {
    console.error(`[analyze] ${error.message}`)
    return 2
  }
  try {
    const report = analyze(input.source, { file: input.file })
    process.stdout.write(JSON.stringify(report, null, 2) + '\n')
    return 0
  } catch (error) {
    console.error(`[analyze] 解析失败: ${error.message}`)
    return 1
  }
}

process.exitCode = main()
