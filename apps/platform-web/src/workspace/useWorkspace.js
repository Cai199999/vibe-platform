import { useCallback, useEffect, useMemo, useState } from 'react'
import { INITIAL_APP } from '../mockProject.js'

const STORAGE_KEY = 'vibe.workspace.v1'
const STORAGE_VERSION = 1
// 内置模板版本：旧 localStorage 里的项目文件可能还带着旧版卡片样式
// （content-box / 默认 p 外边距），会导致画布与沙盒预览的卡片尺寸不一致。
// 升级此版本号即可在下次加载时把 vibe-react 项目迁回最新模板。
const TEMPLATE_VERSION = 3
const TARGET_FILE = 'src/App.jsx'

/**
 * 工作区最多同时打开 5 个项目，前端只是第一道防线。
 * 真实上限还需要后端按租户做配额校验（M2）。
 */
export const MAX_OPEN_TABS = 5

const newId = (prefix) => {
  const random =
    typeof crypto !== 'undefined' && crypto.randomUUID
      ? crypto.randomUUID().replace(/-/g, '').slice(0, 12)
      : Math.random().toString(36).slice(2, 14)
  return `${prefix}_${random}`
}

const templateFiles = () => ({ [TARGET_FILE]: INITIAL_APP })

/**
 * 创建项目。Phase 2 只支持平台内置 React 模板；
 * Phase 3 导入项目会复用同一结构，替换 template / files。
 */
const createProject = (name) => ({
  id: newId('proj'),
  name,
  template: 'vibe-react',
  templateVersion: TEMPLATE_VERSION,
  files: templateFiles(),
  createdAt: new Date().toISOString(),
})

const createInitialState = () => {
  const project = createProject('演示项目')
  return {
    version: STORAGE_VERSION,
    projects: [project],
    openTabs: [project.id],
    activeTabId: project.id,
  }
}

/**
 * 分配项目名称：以「当前存活 Tab 数 + 1」为起点递增。
 * 例如初始只有「演示项目」时，新项目为 2；存活 4 个 Tab 时从 5 起编号。
 * 若该编号已被历史项目占用（中间关闭过项目），继续顺延，保证绝不重名。
 * 编号从 2 开始，1 号语义上留给初始「演示项目」。
 */
function nextProjectName(projects, openTabCount) {
  const usedNames = new Set(projects.map((project) => project.name))
  let index = Math.max(2, openTabCount + 1)
  while (usedNames.has(`未命名项目 ${index}`)) {
    index += 1
  }
  return `未命名项目 ${index}`
}

function normalizeState(raw) {
  if (!raw || !Array.isArray(raw.projects) || raw.projects.length === 0) {
    return createInitialState()
  }
  const projects = raw.projects
    .filter(
      (project) =>
        project?.id &&
        project?.name &&
        // 内置项目必须有模板入口；导入项目只要记录 entryFile 即可，
        // 完整源码在沙盒里，浏览器只保留有体积上限的文本快照。
        (project?.files?.[TARGET_FILE] || project?.entryFile),
    )
    .map((project) => {
      // 内置模板升级：旧项目文件统一替换为当前 INITIAL_APP，避免旧样式造成
      // 画布 Mock 与沙盒真实页面尺寸不一致。导入的外部项目后续走独立迁移。
      if (
        project.template === 'vibe-react' &&
        project.templateVersion !== TEMPLATE_VERSION
      ) {
        return {
          ...project,
          files: templateFiles(),
          templateVersion: TEMPLATE_VERSION,
        }
      }
      return project
    })
  if (projects.length === 0) {
    return createInitialState()
  }
  const projectIds = new Set(projects.map((project) => project.id))
  const openTabs = Array.isArray(raw.openTabs)
    ? raw.openTabs.filter((id) => projectIds.has(id)).slice(0, MAX_OPEN_TABS)
    : []
  const activeTabId =
    raw.activeTabId && openTabs.includes(raw.activeTabId)
      ? raw.activeTabId
      : (openTabs[0] ?? null)
  return {
    version: STORAGE_VERSION,
    projects,
    openTabs,
    activeTabId,
  }
}

function loadState() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return createInitialState()
    return normalizeState(JSON.parse(raw))
  } catch {
    return createInitialState()
  }
}

/**
 * 多项目工作区状态：
 *  - projects   : 所有项目（关闭 Tab 不删除项目）
 *  - openTabs   : 当前打开的 Tab，顺序即展示顺序
 *  - activeTabId: 当前激活项目
 *
 * 仅持久化工作区结构；沙盒预览与 Run 结果属于会话态，刷新后重置。
 */
export function useWorkspace() {
  const [state, setState] = useState(loadState)

  useEffect(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
    } catch {
      // 存储配额不足 / 隐私模式：工作区仍可在内存中正常使用。
    }
  }, [state])

  const createNewProject = useCallback(() => {
    setState((previous) => {
      if (previous.openTabs.length >= MAX_OPEN_TABS) {
        return previous
      }
      const created = createProject(
        nextProjectName(previous.projects, previous.openTabs.length),
      )
      return {
        ...previous,
        projects: [...previous.projects, created],
        openTabs: [...previous.openTabs, created.id],
        activeTabId: created.id,
      }
    })
  }, [])

  /**
   * 导入项目：沙盒已经由 Python 启动，前端只负责建立项目 / Tab。
   *
   * @param {object} payload
   * @param {string} [payload.id]        预生成的 projectId（与沙盒标签一致）
   * @param {string} payload.name        项目名（通常是上传文件名）
   * @param {object} payload.files       文本源码快照
   * @param {string} payload.entryFile   入口文件（src/App.jsx 或 index.html）
   * @param {string} payload.kind        react / html
   * @param {string} [payload.sourceName] 原始文件名
   * @returns {string} projectId
   */
  const importProject = useCallback((payload) => {
    const projectId = payload?.id || newId('proj')
    setState((previous) => {
      if (previous.openTabs.length >= MAX_OPEN_TABS) {
        return previous
      }
      const project = {
        id: projectId,
        name: payload?.name || '导入项目',
        template: payload?.kind === 'html' ? 'imported-html' : 'imported-react',
        kind: payload?.kind === 'html' ? 'html' : 'react',
        entryFile: payload?.entryFile || TARGET_FILE,
        files: payload?.files || {},
        sourceName: payload?.sourceName || '',
        imported: true,
        createdAt: new Date().toISOString(),
      }
      return {
        ...previous,
        projects: [...previous.projects, project],
        openTabs: [...previous.openTabs, projectId],
        activeTabId: projectId,
      }
    })
    return projectId
  }, [])

  const openProject = useCallback((projectId) => {
    setState((previous) => {
      if (!previous.projects.some((project) => project.id === projectId)) {
        return previous
      }
      if (
        !previous.openTabs.includes(projectId) &&
        previous.openTabs.length >= MAX_OPEN_TABS
      ) {
        return previous
      }
      const openTabs = previous.openTabs.includes(projectId)
        ? previous.openTabs
        : [...previous.openTabs, projectId]
      return { ...previous, openTabs, activeTabId: projectId }
    })
  }, [])

  /**
   * 关闭项目：Tab、项目本身与上下文一并移除。
   * 与旧行为（仅关闭 Tab、保留下拉栏入口）不同，关闭即彻底销毁项目；
   * 沙盒容器的清理由 WorkspacePage 在调用前触发。
   */
  const closeTab = useCallback((projectId) => {
    setState((previous) => {
      const index = previous.openTabs.indexOf(projectId)
      if (index < 0) return previous
      const openTabs = previous.openTabs.filter((id) => id !== projectId)
      let activeTabId = previous.activeTabId
      if (activeTabId === projectId) {
        activeTabId = openTabs[Math.min(index, openTabs.length - 1)] ?? null
      }
      return {
        ...previous,
        projects: previous.projects.filter((project) => project.id !== projectId),
        openTabs,
        activeTabId,
      }
    })
  }, [])

  const setActiveTab = useCallback((projectId) => {
    setState((previous) =>
      previous.openTabs.includes(projectId)
        ? { ...previous, activeTabId: projectId }
        : previous,
    )
  }, [])

  const reorderTabs = useCallback((fromIndex, toIndex) => {
    setState((previous) => {
      if (
        fromIndex === toIndex ||
        fromIndex < 0 ||
        toIndex < 0 ||
        fromIndex >= previous.openTabs.length ||
        toIndex >= previous.openTabs.length
      ) {
        return previous
      }
      const openTabs = [...previous.openTabs]
      const [moved] = openTabs.splice(fromIndex, 1)
      openTabs.splice(toIndex, 0, moved)
      return { ...previous, openTabs }
    })
  }, [])

  const activeProject = useMemo(
    () => state.projects.find((project) => project.id === state.activeTabId) ?? null,
    [state.projects, state.activeTabId],
  )

  return {
    projects: state.projects,
    openTabs: state.openTabs,
    activeTabId: state.activeTabId,
    activeProject,
    maxTabs: MAX_OPEN_TABS,
    limitReached: state.openTabs.length >= MAX_OPEN_TABS,
    createProject: createNewProject,
    importProject,
    openProject,
    closeTab,
    setActiveTab,
    reorderTabs,
  }
}
