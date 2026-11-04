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
const MAX_ANNOTATIONS = 3
const MODEL_STORAGE_KEY = 'vibe.llmProvider'
const MODEL_PROVIDERS = new Set(['auto', 'deepseek', 'mock'])

// 一个画布标注：选区 + 目标元素 + 用户自己的意图草稿。
// 一次 Run 最多 3 个，提交时按顺序打包成 AnnotationEnvelope[]。
const EMPTY_RUN = {
  annotations: [],
  submitting: false,
  // 单标注迷你 Chatbox 发送时记录是哪一个，用于只在该浮层显示 spinner。
  pendingAnnotationId: null,
  // run | rollback：同一个 pendingAnnotationId 下区分“执行修改”和“撤销”。
  pendingAction: null,
  elapsed: 0,
  startedAt: null,
  result: null,
  error: null,
  // 最近一次全部指令的拼接，用于预览失联后一键重新执行。
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
      // 兼容旧版本只保存单个 annotation 的会话快照。
      const rawAnnotations = Array.isArray(saved.annotations)
        ? saved.annotations
        : saved.annotation
          ? [
              {
                id: `ann_restored_${projectId}`,
                rect: saved.annotation,
                intent: saved.intent ?? '',
                target: null,
              },
            ]
          : []
      // 老快照没有 threadId / chatHistory：补齐，保证每个圈都能继续对话。
      const annotations = rawAnnotations.map((annotation) => ({
        ...annotation,
        threadId: annotation.threadId || newId('thread'),
        chatHistory: Array.isArray(annotation.chatHistory)
          ? annotation.chatHistory
          : [],
      }))
      restored[projectId] = {
        ...EMPTY_RUN,
        annotations,
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

const newMessage = (role, content, extra = {}) => ({
  id: newId('msg'),
  role,
  content,
  createdAt: new Date().toISOString(),
  ...extra,
})

/**
 * 根据 data-vibe-target 标记定位源码行号。
 * 找不到时回退到 1，仍保留文件 + 选择器 + 完整源码片段，模型可以自行定位。
 */
function findTargetLine(source, marker) {
  if (!marker) return 1
  const index = source.indexOf(`data-vibe-target="${marker}"`)
  const fallback = source.indexOf(`data-vibe-target='${marker}'`)
  const offset = index >= 0 ? index : fallback
  return offset >= 0 ? source.slice(0, offset).split('\n').length : 1
}

/**
 * 将画布上的一个归一化选区组装为 AnnotationEnvelope。
 * worldTarget 保留归一化坐标；target 记录圈选命中的元素，帮助模型把
 * “改成蓝色 / 文字改为提交 / 放大一倍”绑定到正确的组件。
 */
function buildAnnotation(entry, source) {
  const rect = entry.rect
  const instruction = entry.intent.trim()
  const target = entry.target ?? {
    marker: 'cta-button',
    selector: "[data-vibe-target='cta-button']",
    label: '按钮',
    role: 'button',
    component: 'App',
    tag: 'button',
  }
  const line = findTargetLine(source, target.marker)
  return {
    version: 1,
    annotationId: entry.id,
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
        line,
        column: 7,
        component: target.component || 'App',
        sourceKind: 'jsx',
      },
      selector: target.selector,
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
        summary: instruction,
        acceptance: [
          `按指令修改${target.label || '目标组件'}：${instruction}`,
          '其余布局、文案与交互保持不变',
        ],
        elementRole: target.role || 'button',
      },
    },
    payload: {
      text: instruction,
      from: null,
      to: null,
      strokes: [],
    },
    createdAt: entry.createdAt ?? new Date().toISOString(),
    threadId: entry.threadId,
    // 每个圈独立记忆：后端据此把上一轮指令、助手回复与修改记录传给模型。
    // commitHash / turnCount 让 python-ai 重启后仍能恢复 thread 轮次。
    chatHistory: (entry.chatHistory ?? []).slice(-40).map((message) => ({
      id: message.id,
      role: message.role,
      content: message.content,
      runId: message.runId,
      commitHash: message.commitHash,
      turnCount: message.turnCount,
      createdAt: message.createdAt,
    })),
  }
}

/**
 * 助手回复只追加一次；批量提交时每条标注都会得到同一条 run 级回复。
 *
 * 成功 Run 会带一个全局 commit hash；从 result.commits 中取当前 thread
 * 自己的 turnCount，写进消息后即可在气泡旁渲染“回滚到此步”。
 */
function appendAssistantMessage(annotation, result, fallback = '') {
  const content =
    result?.assistantMessage ||
    fallback ||
    (result?.status === 'FAILED'
      ? `执行失败：${result?.error || '构建未通过'}`
      : '已更新预览，可以继续在这个圈里提出修改。')
  const runId = result?.runId
  const commit = result?.commits?.find(
    (item) =>
      item.threadId === annotation.threadId ||
      item.threadId === annotation.id,
  )
  const history = annotation.chatHistory ?? []
  const last = history[history.length - 1]
  if (
    last?.role === 'assistant' &&
    last.content === content &&
    (!runId || last.runId === runId)
  ) {
    return history
  }
  return [
    ...history,
    newMessage('assistant', content, {
      runId,
      sandboxId: result?.sandboxId,
      commitHash: commit?.commitHash,
      turnCount: commit?.turnCount,
    }),
  ]
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
  // 默认 auto：配置了 DEEPSEEK_API_KEY 时自动走真实模型；没有 Key 时回退 Mock。
  // 用户也可以显式选择 DeepSeek / Mock，便于真实 Key 联调与离线 E2E。
  const [modelProvider, setModelProvider] = useState(() => {
    const saved = window.localStorage.getItem(MODEL_STORAGE_KEY)
    return MODEL_PROVIDERS.has(saved) ? saved : 'auto'
  })
  const [accountOpen, setAccountOpen] = useState(false)
  // 导出状态只保留一个：同时下载多个项目没有意义，且提示条全局唯一。
  const [exportingProjectId, setExportingProjectId] = useState(null)
  const [exportNotice, setExportNotice] = useState(null)
  const inflightRef = useRef(new Set())
  // 记录已关闭项目，防止关闭瞬间仍在执行的 Run 回写状态或泄漏沙盒。
  const closedProjectsRef = useRef(new Set())

  // 刷新页面后恢复已完成的预览；只持久化会话内结果，不写入 localStorage。
  useEffect(() => {
    const snapshot = {}
    Object.entries(runStates).forEach(([projectId, run]) => {
      if (!run.result && !run.error && run.sandboxIds.length === 0) return
      snapshot[projectId] = {
        annotations: run.annotations,
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

  useEffect(() => {
    window.localStorage.setItem(MODEL_STORAGE_KEY, modelProvider)
  }, [modelProvider])

  // 导出提示自动消失，成功 3 秒、失败 6 秒（失败需要更久阅读）。
  useEffect(() => {
    if (!exportNotice) return undefined
    const timer = window.setTimeout(
      () => setExportNotice(null),
      exportNotice.kind === 'error' ? 6000 : 3000,
    )
    return () => window.clearTimeout(timer)
  }, [exportNotice])

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

  const handleAnnotationAdd = (projectId, { rect, target }) => {
    patchRun(projectId, (previous) => {
      if (previous.annotations.length >= MAX_ANNOTATIONS) return previous
      return {
        ...previous,
        annotations: [
          ...previous.annotations,
          {
            id: newId('ann'),
            rect,
            target,
            intent: '',
            createdAt: new Date().toISOString(),
            // 每个标注 = 一个独立 Agent thread，后续对话复用同一个 id。
            threadId: newId('thread'),
            chatHistory: [],
            // M1.5 全局撤销基线：记录“圈出该标注时”的代码 commit。
            // 删除该圈时直接 reset 到这里；之后 A/C 的改动会丢失，M2 用
            // Event Sourcing 重放未删除标注的意图。
            baseCommitHash: previous.result?.commitHash ?? null,
            lastCommitHash: null,
            lastTurnCount: 0,
          },
        ],
      }
    })
  }

  const handleAnnotationIntentChange = (projectId, annotationId, intent) => {
    patchRun(projectId, (previous) => ({
      ...previous,
      annotations: previous.annotations.map((annotation) =>
        annotation.id === annotationId ? { ...annotation, intent } : annotation,
      ),
    }))
  }

  /**
   * M1.5 全局基线回滚：删除圈时恢复“圈选该标注之前”的代码。
   *
   * 这是有意选择的最简策略：全局 Git 是线性的，删除 B 会一并丢弃 B 之后
   * 其它圈的提交。M2 使用 Event Sourcing 重放仍未撤销的 A/C 意图来规避。
   */
  const resetToAnnotationBaseline = async (
    annotation,
    baseCommitHash,
    sandboxId,
  ) => {
    const response = await fetch('/api/v1/runs/rollback', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${session?.token ?? ''}`,
      },
      body: JSON.stringify({
        sandboxId,
        // 删除的 thread 可能没有对应 checkpoint；后端会以 Git 回滚为准。
        threadId: annotation.threadId || annotation.id,
        commitHash: baseCommitHash,
      }),
    })
    if (!response.ok) {
      throw new Error(await extractError(response))
    }
    return response.json()
  }

  const handleAnnotationRemove = async (projectId, annotationId) => {
    const run = runStates[projectId] ?? EMPTY_RUN
    const annotation = run.annotations.find((item) => item.id === annotationId)
    if (!annotation || run.submitting || inflightRef.current.has(projectId)) return

    const baseCommitHash =
      annotation.baseCommitHash || run.result?.baselineCommit || null
    const sandboxId = run.result?.sandboxId || annotation.lastSandboxId
    const needsReset = Boolean(
      annotation.lastCommitHash && baseCommitHash && sandboxId,
    )
    if (!needsReset) {
      patchRun(projectId, (previous) => ({
        ...previous,
        annotations: previous.annotations.filter(
          (item) => item.id !== annotationId,
        ),
      }))
      return
    }

    inflightRef.current.add(projectId)
    patchRun(projectId, {
      submitting: true,
      pendingAnnotationId: annotationId,
      pendingAction: 'rollback',
      error: null,
    })
    try {
      const result = await resetToAnnotationBaseline(
        annotation,
        baseCommitHash,
        sandboxId,
      )
      if (closedProjectsRef.current.has(projectId)) return
      patchRun(projectId, (previous) => ({
        ...previous,
        error: null,
        result: previous.result
          ? {
              ...previous.result,
              previewUrl: result.previewUrl,
              commitHash: result.commitHash,
              turnCount: result.turnCount ?? 0,
              error: null,
            }
          : previous.result,
        annotations: previous.annotations.filter(
          (item) => item.id !== annotationId,
        ),
      }))
    } catch (cause) {
      patchRun(projectId, {
        error: `删除标注并回滚失败：${cause?.message || cause}`,
      })
    } finally {
      inflightRef.current.delete(projectId)
      patchRun(projectId, {
        submitting: false,
        pendingAnnotationId: null,
        pendingAction: null,
      })
    }
  }

  const handleClearAnnotations = async (projectId) => {
    const run = runStates[projectId] ?? EMPTY_RUN
    if (run.submitting || inflightRef.current.has(projectId)) return
    const annotations = run.annotations
    if (annotations.length === 0) return

    // 最早执行过的圈决定 earliest baseline；清空后所有圈都会消失。
    const anchor = annotations.find((annotation) => annotation.lastCommitHash)
    const baseCommitHash = anchor
      ? anchor.baseCommitHash || run.result?.baselineCommit || null
      : null
    const sandboxId = run.result?.sandboxId || anchor?.lastSandboxId
    if (!anchor || !baseCommitHash || !sandboxId) {
      patchRun(projectId, (previous) => ({ ...previous, annotations: [] }))
      return
    }

    inflightRef.current.add(projectId)
    patchRun(projectId, {
      submitting: true,
      pendingAnnotationId: null,
      pendingAction: 'rollback',
      error: null,
    })
    try {
      const result = await resetToAnnotationBaseline(
        anchor,
        baseCommitHash,
        sandboxId,
      )
      if (closedProjectsRef.current.has(projectId)) return
      patchRun(projectId, (previous) => ({
        ...previous,
        error: null,
        annotations: [],
        result: previous.result
          ? {
              ...previous.result,
              previewUrl: result.previewUrl,
              commitHash: result.commitHash,
              turnCount: result.turnCount ?? 0,
              error: null,
            }
          : previous.result,
      }))
    } catch (cause) {
      patchRun(projectId, {
        error: `清除标注并回滚失败：${cause?.message || cause}`,
      })
    } finally {
      inflightRef.current.delete(projectId)
      patchRun(projectId, {
        submitting: false,
        pendingAnnotationId: null,
        pendingAction: null,
      })
    }
  }

  const projectSource = (project) =>
    project.template === 'vibe-react'
      ? INITIAL_APP
      : (project.files?.[TARGET_FILE] ?? INITIAL_APP)

  const postRun = async (payload) => {
    const response = await fetch('/api/v1/runs', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${session?.token ?? ''}`,
      },
      body: JSON.stringify(payload),
    })
    if (!response.ok) {
      throw new Error(await extractError(response))
    }
    return response.json()
  }

  /**
   * 单个圈的多轮发送：只提交当前标注，携带 threadId + chatHistory + 当前
   * 沙盒 ID。后端据此复用沙盒，基于上一轮修改后的源码做增量修改。
   */
  const handleAnnotationSend = async (projectId, annotationId) => {
    const project = projects.find((item) => item.id === projectId)
    const run = runStates[projectId] ?? EMPTY_RUN
    const annotation = run.annotations.find((item) => item.id === annotationId)
    const instruction = annotation?.intent.trim()
    if (
      !project ||
      !annotation ||
      !instruction ||
      run.submitting ||
      inflightRef.current.has(projectId)
    ) {
      return
    }

    const userMessage = newMessage('user', instruction)
    const sendEntry = {
      ...annotation,
      intent: instruction,
      chatHistory: [...(annotation.chatHistory ?? []), userMessage],
    }
    const source = projectSource(project)
    const startedAt = Date.now()
    inflightRef.current.add(projectId)
    patchRun(projectId, (previous) => ({
      ...previous,
      submitting: true,
      pendingAnnotationId: annotationId,
      pendingAction: 'run',
      elapsed: 0,
      startedAt,
      error: null,
      lastInstruction: instruction,
      // 输入框清空，对话气泡保留本轮用户指令。
      annotations: previous.annotations.map((item) =>
        item.id === annotationId
          ? {
              ...item,
              intent: '',
              chatHistory: [...(item.chatHistory ?? []), userMessage],
            }
          : item,
      ),
    }))

    const payload = {
      runId: newId('run'),
      tenantId: user?.tenantId || 'tenant_local',
      projectId: project.id,
      // 复用项目当前沙盒：同一圈后续对话必须基于上一轮修改后的源码。
      sandboxId: run.result?.sandboxId || annotation.lastSandboxId || undefined,
      annotations: [buildAnnotation(sendEntry, source)],
      files: { [TARGET_FILE]: source },
      options: {
        llmProvider: modelProvider,
        // 对话交互要求即时反馈；不注入 Mock 的首次失败演示。
        mockScenario: 'none',
        maxRetries: 3,
        keepSandbox: true,
      },
    }

    try {
      const result = await postRun(payload)
      if (closedProjectsRef.current.has(projectId)) {
        await destroySandbox(result.sandboxId, session?.token)
        return
      }
      patchRun(projectId, (previous) => ({
        ...previous,
        result,
        error:
          result.status === 'FAILED'
            ? result.error || '构建验证未通过'
            : null,
        annotations: previous.annotations.map((item) =>
          item.id === annotationId
            ? {
                ...item,
                lastRunId: result.runId,
                lastSandboxId: result.sandboxId,
                // 第一轮成功后才拿到沙盒 root commit；只补空值，绝不覆盖
                // 圈选时已经确定的真实基线。
                baseCommitHash:
                  item.baseCommitHash || result.baselineCommit || null,
                lastCommitHash: result.commitHash || item.lastCommitHash,
                lastTurnCount:
                  result.commits?.[0]?.turnCount ||
                  result.turnCount ||
                  item.lastTurnCount,
                chatHistory: appendAssistantMessage(item, result),
              }
            : item,
        ),
        sandboxIds:
          result.sandboxId && !previous.sandboxIds.includes(result.sandboxId)
            ? [...previous.sandboxIds, result.sandboxId]
            : previous.sandboxIds,
      }))
    } catch (cause) {
      const message = `网络请求失败：${cause?.message || cause}`
      patchRun(projectId, (previous) => ({
        ...previous,
        error: message,
        annotations: previous.annotations.map((item) =>
          item.id === annotationId
            ? {
                ...item,
                chatHistory: appendAssistantMessage(item, null, message),
              }
            : item,
        ),
      }))
    } finally {
      inflightRef.current.delete(projectId)
      patchRun(projectId, {
        submitting: false,
        pendingAnnotationId: null,
        pendingAction: null,
      })
    }
  }

  const handleSubmit = async (projectId) => {
    const project = projects.find((item) => item.id === projectId)
    const run = runStates[projectId] ?? EMPTY_RUN
    const annotations = run.annotations
    const instructions = annotations.map((annotation) => annotation.intent.trim())
    if (
      !project ||
      annotations.length === 0 ||
      instructions.some((text) => !text) ||
      run.submitting ||
      inflightRef.current.has(projectId)
    ) {
      return
    }

    // 批量提交也把每条指令写入对应圈的聊天历史，并各带自己的 threadId。
    const sendEntries = annotations.map((annotation, index) => ({
      ...annotation,
      chatHistory: [
        ...(annotation.chatHistory ?? []),
        newMessage('user', instructions[index]),
      ],
    }))
    const source = projectSource(project)
    inflightRef.current.add(projectId)
    const startedAt = Date.now()
    const lastInstruction = instructions.join(' / ')
    patchRun(projectId, {
      submitting: true,
      pendingAnnotationId: null,
      pendingAction: 'run',
      elapsed: 0,
      startedAt,
      result: null,
      error: null,
      lastInstruction,
      annotations: sendEntries,
    })

    const payload = {
      runId: newId('run'),
      tenantId: user?.tenantId || 'tenant_local',
      projectId: project.id,
      sandboxId: run.result?.sandboxId || undefined,
      // 关键：一次请求携带全部标注，后端 Planner / Coder 一次性综合处理。
      annotations: sendEntries.map((annotation) =>
        buildAnnotation(annotation, source),
      ),
      files: { [TARGET_FILE]: source },
      options: {
        // auto：有 DEEPSEEK_API_KEY 用 DeepSeek，否则回退 Mock。
        llmProvider: modelProvider,
        mockScenario: 'fail_first',
        maxRetries: 3,
        keepSandbox: true,
      },
    }

    try {
      const result = await postRun(payload)
      if (closedProjectsRef.current.has(projectId)) {
        // 提交后用户立即关闭了项目：后端仍会完成本次 Run，
        // 前端拿到结果后立刻销毁新生成的沙盒，避免资源泄漏。
        await destroySandbox(result.sandboxId, session?.token)
        return
      }
      patchRun(projectId, (previous) => ({
        ...previous,
        result,
        error:
          result.status === 'FAILED'
            ? result.error || '构建验证未通过'
            : null,
        annotations: previous.annotations.map((annotation) => {
          const commit = result.commits?.find(
            (item) =>
              item.threadId === annotation.threadId ||
              item.threadId === annotation.id,
          )
          return {
            ...annotation,
            lastRunId: result.runId,
            lastSandboxId: result.sandboxId,
            baseCommitHash:
              annotation.baseCommitHash || result.baselineCommit || null,
            lastCommitHash: result.commitHash || annotation.lastCommitHash,
            lastTurnCount: commit?.turnCount || annotation.lastTurnCount,
            chatHistory: appendAssistantMessage(annotation, result),
          }
        }),
        sandboxIds:
          result.sandboxId && !previous.sandboxIds.includes(result.sandboxId)
            ? [...previous.sandboxIds, result.sandboxId]
            : previous.sandboxIds,
      }))
    } catch (cause) {
      const message = `网络请求失败：${cause?.message || cause}`
      patchRun(projectId, (previous) => ({
        ...previous,
        error: message,
        annotations: previous.annotations.map((annotation) => ({
          ...annotation,
          chatHistory: appendAssistantMessage(annotation, null, message),
        })),
      }))
    } finally {
      inflightRef.current.delete(projectId)
      patchRun(projectId, {
        submitting: false,
        pendingAnnotationId: null,
        pendingAction: null,
      })
    }
  }

  /**
   * 单标注回滚：把沙盒代码与当前 thread 的上下文恢复到某条成功助手消息。
   *
   * 1. 前端只保留目标消息及其之前的聊天记录；
   * 2. 后端执行 git reset --hard + thread checkpoint 恢复；
   * 3. 替换带 cache-busting 的 previewUrl，强制 iframe 重新加载。
   */
  const handleAnnotationRollback = async (projectId, annotationId, message) => {
    const project = projects.find((item) => item.id === projectId)
    const run = runStates[projectId] ?? EMPTY_RUN
    const annotation = run.annotations.find((item) => item.id === annotationId)
    const sandboxId =
      message?.sandboxId || run.result?.sandboxId || annotation?.lastSandboxId
    if (
      !project ||
      !annotation ||
      !message?.commitHash ||
      !sandboxId ||
      run.submitting ||
      inflightRef.current.has(projectId)
    ) {
      return
    }

    inflightRef.current.add(projectId)
    patchRun(projectId, {
      submitting: true,
      pendingAnnotationId: annotationId,
      pendingAction: 'rollback',
      elapsed: 0,
      startedAt: Date.now(),
      error: null,
    })

    try {
      const response = await fetch('/api/v1/runs/rollback', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${session?.token ?? ''}`,
        },
        body: JSON.stringify({
          sandboxId,
          threadId: annotation.threadId,
          commitHash: message.commitHash,
          runId: message.runId,
          turnCount: message.turnCount,
        }),
      })
      if (!response.ok) {
        throw new Error(await extractError(response))
      }
      const result = await response.json()
      if (closedProjectsRef.current.has(projectId)) return

      patchRun(projectId, (previous) => ({
        ...previous,
        error: null,
        // 全局代码已经回到该 commit；保留其它标注对象，它们的历史由线程
        // checkpoint 各自管理。M1.5 的跨标注覆盖限制记录在架构文档。
        result: previous.result
          ? {
              ...previous.result,
              status: 'AWAITING_APPROVAL',
              previewUrl: result.previewUrl,
              commitHash: result.commitHash,
              turnCount: result.turnCount,
              runId: result.runId || previous.result.runId,
              error: null,
            }
          : previous.result,
        annotations: previous.annotations.map((item) =>
          item.id === annotationId
            ? (() => {
                const history = item.chatHistory ?? []
                const targetIndex = history.findIndex(
                  (entry) => entry.id === message.id,
                )
                const kept =
                  targetIndex >= 0 ? history.slice(0, targetIndex + 1) : history
                return {
                  ...item,
                  intent: '',
                  lastRunId: message.runId,
                  lastSandboxId: result.sandboxId,
                  lastCommitHash: message.commitHash,
                  lastTurnCount: message.turnCount,
                  chatHistory: kept,
                }
              })()
            : item,
        ),
      }))
    } catch (cause) {
      patchRun(projectId, {
        error: `回滚失败：${cause?.message || cause}`,
      })
    } finally {
      inflightRef.current.delete(projectId)
      patchRun(projectId, {
        submitting: false,
        pendingAnnotationId: null,
        pendingAction: null,
      })
    }
  }

  /**
   * 导出当前项目的 tar.gz：
   *  - 优先走 GET /api/v1/runs/{runId}/export?sandboxId=...（BFF 记录映射）；
   *  - 旧会话没有 runId 时直接走 GET /api/v1/sandboxes/{sandboxId}/export；
   *  - 用 Blob + <a download> 触发浏览器原生下载，不依赖弹窗或新标签页。
   */
  const handleExport = async (projectId) => {
    const run = runStates[projectId]
    const sandboxId = run?.result?.sandboxId
    if (!sandboxId) {
      setExportNotice({
        kind: 'error',
        text: '当前项目还没有可导出的沙盒，请先执行一次 Agent。',
      })
      return
    }
    if (exportingProjectId) return

    setExportingProjectId(projectId)
    setExportNotice(null)
    const runId = run?.result?.runId
    // sandboxId 显式带上：BFF 重启后进程内 runId 映射会丢失，前端仍能导出。
    const url = runId
      ? `/api/v1/runs/${encodeURIComponent(runId)}/export?sandboxId=${encodeURIComponent(sandboxId)}`
      : `/api/v1/sandboxes/${encodeURIComponent(sandboxId)}/export`

    try {
      const response = await fetch(url, {
        headers: { Authorization: `Bearer ${session?.token ?? ''}` },
      })
      if (!response.ok) {
        throw new Error(await extractError(response))
      }
      const blob = await response.blob()
      if (blob.size === 0) {
        throw new Error('导出包为空')
      }

      const objectUrl = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = objectUrl
      anchor.download = 'project-export.tar.gz'
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      // 延迟回收，给浏览器留出读取 Blob 的时间。
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
      setExportNotice({
        kind: 'success',
        text: `已导出 ${(blob.size / 1024).toFixed(1)} KB 项目源码包`,
      })
    } catch (cause) {
      setExportNotice({ kind: 'error', text: `导出失败：${cause?.message || cause}` })
    } finally {
      setExportingProjectId(null)
    }
  }

  /**
   * 预览沙盒失联后，用最近一次指令重新执行。
   * 仍复用原画布标注；没有历史指令（旧会话）时由 UI 引导用户手动重发。
   */
  const handleRetry = (projectId) => {
    const run = runStates[projectId]
    if (!run?.lastInstruction || run.annotations.length === 0) return
    void handleSubmit(projectId)
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
            canExport={Boolean(
              activeProject && runStates[activeProject.id]?.result?.sandboxId,
            )}
            exporting={Boolean(
              activeProject && exportingProjectId === activeProject.id,
            )}
            onOpenProject={openProject}
            onNewProject={createProject}
            onExport={handleExport}
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
                modelProvider={modelProvider}
                onModelProviderChange={setModelProvider}
                onAnnotationAdd={(payload) => handleAnnotationAdd(project.id, payload)}
                onAnnotationIntentChange={(annotationId, intent) =>
                  handleAnnotationIntentChange(project.id, annotationId, intent)
                }
                onAnnotationRemove={(annotationId) =>
                  handleAnnotationRemove(project.id, annotationId)
                }
                onAnnotationSend={(annotationId) =>
                  handleAnnotationSend(project.id, annotationId)
                }
                onAnnotationRollback={(annotationId, message) =>
                  handleAnnotationRollback(project.id, annotationId, message)
                }
                onClearAnnotations={() => handleClearAnnotations(project.id)}
                onSubmit={() => handleSubmit(project.id)}
                onRetry={() => handleRetry(project.id)}
                canExport={Boolean(run.result?.sandboxId)}
                exporting={exportingProjectId === project.id}
                onExport={() => handleExport(project.id)}
              />
            )
          })}

          {openProjects.length === 0 && <EmptyWorkspace onNewProject={createProject} />}
        </div>
      </main>

      {exportNotice && (
        <div
          className={`workspace-toast is-${exportNotice.kind}`}
          role="status"
          data-export-notice={exportNotice.kind}
        >
          <span aria-hidden="true">{exportNotice.kind === 'success' ? '✓' : '⚠'}</span>
          {exportNotice.text}
        </div>
      )}

      <AccountDialog open={accountOpen} onClose={() => setAccountOpen(false)} />
    </div>
  )
}
