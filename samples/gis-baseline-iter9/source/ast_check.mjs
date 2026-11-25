#!/usr/bin/env node
/**
 * 沙盒写盘前的静态门禁。
 *
 * 背景：Coder 生成的 JSX 曾出现 `<Plus />` 却没有 `import { Plus }`。
 * esbuild / Vite 只做语法转换，不会报错，浏览器运行到该节点时才抛
 * ReferenceError，整个 React 组件树被卸载 —— 表现为整页白屏。
 *
 * 本脚本做两件事：
 *   1. 用 @babel/parser 解析所有 JS/JSX/TS/TSX 文件，语法错误直接失败；
 *   2. 用 @babel/traverse 做 JSX 标识符作用域检查，未绑定的首字母大写
 *      组件（例如漏 import 的 `<Plus />`）直接失败。
 *
 * 依赖从 /app/package.json 解析，因此脚本本身可以放在 /opt，被校验的
 * 文件可以在任意临时目录。
 *
 * 用法：
 *   node /opt/vibe-ast-check.mjs <file...>
 *   node /opt/vibe-ast-check.mjs --root /tmp/stage
 * 退出码：0 = 通过；1 = 发现错误；2 = 脚本自身异常。
 */
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'

/**
 * 依赖解析顺序：
 *   1. /opt/vibe-ast/node_modules —— 镜像自带，导入项目删改 package.json 也不受影响；
 *   2. /app/node_modules        —— 兼容升级前创建的旧容器。
 * 两者都不可用时退出码 2，让调用方把“检查器缺失”和“代码不合法”区分开。
 */
function loadBabel() {
  const candidates = ['/opt/vibe-ast/index.js', '/app/package.json']
  let lastError = null
  for (const anchor of candidates) {
    try {
      const requireFrom = createRequire(anchor)
      const parser = requireFrom('@babel/parser')
      const traverseModule = requireFrom('@babel/traverse')
      return { parser, traverse: traverseModule.default || traverseModule }
    } catch (error) {
      lastError = error
    }
  }
  console.error(`AST 检查依赖缺失：${lastError?.message || lastError}`)
  process.exit(2)
}

const { parser, traverse } = loadBabel()

const SCRIPT_EXTENSIONS = new Set(['.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx'])
const IGNORED_DIRS = new Set(['node_modules', 'dist', '.git', '.vite', 'coverage'])

/** 收集目录下所有需要检查的脚本文件。 */
function collectFiles(root) {
  const result = []
  const walk = (dir) => {
    let entries
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true })
    } catch {
      return
    }
    for (const entry of entries) {
      const full = path.join(dir, entry.name)
      if (entry.isDirectory()) {
        if (!IGNORED_DIRS.has(entry.name)) walk(full)
      } else if (entry.isFile() && SCRIPT_EXTENSIONS.has(path.extname(entry.name))) {
        result.push(full)
      }
    }
  }
  walk(root)
  return result
}

function pluginsFor(file) {
  const isTs = file.endsWith('.ts') || file.endsWith('.tsx')
  const plugins = [
    'jsx',
    'classProperties',
    'classPrivateProperties',
    'classPrivateMethods',
    'dynamicImport',
    'importMeta',
    'topLevelAwait',
  ]
  if (isTs) plugins.push('typescript')
  return plugins
}

/**
 * 判断 JSX 标签是否按“组件”编译。
 *
 * Babel 的规则：首字母小写（含 `my-tag`）按原生标签输出字符串，其余
 * 全部按变量引用处理，运行时取不到就会抛 ReferenceError。
 */
function isComponentTag(name) {
  return typeof name === 'string' && name.length > 0 && !/^[a-z]/.test(name)
}

/** 取 `<A.B.C />` 的根对象 `A`。 */
function memberRoot(name) {
  let current = name
  while (current && current.type === 'JSXMemberExpression') {
    current = current.object
  }
  return current
}

/**
 * JSX 组件标识符必须处于作用域中。
 *
 * `<div />` 是原生标签，不需要绑定；`<Plus />` 编译成 `Plus` 变量引用，
 * 漏 import 时 esbuild 不报错、浏览器运行时白屏，正是本检查要拦截的场景。
 * `<Icons.Plus />` 则检查根对象 `Icons`。
 */
function checkJsxBindings(ast, file) {
  const problems = []
  const seen = new Set()
  const checkName = (nameNode, nodePath) => {
    const root = memberRoot(nameNode)
    if (!root || root.type !== 'JSXIdentifier') return
    const name = root.name
    if (!isComponentTag(name)) return
    const loc = root.loc?.start || { line: 1, column: 0 }
    const key = `${name}:${loc.line}:${loc.column}`
    if (seen.has(key)) return
    seen.add(key)
    if (nodePath.scope.hasBinding(name, true)) return
    problems.push({
      file,
      line: loc.line,
      column: loc.column + 1,
      message: `未绑定的 JSX 组件 <${name}>：缺少 import 或声明，运行时会白屏`,
    })
  }
  traverse(ast, {
    JSXOpeningElement(nodePath) {
      checkName(nodePath.node.name, nodePath)
    },
    JSXClosingElement(nodePath) {
      // 闭合标签与开始标签同一作用域；seen 去重避免重复报告。
      checkName(nodePath.node.name, nodePath)
    },
  })
  return problems
}

function checkFile(file) {
  const source = fs.readFileSync(file, 'utf8')
  let ast
  try {
    ast = parser.parse(source, {
      sourceType: 'unambiguous',
      errorRecovery: false,
      allowReturnOutsideFunction: true,
      plugins: pluginsFor(file),
    })
  } catch (error) {
    const loc = error.loc || { line: 1, column: 0 }
    return [
      {
        file,
        line: loc.line,
        column: loc.column + 1,
        message: `语法错误：${error.message}`,
      },
    ]
  }
  return checkJsxBindings(ast, file)
}

function main(argv) {
  const args = argv.slice(2)
  let root = null
  const files = []
  for (let i = 0; i < args.length; i += 1) {
    if (args[i] === '--root') {
      root = args[++i]
    } else {
      files.push(args[i])
    }
  }

  let targets = files
  if (root) {
    targets = targets.concat(collectFiles(root))
  }
  targets = [...new Set(targets.map((file) => path.resolve(file)))].filter((file) =>
    fs.existsSync(file),
  )

  if (targets.length === 0) {
    // 没有需要检查的脚本（例如补丁只改了 JSON/CSS）时视为通过。
    return 0
  }

  const problems = []
  for (const file of targets) {
    try {
      problems.push(...checkFile(file))
    } catch (error) {
      problems.push({
        file,
        line: 1,
        column: 1,
        message: `静态检查异常：${error.message}`,
      })
    }
  }

  if (problems.length > 0) {
    for (const problem of problems) {
      console.error(
        `${problem.file}:${problem.line}:${problem.column} ${problem.message}`,
      )
    }
    console.error(`AST 检查失败：${problems.length} 个问题，已拦截写入。`)
    return 1
  }

  console.log(`AST 检查通过：${targets.length} 个文件`)
  return 0
}

try {
  process.exitCode = main(process.argv)
} catch (error) {
  console.error(`AST 检查脚本异常：${error?.stack || error}`)
  process.exitCode = 2
}
