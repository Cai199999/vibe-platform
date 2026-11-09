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

let projectRoot = process.cwd()
let sdkCache = ''

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

function buildJsxPlugin(filename) {
  return function jsxSourcePlugin({ types }) {
    return {
      name: 'vibe-inspect-jsx-source',
      visitor: {
        JSXOpeningElement(pathNode) {
          const nameNode = pathNode.node.name
          if (!nameNode || nameNode.type !== 'JSXIdentifier') return
          const tag = nameNode.name
          // 组件（大写开头）的属性可能不会落到 DOM，只标记原生标签。
          if (!tag || /^[A-Z]/.test(tag)) return
          const attributes = pathNode.node.attributes || []
          const names = new Set(
            attributes
              .filter((item) => item.type === 'JSXAttribute')
              .map((item) => item.name && item.name.name)
              .filter(Boolean),
          )
          if (names.has('data-source-file')) return

          const loc = pathNode.node.loc && pathNode.node.loc.start
          if (!loc) return
          const file = relativeFile(filename)
          const line = loc.line
          const column = loc.column + 1
          const component = componentNameFor(pathNode, filename)
          const kindAttribute = attributes.find(
            (item) =>
              item.type === 'JSXAttribute' &&
              item.name &&
              item.name.name === 'data-source-kind',
          )
          const existingKind =
            kindAttribute && kindAttribute.value && kindAttribute.value.value

          const injection = sourceAttributes({
            file,
            line,
            column,
            component,
            tag,
            hasSource:
              names.has('data-source') || names.has('data-vibe-target'),
            hasKind: names.has('data-source-kind'),
            existingKind,
          })
          for (const attribute of injection) {
            const separator = attribute.indexOf('=')
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
  if (!result || !result.code) return null
  return { code: result.code, map: result.map || null }
}

// ------------------------------------------------------------
// Vue SFC
// ------------------------------------------------------------
function walkTemplate(node, visit) {
  if (!node) return
  visit(node)
  if (Array.isArray(node.children)) {
    for (const child of node.children) walkTemplate(child, visit)
  }
}

function hasVueAttribute(node, name) {
  return (node.props || []).some(
    (prop) => prop.type === 6 && prop.name === name,
  )
}

function transformVue(code, filename) {
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
  const file = relativeFile(filename)
  const component = fileComponent(filename)
  const insertions = []

  let ast
  try {
    ast = parseTemplate(content)
  } catch {
    return null
  }

  walkTemplate(ast, (node) => {
    if (node.type !== 1 || !node.tag) return
    const tag = String(node.tag)
    if (SKIP_TAGS.has(tag) || /^[A-Z]/.test(tag)) return
    if (!node.loc || !node.loc.start) return
    if (hasVueAttribute(node, 'data-source-file')) return

    const targetOffset = node.loc.start.offset
    const header = content.slice(targetOffset)
    const match = /^<[^\s/>]+/.exec(header)
    if (!match) return
    const insertAt = targetOffset + match[0].length
    const absoluteOffset = baseOffset + insertAt
    const { line, column } = lineColumnAt(code, absoluteOffset)

    const hasSource =
      hasVueAttribute(node, 'data-source') ||
      hasVueAttribute(node, 'data-vibe-target')
    const hasKind = hasVueAttribute(node, 'data-source-kind')
    const attributes = sourceAttributes({
      file,
      line,
      column,
      component,
      tag,
      hasSource,
      hasKind,
      existingKind: null,
    })
    insertions.push({
      offset: baseOffset + insertAt,
      text: ` ${attributes.join(' ')}`,
    })
  })

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
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        const pathname = String(request.url || '').split('?')[0]
        if (pathname !== SDK_PATH) {
          next()
          return
        }
        response.setHeader('Content-Type', 'application/javascript; charset=utf-8')
        response.setHeader('Cache-Control', 'no-store')
        response.end(readSdk())
      })
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
      if (!/[<]/.test(code)) return null
      try {
        return transformReact(code, cleanId)
      } catch {
        // 解析失败时交给后续插件 / Vite 原生错误处理，绝不阻断启动。
        return null
      }
    },
  }
}
