/**
 * Vibe Source Inspect 插件
 * ------------------------------------------------------------------
 * 目的：让「预览 iframe 里的任意 DOM 节点」都能回传精确源码出处。
 *
 * 1. React / JSX：使用 Babel 在 JSXOpeningElement 上注入
 *    data-source-file / line / column / component；如果源码没有业务
 *    data-source，再补一个 file:line:column 的唯一标记。
 * 2. Vue SFC：使用 @vue/compiler-sfc + @vue/compiler-dom 解析 template，
 *    在原始 .vue 文本中插入同样的属性，行号保持指向真实 SFC 文件。
 * 3. 通过 transformIndexHtml + configureServer 注入 inspect_sdk.js，
 *    父页面用 postMessage 驱动 elementFromPoint 命中检测。
 *
 * 该插件只做只读的源码标记，不改变组件行为；注入的属性全部是
 * data-*，React 与 Vue 都会原样落到 DOM。
 */

import { createHash } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { transformSync } from '@babel/core'
import * as t from '@babel/types'
import { parse as parseSfc } from '@vue/compiler-sfc'
import { parse as parseTemplate } from '@vue/compiler-dom'

const SDK_PATH = '/__vibe_inspect_sdk.js'
const SDK_FILE = fileURLToPath(new URL('./inspect_sdk.js', import.meta.url))
const CONTAINER_TAGS = new Set([
  'div',
  'section',
  'main',
  'article',
  'header',
  'footer',
  'nav',
  'aside',
  'ul',
  'ol',
  'form',
  'table',
])
const SKIP_TAGS = new Set(['template', 'slot', 'component', 'transition'])
const SCRIPT_EXTENSIONS = new Set(['.js', '.jsx', '.ts', '.tsx'])
const SOURCE_MAP_FILE = 'vibe-source-map.json'
const SOURCE_MAP_VERSION = 1

let projectRoot = process.cwd()
let sdkCache = ''

/**
 * 构建期源码地图。
 *
 * oid -> { file, start, end, line, column, component, tag, isContainer,
 *          parentOid, siblingIndex, siblingCount, depth }
 *
 * oid 由「文件 + 组件 + JSX 结构路径」确定性生成，不依赖行号；行号只
 * 作为调试信息写入地图，写操作以 oid + start/end 为准。
 */
const sourceMapNodes = new Map()
const sourceMapFiles = new Map()
const sourceMapFileMeta = new Map()
const sourceMapCallSites = new Map()
let sourceMapJson = ''
let sourceMapTimer = null

function readSdk() {
  if (!sdkCache) sdkCache = fs.readFileSync(SDK_FILE, 'utf8')
  return sdkCache
}

function relativeFile(filename) {
  const relative = path.relative(projectRoot, filename)
  return relative.split(path.sep).join('/')
}

function fileComponent(filename) {
  const base = path.basename(filename)
  return base.replace(/\.(vue|jsx|tsx|js|ts)$/i, '') || 'Component'
}

function lineColumnAt(text, offset) {
  let line = 1
  let column = 1
  for (let index = 0; index < offset; index += 1) {
    if (text.charCodeAt(index) === 10) {
      line += 1
      column = 1
    } else {
      column += 1
    }
  }
  return { line, column }
}

function escapeAttribute(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

function hashText(value) {
  return createHash('sha256').update(String(value)).digest('hex')
}

/** 稳定 oid：文件 + 组件 + JSX 结构路径，不包含行号。 */
function oidFor(file, component, signature) {
  return `oid_${hashText(`${file}|${component}|${signature}`).slice(0, 16)}`
}

function jsxTagName(node) {
  const name = node?.openingElement?.name
  if (!name) return ''
  if (name.type === 'JSXIdentifier') return name.name
  if (name.type === 'JSXMemberExpression') {
    return `${jsxTagName({ openingElement: { name: name.object } })}.${name.property.name}`
  }
  return ''
}

function jsxAttributeNames(node) {
  return new Set(
    (node?.openingElement?.attributes || [])
      .filter((item) => item.type === 'JSXAttribute')
      .map((item) => item.name && item.name.name)
      .filter(Boolean),
  )
}

function jsxStringAttribute(node, name) {
  const attribute = (node?.openingElement?.attributes || []).find(
    (item) =>
      item.type === 'JSXAttribute' &&
      item.name &&
      item.name.name === name,
  )
  return attribute && attribute.value && attribute.value.type === 'StringLiteral'
    ? attribute.value.value
    : ''
}

function jsxSiblingInfo(elementPath) {
  const parent = elementPath.parentPath
  const children = parent?.node?.children
  if (!Array.isArray(children)) return { index: 0, count: 1 }
  const elements = children.filter(
    (child) =>
      child && (child.type === 'JSXElement' || child.type === 'JSXFragment'),
  )
  return {
    index: Math.max(0, elements.indexOf(elementPath.node)),
    count: Math.max(1, elements.length),
  }
}

/**
 * 同一组件函数内的 JSX 祖先链（含自身）。
 * 遇到函数边界即停止：跨组件调用点由 callSite / importer 信息处理，
 * 不能把另一个函数的 AST 结构硬拼进当前 oid。
 */
function jsxAncestorChain(openingPath) {
  const chain = []
  let current = openingPath.parentPath
  while (current && !current.isFunction?.()) {
    if (current.node?.type === 'JSXElement') {
      const tag = jsxTagName(current.node)
      if (tag) {
        const sibling = jsxSiblingInfo(current)
        chain.unshift({
          tag,
          index: sibling.index,
          count: sibling.count,
        })
      }
    }
    current = current.parentPath
  }
  return chain
}

function signatureOf(chain) {
  return chain.map((item) => `${item.tag}:${item.index}`).join('/')
}

/** 完整 JSX 祖先键路径（含表达式容器 / 条件分支），用于 oid 唯一性。 */
function jsxElementSignature(elementPath) {
  const parts = []
  let current = elementPath
  while (current && !current.isFunction?.()) {
    const node = current.node
    if (node) parts.unshift(`${node.type}:${current.key ?? ''}`)
    current = current.parentPath
  }
  return parts.join('/')
}

function jsxParentElementPath(elementPath) {
  let current = elementPath?.parentPath
  while (current && !current.isFunction?.()) {
    if (current.node?.type === 'JSXElement') return current
    current = current.parentPath
  }
  return null
}

/**
 * 函数作用域签名：避免同一组件内多个匿名 map 回调产生相同结构路径。
 * 使用 AST 祖先 key 而不是行号，保证纯换行 / 空行不影响 oid。
 */
function jsxScopeSignature(openingPath) {
  const functionPath = openingPath.getFunctionParent?.()
  if (!functionPath) return ''
  const ancestry = functionPath.getAncestry?.() || []
  return ancestry
    .map((item) => `${item.node?.type || ''}:${item.key ?? ''}`)
    .join('/')
}

/** 当前 JSX 是否位于 .map(...) 回调产生的重复模板中。 */
function jsxInsideMap(openingPath) {
  let current = openingPath
  while (current) {
    const node = current.node
    if (
      node?.type === 'CallExpression' &&
      node.callee?.type === 'MemberExpression' &&
      node.callee.property?.name === 'map'
    ) {
      return true
    }
    current = current.parentPath
  }
  return false
}

/** 条件渲染下的调用点无法用静态顺序稳定消歧。 */
function jsxInsideConditional(openingPath) {
  let current = openingPath
  while (current) {
    const node = current.node
    if (
      node?.type === 'ConditionalExpression' ||
      node?.type === 'LogicalExpression'
    ) {
      return true
    }
    current = current.parentPath
  }
  return false
}

function recordSourceMapNode(record) {
  sourceMapNodes.set(record.oid, record)
  const file = sourceMapFiles.get(record.file) || {
    file: record.file,
    hash: '',
    oids: new Set(),
  }
  file.oids.add(record.oid)
  sourceMapFiles.set(record.file, file)
}

function recordCallSite(record) {
  sourceMapCallSites.set(record.oid, record)
}

function forgetFile(file) {
  const previous = sourceMapFiles.get(file)
  if (previous) {
    for (const oid of previous.oids) sourceMapNodes.delete(oid)
  }
  for (const [oid, callSite] of sourceMapCallSites.entries()) {
    if (callSite.file === file) sourceMapCallSites.delete(oid)
  }
  sourceMapFiles.delete(file)
  sourceMapFileMeta.delete(file)
}

const MODULE_EXTENSIONS = [
  '',
  '.jsx',
  '.tsx',
  '.js',
  '.ts',
  '/index.jsx',
  '/index.tsx',
  '/index.js',
  '/index.ts',
]

/** 把相对 import 解析成 Source Map 里真实存在的文件。 */
function resolveImportFile(fromFile, source) {
  if (!source || !source.startsWith('.')) return ''
  const base = path.posix.normalize(
    path.posix.join(path.posix.dirname(fromFile), source),
  )
  for (const suffix of MODULE_EXTENSIONS) {
    const candidate = `${base}${suffix}`
    if (sourceMapFiles.has(candidate) || sourceMapFileMeta.has(candidate)) {
      return candidate
    }
  }
  return ''
}

function buildSourceMap() {
  const files = {}
  const entries = [...sourceMapFiles.entries()].sort(([a], [b]) =>
    a.localeCompare(b),
  )
  for (const [file, meta] of entries) {
    const fileMeta = sourceMapFileMeta.get(file) || {}
    files[file] = {
      hash: meta.hash || '',
      oids: [...meta.oids].sort(),
      imports: fileMeta.imports || [],
      defaultExportName: fileMeta.defaultExportName || '',
      namedExports: fileMeta.namedExports || [],
    }
  }
  const nodes = {}
  for (const [oid, record] of [...sourceMapNodes.entries()].sort(([a], [b]) =>
    a.localeCompare(b),
  )) {
    nodes[oid] = { ...record, callSites: [] }
  }

  const callSites = {}
  const components = {}
  for (const [callSiteOid, raw] of [...sourceMapCallSites.entries()].sort(
    ([a], [b]) => a.localeCompare(b),
  )) {
    const meta = sourceMapFileMeta.get(raw.file) || {}
    const localBase = String(raw.component || '').split('.')[0]
    const imported = (meta.imports || []).find(
      (item) => item.local === localBase,
    )
    const targetFile =
      imported && imported.source.startsWith('.')
        ? resolveImportFile(raw.file, imported.source)
        : raw.file
    const targetMeta = sourceMapFileMeta.get(targetFile) || {}
    const definitionName =
      imported?.imported === 'default'
        ? targetMeta.defaultExportName ||
          path.posix.basename(targetFile).replace(/\.[^.]+$/, '') ||
          localBase
        : imported?.imported || raw.component
    const definition =
      (targetMeta.components || {})[definitionName] ||
      (targetMeta.components || {})[localBase] ||
      null
    const forwardsChildren = Boolean(
      (targetMeta.forwardingChildren || {})[definitionName],
    )
    const callSite = {
      ...raw,
      importSource: imported?.source || '',
      importedName: imported?.imported || '',
      targetFile,
      definitionOid: definition?.definitionOid || '',
      definitionFile: definition?.file || '',
      forwardsChildren,
      slot: Boolean(raw.hasChildren && forwardsChildren),
      safeForOrder: !raw.inMap && !raw.conditional,
    }
    callSites[callSiteOid] = callSite
    if (definition?.definitionOid && nodes[definition.definitionOid]) {
      nodes[definition.definitionOid].callSites.push({
        callSiteOid,
        file: raw.file,
        start: raw.start,
        end: raw.end,
        line: raw.line,
        column: raw.column,
        component: raw.component,
        slot: callSite.slot,
        inMap: Boolean(raw.inMap),
        conditional: Boolean(raw.conditional),
        safeForOrder: callSite.safeForOrder,
        props: raw.props || {},
      })
      const key = `${definition.file || targetFile}#${definitionName}`
      const component = components[key] || {
        file: definition.file || targetFile,
        name: definitionName,
        definitionOid: definition.definitionOid,
        callSiteOids: [],
        forwardsChildren,
      }
      component.callSiteOids.push(callSiteOid)
      components[key] = component
    }
  }

  // 调用点按源码顺序排序，运行时实例序号才能与静态顺序对齐。
  for (const node of Object.values(nodes)) {
    if (!Array.isArray(node.callSites) || node.callSites.length < 2) continue
    node.callSites.sort((left, right) => {
      if (left.file !== right.file) return left.file.localeCompare(right.file)
      return Number(left.start) - Number(right.start)
    })
  }

  // 容器落点语义：DOM 容器不等于可插入的 JSX 作用域。
  const importedComponents = new Set(Object.keys(components))
  const slotCounts = new Map()
  for (const callSite of Object.values(callSites)) {
    if (!callSite.slot || !callSite.definitionOid) continue
    slotCounts.set(
      callSite.definitionOid,
      (slotCounts.get(callSite.definitionOid) || 0) + 1,
    )
  }
  const slotRoots = new Set(
    [...slotCounts.entries()]
      .filter(([, count]) => count === 1)
      .map(([oid]) => oid),
  )
  for (const node of Object.values(nodes)) {
    if (node.inMap) {
      node.containerScope = 'repeated_template'
    } else if (slotRoots.has(node.oid)) {
      node.containerScope = 'call_site_slot'
    } else if (
      importedComponents.has(`${node.file}#${node.component}`)
    ) {
      node.containerScope = 'component_definition'
    } else {
      node.containerScope = 'local'
    }
    // 快路径判定：只对本地、非循环、非共享、非页面根的叶子成立。
    // 运行时若发现多实例，SDK 会再降级到确认 / HITL。
    const importedComponent = importedComponents.has(
      `${node.file}#${node.component}`,
    )
    node.deleteSafe = Boolean(
      !node.inMap &&
        !importedComponent &&
        !(Array.isArray(node.callSites) && node.callSites.length) &&
        node.parentOid,
    )
  }

  const revision = hashText(
    JSON.stringify({
      files,
      oids: Object.keys(nodes),
      callSites: Object.keys(callSites),
      components: Object.keys(components),
    }),
  ).slice(0, 16)
  return JSON.stringify(
    {
      version: SOURCE_MAP_VERSION,
      revision,
      generatedAt: new Date().toISOString(),
      files,
      nodes,
      callSites,
      importerGraph: { components },
    },
    null,
    2,
  )
}

function writeSourceMap() {
  sourceMapTimer = null
  sourceMapJson = buildSourceMap()
  try {
    fs.writeFileSync(path.join(projectRoot, SOURCE_MAP_FILE), sourceMapJson)
  } catch {
    // 只读文件系统 / 构建容器没有写权限时，中间件仍然可以用内存地图。
  }
}

function scheduleSourceMapWrite() {
  if (sourceMapTimer) return
  sourceMapTimer = setTimeout(writeSourceMap, 80)
  if (typeof sourceMapTimer.unref === 'function') sourceMapTimer.unref()
}

/**
 * 生成统一属性。已有业务 data-source 时保留它，但仍补齐源码出处，
 * 这样 INITIAL_APP 这类手工标记的模板也能返回精确行号。
 */
function sourceAttributes({
  file,
  line,
  column,
  component,
  tag,
  hasSource,
  hasKind,
  existingKind,
}) {
  const attributes = []
  attributes.push(`data-source-file="${escapeAttribute(file)}"`)
  attributes.push(`data-source-line="${line}"`)
  attributes.push(`data-source-column="${column}"`)
  attributes.push(`data-source-component="${escapeAttribute(component)}"`)
  if (!hasSource) {
    attributes.push(
      `data-source="${escapeAttribute(`${file}:${line}:${column}`)}"`,
    )
  }
  if (!hasKind) {
    const kind =
      existingKind ||
      (CONTAINER_TAGS.has(String(tag).toLowerCase()) ? 'container' : 'element')
    attributes.push(`data-source-kind="${kind}"`)
  }
  return attributes
}

// ------------------------------------------------------------
// React / JSX
// ------------------------------------------------------------
function componentNameFor(pathNode, filename) {
  let current = pathNode
  while (current) {
    const node = current.node
    if (node && node.id && node.id.name) return node.id.name
    if (
      current.isVariableDeclarator?.() &&
      current.node.id &&
      current.node.id.type === 'Identifier'
    ) {
      return current.node.id.name
    }
    if (current.isClassDeclaration?.() && current.node.id) {
      return current.node.id.name
    }
    current = current.parentPath
  }
  return fileComponent(filename)
}

function jsxStaticProps(node) {
  const props = {}
  for (const attribute of node?.openingElement?.attributes || []) {
    if (attribute.type !== 'JSXAttribute' || !attribute.name?.name) continue
    const name = attribute.name.name
    if (attribute.value == null) {
      props[name] = true
    } else if (attribute.value.type === 'StringLiteral') {
      props[name] = attribute.value.value
    } else {
      props[name] = '<expression>'
    }
  }
  return props
}

function collectModuleMeta(programPath, filename) {
  const imports = []
  const namedExports = []
  let defaultExportName = ''
  for (const statement of programPath.node.body || []) {
    if (statement.type === 'ImportDeclaration') {
      const source = String(statement.source?.value || '')
      for (const specifier of statement.specifiers || []) {
        if (specifier.type === 'ImportDefaultSpecifier') {
          imports.push({
            local: specifier.local.name,
            source,
            imported: 'default',
          })
        } else if (specifier.type === 'ImportSpecifier') {
          imports.push({
            local: specifier.local.name,
            source,
            imported: specifier.imported?.name || specifier.local.name,
          })
        } else if (specifier.type === 'ImportNamespaceSpecifier') {
          imports.push({
            local: specifier.local.name,
            source,
            imported: '*',
          })
        }
      }
    } else if (statement.type === 'ExportDefaultDeclaration') {
      const declaration = statement.declaration
      if (declaration?.type === 'FunctionDeclaration' && declaration.id) {
        defaultExportName = declaration.id.name
      } else if (declaration?.type === 'Identifier') {
        defaultExportName = declaration.name
      }
    } else if (statement.type === 'ExportNamedDeclaration') {
      if (statement.declaration?.type === 'FunctionDeclaration') {
        if (statement.declaration.id?.name) {
          namedExports.push(statement.declaration.id.name)
        }
      }
      for (const specifier of statement.specifiers || []) {
        if (specifier.exported?.name) namedExports.push(specifier.exported.name)
      }
    }
  }
  const file = relativeFile(filename)
  const previous = sourceMapFileMeta.get(file) || {}
  sourceMapFileMeta.set(file, {
    file,
    imports,
    namedExports,
    defaultExportName,
    components: previous.components || {},
    forwardingChildren: previous.forwardingChildren || {},
  })
}

function buildJsxPlugin(filename) {
  return function jsxSourcePlugin({ types }) {
    return {
      name: 'vibe-inspect-jsx-source',
      visitor: {
        Program(programPath) {
          collectModuleMeta(programPath, filename)
        },
        JSXExpressionContainer(pathNode) {
          const expression = pathNode.node.expression
          if (
            expression?.type !== 'Identifier' ||
            expression.name !== 'children'
          ) {
            return
          }
          const file = relativeFile(filename)
          const component = componentNameFor(pathNode, filename)
          const meta = sourceMapFileMeta.get(file)
          if (!meta) return
          meta.forwardingChildren = {
            ...(meta.forwardingChildren || {}),
            [component]: true,
          }
          sourceMapFileMeta.set(file, meta)
        },
        JSXOpeningElement(pathNode) {
          const nameNode = pathNode.node.name
          if (!nameNode || nameNode.type !== 'JSXIdentifier') return
          const tag = nameNode.name
          if (!tag) return
          const elementNode = pathNode.parentPath?.node
          if (!elementNode || elementNode.type !== 'JSXElement') return

          const loc = pathNode.node.loc && pathNode.node.loc.start
          if (!loc) return
          const file = relativeFile(filename)
          const line = loc.line
          const column = loc.column + 1
          const component = componentNameFor(pathNode, filename)
          const elementPath = pathNode.parentPath
          const chain = jsxAncestorChain(pathNode)
          const signature = jsxElementSignature(elementPath)
          const scope = jsxScopeSignature(pathNode)
          const inMap = jsxInsideMap(pathNode)
          const conditional = jsxInsideConditional(pathNode)
          const parentElementPath = jsxParentElementPath(elementPath)
          const parentOid = parentElementPath
            ? oidFor(
                file,
                component,
                `${scope}|${jsxElementSignature(parentElementPath)}|` +
                  `${jsxTagName(parentElementPath.node)}`,
              )
            : null

          // 大写组件是调用点：不注入 props，只把 callSiteOid 写进地图，
          // 供 Props 编辑 / 跨文件定位使用。
          if (/^[A-Z]/.test(tag)) {
            const callSiteOid = `csoid_${hashText(
              `${file}|${scope}|${signature}|${tag}`,
            ).slice(0, 16)}`
            const hasChildren = (elementNode.children || []).some(
              (child) =>
                child.type === 'JSXElement' ||
                child.type === 'JSXFragment' ||
                (child.type === 'JSXText' && child.value.trim()) ||
                child.type === 'JSXExpressionContainer',
            )
            recordCallSite({
              oid: callSiteOid,
              file,
              start: Number.isFinite(elementNode.start) ? elementNode.start : 0,
              end: Number.isFinite(elementNode.end) ? elementNode.end : 0,
              line,
              column,
              component: tag,
              parentOid,
              siblingIndex: chain.length ? chain[chain.length - 1].index : 0,
              siblingCount: chain.length ? chain[chain.length - 1].count : 1,
              scope,
              inMap,
              conditional,
              hasChildren,
              props: jsxStaticProps(elementNode),
            })
            return
          }

          const names = jsxAttributeNames(elementNode)
          const existingOid = jsxStringAttribute(elementNode, 'data-oid')
          const oid =
            existingOid ||
            oidFor(file, component, `${scope}|${signature}|${tag}`)

          recordSourceMapNode({
            oid,
            file,
            start: Number.isFinite(elementNode.start) ? elementNode.start : 0,
            end: Number.isFinite(elementNode.end) ? elementNode.end : 0,
            line,
            column,
            component,
            tag,
            isContainer: CONTAINER_TAGS.has(String(tag).toLowerCase()),
            parentOid,
            siblingIndex: chain.length ? chain[chain.length - 1].index : 0,
            siblingCount: chain.length ? chain[chain.length - 1].count : 1,
            depth: Math.max(0, chain.length - 1),
            inMap,
          })
          if (chain.length === 1) {
            const meta = sourceMapFileMeta.get(file) || {
              file,
              imports: [],
              namedExports: [],
              defaultExportName: '',
              components: {},
            }
            meta.components = meta.components || {}
            if (!meta.components[component]) {
              meta.components[component] = {
                definitionOid: oid,
                file,
                start: elementNode.start,
                end: elementNode.end,
                line,
              }
            }
            sourceMapFileMeta.set(file, meta)
          }

          const injection = []
          if (!existingOid) {
            injection.push(`data-oid="${escapeAttribute(oid)}"`)
          }
          // 已有 data-source-file 时只补 oid，不重复写整套出处属性。
          if (!names.has('data-source-file')) {
            const kindAttribute = (pathNode.node.attributes || []).find(
              (item) =>
                item.type === 'JSXAttribute' &&
                item.name &&
                item.name.name === 'data-source-kind',
            )
            const existingKind =
              kindAttribute && kindAttribute.value && kindAttribute.value.value
            injection.push(
              ...sourceAttributes({
                file,
                line,
                column,
                component,
                tag,
                hasSource:
                  names.has('data-source') || names.has('data-vibe-target'),
                hasKind: names.has('data-source-kind'),
                existingKind,
              }),
            )
          }
          for (const attribute of injection) {
            const separator = attribute.indexOf('=')
            if (separator < 0) continue
            const attrName = attribute.slice(0, separator)
            const rawValue = attribute.slice(separator + 2, -1)
            pathNode.node.attributes.push(
              types.jsxAttribute(
                types.jsxIdentifier(attrName),
                types.stringLiteral(rawValue),
              ),
            )
          }
        },
      },
    }
  }
}

function transformReact(code, filename) {
  const file = relativeFile(filename)
  forgetFile(file)
  const result = transformSync(code, {
    filename,
    babelrc: false,
    configFile: false,
    sourceMaps: true,
    comments: true,
    // 保持行号稳定，避免注入后 dev 报错行号大幅漂移。
    retainLines: true,
    parserOpts: {
      sourceType: 'unambiguous',
      plugins: [
        'jsx',
        'typescript',
        'decorators-legacy',
        'classProperties',
        'dynamicImport',
      ],
    },
    plugins: [buildJsxPlugin(filename)],
  })
  if (!result || !result.code) {
    scheduleSourceMapWrite()
    return null
  }
  const current = sourceMapFiles.get(file) || { file, hash: '', oids: new Set() }
  current.hash = hashText(code)
  sourceMapFiles.set(file, current)
  scheduleSourceMapWrite()
  return { code: result.code, map: result.map || null }
}

// ------------------------------------------------------------
// Vue SFC
// ------------------------------------------------------------
function hasVueAttribute(node, name) {
  return (node.props || []).some(
    (prop) => prop.type === 6 && prop.name === name,
  )
}

function vueAttributeValue(node, name) {
  const prop = (node.props || []).find(
    (item) => item.type === 6 && item.name === name,
  )
  return prop && prop.value && typeof prop.value.content === 'string'
    ? prop.value.content
    : ''
}

/** 递归收集模板元素，并携带「元素在父级中的 JSX/模板结构路径」。 */
function collectVueElements(node, ancestors, parentChildren, output) {
  if (!node) return
  if (node.type === 1 && node.tag) {
    const siblings = (parentChildren || []).filter(
      (child) => child && child.type === 1 && child.tag,
    )
    const chain = [
      ...ancestors,
      {
        tag: String(node.tag),
        index: Math.max(0, siblings.indexOf(node)),
        count: Math.max(1, siblings.length),
      },
    ]
    output.push({ node, chain })
    for (const child of node.children || []) {
      collectVueElements(child, chain, node.children, output)
    }
    return
  }
  for (const child of node.children || []) {
    collectVueElements(child, ancestors, node.children, output)
  }
}

function transformVue(code, filename) {
  const file = relativeFile(filename)
  forgetFile(file)
  let descriptor
  try {
    descriptor = parseSfc(code, { filename }).descriptor
  } catch {
    return null
  }
  const template = descriptor.template
  if (!template || !template.content) return null
  if (template.lang && template.lang !== 'html') return null

  const content = template.content
  const baseOffset = template.loc.start.offset
  const component = fileComponent(filename)
  const insertions = []

  let ast
  try {
    ast = parseTemplate(content)
  } catch {
    return null
  }

  const elements = []
  collectVueElements(ast, [], ast.children, elements)

  for (const { node, chain } of elements) {
    const tag = String(node.tag)
    if (SKIP_TAGS.has(tag) || /^[A-Z]/.test(tag)) continue
    if (!node.loc || !node.loc.start) continue

    const existingOid = vueAttributeValue(node, 'data-oid')
    const parentChain = chain.length >= 2 ? chain.slice(0, -1) : []
    const parent = parentChain.length
      ? parentChain[parentChain.length - 1]
      : null
    const parentOid = parent
      ? oidFor(
          file,
          component,
          `${signatureOf(parentChain)}|${parent.tag}`,
        )
      : null
    const oid =
      existingOid ||
      oidFor(file, component, `${signatureOf(chain)}|${tag}`)

    const start = baseOffset + node.loc.start.offset
    const end = baseOffset + (node.loc.end?.offset ?? node.loc.start.offset)
    const { line, column } = lineColumnAt(code, start)
    recordSourceMapNode({
      oid,
      file,
      start,
      end,
      line,
      column,
      component,
      tag,
      isContainer: CONTAINER_TAGS.has(tag.toLowerCase()),
      parentOid,
      siblingIndex: chain.length ? chain[chain.length - 1].index : 0,
      siblingCount: chain.length ? chain[chain.length - 1].count : 1,
      depth: Math.max(0, chain.length - 1),
    })

    const targetOffset = node.loc.start.offset
    const header = content.slice(targetOffset)
    const match = /^<[^\s/>]+/.exec(header)
    if (!match) continue
    const insertAt = targetOffset + match[0].length

    const attributes = []
    if (!existingOid) attributes.push(`data-oid="${escapeAttribute(oid)}"`)
    if (!hasVueAttribute(node, 'data-source-file')) {
      attributes.push(
        ...sourceAttributes({
          file,
          line,
          column,
          component,
          tag,
          hasSource:
            hasVueAttribute(node, 'data-source') ||
            hasVueAttribute(node, 'data-vibe-target'),
          hasKind: hasVueAttribute(node, 'data-source-kind'),
          existingKind: null,
        }),
      )
    }
    if (attributes.length) {
      insertions.push({
        offset: baseOffset + insertAt,
        text: ` ${attributes.join(' ')}`,
      })
    }
  }

  const current = sourceMapFiles.get(file) || { file, hash: '', oids: new Set() }
  current.hash = hashText(code)
  sourceMapFiles.set(file, current)
  scheduleSourceMapWrite()
  if (insertions.length === 0) return null
  insertions.sort((a, b) => b.offset - a.offset)
  let output = code
  for (const item of insertions) {
    output = output.slice(0, item.offset) + item.text + output.slice(item.offset)
  }
  return { code: output, map: null }
}

function shouldSkip(filename) {
  const normalized = filename.replace(/\\/g, '/')
  if (!normalized) return true
  if (normalized.includes('/node_modules/')) return true
  if (normalized.includes('/.vite/')) return true
  if (normalized.startsWith('virtual:')) return true
  if (normalized.includes('?vue&type=')) return true
  if (normalized.includes('/dist/')) return true
  return false
}

export function sourceInspectPlugin() {
  return {
    name: 'vibe-source-inspect',
    enforce: 'pre',
    configResolved(config) {
      projectRoot = config.root || process.cwd()
    },
    buildStart() {
      sourceMapNodes.clear()
      sourceMapFiles.clear()
      sourceMapJson = ''
    },
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        const pathname = String(request.url || '').split('?')[0]
        if (pathname === `/${SOURCE_MAP_FILE}` || pathname === '/__vibe_source_map.json') {
          if (!sourceMapJson) writeSourceMap()
          response.setHeader('Content-Type', 'application/json; charset=utf-8')
          response.setHeader('Cache-Control', 'no-store')
          response.end(sourceMapJson || buildSourceMap())
          return
        }
        if (pathname !== SDK_PATH) {
          next()
          return
        }
        response.setHeader('Content-Type', 'application/javascript; charset=utf-8')
        response.setHeader('Cache-Control', 'no-store')
        response.end(readSdk())
      })
    },
    watchChange(id, change) {
      if (change?.event !== 'delete') return
      const cleanId = String(id || '').split('?')[0]
      if (!cleanId) return
      forgetFile(relativeFile(cleanId))
      scheduleSourceMapWrite()
    },
    generateBundle() {
      sourceMapJson = buildSourceMap()
      this.emitFile({
        type: 'asset',
        fileName: SOURCE_MAP_FILE,
        source: sourceMapJson,
      })
    },
    writeBundle(options) {
      const outDir = options?.dir || options?.file
      if (!outDir) return
      try {
        const target = options.dir
          ? path.join(options.dir, SOURCE_MAP_FILE)
          : path.join(path.dirname(outDir), SOURCE_MAP_FILE)
        fs.writeFileSync(target, sourceMapJson || buildSourceMap())
      } catch {
        // 产物目录只读时忽略；generateBundle 仍会通过 emitFile 输出。
      }
    },
    transformIndexHtml(html) {
      return {
        html,
        tags: [
          {
            tag: 'script',
            attrs: { src: SDK_PATH },
            injectTo: 'body',
          },
        ],
      }
    },
    transform(code, id) {
      const cleanId = String(id || '').split('?')[0]
      if (shouldSkip(cleanId)) return null
      const extension = path.extname(cleanId).toLowerCase()
      if (extension === '.vue') {
        const result = transformVue(code, cleanId)
        return result || null
      }
      if (!SCRIPT_EXTENSIONS.has(extension)) return null
      const base = path.basename(cleanId)
      // 构建配置文件不参与源码标记，避免 Babel 处理 ESM 配置时引入噪音。
      if (/^vite\.config\.|^postcss\.config\.|^tailwind\.config\./.test(base)) {
        return null
      }
      if (!/[<]/.test(code)) {
        // 文件从 JSX 改成纯 JS / 被清空时，必须移除旧 oid，避免地图过期。
        forgetFile(relativeFile(cleanId))
        scheduleSourceMapWrite()
        return null
      }
      try {
        return transformReact(code, cleanId)
      } catch {
        // 解析失败时交给后续插件 / Vite 原生错误处理，绝不阻断启动；
        // 文件节点已被 forget，地图也要同步刷新，避免残留旧 oid。
        scheduleSourceMapWrite()
        return null
      }
    },
  }
}
