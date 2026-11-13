#!/usr/bin/env node
/**
 * 实例作用域 AST 门禁（P0）。
 *
 * 背景：字符串守卫检查既能被无关条件蒙混（在别处写一句
 * `card.id === 'card-2'`），也会被 ±N 行窗口绕过（改动离 callSite
 * 很远时直接跳过）。本脚本把 before / after 两棵 Babel AST 交给
 * 结构性对比，只放行“真正被实例条件包住”的新增 JSX：
 *
 *   1. 公共组件基础文件（file != callSiteFile）零结构变化；
 *   2. callSite 文件中，渲染基础组件的 map 回调里新增的 JSX，
 *      必须有真实的实例判别祖先（&& / 三元 / if / filter）；
 *   3. map 的数据源（例如 CARDS 常量）不能在同一补丁里被修改，
 *      否则所有实例的数据会一起变化。
 *
 * 用法：
 *   node /opt/vibe-ast-scope-check.mjs /tmp/vibe-scope-stage/spec.json
 * 退出码：0 = 通过；1 = 发现越界；2 = 检查器自身异常（调用方 fail-closed）。
 */
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'

/**
 * Babel 依赖解析：优先镜像自带的 /opt/vibe-ast，其次沙盒 /app。
 * 与 ast_check.mjs 保持一致的解析顺序。
 */
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

/** 去掉位置 / 注释 / 引号风格，仅保留结构语义。 */
function normalize(value, key) {
  if (Array.isArray(value)) return value.map((item) => normalize(item, key))
  if (!value || typeof value !== 'object') return value
  if (IGNORED_KEYS.has(key)) return undefined
  if (value.type === 'JSXText' && key === 'value') {
    // 缩进 / 换行属于格式，不构成“修改”；真实文案变化仍会保留。
    return String(value).replace(/\s+/g, ' ').trim()
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

/** 通用 AST 遍历；记录 parent，供祖先链判断使用。 */
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

function parseFile(root, rel, parser) {
  const full = path.join(root, rel)
  let code
  try {
    code = fs.readFileSync(full, 'utf8')
  } catch {
    return null
  }
  const plugins = ['jsx']
  if (/\.(ts|tsx)$/.test(rel)) plugins.push('typescript')
  let ast
  try {
    ast = parser.parse(code, {
      sourceType: 'unambiguous',
      plugins,
      errorRecovery: false,
      attachComment: false,
    })
  } catch (error) {
    const failure = new Error(`AST 解析失败 ${rel}: ${error.message}`)
    failure.parseError = true
    throw failure
  }
  const parents = new Map()
  walk(ast, null, (node, parent) => {
    if (parent) parents.set(node, parent)
  })
  return { code, ast, parents }
}

function jsxName(name) {
  if (!name) return ''
  if (name.type === 'JSXIdentifier') return name.name
  if (name.type === 'JSXMemberExpression') {
    return `${jsxName(name.object)}.${jsxName(name.property)}`
  }
  if (name.type === 'JSXNamespacedName') {
    return `${name.namespace?.name || ''}:${name.name?.name || ''}`
  }
  return ''
}

function matchesComponent(name, component) {
  if (!name || !component) return false
  return name === component || name.endsWith(`.${component}`)
}

function isFunction(node) {
  return [
    'FunctionDeclaration',
    'FunctionExpression',
    'ArrowFunctionExpression',
    'ObjectMethod',
    'ClassMethod',
  ].includes(node.type)
}

function nodeDepth(node, parents) {
  let depth = 0
  let current = parents.get(node)
  while (current) {
    depth += 1
    current = parents.get(current)
  }
  return depth
}

function isAncestor(ancestor, node, parents) {
  let current = parents.get(node)
  while (current) {
    if (current === ancestor) return true
    current = parents.get(current)
  }
  return false
}

function collectJSX(node) {
  const result = []
  walk(node, null, (child) => {
    if (child.type === 'JSXElement' || child.type === 'JSXFragment') {
      result.push(child)
    }
  })
  return result
}

/** 找出所有“直接或间接渲染指定组件”的函数，最内层优先。 */
function findRenderers(ast, component, parents) {
  const found = []
  walk(ast, null, (node) => {
    if (!isFunction(node)) return
    const renders = collectJSX(node).some((element) => {
      if (element.type !== 'JSXElement') return false
      return matchesComponent(jsxName(element.openingElement.name), component)
    })
    if (renders) found.push({ node, depth: nodeDepth(node, parents) })
  })
  found.sort((left, right) => right.depth - left.depth)
  return found.map((item) => item.node)
}

function identityOf(scope) {
  const props = scope.instanceProps || {}
  const strings = new Set()
  const numbers = new Set()
  if (scope.instanceKey) strings.add(String(scope.instanceKey))
  for (const value of Object.values(props)) {
    if (typeof value === 'string' && value.trim()) strings.add(value)
    if (typeof value === 'number' && Number.isFinite(value)) numbers.add(value)
  }
  const instance = Number(scope.instance || 1)
  numbers.add(instance)
  if (instance > 1) numbers.add(instance - 1)
  return { strings, numbers, instance }
}

/** 判断一个条件表达式是否真的在比较实例身份。 */
function referencesIdentity(node, identity) {
  if (!node) return false
  let hit = false
  walk(node, null, (child) => {
    if (hit) return
    if (child.type === 'StringLiteral' && identity.strings.has(child.value)) {
      hit = true
      return
    }
    if (child.type === 'NumericLiteral' && identity.numbers.has(child.value)) {
      hit = true
      return
    }
    if (child.type === 'TemplateElement') {
      const text = String(child.value?.cooked || child.value?.raw || '')
      for (const value of identity.strings) {
        if (text.includes(value)) {
          hit = true
          return
        }
      }
    }
  })
  return hit
}

/** 判断新增节点的祖先链上是否存在实例守卫。 */
function hasInstanceGuard(node, parents, identity) {
  let current = parents.get(node)
  while (current) {
    if (current.type === 'LogicalExpression' && current.operator === '&&') {
      if (referencesIdentity(current.left, identity)) return true
    }
    if (current.type === 'ConditionalExpression') {
      if (referencesIdentity(current.test, identity)) return true
    }
    if (current.type === 'IfStatement') {
      if (referencesIdentity(current.test, identity)) return true
    }
    // items.filter(item => item.id === 'x').map(...) 也是实例级过滤。
    if (current.type === 'CallExpression') {
      const callee = current.callee
      if (
        callee &&
        callee.type === 'MemberExpression' &&
        callee.property?.name === 'map' &&
        callee.object?.type === 'CallExpression'
      ) {
        const filterCall = callee.object
        const filterCallee = filterCall.callee
        if (
          filterCallee &&
          filterCallee.type === 'MemberExpression' &&
          filterCallee.property?.name === 'filter' &&
          (filterCall.arguments || []).some(
            (argument) =>
              isFunction(argument) && referencesIdentity(argument.body, identity),
          )
        ) {
          return true
        }
      }
    }
    if (isFunction(current)) return false
    current = parents.get(current)
  }
  return false
}

/**
 * 元素“存在性”哈希：忽略属性，只保留标签与子树结构。
 *
 * 属性变化（例如 React 把 key 从 <Card> 挪到外层 <div>）不应被当成
 * “新增元素”，否则合法的守卫式重构会被误判。真正的属性污染由基础
 * 组件规则和人工审批兜底；P0 先保证“新增 JSX 必须有实例守卫”。
 */
function presenceHash(node) {
  if (node.type === 'JSXElement') {
    return structuralHash({
      ...node,
      openingElement: {
        ...node.openingElement,
        attributes: [],
      },
    })
  }
  return structuralHash(node)
}

/** 计算 after 中新增的 JSX 节点（按存在性哈希做多重集差）。 */
function addedJSX(beforeAst, afterAst) {
  const remaining = new Map()
  walk(beforeAst, null, (node) => {
    if (node.type !== 'JSXElement' && node.type !== 'JSXFragment') return
    const key = presenceHash(node)
    remaining.set(key, (remaining.get(key) || 0) + 1)
  })
  const added = []
  walk(afterAst, null, (node) => {
    if (node.type !== 'JSXElement' && node.type !== 'JSXFragment') return
    const key = presenceHash(node)
    const count = remaining.get(key) || 0
    if (count > 0) {
      remaining.set(key, count - 1)
    } else {
      added.push(node)
    }
  })
  return added
}

/** 只保留最内层新增元素：父级包装的 hash 变化不代表需要独立守卫。 */
function leafAdded(nodes, parents) {
  const addedSet = new Set(nodes)
  const hasAddedDescendant = new Set()
  for (const node of nodes) {
    let current = parents.get(node)
    while (current) {
      if (addedSet.has(current)) {
        hasAddedDescendant.add(current)
        break
      }
      current = parents.get(current)
    }
  }
  return nodes.filter((node) => !hasAddedDescendant.has(node))
}

function findComponentNode(ast, component, parents) {
  let found = null
  walk(ast, null, (node) => {
    if (found) return
    if (
      node.type === 'FunctionDeclaration' &&
      node.id?.name === component
    ) {
      found = node
      return
    }
    if (
      node.type === 'VariableDeclarator' &&
      node.id?.type === 'Identifier' &&
      node.id.name === component &&
      isFunction(node.init)
    ) {
      found = node
      return
    }
    if (node.type === 'ClassDeclaration' && node.id?.name === component) {
      found = node
      return
    }
  })
  if (!found) {
    // 默认导出：组件名可能来自文件名而不是声明名。
    walk(ast, null, (node) => {
      if (found) return
      if (node.type === 'ExportDefaultDeclaration') {
        const declaration = node.declaration
        found =
          declaration?.type === 'FunctionDeclaration' ||
          declaration?.type === 'ClassDeclaration'
            ? declaration
            : declaration
      }
    })
  }
  return found
}

function findMapAncestor(element, parents) {
  let current = parents.get(element)
  while (current) {
    if (
      current.type === 'CallExpression' &&
      current.callee?.type === 'MemberExpression' &&
      current.callee.property?.name === 'map'
    ) {
      return current
    }
    current = parents.get(current)
  }
  return null
}

function findVariableInit(ast, name) {
  let init = null
  walk(ast, null, (node) => {
    if (
      !init &&
      node.type === 'VariableDeclarator' &&
      node.id?.type === 'Identifier' &&
      node.id.name === name
    ) {
      init = node.init || null
    }
  })
  return init
}

function addedViolation(scopes, code, file, message, line, detail = {}) {
  return {
    code,
    file,
    line: line || 0,
    annotationId: scopes.annotationId || '',
    message,
    ...detail,
  }
}

/** 校验单个文件中的单个作用域。 */
function checkScope(
  scope,
  rel,
  beforeFile,
  afterFile,
  changedFiles,
  violations,
) {
  const component = scope.callSiteComponent || scope.component || ''
  const identity = identityOf(scope)
  const isBaseFile = rel === scope.file
  const isCallSiteFile = Boolean(scope.callSiteFile) && rel === scope.callSiteFile

  // 规则一：公共组件基础文件零结构变化。
  if (scope.protectBase && isBaseFile && changedFiles.includes(rel)) {
    const beforeNode = findComponentNode(beforeFile.ast, scope.component, beforeFile.parents)
    const afterNode = findComponentNode(afterFile.ast, scope.component, afterFile.parents)
    let changed = false
    let evidence = ''
    if (beforeNode && afterNode) {
      changed = structuralHash(beforeNode) !== structuralHash(afterNode)
      evidence = scope.component || 'default export'
    } else {
      changed = structuralHash(beforeFile.ast) !== structuralHash(afterFile.ast)
      evidence = '整文件'
    }
    if (changed) {
      violations.push(
        addedViolation(
          scope,
          'INSTANCE_AST_BASE_COMPONENT_CHANGED',
          rel,
          `公共组件 ${evidence} 在 ${rel} 中被直接修改。` +
            `该组件在 ${scope.callSiteFile || '父级页面'} 中被复用，` +
            '修改基础文件会污染所有实例；请改 callSite 父级 JSX 并用实例条件包住变更。',
          0,
          { component: scope.component, callSiteFile: scope.callSiteFile },
        ),
      )
      return
    }
  }

  // 规则二 / 三：callSite 或（callSite 缺失时）任何渲染该组件的变更文件。
  if (!scope.requireGuard) return
  const renderers = findRenderers(afterFile.ast, component, afterFile.parents)
  let rendererFound = renderers.length > 0
  if (!rendererFound && isCallSiteFile) {
    // callSite 文件里找不到渲染函数：保守地把整个 Program 当作用域。
    renderers.push(afterFile.ast)
    rendererFound = true
  }
  if (!rendererFound) return

  // 只检查“实例容器”内部的 JSX 新增：
  //   - 有 map 时，容器是 map 调用本身（回调里渲染的每个实例共享这段 JSX）；
  //   - 无 map 时，容器是渲染目标组件的那个 JSX 元素的父节点。
  // 页头按钮这类不受实例数量影响的编辑不应被误判为实例污染。
  const added = leafAdded(addedJSX(beforeFile.ast, afterFile.ast), afterFile.parents)
  const reported = new Set()
  for (const renderer of renderers) {
    const callSiteElement = collectJSX(renderer).find(
      (element) =>
        element.type === 'JSXElement' &&
        matchesComponent(jsxName(element.openingElement.name), component),
    )
    if (!callSiteElement) continue
    const mapCall = findMapAncestor(callSiteElement, afterFile.parents)
    const container = mapCall || afterFile.parents.get(callSiteElement) || renderer
    for (const node of added) {
      if (reported.has(node)) continue
      if (!(node === container || isAncestor(container, node, afterFile.parents))) {
        continue
      }
      if (hasInstanceGuard(node, afterFile.parents, identity)) continue
      reported.add(node)
      const line = node.loc?.start?.line || 0
      const label =
        node.type === 'JSXFragment'
          ? '<>'
          : jsxName(node.openingElement?.name) || 'JSX'
      violations.push(
        addedViolation(
          scope,
          'INSTANCE_AST_UNGUARDED_JSX',
          rel,
          `新增 JSX <${label}> 没有实例守卫：它位于 ${component} 的实例容器内，` +
            `但祖先链上没有 ${scope.instanceKey || '实例身份'} 的条件判断。` +
            '无条件新增会同时出现在所有同类实例中。',
          line,
          { element: label, instanceKey: scope.instanceKey },
        ),
      )
    }
  }

  // 规则三：map 数据源被修改，等于一次性改掉所有实例的数据。
  for (const renderer of renderers) {
    const callSiteElement = collectJSX(renderer).find(
      (element) =>
        element.type === 'JSXElement' &&
        matchesComponent(jsxName(element.openingElement.name), component),
    )
    if (!callSiteElement) continue
    const mapCall = findMapAncestor(callSiteElement, afterFile.parents)
    if (!mapCall) continue
    const source = mapCall.callee.object
    if (source?.type !== 'Identifier') continue
    const beforeInit = findVariableInit(beforeFile.ast, source.name)
    const afterInit = findVariableInit(afterFile.ast, source.name)
    if (!beforeInit || !afterInit) continue
    if (structuralHash(beforeInit) === structuralHash(afterInit)) continue
    violations.push(
      addedViolation(
        scope,
        'INSTANCE_AST_SHARED_DATA_CHANGED',
        rel,
        `map 数据源 ${source.name} 被修改：它驱动 ${component} 的全部实例，` +
          '修改数据会一次性改变所有卡片 / 行，而不是只改目标实例。' +
          '请通过实例条件在渲染层做局部差异化。',
        beforeInit.loc?.start?.line || 0,
        { binding: source.name },
      ),
    )
  }
}

function dedupe(violations) {
  const seen = new Set()
  const result = []
  for (const violation of violations) {
    const key = `${violation.code}|${violation.file}|${violation.line}|${violation.annotationId}`
    if (seen.has(key)) continue
    seen.add(key)
    result.push(violation)
  }
  return result
}

function main() {
  const specPath = process.argv[2]
  if (!specPath) {
    console.error('用法: node ast_scope_check.mjs <spec.json>')
    process.exit(2)
  }
  let spec
  try {
    spec = JSON.parse(fs.readFileSync(specPath, 'utf8'))
  } catch (error) {
    console.error(`读取 spec 失败: ${error.message}`)
    process.exit(2)
  }

  const parser = loadParser(spec.workdir)
  const scopes = Array.isArray(spec.scopes) ? spec.scopes : []
  const changedFiles = new Set(spec.changedFiles || [])
  for (const scope of scopes) {
    if (scope.file) changedFiles.add(scope.file)
    if (scope.callSiteFile) changedFiles.add(scope.callSiteFile)
  }

  const beforeFiles = new Map()
  const afterFiles = new Map()
  const violations = []
  for (const rel of changedFiles) {
    if (!rel || !/\.[cm]?[jt]sx?$/.test(rel)) continue
    try {
      beforeFiles.set(rel, parseFile(spec.beforeRoot, rel, parser))
      afterFiles.set(rel, parseFile(spec.afterRoot, rel, parser))
    } catch (error) {
      violations.push({
        code: 'INSTANCE_AST_PARSE_ERROR',
        file: rel,
        line: 0,
        message: error.message,
      })
    }
  }

  for (const scope of scopes) {
    for (const rel of changedFiles) {
      const beforeFile = beforeFiles.get(rel)
      const afterFile = afterFiles.get(rel)
      if (!beforeFile || !afterFile) continue
      const relevant =
        rel === scope.file ||
        (scope.callSiteFile && rel === scope.callSiteFile) ||
        (!scope.callSiteFile && scope.requireGuard)
      if (!relevant) continue
      // 基础文件未变时，跳过基础组件规则但仍允许 callSite 规则。
      if (rel === scope.file && !scope.protectBase) {
        // 同文件组件：只做守卫规则。
      }
      checkScope(
        scope,
        rel,
        beforeFile,
        afterFile,
        [...changedFiles],
        violations,
      )
    }
  }

  const unique = dedupe(violations)
  const output = { ok: unique.length === 0, violations: unique }
  process.stdout.write(JSON.stringify(output, null, 2))
  process.exit(unique.length === 0 ? 0 : 1)
}

try {
  main()
} catch (error) {
  console.error(error.stack || String(error))
  process.exit(2)
}
