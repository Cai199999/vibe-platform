#!/usr/bin/env node
/**
 * ORM / Express 产物语法树校验器。
 *
 * 输入（stdin JSON）：
 *   {"files": {"server/db.js": "...", "server/index.js": "..."}}
 *
 * 输出（stdout JSON）：
 *   {
 *     "engine": "babel-ast",
 *     "ok": true,
 *     "files": {
 *       "server/db.js": {
 *         "table": "resumes",
 *         "columns": ["id", "file_name"],
 *         "columnDefs": [["id", "INTEGER"], ["file_name", "TEXT"]],
 *         "metaColumns": ["id", "file_name"],
 *         "attributeColumns": [],
 *         "defineTables": ["resumes"],
 *         "errors": [{"message": "...", "line": 1, "column": 1}]
 *       }
 *     }
 *   }
 *
 * 为什么必须 AST：
 *   - 旧实现用正则 + 括号扫描读取 const COLUMN_DEFS，能解析声明，
 *     却看不到 `COLUMN_DEFS.push(['name', 'TEXT'])` 这类运行期追加；
 *   - `JSON.parse(process.env.COLUMNS)` 这类动态字段，正则会把字符串里的
 *     数组片段误当成真实字段；
 *   - AST 能统一识别 TABLE / COLUMN_DEFS / COLUMN_META / attributes /
 *     define(..., { tableName }) 的真实语义，并拒绝无法静态验证的写法。
 *
 * 退出码：
 *   0 = 分析完成（ok 字段表示业务校验是否通过，语法错误也会体现在 errors）；
 *   2 = 校验器自身不可用（Babel 未安装 / 输入不是合法 JSON）。
 */
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url))

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
  console.error(`AST 解析器不可用：${failures.join(' | ')}`)
  process.exit(2)
}

const parser = loadParser()

const MUTATING_METHODS = new Set([
  'push',
  'pop',
  'shift',
  'unshift',
  'splice',
  'sort',
  'reverse',
  'fill',
  'copyWithin',
])

function locOf(node) {
  const start = node?.loc?.start || { line: 1, column: 0 }
  return { line: start.line, column: start.column + 1 }
}

function pushError(errors, node, message) {
  const loc = locOf(node)
  errors.push({ ...loc, message })
}

/** 遍历 AST；跳过 loc/start/end 等元数据，避免递归进入非节点对象。 */
function walk(node, visit) {
  if (!node || typeof node !== 'object') return
  if (Array.isArray(node)) {
    for (const child of node) walk(child, visit)
    return
  }
  if (typeof node.type === 'string') visit(node)
  for (const key of Object.keys(node)) {
    if (key === 'loc' || key === 'start' || key === 'end' || key === 'extra') continue
    walk(node[key], visit)
  }
}

function collectBindings(ast) {
  const bindings = new Map()
  walk(ast, (node) => {
    if (node.type !== 'VariableDeclarator' || !node.init) return
    if (node.id.type !== 'Identifier') return
    // 生成模板只使用顶层常量；同名嵌套声明不影响核心字段来源。
    if (!bindings.has(node.id.name)) {
      bindings.set(node.id.name, node.init)
    }
  })
  return bindings
}

/**
 * 静态求值：只接受编译期可确定的字面量 / 常量引用。
 * 任何运行时依赖（process.env、函数调用、成员访问）都返回 { ok:false }，
 * 让门禁明确报告“无法静态验证”，而不是猜一个值继续。
 */
function staticValue(node, bindings, seen = new Set(), depth = 0) {
  if (!node || depth > 32) return { ok: false }
  switch (node.type) {
    case 'StringLiteral':
    case 'NumericLiteral':
    case 'BooleanLiteral':
      return { ok: true, value: node.value }
    case 'NullLiteral':
      return { ok: true, value: null }
    case 'TemplateLiteral':
      if (node.expressions.length > 0) return { ok: false }
      return {
        ok: true,
        value: node.quasis.map((item) => item.value.cooked ?? '').join(''),
      }
    case 'ArrayExpression': {
      const value = []
      for (const element of node.elements) {
        if (!element) return { ok: false }
        if (element.type === 'SpreadElement') {
          const spread = staticValue(element.argument, bindings, seen, depth + 1)
          if (!spread.ok || !Array.isArray(spread.value)) return { ok: false }
          value.push(...spread.value)
          continue
        }
        const item = staticValue(element, bindings, seen, depth + 1)
        if (!item.ok) return { ok: false }
        value.push(item.value)
      }
      return { ok: true, value }
    }
    case 'ObjectExpression': {
      const value = {}
      for (const property of node.properties) {
        if (property.type !== 'ObjectProperty' || property.computed) return { ok: false }
        const keyNode = property.key
        const key =
          keyNode.type === 'Identifier'
            ? keyNode.name
            : keyNode.type === 'StringLiteral'
              ? keyNode.value
              : ''
        if (!key) return { ok: false }
        const item = staticValue(property.value, bindings, seen, depth + 1)
        if (!item.ok) return { ok: false }
        value[key] = item.value
      }
      return { ok: true, value }
    }
    case 'BinaryExpression': {
      if (node.operator !== '+') return { ok: false }
      const left = staticValue(node.left, bindings, seen, depth + 1)
      const right = staticValue(node.right, bindings, seen, depth + 1)
      if (!left.ok || !right.ok) return { ok: false }
      if (
        (typeof left.value === 'string' || typeof left.value === 'number') &&
        (typeof right.value === 'string' || typeof right.value === 'number')
      ) {
        return { ok: true, value: left.value + right.value }
      }
      return { ok: false }
    }
    case 'UnaryExpression': {
      if (!['-', '+'].includes(node.operator)) return { ok: false }
      const item = staticValue(node.argument, bindings, seen, depth + 1)
      if (!item.ok || typeof item.value !== 'number') return { ok: false }
      return { ok: true, value: node.operator === '-' ? -item.value : +item.value }
    }
    case 'Identifier': {
      if (seen.has(node.name) || !bindings.has(node.name)) return { ok: false }
      return staticValue(bindings.get(node.name), bindings, new Set([...seen, node.name]), depth + 1)
    }
    case 'ParenthesizedExpression':
    case 'TSAsExpression':
    case 'TSTypeAssertion':
      return staticValue(node.expression, bindings, seen, depth + 1)
    case 'CallExpression': {
      const callee = node.callee
      if (callee.type !== 'MemberExpression' || callee.computed) return { ok: false }
      const object = callee.object.type === 'Identifier' ? callee.object.name : ''
      const method = callee.property.type === 'Identifier' ? callee.property.name : ''
      // Object.freeze([...]) 是生成模板常见的不可变包装。
      if (object === 'Object' && method === 'freeze' && node.arguments.length === 1) {
        return staticValue(node.arguments[0], bindings, seen, depth + 1)
      }
      // JSON.parse('...') 只有参数本身是静态字符串时才可验证。
      if (object === 'JSON' && method === 'parse' && node.arguments.length === 1) {
        const arg = staticValue(node.arguments[0], bindings, seen, depth + 1)
        if (!arg.ok || typeof arg.value !== 'string') return { ok: false }
        try {
          return { ok: true, value: JSON.parse(arg.value) }
        } catch {
          return { ok: false }
        }
      }
      return { ok: false }
    }
    default:
      return { ok: false }
  }
}

function memberKey(node) {
  if (node.type !== 'MemberExpression') return ''
  if (!node.computed) {
    return node.property.type === 'Identifier' ? node.property.name : ''
  }
  const key = staticValue(node.property, new Map())
  return key.ok && typeof key.value === 'string' ? key.value : ''
}

function isIdentifierCall(node, name) {
  return (
    node.type === 'CallExpression' &&
    node.callee.type === 'MemberExpression' &&
    !node.callee.computed &&
    node.callee.object.type === 'Identifier' &&
    node.callee.object.name === name &&
    node.callee.property.type === 'Identifier'
  )
}

/**
 * 检测对核心常量的运行期修改。
 *
 * 正则实现只能看到声明文本，`COLUMN_DEFS.push(['name', 'TEXT'])` 会让
 * ORM 实际字段多于静态声明，正是 Unknown column 的隐蔽变体。
 */
function collectMutations(ast, name, errors) {
  walk(ast, (node) => {
    if (node.type === 'AssignmentExpression') {
      const left = node.left
      if (left.type === 'Identifier' && left.name === name) {
        pushError(errors, node, `${name} 在运行期被重新赋值，静态字段不可信`)
        return
      }
      if (
        left.type === 'MemberExpression' &&
        left.object.type === 'Identifier' &&
        left.object.name === name
      ) {
        pushError(errors, node, `${name} 的元素在运行期被修改，静态字段不可信`)
      }
    }
    if (node.type === 'UpdateExpression') {
      const argument = node.argument
      if (
        argument.type === 'Identifier' && argument.name === name
      ) {
        pushError(errors, node, `${name} 在运行期被更新，静态字段不可信`)
      }
    }
    if (node.type === 'UnaryExpression' && node.operator === 'delete') {
      const argument = node.argument
      if (
        argument.type === 'MemberExpression' &&
        argument.object.type === 'Identifier' &&
        argument.object.name === name
      ) {
        pushError(errors, node, `${name} 的字段在运行期被删除，静态字段不可信`)
      }
    }
    if (isIdentifierCall(node, name)) {
      const method = node.callee.property.name
      if (MUTATING_METHODS.has(method)) {
        pushError(errors, node, `${name}.${method}() 在运行期改变字段集合，静态字段不可信`)
      }
    }
  })
}

function collectAttributeColumns(ast) {
  const columns = []
  const seen = new Set()
  const add = (name) => {
    if (name && !seen.has(name)) {
      seen.add(name)
      columns.push(name)
    }
  }
  walk(ast, (node) => {
    // const attributes = { id: {...}, file_name: {...} }
    if (
      node.type === 'VariableDeclarator' &&
      node.id.type === 'Identifier' &&
      node.id.name === 'attributes' &&
      node.init?.type === 'ObjectExpression'
    ) {
      const value = staticValue(node.init, new Map())
      if (value.ok && value.value && typeof value.value === 'object') {
        Object.keys(value.value).forEach(add)
      }
    }
    // attributes.name = {...} / attributes['file_name'] = {...}
    if (node.type === 'AssignmentExpression' && node.left.type === 'MemberExpression') {
      if (
        node.left.object.type === 'Identifier' &&
        node.left.object.name === 'attributes'
      ) {
        add(memberKey(node.left))
      }
    }
    // Object.assign(attributes, { ... })
    if (
      node.type === 'CallExpression' &&
      node.callee.type === 'MemberExpression' &&
      !node.callee.computed &&
      node.callee.object.type === 'Identifier' &&
      node.callee.object.name === 'Object' &&
      node.callee.property.type === 'Identifier' &&
      node.callee.property.name === 'assign'
    ) {
      const [target, ...sources] = node.arguments
      if (target?.type === 'Identifier' && target.name === 'attributes') {
        for (const source of sources) {
          const value = staticValue(source, new Map())
          if (value.ok && value.value && typeof value.value === 'object') {
            Object.keys(value.value).forEach(add)
          }
        }
      }
    }
  })
  return columns
}

function collectDefine(ast, bindings, errors) {
  const attributeColumns = []
  const defineTables = []
  walk(ast, (node) => {
    if (node.type !== 'CallExpression') return
    const callee = node.callee
    if (
      callee.type !== 'MemberExpression' ||
      callee.computed ||
      callee.property.type !== 'Identifier' ||
      callee.property.name !== 'define'
    ) {
      return
    }
    const args = node.arguments
    if (args.length >= 2) {
      // sequelize.define('Model', { field: ... }, { tableName })
      const attributes = staticValue(args[1], bindings)
      if (attributes.ok && attributes.value && typeof attributes.value === 'object') {
        Object.keys(attributes.value).forEach((key) => attributeColumns.push(key))
      }
    }
    if (args.length >= 3) {
      const options = staticValue(args[2], bindings)
      if (options.ok && options.value && typeof options.value === 'object') {
        const tableName = options.value.tableName
        if (typeof tableName === 'string' && tableName.trim()) {
          defineTables.push(tableName.trim())
        }
      }
    }
  })
  return { attributeColumns, defineTables }
}

function normalizedStringList(value) {
  if (!Array.isArray(value)) return null
  const names = []
  const seen = new Set()
  for (const item of value) {
    if (typeof item !== 'string' || !item.trim()) return null
    const name = item.trim()
    if (seen.has(name)) continue
    seen.add(name)
    names.push(name)
  }
  return names
}

/**
 * 静态解析 RELATED_MODELS：主从表的关联模型也必须纳入 AST 门禁。
 *
 * 只校验核心表会漏掉 `Resume.findAll({ include: User })` 读取 users.iam_sub
 * 这类关联字段漂移，因此每个关联模型的 table / columns 都要求是静态可验证的
 * 字面量，并与 columnDefs / columnMeta 自洽。
 */
function collectRelatedModels(ast, bindings, errors, coreTable) {
  const models = []
  const node = bindings.get('RELATED_MODELS')
  if (!node) return models
  const value = staticValue(node, bindings, new Set(['RELATED_MODELS']))
  if (!value.ok || !Array.isArray(value.value)) {
    pushError(errors, node, 'RELATED_MODELS 必须是静态数组字面量，禁止运行时拼接')
    return models
  }
  const seen = new Set()
  value.value.forEach((item) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) {
      pushError(errors, node, 'RELATED_MODELS 的每一项都必须是对象')
      return
    }
    const name = typeof item.table === 'string' ? item.table.trim() : ''
    if (!name) {
      pushError(errors, node, 'RELATED_MODELS 的每一项都必须包含非空 table')
      return
    }
    const key = name.toLowerCase()
    if (coreTable && key === coreTable.toLowerCase()) {
      pushError(errors, node, `RELATED_MODELS 不能重复声明核心表 ${name}`)
      return
    }
    if (seen.has(key)) {
      pushError(errors, node, `RELATED_MODELS 存在重复表：${name}`)
      return
    }
    seen.add(key)

    let columns = normalizedStringList(item.columns)
    if (Array.isArray(item.columnDefs)) {
      const defNames = []
      for (const pair of item.columnDefs) {
        if (!Array.isArray(pair) || typeof pair[0] !== 'string' || !pair[0].trim()) {
          pushError(errors, node, `RELATED_MODELS.${name}.columnDefs 必须是 [字段名, 类型]`)
          return
        }
        defNames.push(pair[0].trim())
      }
      if (!columns) {
        columns = [...new Set(defNames)]
      } else {
        const declared = new Set(columns.map((column) => column.toLowerCase()))
        const defined = new Set(defNames.map((column) => column.toLowerCase()))
        const missing = columns.filter((column) => !defined.has(column.toLowerCase()))
        const extra = defNames.filter((column) => !declared.has(column.toLowerCase()))
        if (missing.length > 0 || extra.length > 0) {
          pushError(
            errors,
            node,
            `RELATED_MODELS.${name} 的 columns 与 columnDefs 不一致` +
              (missing.length ? `，缺少列定义：${missing.join('、')}` : '') +
              (extra.length ? `，多出列定义：${extra.join('、')}` : ''),
          )
        }
      }
    }
    if (!columns || columns.length === 0) {
      pushError(errors, node, `RELATED_MODELS.${name} 未声明任何字段，关联模型不可验证`)
      return
    }
    if (item.columnMeta && typeof item.columnMeta === 'object') {
      const declared = new Set(columns.map((column) => column.toLowerCase()))
      const extraMeta = Object.keys(item.columnMeta).filter(
        (column) => !declared.has(column.toLowerCase()),
      )
      if (extraMeta.length > 0) {
        pushError(
          errors,
          node,
          `RELATED_MODELS.${name}.columnMeta 包含不存在的字段：${extraMeta.join('、')}`,
        )
      }
    }
    models.push({ table: name, columns })
  })
  return models
}

/**
 * 静态解析 RELATIONS：每条关联必须指向真实存在的核心表或关联模型，
 * 且外键列必须真实存在于子表字段集合中。契约层面已经校验过一次，
 * 这里校验的是“代码是否忠实实现了契约”，两者不能互相替代。
 */
function collectRelations(ast, bindings, errors, coreTable, coreColumns, relatedModels) {
  const relations = []
  const node = bindings.get('RELATIONS')
  if (!node) return relations
  const value = staticValue(node, bindings, new Set(['RELATIONS']))
  if (!value.ok || !Array.isArray(value.value)) {
    pushError(errors, node, 'RELATIONS 必须是静态数组字面量，禁止运行时拼接')
    return relations
  }
  const tableColumns = new Map()
  if (coreTable) {
    tableColumns.set(coreTable.toLowerCase(), new Set(coreColumns.map((c) => c.toLowerCase())))
  }
  for (const model of relatedModels) {
    tableColumns.set(model.table.toLowerCase(), new Set(model.columns.map((c) => c.toLowerCase())))
  }
  const seen = new Set()
  const readString = (item, key) =>
    typeof item[key] === 'string' && item[key].trim() ? item[key].trim() : ''
  value.value.forEach((item) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) {
      pushError(errors, node, 'RELATIONS 的每一项都必须是对象')
      return
    }
    const parentTable = readString(item, 'parentTable')
    const childTable = readString(item, 'childTable')
    const foreignKey = readString(item, 'foreignKey')
    const type = readString(item, 'type') || 'one_to_many'
    if (!parentTable || !childTable || !foreignKey) {
      pushError(
        errors,
        node,
        'RELATIONS 的每一项都必须包含 parentTable / childTable / foreignKey',
      )
      return
    }
    if (!['one_to_many', 'many_to_one', 'one_to_one'].includes(type)) {
      pushError(errors, node, `RELATIONS 存在未知关联类型：${type}`)
      return
    }
    const key = `${parentTable.toLowerCase()}->${childTable.toLowerCase()}.${foreignKey.toLowerCase()}`
    if (seen.has(key)) {
      pushError(errors, node, `RELATIONS 存在重复关联：${parentTable} -> ${childTable}`)
      return
    }
    seen.add(key)
    if (tableColumns.size > 0) {
      const parentColumns = tableColumns.get(parentTable.toLowerCase())
      const childColumns = tableColumns.get(childTable.toLowerCase())
      if (!parentColumns) {
        pushError(
          errors,
          node,
          `RELATIONS 引用了未声明的父表 ${parentTable}，请检查 TABLE / RELATED_MODELS`,
        )
        return
      }
      if (!childColumns) {
        pushError(
          errors,
          node,
          `RELATIONS 引用了未声明的子表 ${childTable}，请检查 TABLE / RELATED_MODELS`,
        )
        return
      }
      if (!childColumns.has(foreignKey.toLowerCase())) {
        pushError(
          errors,
          node,
          `RELATIONS 的外键 ${childTable}.${foreignKey} 不在子表字段中`,
        )
        return
      }
    }
    relations.push({ parentTable, childTable, foreignKey, type })
  })
  return relations
}

function analyzeSource(source, file) {
  const errors = []
  // index.js 只承担 Express 会话层，不要求声明 COLUMN_DEFS；
  // db.js 才是 ORM 字段的唯一事实来源，缺失必须阻断。
  const isOrmFile = /(^|\/)db\.(js|mjs|cjs)$/i.test(file)
  let ast
  try {
    ast = parser.parse(source, {
      sourceType: 'unambiguous',
      errorRecovery: false,
      allowReturnOutsideFunction: true,
      plugins: ['jsx', 'dynamicImport', 'importMeta', 'topLevelAwait'],
    })
  } catch (error) {
    const loc = error.loc || { line: 1, column: 0 }
    return {
      table: '',
      columns: [],
      columnDefs: [],
      metaColumns: [],
      attributeColumns: [],
      defineTables: [],
      relatedModels: [],
      relations: [],
      errors: [
        {
          line: loc.line,
          column: loc.column + 1,
          message: `语法错误：${error.message}`,
        },
      ],
    }
  }

  const bindings = collectBindings(ast)
  const tableNode = bindings.get('TABLE')
  let table = ''
  if (!tableNode) {
    if (isOrmFile) {
      pushError(errors, ast, '未找到静态 const TABLE，无法确认 ORM 目标表')
    }
  } else {
    const value = staticValue(tableNode, bindings, new Set(['TABLE']))
    if (!value.ok || typeof value.value !== 'string' || !value.value.trim()) {
      pushError(errors, tableNode, 'TABLE 必须是静态字符串字面量，禁止运行时拼接')
    } else {
      table = value.value.trim()
    }
  }
  collectMutations(ast, 'TABLE', errors)

  const columnDefs = []
  const defsNode = bindings.get('COLUMN_DEFS')
  if (!defsNode) {
    if (isOrmFile) {
      pushError(errors, ast, '未声明静态 const COLUMN_DEFS，ORM 字段不可验证')
    }
  } else {
    const value = staticValue(defsNode, bindings, new Set(['COLUMN_DEFS']))
    if (!value.ok) {
      pushError(
        errors,
        defsNode,
        'COLUMN_DEFS 不是静态可解析的字面量：禁止 JSON.parse(变量) / 函数调用 / 环境变量',
      )
    } else if (!Array.isArray(value.value)) {
      pushError(errors, defsNode, 'COLUMN_DEFS 必须是二维数组 [[字段名, 类型], ...]')
    } else {
      const seen = new Set()
      for (const item of value.value) {
        if (!Array.isArray(item) || typeof item[0] !== 'string' || !item[0].trim()) {
          pushError(errors, defsNode, 'COLUMN_DEFS 的每一项都必须是 [字段名字符串, 类型]')
          continue
        }
        const name = item[0].trim()
        if (seen.has(name)) {
          pushError(errors, defsNode, `COLUMN_DEFS 存在重复字段：${name}`)
          continue
        }
        seen.add(name)
        columnDefs.push([name, String(item[1] ?? '').trim()])
      }
    }
  }
  collectMutations(ast, 'COLUMN_DEFS', errors)

  const metaColumns = []
  const metaNode = bindings.get('COLUMN_META')
  if (metaNode) {
    const value = staticValue(metaNode, bindings, new Set(['COLUMN_META']))
    if (!value.ok || !value.value || typeof value.value !== 'object') {
      pushError(errors, metaNode, 'COLUMN_META 必须是静态对象字面量')
    } else {
      Object.keys(value.value).forEach((key) => metaColumns.push(key))
    }
  }

  const define = collectDefine(ast, bindings, errors)
  const attributeColumns = new Set([
    ...collectAttributeColumns(ast),
    ...define.attributeColumns,
  ])
  const defineTables = [...new Set(define.defineTables)]
  if (defineTables.length > 1) {
    pushError(errors, ast, `检测到多个 define tableName：${defineTables.join('、')}`)
  }
  if (table && defineTables.length === 1 && defineTables[0] !== table) {
    pushError(
      errors,
      ast,
      `TABLE=${table} 与 define tableName=${defineTables[0]} 不一致`,
    )
  }
  if (!table && defineTables.length === 1) {
    table = defineTables[0]
  }

  const defNames = columnDefs.map(([name]) => name)
  const extraMeta = metaColumns.filter((name) => !defNames.includes(name))
  if (extraMeta.length > 0) {
    pushError(
      errors,
      metaNode,
      `COLUMN_META 包含 COLUMN_DEFS 中不存在的字段：${extraMeta.join('、')}`,
    )
  }

  const columns = []
  const seenColumns = new Set()
  for (const name of [...defNames, ...metaColumns, ...attributeColumns]) {
    if (name && !seenColumns.has(name)) {
      seenColumns.add(name)
      columns.push(name)
    }
  }
  if (isOrmFile && columns.length === 0) {
    pushError(errors, ast, '未解析出任何字段：COLUMN_DEFS / COLUMN_META / attributes 均为空')
  }

  // 主从表常量与核心字段同等对待：运行期修改会让关联查询字段不可信。
  collectMutations(ast, 'RELATED_MODELS', errors)
  collectMutations(ast, 'RELATIONS', errors)
  const relatedModels = collectRelatedModels(ast, bindings, errors, table)
  const relations = collectRelations(ast, bindings, errors, table, columns, relatedModels)

  return {
    table,
    columns,
    columnDefs,
    metaColumns,
    attributeColumns: [...attributeColumns],
    defineTables,
    relatedModels,
    relations,
    errors,
  }
}

async function readStdin() {
  const chunks = []
  for await (const chunk of process.stdin) chunks.push(chunk)
  return Buffer.concat(chunks).toString('utf8')
}

async function main() {
  let payload
  try {
    payload = JSON.parse(await readStdin())
  } catch (error) {
    console.error(`输入不是合法 JSON：${error.message}`)
    return 2
  }
  const files = payload?.files
  if (!files || typeof files !== 'object') {
    console.error('输入缺少 files 对象')
    return 2
  }

  const results = {}
  let ok = true
  for (const [file, source] of Object.entries(files)) {
    if (typeof source !== 'string') {
      results[file] = {
        table: '',
        columns: [],
        columnDefs: [],
        metaColumns: [],
        attributeColumns: [],
        defineTables: [],
        relatedModels: [],
        relations: [],
        errors: [{ line: 1, column: 1, message: '文件内容不是字符串' }],
      }
      ok = false
      continue
    }
    try {
      const result = analyzeSource(source, file)
      results[file] = result
      if (result.errors.length > 0) ok = false
    } catch (error) {
      results[file] = {
        table: '',
        columns: [],
        columnDefs: [],
        metaColumns: [],
        attributeColumns: [],
        defineTables: [],
        relatedModels: [],
        relations: [],
        errors: [{ line: 1, column: 1, message: `AST 分析异常：${error.message}` }],
      }
      ok = false
    }
  }

  process.stdout.write(
    JSON.stringify({ engine: 'babel-ast', ok, files: results }, null, 0),
  )
  return 0
}

main()
  .then((code) => process.exit(code))
  .catch((error) => {
    console.error(`AST 校验器异常：${error.stack || error.message}`)
    process.exit(2)
  })
