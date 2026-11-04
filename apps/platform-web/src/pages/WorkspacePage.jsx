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
    createProject,
    openProject,
    closeTab,
    setActiveTab,
    reorderTabs,
  } = useWorkspace()

  const [runStates, setRunStates] = useState({})
  const [accountOpen, setAccountOpen] = useState(false)
  const inflightRef = useRef(new Set())

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
    })

    const source = project.files?.[TARGET_FILE] ?? INITIAL_APP
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
      patchRun(projectId, { result })
    } catch (cause) {
      patchRun(projectId, { error: `网络请求失败：${cause?.message || cause}` })
    } finally {
      inflightRef.current.delete(projectId)
      patchRun(projectId, { submitting: false })
    }
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
            onOpenProject={openProject}
            onNewProject={createProject}
          />

          <div className="workspace-stats">
            <span className="chip chip-muted">{projects.length} 个项目</span>
            <span className="chip chip-muted">{openTabs.length} 个窗口</span>
            {anyRunning && <span className="chip chip-live">Agent 执行中</span>}
          </div>
        </div>

        <WorkspaceTabs
          projects={projects}
          openTabs={openTabs}
          activeTabId={activeTabId}
          statuses={statuses}
          onSelect={setActiveTab}
          onClose={closeTab}
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
