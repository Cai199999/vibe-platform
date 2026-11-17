/**
 * 项目模式判定（双轨制前置分流）。
 *
 * 规则必须是确定性的，绝不能交给 Architect LLM 猜测：
 *   - EXISTING_SOURCE：导入项目 / 已有 sandbox / 已生成源码 / 非内置模板源码；
 *   - NEW_PROJECT：空白新建项目 / 仅内置演示模板。
 *
 * 该模块是纯函数层，不依赖 React / Zustand / 后端。
 */

export const PROJECT_MODE = {
  NEW_PROJECT: 'NEW_PROJECT',
  EXISTING_SOURCE: 'EXISTING_SOURCE',
}

export const PROJECT_MODE_LABELS = {
  [PROJECT_MODE.NEW_PROJECT]: '从零开始',
  [PROJECT_MODE.EXISTING_SOURCE]: '存量源码',
}

export const WORKSPACE_STORAGE_KEY = 'vibe.workspace.v1'
const BUILTIN_TEMPLATE = 'vibe-react'

function hasMeaningfulFiles(files) {
  return Object.values(files || {}).some(
    (value) => typeof value === 'string' && value.trim().length > 0,
  )
}

function hasSandbox(project, sandboxId) {
  return Boolean(
    sandboxId ||
      project?.result?.sandboxId ||
      (Array.isArray(project?.sandboxIds) && project.sandboxIds.length > 0),
  )
}

/**
 * 解析项目模式。
 *
 * @param {object|null} project 工作区项目
 * @param {{sandboxId?: string, files?: Record<string,string>}} [context]
 */
export function resolveProjectMode(project, { sandboxId = '', files = null } = {}) {
  if (!project) return PROJECT_MODE.NEW_PROJECT
  const sourceFiles = files ?? project.files ?? {}
  const imported =
    project.imported === true ||
    String(project.template || '').startsWith('imported-')
  const generated = project.generated === true
  if (imported || generated || hasSandbox(project, sandboxId)) {
    return PROJECT_MODE.EXISTING_SOURCE
  }
  // 内置演示模板不是用户源码：仍按新建项目处理。
  const builtinOnly =
    project.template === BUILTIN_TEMPLATE && !generated && !imported
  if (!builtinOnly && hasMeaningfulFiles(sourceFiles)) {
    return PROJECT_MODE.EXISTING_SOURCE
  }
  return PROJECT_MODE.NEW_PROJECT
}

export function isExistingSource(project, context) {
  return resolveProjectMode(project, context) === PROJECT_MODE.EXISTING_SOURCE
}

export function projectSourceFiles(project) {
  const files = project?.files || {}
  return Object.fromEntries(
    Object.entries(files).filter(
      ([, value]) => typeof value === 'string' && value.trim().length > 0,
    ),
  )
}

export function projectEntrySource(project) {
  const files = projectSourceFiles(project)
  const entry = project?.entryFile || 'src/App.jsx'
  if (files[entry]) return { fileName: entry, source: files[entry] }
  const first = Object.entries(files)[0]
  return first ? { fileName: first[0], source: first[1] } : null
}

/** 读取 localStorage 中持久化的工作区项目（跨页面只读入口）。 */
export function readStoredProject(projectId) {
  if (!projectId || typeof window === 'undefined') return null
  try {
    const raw = window.localStorage.getItem(WORKSPACE_STORAGE_KEY)
    if (!raw) return null
    const state = JSON.parse(raw)
    return (
      state?.projects?.find((project) => project?.id === projectId) || null
    )
  } catch {
    return null
  }
}

export function importedSourceKey(projectId) {
  return `vibe.source.import.${projectId}`
}

export function storeImportedSource(projectId, fileName, source) {
  if (!projectId || typeof window === 'undefined') return
  try {
    window.sessionStorage.setItem(
      importedSourceKey(projectId),
      JSON.stringify({ fileName, source }),
    )
  } catch {
    // 隐私模式无法写 sessionStorage 时，项目 files 仍可作为兜底。
  }
}

export function readImportedSource(projectId) {
  if (!projectId || typeof window === 'undefined') return null
  try {
    const raw = window.sessionStorage.getItem(importedSourceKey(projectId))
    if (!raw) return null
    const parsed = JSON.parse(raw)
    if (!parsed?.source) return null
    return {
      fileName: String(parsed.fileName || 'App.jsx'),
      source: String(parsed.source),
    }
  } catch {
    return null
  }
}
