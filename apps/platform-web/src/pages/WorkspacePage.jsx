import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import AppHeader from '../components/AppHeader.jsx'
import AccountDialog from '../components/AccountDialog.jsx'
import SettingsDialog from '../components/SettingsDialog.jsx'
import ProjectSwitcher from '../components/ProjectSwitcher.jsx'
import ProjectWorkspace from '../components/ProjectWorkspace.jsx'
import WorkspaceTabs from '../components/WorkspaceTabs.jsx'
import { useAuth } from '../auth/AuthContext.jsx'
import { useSettings } from '../settings/SettingsContext.jsx'
import { useWorkspace } from '../workspace/useWorkspace.js'
import { EMPTY_ARCHITECT, useArchitect } from '../workspace/useArchitect.js'
import { streamRun } from '../workspace/runStream.js'
import { importProjectFile } from '../workspace/importClient.js'
import {
  dispatchSupervisor,
  fetchSupervisorAgents,
  releaseSupervisorGate,
  routeSupervisor,
} from '../workspace/supervisorClient.js'
import { INITIAL_APP } from '../mockProject.js'

const TARGET_FILE = 'src/App.jsx'
const MAX_ANNOTATIONS = 3

/**
 * 导入项目可能使用不同的入口文件（React 常见 src/App.tsx，
 * 纯 HTML 项目为 index.html）。画布标注必须指向真实存在、可被 Agent
 * 修改的文件，否则会退回内置模板源码，导致“改了预览却没变”。
 */
function projectEntryFile(project) {
  return project?.entryFile || TARGET_FILE
}

function projectSourceFile(project) {
  const entry = projectEntryFile(project)
  if (project?.files?.[entry]) return project.files[entry]
  return project?.files?.[TARGET_FILE] ?? INITIAL_APP
}
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
  // M2 SSE：当前 runId / 最后一条事件 seq / 实时时间轴 / 待澄清问题。
  runId: null,
  lastSeq: 0,
  timeline: [],
  clarification: null,
  // 本次 Run 对应的标注：审批 / 取消恢复事件流时用它定位要写入哪条聊天。
  // null 表示批量提交，终态回复写给全部标注。
  targetAnnotationId: null,
  // Diff 审批：approval.required 的 payload，非空时 RuntimePanel 显示审批栏。
  approval: null,
  // 协作式取消：已请求但尚未收到 run.cancelled。
  cancelRequested: false,
  // patch.proposed 的截断 diff，用于详情抽屉；完整结果仍以终态为准。
  patchPreview: '',
  // Supervisor 最近一次意图：handoff_ui / handoff_architect / handoff_both /
  // handoff_custom / rejected。
  supervisorHandoff: null,
  // 可视化“AI 思考”的轻量状态：analyzing → decided → dispatching →
  // both_waiting / ui_running / custom / rejected / error。
  supervisorPhase: null,
  // route 返回的完整决策 + handoff 展示名，用于顶部轨迹与专属 Agent 卡片。
  supervisorDecision: null,
}

// 成功终态：M2 的旧快照仍可能是 AWAITING_APPROVAL，统一按成功渲染。
const SUCCESS_STATUSES = new Set(['SUCCEEDED', 'AWAITING_APPROVAL'])
const isSuccessStatus = (status) => SUCCESS_STATUSES.has(status)

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

// both 门控的前端镜像：只用于 UI 提示与刷新后恢复，权威状态仍在 AI Core。
const GATE_STORAGE_KEY = 'vibe.supervisor.gates.v1'

function loadGateStates(projects) {
  const projectIds = new Set(projects.map((project) => project.id))
  try {
    const raw = window.sessionStorage.getItem(GATE_STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    const restored = {}
    Object.entries(parsed).forEach(([projectId, gate]) => {
      if (!projectIds.has(projectId) || !gate?.gateId) return
      restored[projectId] = {
        ...gate,
        releasing: false,
        error: null,
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

/**
 * 可中止的等待。
 *
 * Supervisor 路由后刻意停留 480ms 展示“分析中”，若用户此时关闭标注，
 * 定时器必须立即清理并抛 AbortError，不能在卸载后继续写状态。
 */
function abortableDelay(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('Aborted', 'AbortError'))
      return
    }
    const timer = window.setTimeout(() => {
      cleanup()
      resolve()
    }, ms)
    const onAbort = () => {
      window.clearTimeout(timer)
      cleanup()
      reject(new DOMException('Aborted', 'AbortError'))
    }
    const cleanup = () => {
      signal?.removeEventListener('abort', onAbort)
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
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
  // 新源码写 data-source，旧模板仍可能只有 data-vibe-target。
  const patterns = [
    `data-source="${marker}"`,
    `data-source='${marker}'`,
    `data-vibe-target="${marker}"`,
    `data-vibe-target='${marker}'`,
  ]
  let offset = -1
  for (const pattern of patterns) {
    offset = source.indexOf(pattern)
    if (offset >= 0) break
  }
  return offset >= 0 ? source.slice(0, offset).split('\n').length : 1
}

// “添加 / 新建 / 插入 / add” 等动词优先于圈选命中结果：即使用户圈到了
// 卡片本身，只要指令是在新增内容，就按 ADD 处理，避免模型去改卡片颜色。
const ADD_INTENT_PATTERN = /添加|新增|新建|插入|加一个|加个|(?:^|\s)add\s/i

/**
 * 自动推断操作类型：
 *  - 圈中没有命中任何 data-source（空白区域）→ ADD；
 *  - 圈的展示面积远大于命中的节点 → ADD（由 CanvasStage 的几何判定给出）；
 *  - 指令包含新增语义 → ADD；
 *  - 其余命中已有元素 → MODIFY。
 * 用户手动点选浮层内的“修改 / 新增”后 actionSource=manual，不再自动覆盖。
 */
function inferAnnotationAction(target, intent, geometryAction) {
  if (ADD_INTENT_PATTERN.test(intent ?? '')) return 'ADD'
  if (geometryAction === 'ADD' || geometryAction === 'MODIFY') {
    return geometryAction
  }
  return target ? 'MODIFY' : 'ADD'
}

/**
 * 将画布上的一个归一化选区组装为 AnnotationEnvelope。
 * worldTarget 保留归一化坐标；target 记录圈选命中的元素，帮助模型把
 * “改成蓝色 / 文字改为提交 / 放大一倍”绑定到正确的组件。
 */
function buildAnnotation(entry, source, targetFile = TARGET_FILE, files = null) {
  const rect = entry.rect
  const instruction = entry.intent.trim()
  // 旧 localStorage 里的标注没有 action：按指令与几何判定即时推断。
  const action =
    entry.action === 'ADD' || entry.action === 'MODIFY'
      ? entry.action
      : inferAnnotationAction(
          entry.target,
          instruction,
          entry.geometryAction,
        )
  // ADD 的目标必须是“最近 data-source 父容器”，由 CanvasStage 在圈选时
  // 计算；只命中叶子时不能把新增 JSX 塞进 button / p / h1。
  // MODIFY 没有命中元素时才回退到默认 CTA，保持旧行为。
  const addTarget = entry.addTarget ?? null
  const target =
    action === 'ADD'
      ? addTarget
      : (entry.target ??
        {
          marker: 'cta-button',
          selector: "[data-source='cta-button']",
          label: '按钮',
          role: 'button',
          component: 'App',
          tag: 'button',
        })
  // Inspect 模式由 SDK 回传真实文件 / 行号 / 列号；圈选模式继续使用
  // data-source 标记反查行号。两者最终都收敛到同一个 AnnotationEnvelope。
  const precise = entry.inspectSource ?? entry.source ?? null
  const resolvedFile = precise?.file || targetFile
  const line = Number(precise?.line) || findTargetLine(source, target?.marker)
  const column = Number(precise?.column) || 1
  const sourceText = files?.[resolvedFile] ?? source
  const sourceLines = String(sourceText || '').split('\n')
  const startLine = precise ? Math.max(1, line - 2) : 1
  const endLine = precise
    ? Math.min(sourceLines.length, line + 2)
    : sourceLines.length
  const snippetContent = precise
    ? sourceLines.slice(startLine - 1, endLine).join('\n')
    : sourceText
  const acceptance =
    action === 'ADD'
      ? [
          `在${target?.label || '圈选区域'}（${target?.selector || '父容器'}）内部新增组件：${instruction}`,
          '按圈相对父容器的坐标决定插入顺序，原有 DOM 结构、布局、文案与交互保持不变',
        ]
      : [
          `按指令修改${target?.label || '目标组件'}：${instruction}`,
          '其余布局、文案与交互保持不变',
        ]
  // relativeTarget 只在 ADD 时有意义：
  //  - x/y/width/height 为兼容字段（相对父容器的归一化坐标 0~1）；
  //  - relativeX/Y 与 widthPx/heightPx/parentWidth/parentHeight 为开发者级
  //    像素坐标，模型据此用 flex/grid/absolute/margin 精确落位。
  const relative = action === 'ADD' ? entry.relativeRect : null
  return {
    version: 1,
    annotationId: entry.id,
    // visual-draw = 精准点击选择；drag = 圈选。后端两者都按 UI 标注处理，
    // 保留 type 便于审计与后续差异化策略。
    type: precise ? 'visual-draw' : 'drag',
    action,
    worldTarget: {
      x: Number(rect.x.toFixed(6)),
      y: Number(rect.y.toFixed(6)),
      width: Number(rect.width.toFixed(6)),
      height: Number(rect.height.toFixed(6)),
      coordinateSpace: 'normalized',
    },
    relativeTarget:
      action === 'ADD'
        ? {
            x: relative?.x ?? 0,
            y: relative?.y ?? 0,
            width: relative?.width ?? 1,
            height: relative?.height ?? 1,
            relativeX: relative?.relativeX ?? null,
            relativeY: relative?.relativeY ?? null,
            widthPx: relative?.widthPx ?? null,
            heightPx: relative?.heightPx ?? null,
            parentWidth: relative?.parentWidth ?? null,
            parentHeight: relative?.parentHeight ?? null,
            coordinateSpace: 'parent-relative',
            parentSelector: target?.selector ?? null,
            parentMarker: target?.marker ?? null,
          }
        : {},
    target: target
      ? {
          mode: 'source',
          source: {
            file: resolvedFile,
            line,
            column,
            endLine: precise ? line : undefined,
            endColumn: precise
              ? (sourceLines[line - 1]?.length ?? 0) + 1
              : undefined,
            component: precise?.component || target.component || 'App',
            sourceKind:
              precise?.sourceKind ||
              (resolvedFile.endsWith('.vue')
                ? 'vue'
                : resolvedFile.endsWith('.html')
                  ? 'html'
                  : 'jsx'),
          },
          selector: target.selector,
          snippet: {
            startLine,
            endLine,
            content: snippetContent,
            hash: precise
              ? `inspect:${resolvedFile}:${line}`
              : 'sha256:canvas-demo',
          },
        }
      : null,
    // Inspect 模式保留命中测量信息（点击元素 vs. ADD 落点父容器），
    // 便于审计“高亮的是谁、实际改的是谁”。圈选模式该字段为空。
    measured: entry.measured ?? undefined,
    intent: {
      kind: 'UI_ONLY',
      ui: {
        summary: instruction,
        acceptance,
        elementRole: target?.role || 'container',
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

/**
 * SSE 事件 -> 时间轴。
 *
 * 节点可能因为 LangGraph 恢复 / 自动修复而重跑，因此按
 * node + round + attempt 生成稳定 key；重放同一条事件不会产生重复行。
 */
function reduceTimeline(timeline, event) {
  const payload = event.payload ?? {}
  if (event.type === 'node.started') {
    const key = `${payload.node}:${payload.round ?? 0}:${payload.attempt ?? 0}`
    if (timeline.some((item) => item.id === key)) {
      return timeline.map((item) =>
        item.id === key ? { ...item, status: 'running', durationMs: null } : item,
      )
    }
    return [
      ...timeline,
      {
        id: key,
        node: payload.node,
        label: payload.label || payload.node,
        status: 'running',
        attempt: payload.attempt ?? 0,
        round: payload.round ?? 0,
        durationMs: null,
        summary: '',
        message: '',
      },
    ]
  }

  if (event.type === 'node.progress') {
    return timeline.map((item) =>
      item.node === payload.node && item.status === 'running'
        ? { ...item, message: payload.message || item.message }
        : item,
    )
  }

  if (event.type === 'node.finished') {
    const index = [...timeline]
      .map((item, itemIndex) => ({ item, itemIndex }))
      .reverse()
      .find(
        ({ item }) => item.node === payload.node && item.status === 'running',
      )?.itemIndex
    if (index === undefined) {
      return [
        ...timeline,
        {
          id: `${payload.node}:finished:${event.seq}`,
          node: payload.node,
          label: payload.node,
          status: payload.status || 'ok',
          durationMs: payload.durationMs ?? null,
          summary: payload.summary || '',
          message: '',
        },
      ]
    }
    return timeline.map((item, itemIndex) =>
      itemIndex === index
        ? {
            ...item,
            status: payload.status || 'ok',
            durationMs: payload.durationMs ?? item.durationMs,
            summary: payload.summary || item.summary,
            message: '',
          }
        : item,
    )
  }

  if (event.type === 'verify.passed' || event.type === 'verify.failed') {
    return timeline.map((item) =>
      item.node === 'verifier' && item.status === 'running'
        ? {
            ...item,
            status: event.type === 'verify.passed' ? 'ok' : 'error',
            durationMs: payload.durationMs ?? item.durationMs,
          }
        : item,
    )
  }

  if (event.type === 'repair.started') {
    return [
      ...timeline,
      {
        id: `repair:${payload.attempt}:${event.seq}`,
        node: 'repair',
        label: `自动修复 ${payload.attempt}/${payload.maxAttempts}`,
        status: 'running',
        durationMs: null,
        summary: payload.reason || '',
        message: '',
      },
    ]
  }

  return timeline
}

function timelineMessages(timeline) {
  return timeline
    .filter((item) => item.summary || item.message)
    .map((item) => ({
      node: item.node,
      content: item.summary || item.message,
      level: item.status === 'error' ? 'error' : 'info',
    }))
}

function resultFromTerminalEvent(run, event) {
  const payload = event.payload ?? {}
  const success = event.type === 'run.succeeded'
  const cancelled = event.type === 'run.cancelled'
  const rejected = event.type === 'run.rejected'
  const status = success
    ? payload.status || 'SUCCEEDED'
    : cancelled
      ? 'CANCELLED'
      : rejected
        ? 'REJECTED'
        : 'FAILED'
  const fallbackMessage = success
    ? '已更新预览，可以继续提出修改。'
    : cancelled
      ? '已取消本次修改，代码已回滚到执行前状态。'
      : rejected
        ? '已拒绝本次修改，代码已回滚到执行前状态。'
        : ''
  return {
    runId: event.runId,
    status,
    sandboxId: payload.sandboxId ?? '',
    // 拒绝 / 取消的 previewUrl 带 cache-busting，iframe 会强制刷新回滚后的代码。
    previewUrl: payload.previewUrl ?? '',
    commitHash: payload.commitHash ?? null,
    baselineCommit: payload.baselineCommit ?? null,
    retryCount: payload.retryCount ?? 0,
    turnCount: payload.turnCount ?? 0,
    commits: payload.commits ?? [],
    assistantMessage:
      payload.assistantMessage ||
      payload.message ||
      fallbackMessage,
    error:
      event.type === 'run.failed'
        ? payload.message || '构建验证未通过'
        : null,
    generatedPatch: success ? run.patchPreview || '' : '',
    verifyResult: null,
    messages: timelineMessages(run.timeline),
  }
}

/**
 * 终态事件落地：更新标注的 Git 坐标、追加助手消息、记录沙盒。
 * annotationId 为空表示批量提交，所有参与标注都收到同一条 run 级回复。
 */
function finalizeFromResult(run, result, annotationId) {
  const annotations = run.annotations.map((annotation) => {
    if (annotationId && annotation.id !== annotationId) return annotation
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
      lastTurnCount:
        commit?.turnCount || result.turnCount || annotation.lastTurnCount,
      chatHistory: appendAssistantMessage(annotation, result),
    }
  })
  return {
    ...run,
    result,
    error:
      result.status === 'FAILED'
        ? result.error || '构建验证未通过'
        : null,
    annotations,
    sandboxIds:
      result.sandboxId && !run.sandboxIds.includes(result.sandboxId)
        ? [...run.sandboxIds, result.sandboxId]
        : run.sandboxIds,
    submitting: false,
    pendingAnnotationId: null,
    pendingAction: null,
    clarification: null,
    approval: null,
    cancelRequested: false,
  }
}

function applyStreamEvent(run, event, annotationId) {
  const payload = event.payload ?? {}
  const base = {
    ...run,
    runId: event.runId || run.runId,
    lastSeq: Math.max(run.lastSeq ?? 0, event.seq ?? 0),
    timeline: reduceTimeline(run.timeline ?? [], event),
  }

  if (event.type === 'patch.proposed') {
    return { ...base, patchPreview: payload.diffPreview || base.patchPreview }
  }
  if (event.type === 'clarification.required') {
    return {
      ...base,
      clarification: payload,
      submitting: false,
      pendingAnnotationId: null,
      pendingAction: null,
    }
  }
  if (event.type === 'clarification.answered') {
    return { ...base, clarification: null }
  }
  if (event.type === 'approval.required') {
    return {
      ...base,
      approval: payload,
      clarification: null,
      submitting: false,
      pendingAnnotationId: null,
      pendingAction: null,
      cancelRequested: false,
    }
  }
  if (event.type === 'approval.answered') {
    return { ...base, approval: null }
  }
  if (event.type === 'run.cancel_requested') {
    return { ...base, cancelRequested: true }
  }
  if (
    event.type === 'run.succeeded' ||
    event.type === 'run.failed' ||
    event.type === 'run.cancelled' ||
    event.type === 'run.rejected'
  ) {
    return finalizeFromResult(
      base,
      resultFromTerminalEvent(base, event),
      annotationId ?? base.targetAnnotationId,
    )
  }
  return base
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
  const { requireManualApproval } = useSettings()
  const {
    projects,
    openTabs,
    activeTabId,
    activeProject,
    maxTabs,
    limitReached,
    createProject,
    importProject,
    openProject,
    closeTab,
    setActiveTab,
    reorderTabs,
  } = useWorkspace()

  // M2.5 全局架构澄清：与画布 Run 完全独立的 SSE / 状态存储。
  const architect = useArchitect({ token: session?.token })
  const architectStates = architect.states

  const [runStates, setRunStates] = useState(() => loadRunStates(projects))
  // Supervisor both 门控：AWAITING_CONTRACT 时 UI 子图不执行。
  const [gateStates, setGateStates] = useState(() => loadGateStates(projects))
  // “我的专属 Agent”目录：只读 AgentRegistry，不触发任何执行。
  const [agentCatalog, setAgentCatalog] = useState({
    agents: [],
    loading: true,
    error: null,
  })
  const agentCatalogRequestRef = useRef(0)
  const loadAgentCatalog = useCallback(async () => {
    const requestId = agentCatalogRequestRef.current + 1
    agentCatalogRequestRef.current = requestId
    setAgentCatalog((previous) => ({ ...previous, loading: true, error: null }))
    try {
      const payload = await fetchSupervisorAgents(session?.token)
      if (agentCatalogRequestRef.current !== requestId) return
      setAgentCatalog({
        agents: Array.isArray(payload?.agents) ? payload.agents : [],
        loading: false,
        error: null,
      })
    } catch (cause) {
      if (agentCatalogRequestRef.current !== requestId) return
      setAgentCatalog((previous) => ({
        ...previous,
        loading: false,
        error: `专属 Agent 加载失败：${cause?.message || cause}`,
      }))
    }
  }, [session?.token])

  useEffect(() => {
    void loadAgentCatalog()
  }, [loadAgentCatalog])
  // 通用操作提示（rejected / 门控释放失败等），与导出提示分离。
  const [toast, setToast] = useState(null)
  // 递增后 ProjectWorkspace 自动切到预览 Tab，露出局部的执行时间轴。
  const [previewFocus, setPreviewFocus] = useState({})
  // 默认 auto：配置了 DEEPSEEK_API_KEY 时自动走真实模型；没有 Key 时回退 Mock。
  // 旧版本可能持久化过显式 deepseek，这里统一归一到 auto，UI 不再暴露内部
  // provider 名称（测试仍可通过 mock 进入离线模式）。
  const [modelProvider, setModelProvider] = useState(() => {
    const saved = window.localStorage.getItem(MODEL_STORAGE_KEY)
    if (saved === 'deepseek') return 'auto'
    return MODEL_PROVIDERS.has(saved) ? saved : 'auto'
  })
  const [accountOpen, setAccountOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  // 导出状态只保留一个：同时下载多个项目没有意义，且提示条全局唯一。
  const [exportingProjectId, setExportingProjectId] = useState(null)
  const [exportNotice, setExportNotice] = useState(null)
  // 导入是“上传 + 容器启动 + npm install”的长请求，同一时间只允许一个。
  const [importing, setImporting] = useState(false)
  const inflightRef = useRef(new Set())
  // 每个项目最多一个 SSE 连接；关闭 Tab / 重新提交时主动 abort。
  const streamControllersRef = useRef(new Map())
  // 记录已关闭项目，防止关闭瞬间仍在执行的 Run 回写状态或泄漏沙盒。
  const closedProjectsRef = useRef(new Set())
  // 门控释放只允许一个在途请求；自动释放每个 gate 只尝试一次，失败后由用户
  // 在架构面板中手动重试，避免 effect 死循环。
  const gateReleaseInflightRef = useRef(new Set())
  const gateAutoReleaseAttemptedRef = useRef(new Set())
  // 页面级挂载标记：所有异步回调写状态前必须检查，避免卸载后 setState。
  const workspaceMountedRef = useRef(true)
  // 每个项目一个单调递增 epoch；新 Run / 强制关闭都会递增，旧请求的
  // 迟到响应通过 epoch 判定为过期，绝不能覆盖新状态。
  const runEpochRef = useRef(new Map())
  // 已关闭的标注：即使后端任务仍在执行，也丢弃该 thread 的后续事件。
  const closedAnnotationsRef = useRef(new Map())
  // 按 projectId:annotationId 维度持有 AbortController，关闭浮层即中止。
  const annotationAbortRef = useRef(new Map())
  // 异步回调里需要读取最新 run 状态（state 本身是渲染快照）。
  const runStatesRef = useRef(runStates)

  useEffect(() => {
    runStatesRef.current = runStates
  }, [runStates])

  // 页面卸载 / 切换项目时关闭所有 SSE，避免后台继续占用连接。
  useEffect(() => {
    workspaceMountedRef.current = true
    return () => {
      workspaceMountedRef.current = false
      streamControllersRef.current.forEach((controller) => controller.abort())
      streamControllersRef.current.clear()
      annotationAbortRef.current.forEach((controller) => controller.abort())
      annotationAbortRef.current.clear()
    }
  }, [])

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

  useEffect(() => {
    if (!toast) return undefined
    const timer = window.setTimeout(
      () => setToast(null),
      toast.kind === 'error' ? 6000 : 3500,
    )
    return () => window.clearTimeout(timer)
  }, [toast])

  // 门控镜像持久化到 sessionStorage；刷新后仍能显示“请先确认契约”。
  useEffect(() => {
    const snapshot = {}
    Object.entries(gateStates).forEach(([projectId, gate]) => {
      if (!gate?.gateId) return
      snapshot[projectId] = gate
    })
    try {
      window.sessionStorage.setItem(GATE_STORAGE_KEY, JSON.stringify(snapshot))
    } catch {
      // 忽略隐私模式 / 配额错误。
    }
  }, [gateStates])

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
    if (!workspaceMountedRef.current) return
    if (closedProjectsRef.current.has(projectId)) return
    setRunStates((previous) => {
      const base = previous[projectId] ?? EMPTY_RUN
      const next = typeof patch === 'function' ? patch(base) : { ...base, ...patch }
      if (next === base) return previous
      return { ...previous, [projectId]: next }
    })
  }

  const currentRunEpoch = (projectId) => runEpochRef.current.get(projectId) ?? 0

  const bumpRunEpoch = (projectId) => {
    const next = currentRunEpoch(projectId) + 1
    runEpochRef.current.set(projectId, next)
    return next
  }

  const isAnnotationClosed = (projectId, annotationId) => {
    if (!annotationId) return false
    return closedAnnotationsRef.current.get(projectId)?.has(annotationId) ?? false
  }

  const markAnnotationClosed = (projectId, annotationId) => {
    if (!annotationId) return
    let closed = closedAnnotationsRef.current.get(projectId)
    if (!closed) {
      closed = new Set()
      closedAnnotationsRef.current.set(projectId, closed)
    }
    closed.add(annotationId)
  }

  const annotationScopeKey = (projectId, annotationId) =>
    `${projectId}:${annotationId ?? 'batch'}`

  /** 后台静默取消：不订阅事件、不写 UI 状态；失败也不打扰用户。 */
  const cancelRunSilently = (runId) => {
    if (!runId) return
    void fetch(`/api/v1/runs/${encodeURIComponent(runId)}/cancel`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${session?.token ?? ''}`,
      },
      body: JSON.stringify({ reason: '标注已由用户关闭' }),
      keepalive: true,
    }).catch(() => {
      // 后端可能已进入终态；静默取消无需向用户报错。
    })
  }

  /**
   * 中止某个标注关联的所有异步工作。
   *
   * 触发时机：浮层卸载、用户确认强制关闭。函数必须幂等：
   *  - abort 已发出的 route / dispatch；
   *  - 中止该项目 SSE，并递增 epoch 让旧回调全部失效；
   *  - 后端 Run 若已启动，再尽力发一次 cancel；
   *  - 清理 submitting / pending / error，避免预览落入“执行失败”。
   */
  const abortAnnotationScope = (projectId, annotationId) => {
    if (!workspaceMountedRef.current) return
    if (!annotationId) return
    const key = annotationScopeKey(projectId, annotationId)
    const controller = annotationAbortRef.current.get(key)
    if (controller) {
      controller.abort()
      annotationAbortRef.current.delete(key)
    }

    const run = runStatesRef.current?.[projectId]
    if (!run) return
    // 只有真正在途 / 等待澄清 / 等待审批的 Run 才需要中止。
    // 已完成的 Run 保留 targetAnnotationId，不能因为删除旧标注而误取消。
    const activeRun =
      run.submitting ||
      Boolean(run.clarification) ||
      Boolean(run.approval) ||
      inflightRef.current.has(projectId)
    const ownsRun =
      activeRun &&
      (run.targetAnnotationId === annotationId ||
        run.pendingAnnotationId === annotationId ||
        run.annotations.some((item) => item.id === annotationId))
    if (!ownsRun) return

    bumpRunEpoch(projectId)
    streamControllersRef.current.get(projectId)?.abort()
    streamControllersRef.current.delete(projectId)
    inflightRef.current.delete(projectId)
    cancelRunSilently(run.runId)
    patchRun(projectId, (previous) => ({
      ...previous,
      submitting: false,
      pendingAnnotationId: null,
      pendingAction: null,
      // 关闭标注不再把取消 / 断流呈现为失败。
      error: null,
      clarification: null,
      approval: null,
      cancelRequested: false,
      targetAnnotationId:
        previous.targetAnnotationId === annotationId
          ? null
          : previous.targetAnnotationId,
    }))
  }

  const patchGate = (projectId, patch) => {
    if (!workspaceMountedRef.current) return
    setGateStates((previous) => {
      const base = previous[projectId]
      if (!base) return previous
      const next = typeof patch === 'function' ? patch(base) : { ...base, ...patch }
      if (next === base) return previous
      return { ...previous, [projectId]: next }
    })
  }

  const showToast = (kind, text) => {
    setToast({ id: Date.now(), kind, text })
  }

  const focusPreview = (projectId) => {
    setPreviewFocus((previous) => ({
      ...previous,
      [projectId]: (previous[projectId] ?? 0) + 1,
    }))
  }

  /**
   * 导入项目：上传 zip / 截图 -> Python 解包并启动沙盒 -> 新建 Tab。
   *
   * 导入完成后直接写入一个“已完成”的 Run 结果，使预览面板立刻显示沙盒，
   * 后续画布标注与增量修改复用同一个 sandboxId。
   */
  const handleImportFile = async (file) => {
    if (!file || importing) return
    if (limitReached) {
      showToast('error', '最多只能同时打开 5 个项目，请关闭其他项目后再导入')
      return
    }
    const projectId = newId('proj')
    const baseName = file.name.replace(/\.(zip|png|jpe?g)$/i, '').trim()
    const projectName = baseName || '导入项目'
    setImporting(true)
    showToast('info', `正在导入 ${file.name}，首次 npm install 可能需要几分钟…`)
    try {
      const payload = await importProjectFile({
        file,
        projectId,
        token: session?.token,
      })
      importProject({
        id: projectId,
        name: projectName,
        files: payload.files ?? {},
        entryFile: payload.entryFile || TARGET_FILE,
        kind: payload.kind,
        sourceName: payload.sourceName || file.name,
      })
      const result = {
        // 导入不是一次 Agent Run，没有 runId；导出 / 预览仍按 sandboxId 工作。
        runId: null,
        status: 'SUCCEEDED',
        sandboxId: payload.sandboxId,
        previewUrl: payload.previewUrl,
        commitHash: null,
        baselineCommit: null,
        retryCount: 0,
        turnCount: 0,
        commits: [],
        assistantMessage: `已导入 ${payload.sourceName || file.name}（${
          payload.kind === 'html' ? 'HTML' : 'React'
        }），可以继续在画布上圈选修改。`,
        generatedPatch: '',
        verifyResult: null,
        messages: [],
        error: null,
      }
      setRunStates((previous) => ({
        ...previous,
        [projectId]: {
          ...EMPTY_RUN,
          result,
          sandboxIds: payload.sandboxId ? [payload.sandboxId] : [],
        },
      }))
      focusPreview(projectId)
      const warnings = Array.isArray(payload.warnings) ? payload.warnings : []
      showToast(
        'success',
        `已导入 ${projectName}，沙盒已启动${
          warnings.length ? `（${warnings[0]}）` : ''
        }`,
      )
    } catch (cause) {
      showToast('error', `导入失败：${cause?.message || cause}`)
    } finally {
      setImporting(false)
    }
  }

  const gateIsActive = (projectId) => {
    const status = gateStates[projectId]?.status
    return status === 'AWAITING_CONTRACT' || status === 'RELEASING'
  }

  const handleAnnotationAdd = (
    projectId,
    {
      rect,
      target,
      addTarget,
      relativeRect,
      action: geometryAction,
      measured,
      intent: presetIntent = '',
      source: inspectSource = null,
    },
  ) => {
    // 开发者详情面板需要 file:line:column。Inspect 模式由 SDK 直接给出；
    // 圈选模式用入口源码 + data-source 标记反查行号，便于在 IDE 中跳转。
    const project = projects.find((item) => item.id === projectId)
    const anchored = inspectSource || addTarget || target
    let astPath = null
    if (inspectSource?.file) {
      astPath = `${inspectSource.file}:${inspectSource.line ?? 1}:${inspectSource.column ?? 1}`
    } else if (project && anchored?.marker) {
      const entryFile = projectEntryFile(project)
      const sourceCode =
        project.template === 'vibe-react' ? INITIAL_APP : projectSourceFile(project)
      astPath = `${entryFile}:${findTargetLine(sourceCode, anchored.marker)}:1`
    }

    patchRun(projectId, (previous) => {
      if (previous.annotations.length >= MAX_ANNOTATIONS) return previous
      const autoAction =
        geometryAction === 'ADD' || geometryAction === 'MODIFY'
          ? geometryAction
          : inferAnnotationAction(target, '')
      return {
        ...previous,
        annotations: [
          ...previous.annotations,
          {
            id: newId('ann'),
            rect,
            // 圈实际覆盖到的叶子节点（MODIFY 目标）。
            target,
            // 最近 data-source 容器（ADD 的插入父级）。
            addTarget,
            // 圈相对父容器的归一化坐标，直接进入 AnnotationEnvelope.relativeTarget。
            relativeRect,
            geometryAction: autoAction,
            measured,
            // 空白区域 / 圈远大于节点 → ADD；命中元素 → MODIFY，
            // 输入“添加”后自动切换，用户也可在浮层内手动覆盖。
            action: autoAction,
            actionSource: inspectSource ? 'inspect' : 'auto',
            // Inspect 模式会直接带上用户填写的意图与真实源码出处；
            // 圈选模式仍从空输入框开始。
            intent: presetIntent,
            inspectSource,
            astPath,
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
        annotation.id === annotationId
          ? {
              ...annotation,
              intent,
              // 自动模式随文案实时切换；手动选择过类型后尊重用户选择。
              action:
                annotation.actionSource === 'manual'
                  ? annotation.action
                  : inferAnnotationAction(
                      annotation.target,
                      intent,
                      annotation.geometryAction,
                    ),
            }
          : annotation,
      ),
    }))
  }

  const handleAnnotationActionChange = (projectId, annotationId, action) => {
    patchRun(projectId, (previous) => ({
      ...previous,
      annotations: previous.annotations.map((annotation) =>
        annotation.id === annotationId
          ? { ...annotation, action, actionSource: 'manual' }
          : annotation,
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
    if (!annotation) return

    // 执行中强行关闭：浮层已向用户确认过。先中止该标注的 route / dispatch /
    // SSE，再立刻从 UI 移除；后端 Run 用 cancel 尽力收尾，但所有迟到结果
    // 都会因 closedAnnotations + epoch 被丢弃，绝不会回写到已关闭的对话。
    if (run.submitting || inflightRef.current.has(projectId)) {
      markAnnotationClosed(projectId, annotationId)
      abortAnnotationScope(projectId, annotationId)
      patchRun(projectId, (previous) => ({
        ...previous,
        annotations: previous.annotations.filter(
          (item) => item.id !== annotationId,
        ),
      }))
      return
    }

    const baseCommitHash =
      annotation.baseCommitHash || run.result?.baselineCommit || null
    const sandboxId = run.result?.sandboxId || annotation.lastSandboxId
    const needsReset = Boolean(
      annotation.lastCommitHash && baseCommitHash && sandboxId,
    )
    if (!needsReset) {
      markAnnotationClosed(projectId, annotationId)
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
      markAnnotationClosed(projectId, annotationId)
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

  const projectSource = (project) => {
    // 内置模板始终以最新 INITIAL_APP 为准；导入项目使用真实入口文件。
    if (project.template === 'vibe-react') return INITIAL_APP
    return projectSourceFile(project)
  }

  /**
   * 构造 Supervisor 统一请求。
   *
   * 同一份 payload 先用于 /route 判定 UI 开关，再用于 /dispatch 真正启动
   * 子图；两个子图的选项分仓，避免 Architect 选项污染 UI Run。
   */
  const buildSupervisorPayload = (
    projectId,
    { message = '', annotations = [], scopeHint = 'auto' } = {},
  ) => {
    const project = projects.find((item) => item.id === projectId)
    const run = runStates[projectId] ?? EMPTY_RUN
    if (!project) return null
    const sourceCode = projectSource(project)
    const targetFile = projectEntryFile(project)
    const sandboxId = run.result?.sandboxId
    const files = { [targetFile]: sourceCode }
    const source = {
      sandboxId: sandboxId || undefined,
      baseRunId: run.result?.runId || undefined,
      baseCommitHash: run.result?.commitHash || undefined,
      ...(sandboxId ? {} : { files }),
    }
    // Architect 只接收自己面板的澄清 / 契约对话。绝不能把 run.annotations
    // 里的 UI Coder 聊天拼进来，否则代码指令会污染全局架构上下文。
    const architectHistory = (architectStates[projectId]?.messages ?? [])
      .map((item) => ({ role: item.role, content: item.content }))
      .filter((item) => item.content)
      .slice(-20)

    return {
      tenantId: user?.tenantId || 'tenant_local',
      projectId: project.id,
      projectName: project.name,
      message,
      annotations: annotations
        .filter((annotation) => annotation.intent?.trim())
        .map((annotation) =>
          buildAnnotation(annotation, sourceCode, targetFile, {
            ...(project.files ?? {}),
            [targetFile]: sourceCode,
          }),
        ),
      sandboxId: sandboxId || undefined,
      files,
      source,
      architectHistory,
      options: {
        scopeHint,
        ui: {
          llmProvider: modelProvider,
          mockScenario: 'none',
          maxRetries: 3,
          keepSandbox: true,
          requireManualApproval,
        },
        architect: {
          llmProvider: modelProvider,
          confidenceThreshold: 0.85,
          maxClarificationRounds: 5,
          ragProvider: 'markdown',
          ragTopK: 5,
        },
      },
    }
  }

  /** 请求已成功派发后，才清空标注输入并写入用户气泡，失败时保留草稿。 */
  const commitSentAnnotations = (projectId, sendEntries) => {
    const byId = new Map(sendEntries.map((entry) => [entry.id, entry]))
    patchRun(projectId, (previous) => ({
      ...previous,
      annotations: previous.annotations.map((annotation) => {
        const entry = byId.get(annotation.id)
        if (!entry) return annotation
        return {
          ...annotation,
          intent: '',
          chatHistory: [
            ...(annotation.chatHistory ?? []),
            ...(entry.userMessage ? [entry.userMessage] : []),
          ],
        }
      }),
    }))
  }

  /**
   * 订阅 Supervisor 已经启动的 UI Run。
   *
   * 关键：attach 模式只 GET SSE，不再 POST /runs/stream，避免创建第二个沙盒。
   */
  const attachUiDispatch = async (
    projectId,
    { runId, streamPath, annotationId, handoff, epoch },
  ) => {
    // 浮层在 dispatch 返回前已被用户关闭：不要订阅，也不要写任何状态；
    // 后端 Run 可能已经启动，尽力取消它。
    if (annotationId && isAnnotationClosed(projectId, annotationId)) {
      cancelRunSilently(runId)
      return
    }
    patchRun(projectId, (previous) => ({
      ...previous,
      submitting: true,
      pendingAnnotationId: annotationId,
      pendingAction: 'run',
      elapsed: 0,
      startedAt: Date.now(),
      error: null,
      runId,
      lastSeq: 0,
      timeline: [],
      patchPreview: '',
      clarification: null,
      approval: null,
      cancelRequested: false,
      targetAnnotationId: annotationId,
      supervisorHandoff: handoff,
      supervisorPhase: 'ui_running',
    }))
    focusPreview(projectId)
    await startRunStream(projectId, null, annotationId, {
      attach: true,
      runId,
      streamPath,
      epoch,
    })
  }

  /**
   * 统一入口：route → 检查 handoff / 可派发性 → dispatch。
   *
   * 既处理画布“发送 / 提交”，也处理工具栏“下一步”；两者都不再硬编码
   * UI / Architect 的启动接口。
   */
  const submitViaSupervisor = async (
    projectId,
    {
      message,
      annotations = [],
      annotationId = null,
      sendEntries = [],
      commitOnSuccess = false,
    },
  ) => {
    const project = projects.find((item) => item.id === projectId)
    const run = runStates[projectId] ?? EMPTY_RUN
    if (!project || run.submitting || inflightRef.current.has(projectId)) return
    if (run.clarification || run.approval) {
      showToast('error', '请先处理当前的澄清或审批，再发起新的请求')
      return
    }
    if (gateIsActive(projectId)) {
      showToast('info', '正在进行全局架构设计，请先确认契约')
      return
    }

    const payload = buildSupervisorPayload(projectId, { message, annotations })
    if (!payload) return

    // 每次提交都开启新的 epoch，并为本标注持有 AbortController。
    // 用户关闭浮层时 abort 会同时中断 route / dispatch 和后续 SSE。
    const epoch = bumpRunEpoch(projectId)
    const scopeKey = annotationId
      ? annotationScopeKey(projectId, annotationId)
      : null
    let scopeController = null
    if (scopeKey) {
      annotationAbortRef.current.get(scopeKey)?.abort()
      scopeController = new AbortController()
      annotationAbortRef.current.set(scopeKey, scopeController)
    }
    const signal = scopeController?.signal
    const isStale = () =>
      !workspaceMountedRef.current ||
      currentRunEpoch(projectId) !== epoch ||
      isAnnotationClosed(projectId, annotationId)
    const releaseInflight = () => {
      if (currentRunEpoch(projectId) === epoch) {
        inflightRef.current.delete(projectId)
      }
    }

    inflightRef.current.add(projectId)
    patchRun(projectId, (previous) => ({
      ...previous,
      submitting: true,
      pendingAnnotationId: annotationId,
      pendingAction: 'run',
      elapsed: 0,
      startedAt: Date.now(),
      error: null,
      lastInstruction: message,
      runId: null,
      lastSeq: 0,
      timeline: [],
      patchPreview: '',
      clarification: null,
      approval: null,
      cancelRequested: false,
      targetAnnotationId: annotationId,
      supervisorHandoff: null,
      supervisorPhase: 'analyzing',
      supervisorDecision: null,
    }))

    try {
      // 让“正在分析意图”至少可见一个呼吸节拍，避免本地路由过快导致
      // 用户只看到结果、看不到 Supervisor 的思考过程。
      const routeStartedAt = Date.now()
      const route = await routeSupervisor(payload, session?.token, signal)
      if (isStale()) return
      const minThinkingMs = 480
      const remaining = minThinkingMs - (Date.now() - routeStartedAt)
      if (remaining > 0) {
        await abortableDelay(remaining, signal)
      }
      if (isStale()) return
      const decision = route.decision ?? {}
      const handoffMeta = (route.handoffs ?? []).find(
        (item) => item.agentId === decision.target,
      )
      const decisionView = {
        handoff: decision.handoff,
        target: decision.target,
        taskType: decision.taskType,
        reason: decision.reason,
        confidence: decision.confidence,
        signals: decision.signals ?? [],
        strategy: decision.strategy,
        missingInputs: decision.missing_inputs ?? [],
        agentDisplayName: handoffMeta?.displayName ?? null,
      }

      // handoff_custom：路由已成功命中 Manifest Agent，但运行时尚未接入。
      // 此时不能 dispatch，也不应提示成错误；展示专属 Agent 卡片即可。
      if (decision.handoff === 'handoff_custom') {
        patchRun(projectId, {
          submitting: false,
          pendingAnnotationId: null,
          pendingAction: null,
          supervisorHandoff: 'handoff_custom',
          supervisorPhase: 'custom',
          supervisorDecision: decisionView,
        })
        releaseInflight()
        showToast(
          'info',
          `已匹配专属 Agent：${decisionView.agentDisplayName ?? decision.target}（本次演示仅完成路由）`,
        )
        return
      }

      if (decision.handoff === 'rejected' || !decision.dispatchable) {
        const missing = decision.missing_inputs ?? []
        patchRun(projectId, {
          submitting: false,
          pendingAnnotationId: null,
          pendingAction: null,
          supervisorHandoff: decision.handoff ?? 'rejected',
          supervisorPhase: 'rejected',
          supervisorDecision: decisionView,
        })
        releaseInflight()
        if (missing.includes('annotations')) {
          showToast('error', '请先在画布上圈选组件，再发送修改指令')
        } else {
          showToast(
            'error',
            decision.reason || '没有识别到明确意图，请补充标注或说明',
          )
        }
        return
      }

      const result = await dispatchSupervisor(payload, session?.token, signal)
      if (isStale()) {
        // dispatch 已经可能创建了子图 Run：尽力取消，避免后台继续占用沙盒。
        ;(result.dispatches ?? []).forEach((dispatch) =>
          cancelRunSilently(dispatch.runId),
        )
        return
      }
      const handoff = result.decision?.handoff ?? decision.handoff
      if (commitOnSuccess || handoff === 'handoff_ui' || handoff === 'handoff_both') {
        commitSentAnnotations(projectId, sendEntries)
      }

      if (handoff === 'handoff_ui') {
        const ui = (result.dispatches ?? []).find(
          (item) => item.agentId === 'ui_graph',
        )
        if (!ui) throw new Error('Supervisor 未返回 UI Run')
        patchRun(projectId, {
          supervisorDecision: decisionView,
          supervisorHandoff: handoff,
        })
        await attachUiDispatch(projectId, {
          runId: ui.runId,
          streamPath: ui.streamPath,
          annotationId,
          handoff,
          epoch,
        })
        return
      }

      if (handoff === 'handoff_architect' || handoff === 'handoff_both') {
        const architectDispatch = (result.dispatches ?? []).find(
          (item) => item.agentId === 'architect_graph',
        )
        if (!architectDispatch) {
          throw new Error('Supervisor 未返回 Architect Run')
        }
        architect.attach(projectId, {
          runId: architectDispatch.runId,
          streamPath: architectDispatch.streamPath,
        })
        patchRun(projectId, {
          submitting: false,
          pendingAnnotationId: null,
          pendingAction: null,
          error: null,
          supervisorHandoff: handoff,
          supervisorPhase: handoff === 'handoff_both' ? 'both_waiting' : 'architect_running',
          supervisorDecision: decisionView,
        })
        if (handoff === 'handoff_both') {
          const gate = result.gate
          if (!gate?.gateId) throw new Error('both 模式缺少门控信息')
          setGateStates((previous) => ({
            ...previous,
            [projectId]: {
              gateId: gate.gateId,
              status: gate.status,
              architectRunId: gate.architectRunId,
              uiRunId: gate.uiRunId ?? null,
              uiStreamPath: gate.uiStreamPath ?? null,
              uiAnnotationId: annotationId,
              releaseCondition: gate.releaseCondition,
              releasing: false,
              error: null,
            },
          }))
          showToast('info', '正在进行全局架构设计，请先确认契约')
        }
        releaseInflight()
        return
      }

      throw new Error(`暂不支持的 Handoff: ${handoff}`)
    } catch (cause) {
      // 用户主动关闭 / 新一轮提交接管：静默退出，绝不能把 AbortError 或
      // 过期请求渲染成“执行失败”。
      if (cause?.name === 'AbortError' || isStale()) return
      releaseInflight()
      const messageText = cause?.message || String(cause)
      patchRun(projectId, {
        submitting: false,
        pendingAnnotationId: null,
        pendingAction: null,
        error: `Supervisor 调度失败：${messageText}`,
        supervisorPhase: 'error',
      })
      showToast('error', `调度失败：${messageText}`)
    } finally {
      if (scopeKey) {
        // 若同一 scope 已被新一轮提交替换，不要误删新 controller。
        if (annotationAbortRef.current.get(scopeKey) === scopeController) {
          annotationAbortRef.current.delete(scopeKey)
        }
      }
    }
  }

  /**
   * 释放 both 门控。只有 Architect 契约 CONFIRMED 后服务端才会启动 UI；
   * 重复调用由后端幂等返回同一个 UI Run。
   */
  const releaseGateFor = async (projectId, gateId) => {
    if (!gateId || gateReleaseInflightRef.current.has(projectId)) return
    gateReleaseInflightRef.current.add(projectId)
    patchGate(projectId, { releasing: true, error: null })
    const gate = gateStates[projectId]
    try {
      const result = await releaseSupervisorGate(gateId, session?.token)
      const remoteGate = result.gate
      const ui = (result.dispatches ?? []).find(
        (item) => item.agentId === 'ui_graph',
      )
      patchGate(projectId, {
        status: remoteGate?.status ?? 'RELEASED',
        uiRunId: remoteGate?.uiRunId ?? ui?.runId ?? null,
        uiStreamPath: remoteGate?.uiStreamPath ?? ui?.streamPath ?? null,
        releasing: false,
        error: null,
      })
      if (!ui) {
        if (remoteGate?.status === 'RELEASED') {
          showToast('info', '门控已释放，UI Run 已由先前的请求启动')
        }
        return
      }
      const annotationId = gate?.uiAnnotationId ?? null
      showToast('success', '契约已确认，开始执行界面修改')
      await attachUiDispatch(projectId, {
        runId: ui.runId,
        streamPath: ui.streamPath,
        annotationId,
        handoff: 'handoff_both',
      })
    } catch (cause) {
      const messageText = cause?.message || String(cause)
      patchGate(projectId, { releasing: false, error: messageText })
      showToast('error', `释放门控失败：${messageText}`)
    } finally {
      gateReleaseInflightRef.current.delete(projectId)
    }
  }

  const handleReleaseGate = (projectId) => {
    const gateId = gateStates[projectId]?.gateId
    if (!gateId) return
    // 手动重试：允许再次自动尝试。
    gateAutoReleaseAttemptedRef.current.delete(gateId)
    void releaseGateFor(projectId, gateId)
  }

  // architect.contract.confirmed → 自动释放门控。每个 gate 只自动尝试一次，
  // 避免 409 未确认时 effect 反复触发。
  useEffect(() => {
    Object.entries(gateStates).forEach(([projectId, gate]) => {
      if (!gate?.gateId || gate.status !== 'AWAITING_CONTRACT') return
      if (gate.releasing) return
      const architectStatus = architectStates[projectId]?.status
      if (architectStatus !== 'CONFIRMED') return
      if (gateAutoReleaseAttemptedRef.current.has(gate.gateId)) return
      gateAutoReleaseAttemptedRef.current.add(gate.gateId)
      void releaseGateFor(projectId, gate.gateId)
    })
  }, [architectStates, gateStates])

  /**
   * 工具栏主 CTA：已有 Run 时恢复同一面板；否则也走 Supervisor，
   * 由后端决定是纯 Architect 还是 UI + Architect 的 both。
   */
  const handleStartArchitect = (projectId) => {
    const run = runStates[projectId] ?? EMPTY_RUN
    const annotations = run.annotations.filter((item) => item.intent.trim())
    const message = [
      '下一步：构建全栈方案',
      ...annotations.map((item) => item.intent.trim()),
    ].join('。')
    void submitViaSupervisor(projectId, {
      message,
      annotations,
      annotationId: null,
      sendEntries: [],
      commitOnSuccess: false,
    })
  }

  const handleOpenArchitect = (projectId) => {
    if (architectStates[projectId]?.runId) {
      architect.openPanel(projectId)
      return
    }
    handleStartArchitect(projectId)
  }

  /**
   * 启动 / 恢复 SSE 并持续把事件写入项目状态。
   *
   * HTTP 4xx（参数 / 模型配置）在流开始前返回，直接走 catch；流开始后的
   * 业务错误由 run.failed 事件表达，不抛异常。
   */
  const startRunStream = async (
    projectId,
    payload,
    annotationId,
    {
      replay = false,
      attach = false,
      streamPath = null,
      runId = null,
      afterSeq = 0,
      epoch = null,
    } = {},
  ) => {
    const operationEpoch = epoch ?? currentRunEpoch(projectId)
    const isStale = () =>
      !workspaceMountedRef.current ||
      currentRunEpoch(projectId) !== operationEpoch ||
      (annotationId && isAnnotationClosed(projectId, annotationId))
    if (isStale()) return

    const controller = new AbortController()
    streamControllersRef.current.get(projectId)?.abort()
    streamControllersRef.current.set(projectId, controller)

    try {
      await streamRun({
        payload,
        runId: runId || payload?.runId,
        replay,
        attach,
        streamPath,
        afterSeq,
        token: session?.token,
        signal: controller.signal,
        onEvent: (event) => {
          if (isStale()) return
          if (closedProjectsRef.current.has(projectId)) {
            // 用户关闭项目后 Run 仍会完成；终态到达时销毁后端新沙盒，
            // 避免前端不再持有 sandboxId 导致容器泄漏。
            if (
              event.type === 'run.succeeded' ||
              event.type === 'run.failed' ||
              event.type === 'run.cancelled' ||
              event.type === 'run.rejected'
            ) {
              void destroySandbox(event.payload?.sandboxId, session?.token)
            }
            return
          }
          patchRun(projectId, (previous) =>
            applyStreamEvent(previous, event, annotationId),
          )
        },
      })
    } catch (cause) {
      if (cause?.name === 'AbortError' || isStale()) return
      if (closedProjectsRef.current.has(projectId)) return
      const message = `事件流失败：${cause?.message || cause}`
      patchRun(projectId, (previous) => ({
        ...previous,
        submitting: false,
        pendingAnnotationId: null,
        pendingAction: null,
        error: message,
        annotations: previous.annotations.map((annotation) =>
          !annotationId || annotation.id === annotationId
            ? {
                ...annotation,
                chatHistory: appendAssistantMessage(annotation, null, message),
              }
            : annotation,
        ),
      }))
    } finally {
      // 只有当前 controller 仍是自己的才允许清理；强制关闭 / 新一轮提交
      // 可能已经替换了 Map 中的 controller，旧流不能误删新连接。
      if (streamControllersRef.current.get(projectId) === controller) {
        streamControllersRef.current.delete(projectId)
      }
      if (
        workspaceMountedRef.current &&
        currentRunEpoch(projectId) === operationEpoch
      ) {
        inflightRef.current.delete(projectId)
        // 终态 / 澄清 / 出错都已显式结束；这里只兜底清理 submitting。
        patchRun(projectId, (previous) =>
          previous.submitting
            ? {
                ...previous,
                submitting: false,
                pendingAnnotationId: null,
                pendingAction: null,
              }
            : previous,
        )
      }
    }
  }

  /**
   * 提交 HITL 答案：POST 202 后用 lastSeq 恢复 SSE，时间轴继续推进。
   */
  const handleClarificationAnswer = async (
    projectId,
    annotationId,
    clarification,
    answers,
    comment = '',
  ) => {
    const run = runStates[projectId] ?? EMPTY_RUN
    if (
      !run.runId ||
      !clarification?.clarificationId ||
      run.submitting ||
      inflightRef.current.has(projectId)
    ) {
      return
    }
    inflightRef.current.add(projectId)
    patchRun(projectId, {
      submitting: true,
      pendingAnnotationId: annotationId,
      pendingAction: 'run',
      clarification: null,
      error: null,
    })

    try {
      const response = await fetch(
        `/api/v1/runs/${encodeURIComponent(run.runId)}/clarifications/${encodeURIComponent(clarification.clarificationId)}/answers`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${session?.token ?? ''}`,
          },
          body: JSON.stringify({
            answers,
            // 主输入栏 / 自定义文本域里的补充说明随澄清一起提交；服务端
            // 把它作为开发者注释并入标注上下文，不会丢失。
            comment: String(comment || '').trim() || null,
            expectedVersion: clarification.round,
          }),
        },
      )
      if (!response.ok) {
        throw new Error(await extractError(response))
      }
      // 202 已把 clarification.answered 写入事件缓冲；从 lastSeq 重放即可
      // 接上同一时间轴，不重复执行 Run。
      await startRunStream(projectId, null, annotationId, {
        replay: true,
        runId: run.runId,
        afterSeq: run.lastSeq,
      })
    } catch (cause) {
      patchRun(projectId, {
        submitting: false,
        pendingAnnotationId: null,
        pendingAction: null,
        error: `提交澄清失败：${cause?.message || cause}`,
      })
      inflightRef.current.delete(projectId)
    }
  }

  /**
   * Diff 审批决策：POST 202 后用 lastSeq 重放事件流。
   *
   * 批准 → 服务端提交 Git checkpoint 并发送 run.succeeded；
   * 拒绝 → 服务端 git reset 回执行前 HEAD 并发送 run.rejected。
   * 决策本身不重建 Run，因此时间轴与审批卡片保持连续。
   */
  const handleApprovalDecision = async (projectId, decision, comment = '') => {
    const run = runStates[projectId] ?? EMPTY_RUN
    const approval = run.approval
    if (
      !run.runId ||
      !approval?.approvalId ||
      run.submitting ||
      inflightRef.current.has(projectId)
    ) {
      return
    }
    inflightRef.current.add(projectId)
    patchRun(projectId, {
      submitting: true,
      pendingAnnotationId: run.targetAnnotationId,
      pendingAction: 'run',
      error: null,
    })

    try {
      const response = await fetch(
        `/api/v1/runs/${encodeURIComponent(run.runId)}/approvals/${encodeURIComponent(approval.approvalId)}/decision`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${session?.token ?? ''}`,
          },
          body: JSON.stringify({ decision, comment: comment || undefined }),
        },
      )
      if (!response.ok) {
        throw new Error(await extractError(response))
      }
      // 202 已把 approval.answered 写入缓冲；从 lastSeq 继续消费终态。
      await startRunStream(projectId, null, run.targetAnnotationId, {
        replay: true,
        runId: run.runId,
        afterSeq: run.lastSeq,
      })
    } catch (cause) {
      patchRun(projectId, {
        submitting: false,
        pendingAnnotationId: null,
        pendingAction: null,
        error: `提交审批失败：${cause?.message || cause}`,
      })
      inflightRef.current.delete(projectId)
    }
  }

  /**
   * 协作式取消：停止后续节点，服务端回滚未提交的沙盒改动。
   *
   * 暂停中的 Run（等待澄清 / 审批）会立即产生 run.cancelled；执行中的
   * Run 进入 CANCELLING，终态仍需通过事件流送达。
   */
  const handleCancelRun = async (projectId) => {
    const run = runStates[projectId] ?? EMPTY_RUN
    // 只有“正在执行”或“等待审批”的 Run 可取消；终态 Run 不能再取消。
    // 注意不能判断 run.result：同一标注多轮对话会保留上一轮的预览结果。
    if (
      !run.runId ||
      run.cancelRequested ||
      (!run.submitting && !run.approval) ||
      inflightRef.current.has(projectId)
    ) {
      return
    }
    // 审批暂停时 run.submitting 已是 false，这里重新置为 true 以禁用按钮；
    // 若执行中则保持不变。
    patchRun(projectId, {
      submitting: true,
      pendingAnnotationId: run.pendingAnnotationId ?? run.targetAnnotationId,
      pendingAction: 'run',
      cancelRequested: true,
      error: null,
    })

    try {
      const response = await fetch(
        `/api/v1/runs/${encodeURIComponent(run.runId)}/cancel`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${session?.token ?? ''}`,
          },
          body: JSON.stringify({ reason: '用户在工作区取消' }),
        },
      )
      if (!response.ok) {
        throw new Error(await extractError(response))
      }
      await startRunStream(projectId, null, run.targetAnnotationId, {
        replay: true,
        runId: run.runId,
        afterSeq: run.lastSeq,
      })
    } catch (cause) {
      patchRun(projectId, {
        submitting: false,
        pendingAnnotationId: null,
        pendingAction: null,
        error: `取消执行失败：${cause?.message || cause}`,
      })
    }
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
      isAnnotationClosed(projectId, annotationId) ||
      run.submitting ||
      run.clarification ||
      // 审批未决时禁止启动新 Run：否则旧沙盒改动会被后续提交覆盖。
      run.approval ||
      inflightRef.current.has(projectId)
    ) {
      return
    }

    const userMessage = newMessage('user', instruction)
    // 单标注也带自己的 threadId；commit 在 Supervisor 成功派发后才执行，
    // 被 rejected / 调度失败时用户输入不会丢失。
    const sendEntry = {
      ...annotation,
      intent: instruction,
      userMessage,
    }
    await submitViaSupervisor(projectId, {
      message: instruction,
      annotations: [sendEntry],
      annotationId,
      sendEntries: [sendEntry],
      commitOnSuccess: true,
    })
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
      run.clarification ||
      run.approval ||
      inflightRef.current.has(projectId)
    ) {
      return
    }

    // 批量提交一次携带全部标注；每条指令写入各自圈的聊天历史。
    const sendEntries = annotations.map((annotation, index) => ({
      ...annotation,
      userMessage: newMessage('user', instructions[index]),
    }))
    const message = instructions.join(' / ')
    await submitViaSupervisor(projectId, {
      message,
      annotations: sendEntries,
      annotationId: null,
      sendEntries,
      commitOnSuccess: true,
    })
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
          // 项目作用域：后端据此定位 ThreadMemoryStore，避免跨项目命中
          // 同名 threadId 的聊天记忆。
          tenantId: user?.tenantId || 'tenant_local',
          projectId: project.id,
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
    streamControllersRef.current.get(projectId)?.abort()
    streamControllersRef.current.delete(projectId)
    // 关闭 Tab 时清理所有标注级 controller / 已关闭标记，避免 Map 泄漏。
    Array.from(annotationAbortRef.current.keys())
      .filter((key) => key.startsWith(`${projectId}:`))
      .forEach((key) => {
        annotationAbortRef.current.get(key)?.abort()
        annotationAbortRef.current.delete(key)
      })
    closedAnnotationsRef.current.delete(projectId)
    runEpochRef.current.delete(projectId)
    void architect.dispose(projectId)
    closeTab(projectId)
    setRunStates((previous) => {
      if (!(projectId in previous)) return previous
      const next = { ...previous }
      delete next[projectId]
      return next
    })
    setGateStates((previous) => {
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
      } else if (run.approval) {
        // 待审批：Tab 上用警示色提示需要人工处理，不当作成功或失败。
        map[projectId] = 'pending'
      } else if (run.error || run.result?.status === 'FAILED') {
        map[projectId] = 'error'
      } else if (run.result) {
        map[projectId] = isSuccessStatus(run.result.status) ? 'success' : 'error'
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
      <AppHeader
        busy={anyRunning}
        onOpenAccount={() => setAccountOpen(true)}
        onOpenSettings={() => setSettingsOpen(true)}
      />

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
            importing={importing}
            onOpenProject={openProject}
            onNewProject={createProject}
            onImportFile={handleImportFile}
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
                onAnnotationActionChange={(annotationId, action) =>
                  handleAnnotationActionChange(project.id, annotationId, action)
                }
                onAnnotationRemove={(annotationId) =>
                  handleAnnotationRemove(project.id, annotationId)
                }
                onAnnotationUnmount={(annotationId) =>
                  // 浮层卸载（关闭标注 / 错误边界降级 / 项目销毁）时，
                  // 主动中止该标注的异步请求，并丢弃迟到结果。
                  abortAnnotationScope(project.id, annotationId)
                }
                onAnnotationSend={(annotationId) =>
                  handleAnnotationSend(project.id, annotationId)
                }
                onClarificationAnswer={(
                  annotationId,
                  clarification,
                  answers,
                  comment,
                ) =>
                  handleClarificationAnswer(
                    project.id,
                    annotationId,
                    clarification,
                    answers,
                    comment,
                  )
                }
                onAnnotationRollback={(annotationId, message) =>
                  handleAnnotationRollback(project.id, annotationId, message)
                }
                onClearAnnotations={() => handleClearAnnotations(project.id)}
                onApprovalDecision={(decision, comment) =>
                  handleApprovalDecision(project.id, decision, comment)
                }
                onCancelRun={() => handleCancelRun(project.id)}
                onSubmit={() => handleSubmit(project.id)}
                onRetry={() => handleRetry(project.id)}
                canExport={Boolean(run.result?.sandboxId)}
                exporting={exportingProjectId === project.id}
                onExport={() => handleExport(project.id)}
                architectState={
                  architectStates[project.id] ?? EMPTY_ARCHITECT
                }
                supervisorGate={gateStates[project.id] ?? null}
                agentCatalog={agentCatalog}
                onRefreshAgents={loadAgentCatalog}
                previewFocusNonce={previewFocus[project.id] ?? 0}
                onArchitectOpen={() => handleOpenArchitect(project.id)}
                onArchitectStart={() => handleStartArchitect(project.id)}
                onArchitectClose={() => architect.closePanel(project.id)}
                onArchitectAnswer={(answers, comment) =>
                  architect.answer(project.id, answers, comment)
                }
                onArchitectDecide={(decision, comment) =>
                  architect.decide(project.id, decision, comment)
                }
                onArchitectCancel={() => architect.cancel(project.id)}
                onReleaseGate={() => handleReleaseGate(project.id)}
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

      {toast && (
        <div
          key={toast.id}
          className={`workspace-toast supervisor-toast is-${toast.kind}`}
          role={toast.kind === 'error' ? 'alert' : 'status'}
          data-toast={toast.kind}
        >
          <span aria-hidden="true">
            {toast.kind === 'success'
              ? '✓'
              : toast.kind === 'error'
                ? '⚠'
                : '◈'}
          </span>
          {toast.text}
        </div>
      )}

      <AccountDialog open={accountOpen} onClose={() => setAccountOpen(false)} />
      <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </div>
  )
}
