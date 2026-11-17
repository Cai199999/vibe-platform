import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
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
 * 同步落盘工作区状态。
 *
 * 导入项目后页面会立刻 navigate 到只读投影；如果只依赖 useEffect 落盘，
 * WorkspacePage 可能先卸载，导致新项目丢失。导入流程必须同步写入。
 */
function persistWorkspaceState(state) {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
  } catch {
    // 存储配额不足 / 隐私模式：内存状态仍可用。
  }
}

/**
 * 创建项目。Phase 2 只支持平台内置 React 模板；
 * Phase 3 导入项目会复用同一结构，替换 template / files。
 */
const createProject = (name, { blank = false } = {}) => ({
  id: newId('proj'),
  name,
  template: 'vibe-react',
  templateVersion: TEMPLATE_VERSION,
  // 新建项目不再预置 Vibe Studio Mock 页面：
  //  - files 为空，等待 Architect 契约确认后由 FrontendCoder 写入真实骨架；
  //  - bootstrap=pending 让画布显示【✨ 开始创建】；
  //  - canvasMode 在生成成功后切换为 live。
  files: blank ? {} : templateFiles(),
  entryFile: TARGET_FILE,
  bootstrap: blank ? 'pending' : 'ready',
  canvasMode: blank ? 'blank' : 'mock',
  generated: false,
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
        // 新建空白项目允许 files 为空，等待前端骨架生成；其他项目必须有
        // 内置模板入口，导入项目只要记录 entryFile 即可。
        (project?.files?.[TARGET_FILE] ||
          project?.entryFile ||
          project?.bootstrap === 'pending'),
    )
    .map((project) => {
      // 旧工作区没有 bootstrap / canvasMode：统一视为已完成模板项目。
      const normalized = {
        ...project,
        bootstrap: project.bootstrap ?? 'ready',
        canvasMode:
          project.canvasMode ??
          (project.bootstrap === 'pending' ? 'blank' : 'mock'),
      }
      // 只有仍在使用内置 Mock 的项目才升级模板；新建空白 / 已生成项目
      // 绝不能把 Vibe Studio 模板重新写回去。
      if (
        normalized.template === 'vibe-react' &&
        normalized.templateVersion !== TEMPLATE_VERSION &&
        !normalized.generated &&
        normalized.bootstrap !== 'pending'
      ) {
        return {
          ...normalized,
          files: templateFiles(),
          templateVersion: TEMPLATE_VERSION,
        }
      }
      return normalized
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
  // 同步操作（导入后立即跳转）需要读取最新工作区状态，绕开 setState 的
  // 异步批处理，确保 localStorage 在 navigate 之前已写入。
  const stateRef = useRef(state)
  stateRef.current = state

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
        { blank: true },
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
   * @param {string} payload.kind        react / html / fullstack
   * @param {string[]} [payload.services] 全栈导入时实际启动的服务
   * @param {number} [payload.dataSourceCount] 重新注入的 data-source 节点数
   * @param {string} [payload.sourceName] 原始文件名
   * @returns {string} projectId
   */
  const importProject = useCallback((payload) => {
    const projectId = payload?.id || newId('proj')
    const previous = stateRef.current
    if (!previous || previous.openTabs.length >= MAX_OPEN_TABS) {
      return projectId
    }
    const kind =
      payload?.kind === 'html'
        ? 'html'
        : payload?.kind === 'fullstack'
          ? 'fullstack'
          : 'react'
    const project = {
      id: projectId,
      name: payload?.name || '导入项目',
      template: kind === 'html' ? 'imported-html' : `imported-${kind}`,
      kind,
      entryFile: payload?.entryFile || TARGET_FILE,
      files: payload?.files || {},
      services: Array.isArray(payload?.services) ? payload.services : [],
      dataSourceCount: payload?.dataSourceCount || 0,
      sourceName: payload?.sourceName || '',
      imported: true,
      bootstrap: 'ready',
      canvasMode: 'mock',
      generated: false,
      createdAt: new Date().toISOString(),
    }
    const next = {
      ...previous,
      projects: [...previous.projects, project],
      openTabs: [...previous.openTabs, projectId],
      activeTabId: projectId,
    }
    // 导入后立即跳转只读投影，必须同步落盘并同步 ref，避免卸载丢项目。
    stateRef.current = next
    persistWorkspaceState(next)
    setState(next)
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

  /**
   * 更新项目元数据（bootstrap / canvasMode / files 等）。
   *
   * FrontendCoder 完成后由 WorkspacePage 调用：写入真实 App.jsx 快照，
   * 并把画布从 blank / bootstrapping 切换为 live。不存在项目时无操作。
   */
  const updateProject = useCallback((projectId, patch) => {
    setState((previous) => {
      const index = previous.projects.findIndex(
        (project) => project.id === projectId,
      )
      if (index < 0) return previous
      const current = previous.projects[index]
      const next =
        typeof patch === 'function' ? patch(current) : { ...current, ...patch }
      if (next === current) return previous
      const projects = [...previous.projects]
      projects[index] = next
      return { ...previous, projects }
    })
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
    updateProject,
    openProject,
    closeTab,
    setActiveTab,
    reorderTabs,
  }
}
