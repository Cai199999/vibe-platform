import { useEffect, useMemo, useRef, useState } from 'react'
import AppHeader from '../components/AppHeader.jsx'
import AccountDialog from '../components/AccountDialog.jsx'
import ProjectSwitcher from '../components/ProjectSwitcher.jsx'
import ProjectWorkspace from '../components/ProjectWorkspace.jsx'
import WorkspaceTabs from '../components/WorkspaceTabs.jsx'
import { useAuth } from '../auth/AuthContext.jsx'
import { useWorkspace } from '../workspace/useWorkspace.js'
import { INITIAL_APP } from '../mockProject.js'

const TARGET_FILE = 'src/App.jsx'

const EMPTY_RUN = {
  annotation: null,
  intent: '',
  submitting: false,
  elapsed: 0,
  startedAt: null,
  result: null,
  error: null,
  // 最近一次指令用于预览失联后一键重新执行。
  lastInstruction: '',
  // 同一项目可能执行多次，每次都会生成新沙盒；关闭项目时需全部销毁。
  sandboxIds: [],
}

// 已完成 Run 的轻量会话快照：刷新页面后仍能显示「查看效果」，
// 且 sandboxIds 不丢失，关闭 Tab 时仍能正确销毁容器。
const RUN_STORAGE_KEY = 'vibe.runs.v1'

function loadRunStates(projects) {
  const projectIds = new Set(projects.map((project) => project.id))
  try {
    const raw = window.sessionStorage.getItem(RUN_STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    const restored = {}
    Object.entries(parsed).forEach(([projectId, saved]) => {
      if (!projectIds.has(projectId) || !saved) return
      const sandboxIds = Array.isArray(saved.sandboxIds) ? saved.sandboxIds : []
      if (saved.result?.sandboxId && !sandboxIds.includes(saved.result.sandboxId)) {
        sandboxIds.push(saved.result.sandboxId)
      }
      restored[projectId] = {
        ...EMPTY_RUN,
        annotation: saved.annotation ?? null,
        result: saved.result ?? null,
        error: saved.error ?? null,
        lastInstruction: saved.lastInstruction ?? '',
        sandboxIds,
      }
    })
    return restored
  } catch {
    return {}
  }
}

/**
 * 通过 BFF 销毁沙盒。BFF 再调用 Python 内部接口
 * DELETE /api/v1/internal/sandbox/{sandbox_id}。
 * 404 视为已销毁（幂等）；失败只记录日志，不阻塞前端关闭 Tab。
 */
async function destroySandbox(sandboxId, token) {
  if (!sandboxId) return
  try {
    const response = await fetch(`/api/v1/sandboxes/${encodeURIComponent(sandboxId)}`, {
      method: 'DELETE',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      keepalive: true,
    })
    if (!response.ok && response.status !== 404) {
      console.warn(`销毁沙盒失败 ${sandboxId}: HTTP ${response.status}`)
    }
  } catch (cause) {
    console.warn(`销毁沙盒失败 ${sandboxId}: ${cause?.message || cause}`)
  }
}

const newId = (prefix) => {
  const random =
    typeof crypto !== 'undefined' && crypto.randomUUID
      ? crypto.randomUUID().replace(/-/g, '').slice(0, 12)
      : Math.random().toString(36).slice(2, 14)
  return `${prefix}_${random}`
}

/**
 * 将画布上的归一化选区组装为 AnnotationEnvelope。
 * worldTarget 保留归一化坐标，后端可换算到任意分辨率。
 */
function buildAnnotation(rect, instruction, source) {
  const now = new Date().toISOString()
  return {
    version: 1,
    annotationId: newId('ann'),
    type: 'drag',
    worldTarget: {
      x: Number(rect.x.toFixed(6)),
      y: Number(rect.y.toFixed(6)),
      width: Number(rect.width.toFixed(6)),
      height: Number(rect.height.toFixed(6)),
      coordinateSpace: 'normalized',
    },
    target: {
      mode: 'source',
      source: {
        file: TARGET_FILE,
        line: 11,
        column: 7,
        component: 'App',
        sourceKind: 'jsx',
      },
      selector: "[data-vibe-target='cta-button']",
      snippet: {
        startLine: 1,
        endLine: source.split('\n').length,
        content: source,
        hash: 'sha256:canvas-demo',
      },
    },
    intent: {
      kind: 'UI_ONLY',
      ui: {
        summary: `将按钮改为红色（用户标注：${instruction}）`,
        acceptance: ['按钮背景色变为红色', '其余布局与文案保持不变'],
        elementRole: 'button',
      },
    },
    payload: {
      text: instruction,
      from: null,
      to: null,
      strokes: [],
    },
    createdAt: now,
  }
}

async function extractError(response) {
  let body = null
  try {
    body = await response.json()
  } catch {
    // 非 JSON 错误体，走兜底。
  }
  const detail = body?.detail
  if (typeof detail === 'string') return detail
  if (detail?.message) return `${detail.code || response.status} · ${detail.message}`
  if (body?.message) return `${body.code || response.status} · ${body.message}`
  return `HTTP ${response.status} ${response.statusText || ''}`.trim()
}

function EmptyWorkspace({ onNewProject }) {
  return (
    <div className="workspace-empty">
      <div className="empty-mark" aria-hidden="true">
        ▦
      </div>
      <h2>工作区里还没有打开的窗口</h2>
      <p>新建一个项目，或从左上角项目下拉栏重新打开已有项目。</p>
      <button className="primary-button" type="button" onClick={onNewProject}>
        ＋ 新建项目
      </button>
    </div>
  )
}

export default function WorkspacePage() {
  const { user, session } = useAuth()
  const {
    projects,
    openTabs,
    activeTabId,
    activeProject,
    maxTabs,
    limitReached,
    createProject,
    openProject,
    closeTab,
    setActiveTab,
    reorderTabs,
  } = useWorkspace()

  const [runStates, setRunStates] = useState(() => loadRunStates(projects))
  const [accountOpen, setAccountOpen] = useState(false)
  const inflightRef = useRef(new Set())
  // 记录已关闭项目，防止关闭瞬间仍在执行的 Run 回写状态或泄漏沙盒。
  const closedProjectsRef = useRef(new Set())

  // 刷新页面后恢复已完成的预览；只持久化会话内结果，不写入 localStorage。
  useEffect(() => {
    const snapshot = {}
    Object.entries(runStates).forEach(([projectId, run]) => {
      if (!run.result && !run.error && run.sandboxIds.length === 0) return
      snapshot[projectId] = {
        annotation: run.annotation,
        result: run.result,
        error: run.error,
        lastInstruction: run.lastInstruction,
        sandboxIds: run.sandboxIds,
      }
    })
    try {
      window.sessionStorage.setItem(RUN_STORAGE_KEY, JSON.stringify(snapshot))
    } catch {
      // 快照过大 / 隐私模式：忽略，当前会话仍可正常使用。
    }
  }, [runStates])

  const anyRunning = useMemo(
    () => Object.values(runStates).some((run) => run.submitting),
    [runStates],
  )

  // 所有 Tab 共用同一个计时器；按 startedAt 计算各自耗时，避免多项目并发时互相覆盖。
  useEffect(() => {
    if (!anyRunning) return undefined
    const timer = window.setInterval(() => {
      setRunStates((previous) => {
        let changed = false
        const next = { ...previous }
        Object.entries(previous).forEach(([projectId, run]) => {
          if (!run.submitting) return
          const elapsed = Math.max(
            0,
            Math.floor((Date.now() - (run.startedAt ?? Date.now())) / 1000),
          )
          if (elapsed !== run.elapsed) {
            next[projectId] = { ...run, elapsed }
            changed = true
          }
        })
        return changed ? next : previous
      })
    }, 400)
    return () => window.clearInterval(timer)
  }, [anyRunning])

  const patchRun = (projectId, patch) => {
    if (closedProjectsRef.current.has(projectId)) return
    setRunStates((previous) => {
      const base = previous[projectId] ?? EMPTY_RUN
      const next = typeof patch === 'function' ? patch(base) : { ...base, ...patch }
      if (next === base) return previous
      return { ...previous, [projectId]: next }
    })
  }

  const handleAnnotationChange = (projectId, rect) => {
    // 无论新建还是清除选区，都重置该项目的意图草稿，避免旧意图误发到新目标。
    patchRun(projectId, (previous) => ({ ...previous, annotation: rect, intent: '' }))
  }

  const handleIntentChange = (projectId, intent) => {
    patchRun(projectId, (previous) => ({ ...previous, intent }))
  }

  const handleClearAnnotation = (projectId) => {
    patchRun(projectId, (previous) => ({ ...previous, annotation: null, intent: '' }))
  }

  const handleSubmit = async (projectId, instruction) => {
    const project = projects.find((item) => item.id === projectId)
    const run = runStates[projectId] ?? EMPTY_RUN
    const text = instruction.trim()
    if (
      !project ||
      !run.annotation ||
      !text ||
      run.submitting ||
      inflightRef.current.has(projectId)
    ) {
      return
    }

    inflightRef.current.add(projectId)
    const startedAt = Date.now()
    patchRun(projectId, {
      submitting: true,
      elapsed: 0,
      startedAt,
      result: null,
      error: null,
      intent: '',
      lastInstruction: text,
    })

    // 内置模板始终以当前 INITIAL_APP 为准：即使 localStorage 里残留了旧版
    // 文件，也不会把旧卡片样式带进沙盒，保证画布与预览框尺寸一致。
    const source =
      project.template === 'vibe-react'
        ? INITIAL_APP
        : (project.files?.[TARGET_FILE] ?? INITIAL_APP)
    const payload = {
      runId: newId('run'),
      tenantId: user?.tenantId || 'tenant_local',
      projectId: project.id,
      annotation: buildAnnotation(run.annotation, text, source),
      files: { [TARGET_FILE]: source },
      options: {
        llmProvider: 'mock',
        mockScenario: 'fail_first',
        maxRetries: 3,
        keepSandbox: true,
      },
    }

    try {
      const response = await fetch('/api/v1/runs', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${session?.token ?? ''}`,
        },
        body: JSON.stringify(payload),
      })
      if (!response.ok) {
        const message = await extractError(response)
        patchRun(projectId, { error: message })
        return
      }
      const result = await response.json()
      if (closedProjectsRef.current.has(projectId)) {
        // 提交后用户立即关闭了项目：后端仍会完成本次 Run，
        // 前端拿到结果后立刻销毁新生成的沙盒，避免资源泄漏。
        await destroySandbox(result.sandboxId, session?.token)
        return
      }
      patchRun(projectId, (previous) => ({
        ...previous,
        result,
        sandboxIds:
          result.sandboxId && !previous.sandboxIds.includes(result.sandboxId)
            ? [...previous.sandboxIds, result.sandboxId]
            : previous.sandboxIds,
      }))
    } catch (cause) {
      patchRun(projectId, { error: `网络请求失败：${cause?.message || cause}` })
    } finally {
      inflightRef.current.delete(projectId)
      patchRun(projectId, { submitting: false })
    }
  }

  /**
   * 预览沙盒失联后，用最近一次指令重新执行。
   * 仍复用原画布标注；没有历史指令（旧会话）时由 UI 引导用户手动重发。
   */
  const handleRetry = (projectId) => {
    const run = runStates[projectId]
    if (!run?.lastInstruction || !run.annotation) return
    void handleSubmit(projectId, run.lastInstruction)
  }

  /**
   * 关闭项目 = 彻底销毁：
   * 1. 标记 closed，阻止在途 Run 回写；
   * 2. 从工作区删除项目 / Tab；
   * 3. 删除该项目的运行状态；
   * 4. 调用 BFF 依次销毁所有关联沙盒。
   */
  const handleCloseTab = (projectId) => {
    const sandboxIds = runStates[projectId]?.sandboxIds ?? []
    closedProjectsRef.current.add(projectId)
    inflightRef.current.delete(projectId)
    closeTab(projectId)
    setRunStates((previous) => {
      if (!(projectId in previous)) return previous
      const next = { ...previous }
      delete next[projectId]
      return next
    })
    sandboxIds.forEach((sandboxId) => {
      void destroySandbox(sandboxId, session?.token)
    })
  }

  const statuses = useMemo(() => {
    const map = {}
    openTabs.forEach((projectId) => {
      const run = runStates[projectId]
      if (!run) {
        map[projectId] = 'idle'
      } else if (run.submitting) {
        map[projectId] = 'running'
      } else if (run.error) {
        map[projectId] = 'error'
      } else if (run.result) {
        map[projectId] = 'success'
      } else {
        map[projectId] = 'idle'
      }
    })
    return map
  }, [openTabs, runStates])

  const openProjects = openTabs
    .map((projectId) => projects.find((project) => project.id === projectId))
    .filter(Boolean)

  return (
    <div className="app-shell app-shell-wide">
      <AppHeader busy={anyRunning} onOpenAccount={() => setAccountOpen(true)} />

      <main className="workspace">
        <div className="workspace-toolbar">
          <ProjectSwitcher
            projects={projects}
            activeProject={activeProject}
            openTabs={openTabs}
            limitReached={limitReached}
            onOpenProject={openProject}
            onNewProject={createProject}
          />

          <div className="workspace-stats">
            <span className="chip chip-muted">
              {openTabs.length} / {maxTabs} 个项目
            </span>
            {anyRunning && <span className="chip chip-live">Agent 执行中</span>}
            {limitReached && (
              <span className="workspace-limit" role="status">
                最多只能同时打开 {maxTabs} 个项目，请关闭其他项目后再新建。
              </span>
            )}
          </div>
        </div>

        <WorkspaceTabs
          projects={projects}
          openTabs={openTabs}
          activeTabId={activeTabId}
          statuses={statuses}
          limitReached={limitReached}
          onSelect={setActiveTab}
          onClose={handleCloseTab}
          onReorder={reorderTabs}
          onNewProject={createProject}
        />

        <div className="workspace-body">
          {openProjects.map((project) => {
            const run = runStates[project.id] ?? EMPTY_RUN
            return (
              <ProjectWorkspace
                key={project.id}
                project={project}
                run={run}
                active={project.id === activeTabId}
                onAnnotationChange={(rect) => handleAnnotationChange(project.id, rect)}
                onIntentChange={(intent) => handleIntentChange(project.id, intent)}
                onClearAnnotation={() => handleClearAnnotation(project.id)}
                onSubmit={(instruction) => handleSubmit(project.id, instruction)}
                onRetry={() => handleRetry(project.id)}
              />
            )
          })}

          {openProjects.length === 0 && <EmptyWorkspace onNewProject={createProject} />}
        </div>
      </main>

      <AccountDialog open={accountOpen} onClose={() => setAccountOpen(false)} />
    </div>
  )
}
