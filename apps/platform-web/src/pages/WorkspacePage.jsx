import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import AppHeader from '../components/AppHeader.jsx'
import AccountDialog from '../components/AccountDialog.jsx'
import SettingsDialog from '../components/SettingsDialog.jsx'
import ProjectSwitcher from '../components/ProjectSwitcher.jsx'
import ProjectWorkspace from '../components/ProjectWorkspace.jsx'
import SnapshotWorkspace from '../components/SnapshotWorkspace.jsx'
import VirtualList from '../components/VirtualList.jsx'
import WorkspaceTabs from '../components/WorkspaceTabs.jsx'
import { useAuth } from '../auth/AuthContext.jsx'
import { useSettings } from '../settings/SettingsContext.jsx'
import { useWorkspace } from '../workspace/useWorkspace.js'
import {
  EMPTY_ARCHITECT,
  countPendingKeyQuestions,
  useArchitect,
} from '../workspace/useArchitect.js'
import { createFrameBatcher, streamRun } from '../workspace/runStream.js'
import { importProjectFile } from '../workspace/importClient.js'
import {
  CREATE_NEW,
  MODIFY_EXISTING,
  isCreateAction,
  isModifyAction,
  normalizeAction,
} from '../workspace/annotationActions.js'
import {
  createSnapshot,
  fetchSnapshotDiff,
  listSnapshots,
  restoreSnapshot,
} from '../workspace/snapshotClient.js'
import {
  dispatchSupervisor,
  fetchSupervisorAgents,
  releaseSupervisorGate,
  routeSupervisor,
} from '../workspace/supervisorClient.js'
import { INITIAL_APP } from '../mockProject.js'

const TARGET_FILE = 'src/App.jsx'
const MAX_ANNOTATIONS = 3
// 直接拖拽的静默同步超时：Coder 已在后端 30s 熔断，这里留出构建与
// HMR 的余量；超过后前端必须回滚视觉位置，不能无限等待。
const MOVE_SYNC_TIMEOUT_MS = 60000
// 松手后只提交一次最终位移；300ms 足以合并连续微调手势。
const MOVE_SYNC_DEBOUNCE_MS = 300
const MOVE_BATCH_LIMIT = 3

/** Promise + 超时竞速；超时返回 `{ timedOut: true }`，不拒绝。 */
function raceWithTimeout(promise, ms) {
  let timer = 0
  const timeout = new Promise((resolve) => {
    timer = window.setTimeout(() => resolve({ timedOut: true }), ms)
  })
  return Promise.race([promise, timeout]).finally(() => {
    if (timer) window.clearTimeout(timer)
  })
}

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
  // 执行耗时由 RuntimePanel 本地计时，不再写入页面级状态。
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
  // handoff_agent / handoff_custom / rejected。
  supervisorHandoff: null,
  // 可视化“AI 思考”的轻量状态：analyzing → decided → dispatching →
  // both_waiting / ui_running / custom / rejected / error。
  supervisorPhase: null,
  // route 返回的完整决策 + handoff 展示名，用于顶部轨迹与专属 Agent 卡片。
  supervisorDecision: null,
  // 保存门控：生成 / 拖拽 / 改样式后置 true，保存快照成功后置 false。
  // dirty=true 时【下一步：构建全栈方案】置灰，必须先保存。
  dirty: false,
  // CREATE_NEW 的拖拽定位层：组件 marker + 父容器 selector + 用户落点。
  // 结构：{ annotationId, selector, parentSelector, x, y, width, height,
  //        confirmed, error }
  placement: null,
  // 保存快照后缓存的项目全局摘要 / 本次快照改动说明，随 Architect 请求发送。
  projectSummary: '',
  latestChanges: '',
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
        // 旧会话里是 ADD / MODIFY，统一升级为 CREATE_NEW / MODIFY_EXISTING。
        ...(annotation.action
          ? { action: normalizeAction(annotation.action) }
          : {}),
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
        dirty: Boolean(saved.dirty),
        // 旧快照里的拖拽定位状态不再恢复：预览区只展示最终代码。
        placement: null,
        projectSummary: saved.projectSummary ?? '',
        latestChanges: saved.latestChanges ?? '',
      }
    })
    return restored
  } catch {
    return {}
  }
}

/**
 * 只序列化“刷新后仍有价值”的字段。
 *
 * 注意不要把整个 run 直接 JSON.stringify：timeline / patchPreview /
 * chatHistory 在大项目里可能上百 KB，之前每次 runStates 变化都同步写
 * sessionStorage，会把主线程卡住。这里与 loadRunStates 的字段一一对应。
 */
function buildRunSnapshot(runStates) {
  const snapshot = {}
  Object.entries(runStates).forEach(([projectId, run]) => {
    if (!run.result && !run.error && run.sandboxIds.length === 0) return
    snapshot[projectId] = {
      annotations: run.annotations,
      result: run.result,
      error: run.error,
      lastInstruction: run.lastInstruction,
      sandboxIds: run.sandboxIds,
      // 保存门控必须跨刷新保留，否则刷新后按钮误解锁。
      dirty: run.dirty,
      placement: null,
      projectSummary: run.projectSummary,
      latestChanges: run.latestChanges,
    }
  })
  return snapshot
}

// Run 状态落盘延迟：SSE / 计时器更新远高于持久化需要，合并写入即可。
const RUN_PERSIST_DELAY_MS = 800

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
 * 快照时间展示：本地化到分钟，避免抽屉里出现冗长的 ISO 字符串。
 */
function formatSnapshotTime(value) {
  if (!value) return '未知时间'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** 同一 commit 只保留一个虚拟快照标签。 */
const snapshotTabId = (projectId, commitHash) => `snapshot:${projectId}:${commitHash}`

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
// 卡片本身，只要指令是在新增内容，就按 CREATE_NEW 处理，避免模型去改卡片颜色。
const ADD_INTENT_PATTERN = /添加|新增|新建|插入|加一个|加个|(?:^|\s)add\s/i

/**
 * 自动推断操作类型：
 *  - 圈中没有命中任何 data-source（空白区域）→ CREATE_NEW；
 *  - 圈的展示面积远大于命中的节点 → CREATE_NEW（CanvasStage 几何判定）；
 *  - 指令包含新增语义 → CREATE_NEW；
 *  - 其余命中已有元素 → MODIFY_EXISTING。
 * 用户手动点选浮层内的“修改 / 新增”后 actionSource=manual，不再自动覆盖。
 */
function inferAnnotationAction(target, intent, geometryAction) {
  if (ADD_INTENT_PATTERN.test(intent ?? '')) return CREATE_NEW
  if (geometryAction) return normalizeAction(geometryAction)
  return target ? MODIFY_EXISTING : CREATE_NEW
}

/**
 * 将画布上的一个归一化选区组装为 AnnotationEnvelope。
 * worldTarget 保留归一化坐标；target 记录圈选命中的元素，帮助模型把
 * “改成蓝色 / 文字改为提交 / 放大一倍”绑定到正确的组件。
 */
function buildAnnotation(entry, source, targetFile = TARGET_FILE, files = null) {
  const rect = entry.rect
  const instruction = entry.intent.trim()
  // 直接拖拽使用独立的 MOVE 语义：目标、累计位移都来自 SDK 的几何事实，
  // 不需要 LLM 解析自然语言。其余动作仍走旧别名归一化。
  const rawAction = String(entry.action || '').toUpperCase()
  const isMove = rawAction === 'MOVE'
  // 旧 localStorage 里的标注没有 action：按指令与几何判定即时推断。
  // 有 action 时统一归一化，旧 ADD / MODIFY 也在这里升级。
  const action = isMove
    ? 'MOVE'
    : entry.action
      ? normalizeAction(entry.action)
      : inferAnnotationAction(entry.target, instruction, entry.geometryAction)
  // CREATE_NEW 的目标必须是“最近 data-source 父容器”，由 CanvasStage 在
  // 圈选时计算；只命中叶子时不能把新增 JSX 塞进 button / p / h1。
  // MODIFY_EXISTING 没有命中元素时才回退到默认 CTA，保持旧行为。
  const addTarget = entry.addTarget ?? null
  const target = isCreateAction(action)
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
  const moveOffset = isMove ? entry.moveOffset ?? null : null
  const acceptance = isMove
    ? [
        `把${target?.label || '目标组件'}移动到累计偏移 (${Math.round(Number(moveOffset?.dx) || 0)}, ${Math.round(Number(moveOffset?.dy) || 0)}) px`,
        '只允许追加 transform: translate3d(dx,dy,0)，禁止修改 width / height / left / top',
        '其余布局、文案与交互保持不变',
      ]
    : isCreateAction(action)
      ? [
          `在${target?.label || '圈选区域'}（${target?.selector || '父容器'}）内部新增组件：${instruction}`,
          '只生成组件主体与 data-vibe-created 审计标记，不写死绝对坐标；按父容器自然文档流插入',
          '原有 DOM 结构、布局、文案与交互保持不变',
        ]
      : [
          `按指令修改${target?.label || '目标组件'}：${instruction}`,
          '若为移动指令，只允许追加 transform: translate3d(dx,dy,0)，禁止修改宽高',
          '其余布局、文案与交互保持不变',
        ]
  // relativeTarget 只在 CREATE_NEW 时有意义：作为插入父容器与相对顺序
  // 参考，不再驱动绝对定位。
  const relative = isCreateAction(action) ? entry.relativeRect : null
  return {
    version: 1,
    annotationId: entry.id,
    // visual-draw = 精准点击选择；drag = 圈选 / 直接拖拽。后端都按 UI
    // 标注处理，保留 type 便于审计与后续差异化策略。
    type: !isMove && precise ? 'visual-draw' : 'drag',
    action,
    worldTarget: {
      x: Number(rect.x.toFixed(6)),
      y: Number(rect.y.toFixed(6)),
      width: Number(rect.width.toFixed(6)),
      height: Number(rect.height.toFixed(6)),
      coordinateSpace: 'normalized',
    },
    relativeTarget:
      isCreateAction(action)
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
      // 直接拖拽：dx / dy 是相对组件原始位置的累计偏移（不是本次手势
      // 增量）。后端 _try_move_tool 直接读取它调用 move_component，
      // 不再让模型从自然语言里猜方向与距离。
      ...(isMove && moveOffset
        ? {
            // 顶层 dx / dy 是协议约定字段；move 子对象携带选择器与 token，
            // 方便后端确定性 Tool 与前端回滚精确定位。
            dx: Math.round(Number(moveOffset.dx) || 0),
            dy: Math.round(Number(moveOffset.dy) || 0),
            move: {
              dx: Math.round(Number(moveOffset.dx) || 0),
              dy: Math.round(Number(moveOffset.dy) || 0),
              selector: target?.selector ?? null,
              token: entry.moveToken ?? null,
            },
          }
        : {}),
      // 旧版拖拽坐标已停用：位置调整必须走 move_component Skill 的
      // transform 规则，模型不得自行写死 left/top 或宽高。
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

  // 轻量 token 流：只取最后 2000 字展示，避免长代码把 React 状态拖慢。
  // node.finished 会清空 message，最终仍以结构化 summary 为准。
  if (event.type === 'token') {
    const text = String(payload.text || '')
    if (!text) return timeline
    let index = -1
    timeline.forEach((item, itemIndex) => {
      if (item.node === payload.node && item.status === 'running') index = itemIndex
    })
    if (index < 0) return timeline
    return timeline.map((item, itemIndex) =>
      itemIndex === index
        ? {
            ...item,
            message: `${String(item.message || '')}${text}`.slice(-2000),
            streaming: true,
          }
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

/** 合并“本次保存前”的改动说明，去重并限制在 4000 字内。 */
function mergeLatestChanges(previous, next) {
  const before = String(previous || '').trim()
  const after = String(next || '').trim()
  if (!after) return before
  if (!before) return after.slice(-4000)
  if (before.includes(after)) return before
  return `${before}\n${after}`.slice(-4000)
}

/**
 * 终态事件落地：更新标注的 Git 坐标、追加助手消息、记录沙盒。
 * annotationId 为空表示批量提交，所有参与标注都收到同一条 run 级回复。
 *
 * 保存门控：
 *  - 成功 Run 一律把 dirty 置 true，等待用户保存快照；
 *  - 新组件不再自动进入拖拽定位；位置调整统一走自然语言指令 +
 *    Agent `move_component` Skill（只写 transform，不改宽高）。
 */
function finalizeFromResult(run, result, annotationId) {
  const success = isSuccessStatus(result.status)
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

  const changeText =
    result.assistantMessage ||
    (success
      ? `完成一次界面修改${
          result.commitHash ? `（${result.commitHash.slice(0, 7)}）` : ''
        }`
      : '')
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
    // 任何成功的界面改动都进入“未保存”状态。
    dirty: success ? true : run.dirty,
    // 上一次保存后已经有了改动：累加；已保存干净时替换为本次改动。
    latestChanges: success
      ? run.dirty
        ? mergeLatestChanges(run.latestChanges, changeText)
        : changeText
      : run.latestChanges,
    // 旧版 CREATE_NEW 拖拽定位层彻底停用；预览区只渲染最终代码。
    placement: null,
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
    updateProject,
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
  // 快照回滚后递增：RuntimePanel 用它作为 iframe key，强制重载最新构建。
  const [previewRefresh, setPreviewRefresh] = useState({})
  // 每次切换到某个项目 Tab 时递增：即使沙盒 URL 完全相同（例如两个项目
  // 共用同一预览端口 / HMR 缓存），也强制 iframe 以新 key 重建，确保
  // 画布与预览展示的是当前 Tab 对应的项目数据。
  const [tabActivation, setTabActivation] = useState({})
  // 右侧“版本历史”抽屉。
  const [snapshotDrawer, setSnapshotDrawer] = useState({
    open: false,
    projectId: null,
    loading: false,
    error: null,
    snapshots: [],
    headCommit: null,
    restoring: null,
    confirmHash: null,
    confirmError: null,
  })
  // “保存快照”Modal。
  const [snapshotSave, setSnapshotSave] = useState({
    open: false,
    projectId: null,
    message: '',
    saving: false,
    error: null,
  })
  // 虚拟只读快照标签。
  const [snapshotTabs, setSnapshotTabs] = useState([])
  const [activeSnapshotId, setActiveSnapshotId] = useState(null)
  const snapshotDrawerRequestRef = useRef(0)
  const snapshotDiffAbortRef = useRef(new Map())
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
  // 每个项目只处理一次 fullstack.completed：刷新预览 / 聚焦 / Toast 不能因
  // Architect 状态的其他字段更新而重复触发。
  const fullstackHandledRef = useRef(new Map())
  // 前端骨架初始化只允许每个契约触发一次；失败重试时由 handler 清除。
  const frontendBootstrapRef = useRef(new Map())
  const frontendBootstrapFailureRef = useRef(new Map())
  // 页面级挂载标记：所有异步回调写状态前必须检查，避免卸载后 setState。
  const workspaceMountedRef = useRef(true)
  // 每个项目一个单调递增 epoch；新 Run / 强制关闭都会递增，旧请求的
  // 迟到响应通过 epoch 判定为过期，绝不能覆盖新状态。
  const runEpochRef = useRef(new Map())
  // 已关闭的标注：即使后端任务仍在执行，也丢弃该 thread 的后续事件。
  const closedAnnotationsRef = useRef(new Map())
  // 按 projectId:annotationId 维度持有 AbortController，关闭浮层即中止。
  const annotationAbortRef = useRef(new Map())
  // 直接拖拽：每个项目独立防抖队列 / 已提交累计位移 / 回滚与提交信号。
  // pending: projectId -> Map<selector, {payload, base, next, token, ...}>
  const dragPendingRef = useRef(new Map())
  const dragTimersRef = useRef(new Map())
  // 已成功写入代码的累计位移，下一次拖拽在此基础上叠加。
  const dragCommittedRef = useRef(new Map())
  const dragSeqRef = useRef(0)
  const [dragRollbacks, setDragRollbacks] = useState({})
  const [dragCommits, setDragCommits] = useState({})
  // 静默同步期间仍然保持拖拽模式：submitting=true 只是后台在写代码，
  // 不应该让用户重新点一次【拖拽】才能继续调整布局。
  const [dragSyncStates, setDragSyncStates] = useState({})
  // 等待 submitting=false 真正渲染后再清除同步态，避免中间帧退出拖拽模式。
  // 用事件驱动（runStates effect）替代 120ms 轮询。
  const dragSyncReleaseRef = useRef(new Set())
  const [dragReleaseNonce, setDragReleaseNonce] = useState(0)
  // 异步回调里需要读取最新 run 状态（state 本身是渲染快照）。
  const runStatesRef = useRef(runStates)
  // 会话快照的延迟写盘句柄；卸载 / pagehide 时立即 flush。
  const runPersistTimerRef = useRef(0)

  useEffect(() => {
    runStatesRef.current = runStates
  }, [runStates])

  // 拖拽同步结束：等本项目的 submitting 真正落回 false 后再解除。
  // runStates 变化会触发本 effect，不需要任何轮询。
  useEffect(() => {
    const ready = [...dragSyncReleaseRef.current].filter(
      (projectId) => !(runStates[projectId] ?? EMPTY_RUN).submitting,
    )
    if (ready.length === 0) return
    ready.forEach((projectId) => dragSyncReleaseRef.current.delete(projectId))
    setDragSyncStates((previous) => {
      if (!ready.some((projectId) => previous[projectId])) return previous
      const next = { ...previous }
      ready.forEach((projectId) => delete next[projectId])
      return next
    })
  }, [runStates, dragReleaseNonce])

  const persistRunStates = useCallback(() => {
    try {
      window.sessionStorage.setItem(
        RUN_STORAGE_KEY,
        JSON.stringify(buildRunSnapshot(runStatesRef.current)),
      )
    } catch {
      // 快照过大 / 隐私模式：忽略，当前会话仍可正常使用。
    }
  }, [])

  // 页面卸载 / 切换项目时关闭所有 SSE，避免后台继续占用连接。
  useEffect(() => {
    workspaceMountedRef.current = true
    return () => {
      workspaceMountedRef.current = false
      streamControllersRef.current.forEach((controller) => controller.abort())
      streamControllersRef.current.clear()
      annotationAbortRef.current.forEach((controller) => controller.abort())
      annotationAbortRef.current.clear()
      snapshotDiffAbortRef.current.forEach((controller) => controller.abort())
      snapshotDiffAbortRef.current.clear()
      // 未触发的拖拽防抖必须取消，避免卸载后仍发起静默 Run。
      dragTimersRef.current.forEach((timer) => window.clearTimeout(timer))
      dragTimersRef.current.clear()
      dragPendingRef.current.clear()
    }
  }, [])

  // 刷新页面后恢复已完成的预览；只持久化会话内结果，不写入 localStorage。
  // 合并 800ms 内的多次状态变化，避免 SSE 批次触发同步 JSON 序列化。
  useEffect(() => {
    if (runPersistTimerRef.current) {
      window.clearTimeout(runPersistTimerRef.current)
    }
    runPersistTimerRef.current = window.setTimeout(() => {
      runPersistTimerRef.current = 0
      persistRunStates()
    }, RUN_PERSIST_DELAY_MS)
    return () => {
      if (runPersistTimerRef.current) {
        window.clearTimeout(runPersistTimerRef.current)
        runPersistTimerRef.current = 0
      }
    }
  }, [runStates, persistRunStates])

  // 关闭页面 / 切到后台时同步落盘一次，避免刷新前最后 800ms 的结果丢失。
  useEffect(() => {
    const flush = () => {
      if (runPersistTimerRef.current) {
        window.clearTimeout(runPersistTimerRef.current)
        runPersistTimerRef.current = 0
      }
      persistRunStates()
    }
    window.addEventListener('pagehide', flush)
    return () => window.removeEventListener('pagehide', flush)
  }, [persistRunStates])

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

  // 注意：执行耗时不再写回页面级 runStates。
  // 之前每 400ms 一次全量 setRunStates，会连带重渲染所有 Tab 并触发
  // sessionStorage 序列化；现在由 RuntimePanel 在本地按 1s 计时。
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
   * 全栈生成完成：更新预览 URL 并强制刷新 iframe。
   *
   * fullstack.completed 的 previewUrl 可能带新的端口 / cache-busting 参数；
   * 同时把新沙盒 ID 写入 run.sandboxIds，保证关闭项目时能正确销毁容器。
   */
  useEffect(() => {
    Object.entries(architectStates).forEach(([projectId, architectState]) => {
      const fullstack = architectState?.fullstack
      if (!fullstack?.runId || fullstack.status !== 'SUCCEEDED') return
      if (fullstackHandledRef.current.get(projectId) === fullstack.runId) return
      fullstackHandledRef.current.set(projectId, fullstack.runId)
      patchRun(projectId, (previous) => {
        const sandboxId = fullstack.sandboxId || previous.result?.sandboxId
        // 纯 Architect 流程此前没有 run.result；全栈生成第一次创建沙盒，
        // 必须补一份最小结果，预览面板才会切换到新服务。
        const nextResult = previous.result
          ? {
              ...previous.result,
              sandboxId: sandboxId || previous.result.sandboxId,
              previewUrl: fullstack.previewUrl || previous.result.previewUrl,
            }
          : sandboxId || fullstack.previewUrl
            ? {
                runId: fullstack.runId,
                status: 'SUCCEEDED',
                sandboxId: sandboxId || null,
                previewUrl: fullstack.previewUrl || '',
                commitHash: null,
                baselineCommit: null,
                retryCount: 0,
                turnCount: 0,
                commits: [],
                assistantMessage: '全栈代码已生成，前后端服务已启动。',
                generatedPatch: '',
                verifyResult: null,
                messages: [],
                error: null,
              }
            : previous.result
        return {
          ...previous,
          result: nextResult,
          sandboxIds:
            sandboxId && !previous.sandboxIds.includes(sandboxId)
              ? [...previous.sandboxIds, sandboxId]
              : previous.sandboxIds,
        }
      })
      setPreviewRefresh((previous) => ({
        ...previous,
        [projectId]: (previous[projectId] ?? 0) + 1,
      }))

      if (fullstack.mode === 'frontend') {
        // 对话确认后的初始骨架：把真实 App.jsx 写回项目，并把画布从
        // 空白 / 骨架屏切换为 live。后续标注修改都基于这份源码。
        const entrySource = fullstack.result?.frontendEntry || ''
        if (!entrySource) {
          console.warn('[frontend-bootstrap] 完成事件缺少 frontendEntry', {
            projectId,
            runId: fullstack.runId,
          })
        }
        updateProject(projectId, (project) => ({
          ...project,
          bootstrap: 'ready',
          canvasMode: 'live',
          generated: true,
          entryFile: 'src/App.jsx',
          files: entrySource
            ? { ...project.files, 'src/App.jsx': entrySource }
            : project.files,
        }))
        showToast('success', '初始前端页面已生成，画布已切换为实时预览')
      } else {
        focusPreview(projectId)
        showToast('success', '全栈代码已生成，沙盒前后端已重启')
      }
    })
  }, [architectStates, updateProject])

  /**
   * 导入项目：上传 zip / tar.gz 全栈包 / 截图 -> Python 解包并启动沙盒 -> 新建 Tab。
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
    const baseName = file.name.replace(/\.(tar\.gz|tgz|zip|png|jpe?g)$/i, '').trim()
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
        services: payload.services,
        dataSourceCount: payload.dataSourceCount,
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
          payload.kind === 'html'
            ? 'HTML'
            : payload.kind === 'fullstack'
              ? `全栈 · ${(payload.services || ['vite', 'express']).join(' + ')}`
              : 'React'
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
      const autoAction = geometryAction
        ? normalizeAction(geometryAction)
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
          ? {
              ...annotation,
              action: normalizeAction(action),
              actionSource: 'manual',
            }
          : annotation,
      ),
    }))
  }

  /**
   * CREATE_NEW 拖拽确认：把用户松手时的父子相对坐标作为一次
   * MODIFY_EXISTING Patch 交给 Coder 回写代码。
   *
   * 这里不新开专用接口：复用 Supervisor → UI Graph → Coder 全链路，
   * 沙盒 HMR 刷新后用户就能看到自己拖出来的位置。
   */
  const handlePlacementConfirm = (projectId, placement, position) => {
    if (!placement || !position) return
    const run = runStates[projectId] ?? EMPTY_RUN
    const original = run.annotations.find(
      (annotation) => annotation.id === placement.annotationId,
    )
    const x = Number(position.x)
    const y = Number(position.y)
    if (!Number.isFinite(x) || !Number.isFinite(y)) return

    const selector =
      placement.selector || `[data-vibe-created="${placement.annotationId}"]`
    const entry = {
      ...(original ?? {}),
      id: placement.annotationId,
      rect: original?.rect ?? { x: 0, y: 0, width: 0, height: 0 },
      // 回写坐标的目标是“新增组件的 marker 本身”，不是父容器。
      target: {
        marker: '',
        selector,
        label: '新增组件',
        role: 'element',
        component: 'App',
        tag: 'div',
      },
      addTarget: null,
      action: MODIFY_EXISTING,
      actionSource: 'manual',
      intent: `将新增组件定位到 left:${Math.round(x)}px / top:${Math.round(y)}px`,
      position: {
        x,
        y,
        width: position.width ?? null,
        height: position.height ?? null,
        selector,
        parentSelector: placement.parentSelector || null,
      },
    }

    // 拖拽已经是“未保存修改”；先落 UI 状态，再走异步 Patch。
    patchRun(projectId, (previous) => ({
      ...previous,
      dirty: true,
      placement: null,
      latestChanges: mergeLatestChanges(
        previous.dirty ? previous.latestChanges : '',
        `拖拽定位新增组件：left:${Math.round(x)}px; top:${Math.round(y)}px`,
      ),
      annotations: previous.annotations.map((annotation) =>
        annotation.id === placement.annotationId
          ? { ...annotation, placementPending: false, placementDone: true }
          : annotation,
      ),
    }))

    void submitViaSupervisor(projectId, {
      message: entry.intent,
      annotations: [entry],
      annotationId: null,
      sendEntries: [],
      commitOnSuccess: false,
    })
  }

  /** 放弃拖拽：不写代码，但记录已处理，避免每次 Run 都重新浮出定位层。 */
  const handlePlacementDismiss = (projectId, placement) => {
    if (!placement) return
    patchRun(projectId, (previous) => ({
      ...previous,
      placement: null,
      annotations: previous.annotations.map((annotation) =>
        annotation.id === placement.annotationId
          ? { ...annotation, placementPending: false, placementDone: true }
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
    // 新建项目在 Architect 契约确认前没有源码；生成后必须使用真实
    // App.jsx，否则后续局部修改会把 Vibe Studio Mock 模板写回沙盒。
    if (project.bootstrap === 'pending' || project.generated) {
      return projectSourceFile(project)
    }
    // 演示项目 / 旧内置模板始终以最新 INITIAL_APP 为准。
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
    {
      message = '',
      annotations = [],
      scopeHint = 'auto',
      targetAgent = null,
      requireManualApproval: approvalOverride,
    } = {},
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
      // 全栈澄清上下文：项目全局摘要 + 本次快照改动。Architect 据此
      // 复用已确认的技术选型，不再重复询问。
      projectSummary: run.projectSummary || '',
      latestChanges: run.latestChanges || '',
      options: {
        scopeHint,
        ...(targetAgent ? { targetAgent } : {}),
        ui: {
          llmProvider: modelProvider,
          mockScenario: 'none',
          maxRetries: 3,
          keepSandbox: true,
          requireManualApproval:
            approvalOverride === undefined
              ? requireManualApproval
              : approvalOverride,
        },
        architect: {
          llmProvider: modelProvider,
          confidenceThreshold: 0.85,
          maxClarificationRounds: 10,
          ragProvider: 'hybrid',
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
    {
      runId,
      streamPath,
      annotationId,
      handoff,
      epoch,
      // 静默拖拽同步不能把用户从画布强行切到预览：拖拽模式要保持上下文。
      switchToPreview = true,
    },
  ) => {
    // 浮层在 dispatch 返回前已被用户关闭：不要订阅，也不要写任何状态；
    // 后端 Run 可能已经启动，尽力取消它。
    if (annotationId && isAnnotationClosed(projectId, annotationId)) {
      cancelRunSilently(runId)
      return null
    }
    patchRun(projectId, (previous) => ({
      ...previous,
      submitting: true,
      pendingAnnotationId: annotationId,
      pendingAction: 'run',
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
    if (switchToPreview) focusPreview(projectId)
    return await startRunStream(projectId, null, annotationId, {
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
      // 直接拖拽：显式指定 UI Agent、跳过人工审批，并把结果返回给调用方。
      scopeHint = 'auto',
      targetAgent = null,
      requireManualApproval: approvalOverride,
      // 静默拖拽同步传 false：成功后仍停留在画布，不切到预览。
      switchToPreview = true,
    },
  ) => {
    const project = projects.find((item) => item.id === projectId)
    const run = runStates[projectId] ?? EMPTY_RUN
    if (!project || run.submitting || inflightRef.current.has(projectId)) {
      return { ok: false, reason: 'busy' }
    }
    if (run.clarification || run.approval) {
      showToast('error', '请先处理当前的澄清或审批，再发起新的请求')
      return { ok: false, reason: 'paused' }
    }
    if (gateIsActive(projectId)) {
      showToast('info', '正在进行全局架构设计，请先确认契约')
      return { ok: false, reason: 'gate' }
    }

    const payload = buildSupervisorPayload(projectId, {
      message,
      annotations,
      scopeHint,
      targetAgent,
      requireManualApproval: approvalOverride,
    })
    if (!payload) return { ok: false, reason: 'payload' }

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
      if (isStale()) return { ok: false, reason: 'stale' }
      const minThinkingMs = 480
      const remaining = minThinkingMs - (Date.now() - routeStartedAt)
      if (remaining > 0) {
        await abortableDelay(remaining, signal)
      }
      if (isStale()) return { ok: false, reason: 'stale' }
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
        return { ok: false, reason: 'custom', decision: decisionView }
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
        return { ok: false, reason: 'rejected', decision: decisionView }
      }

      const result = await dispatchSupervisor(payload, session?.token, signal)
      if (isStale()) {
        // dispatch 已经可能创建了子图 Run：尽力取消，避免后台继续占用沙盒。
        ;(result.dispatches ?? []).forEach((dispatch) =>
          cancelRunSilently(dispatch.runId),
        )
        return { ok: false, reason: 'stale' }
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
        const terminal = await attachUiDispatch(projectId, {
          runId: ui.runId,
          streamPath: ui.streamPath,
          annotationId,
          handoff,
          epoch,
          switchToPreview,
        })
        return {
          ok: terminal?.type === 'run.succeeded',
          reason: terminal?.type || 'no-terminal',
          event: terminal,
          decision: decisionView,
        }
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
        return { ok: false, reason: handoff, decision: decisionView }
      }

      // handoff_agent：backend_coder / dba_agent 已激活，后端启动
      // FullstackGraph 的 backend / dba 子图；前端挂载同一 Run 的事件流，
      // 展示真实生成进度，而不是只显示“已路由”。
      if (handoff === 'handoff_agent') {
        const expertDispatch = (result.dispatches ?? [])[0]
        if (!expertDispatch) {
          throw new Error('Supervisor 未返回专家子图 Run')
        }
        const expertMode =
          expertDispatch.agentId === 'dba_agent' ? 'dba' : 'backend'
        architect.attachFullstack(projectId, {
          runId: expertDispatch.runId,
          mode: expertMode,
        })
        patchRun(projectId, {
          submitting: false,
          pendingAnnotationId: null,
          pendingAction: null,
          error: null,
          supervisorHandoff: handoff,
          supervisorPhase: 'agent_running',
          supervisorDecision: decisionView,
        })
        releaseInflight()
        showToast(
          'info',
          `已派发给${decisionView.agentDisplayName ?? decision.target}，正在生成代码`,
        )
        return { ok: true, reason: handoff, decision: decisionView }
      }

      throw new Error(`暂不支持的 Handoff: ${handoff}`)
    } catch (cause) {
      // 用户主动关闭 / 新一轮提交接管：静默退出，绝不能把 AbortError 或
      // 过期请求渲染成“执行失败”。
      if (cause?.name === 'AbortError' || isStale()) {
        return { ok: false, reason: 'aborted' }
      }
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
      return { ok: false, reason: 'error', message: messageText }
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
   *
   * @param {object} [options]
   * @param {boolean} [options.awaitUi=true] 是否等待 UI Run 事件流结束。
   *   全栈生成场景只需要“门控已释放”即可继续写 server 文件，不必等 UI
   *   修改跑完，因此传 false 可避免阻塞生成链路。
   */
  const releaseGateFor = async (projectId, gateId, { awaitUi = true } = {}) => {
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
      const uiDispatch = attachUiDispatch(projectId, {
        runId: ui.runId,
        streamPath: ui.streamPath,
        annotationId,
        handoff: 'handoff_both',
      })
      if (awaitUi) {
        await uiDispatch
      } else {
        // 后台继续消费 UI 事件；失败只记录，不影响全栈生成。
        void uiDispatch.catch((cause) => {
          console.warn('[supervisor] 后台 UI Run 事件流失败', cause)
        })
      }
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
  const handleStartArchitect = (projectId, goal = '') => {
    const project = projects.find((item) => item.id === projectId)
    const run = runStates[projectId] ?? EMPTY_RUN
    // 保存门控：未保存的生成 / 拖拽 / 样式修改绝不能进入全栈澄清。
    if (run.dirty) {
      showToast('info', '请先保存当前修改')
      return
    }

    // 新建项目：纯自然语言驱动，不再要求先有画布标注。
    // 直接把用户目标作为首条 user 消息交给 ArchitectGraph。
    const needsConversationFirst =
      project?.bootstrap === 'pending' || project?.bootstrap === 'failed'
    if (needsConversationFirst || goal.trim()) {
      const text = goal.trim()
      if (!text) {
        // 没有目标时只打开面板，让用户在 IDLE 表单里输入。
        architect.openPanel(projectId)
        return
      }
      const history = (architectStates[projectId]?.messages ?? [])
        .map((item) => ({ role: item.role, content: item.content }))
        .filter((item) => item.content)
        .slice(-20)
      architect.start(projectId, {
        runId: newId('arc'),
        tenantId: user?.tenantId || 'tenant_local',
        projectId,
        projectName: project?.name || '未命名项目',
        // 新项目没有源码；Architect 仅依据自然语言目标推演架构。
        source: run.result?.sandboxId
          ? { sandboxId: run.result.sandboxId, files: {} }
          : { files: {} },
        uiHistory: [...history, { role: 'user', content: text }],
        options: { llmProvider: modelProvider },
      })
      return
    }

    // 旧项目 / 演示项目：保留 Supervisor 统一入口，兼容既有画布标注流程。
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
    const project = projects.find((item) => item.id === projectId)
    if ((runStates[projectId] ?? EMPTY_RUN).dirty) {
      showToast('info', '请先保存当前修改')
      return
    }
    if (project?.bootstrap === 'pending' || project?.bootstrap === 'failed') {
      // 空白项目：只打开对话面板，等待用户输入目标后再启动 Run。
      architect.openPanel(projectId)
      return
    }
    if (architectStates[projectId]?.runId) {
      architect.openPanel(projectId)
      return
    }
    handleStartArchitect(projectId)
  }

  /** 架构澄清失败后重新开始：清空旧 Run，回到 IDLE 目标输入框。 */
  const handleRestartArchitect = (projectId) => {
    const project = projects.find((item) => item.id === projectId)
    if (project?.bootstrap === 'failed') {
      // 重新设计架构视为放弃旧契约，画布回到空白等待状态。
      updateProject(projectId, { bootstrap: 'pending', canvasMode: 'blank' })
    }
    architect.reset(projectId)
  }

  /**
   * 主流程仍有必答问题时，拦截“跳过对话直接生成”。
   *
   * Banner 只是提醒，用户仍可能从其它入口直接点生成；这里做最后一道
   * 前端门禁：打开架构面板并提示剩余数量，绝不带着半成品架构进入生成。
   */
  const blockGenerateUntilAnswered = (projectId) => {
    const pending = countPendingKeyQuestions(architectStates[projectId])
    if (pending <= 0) return false
    architect.openPanel(projectId)
    showToast('info', `请先回答关键问题（还剩 ${pending} 个）`)
    return true
  }

  /**
   * 契约确认 -> 全栈代码生成。
   *
   * 复用当前项目的沙盒；导入 / 纯前端项目没有 sandboxId 时，把当前入口
   * 源码作为 source.files 兜底，服务端会先创建一个沙盒再写入代码。
   */
  const handleFullstackGenerate = async (projectId) => {
    if (blockGenerateUntilAnswered(projectId)) return
    const project = projects.find((item) => item.id === projectId)
    const architectState = architectStates[projectId]
    const contract = architectState?.contract
    const run = runStates[projectId] ?? EMPTY_RUN
    const sandboxId = run.result?.sandboxId
    const gate = gateStates[projectId]
    const apiRouteCount = Array.isArray(contract?.apiRoutes)
      ? contract.apiRoutes.length
      : 0
    const hasDdl =
      typeof contract?.databaseDdl === 'string' &&
      contract.databaseDdl.trim().length > 0

    // 点击入口的完整门控快照：按钮“点不动”时，先看这一行日志。
    const diagnostics = {
      projectId,
      hasProject: Boolean(project),
      architectStatus: architectState?.status ?? null,
      hasContract: Boolean(contract),
      contractId: contract?.contractId ?? null,
      contractStatus: contract?.status ?? null,
      appShape: contract?.appShape ?? null,
      apiRouteCount,
      hasDdl,
      sandboxId: sandboxId ?? null,
      fullstackStatus: architectState?.fullstack?.status ?? 'IDLE',
      gateId: gate?.gateId ?? null,
      gateStatus: gate?.status ?? null,
    }
    console.info('[fullstack] generate clicked', diagnostics)

    if (!project || !contract?.contractId) {
      console.warn('[fullstack] aborted: contract missing', diagnostics)
      showToast('error', '契约尚未确认，无法生成全栈代码')
      return
    }
    if (contract.status && contract.status !== 'CONFIRMED') {
      console.warn('[fullstack] aborted: contract not confirmed', diagnostics)
      showToast('error', `契约状态为 ${contract.status}，确认后才能生成全栈代码`)
      return
    }
    if (contract.appShape !== 'full_stack_system') {
      console.warn('[fullstack] aborted: app shape is not fullstack', diagnostics)
      showToast('info', '当前契约形态不需要生成后端代码')
      return
    }
    const missing = [
      apiRouteCount === 0 ? 'REST 接口（apiRoutes）' : null,
      !hasDdl ? '数据库 DDL（databaseDdl）' : null,
    ].filter(Boolean)
    if (missing.length > 0) {
      console.warn('[fullstack] aborted: contract incomplete', {
        ...diagnostics,
        missing,
      })
      showToast(
        'error',
        `契约缺少${missing.join(' 与 ')}，无法生成后端；请修订契约后重试`,
      )
      return
    }

    // both 门控：先释放 UI 锁，再启动全栈生成。releaseGateFor 只等待
    // release 接口返回，不阻塞 UI Run 的事件流，避免生成被长时间卡住。
    if (
      gate?.gateId &&
      (gate.status === 'AWAITING_CONTRACT' || gate.status === 'RELEASING')
    ) {
      console.info('[fullstack] releasing supervisor gate first', diagnostics)
      await releaseGateFor(projectId, gate.gateId, { awaitUi: false })
    }

    // 有沙盒时不需要重复上传源码；没有沙盒时至少携带入口文件。
    const files = sandboxId
      ? {}
      : { [projectEntryFile(project)]: projectSource(project) }
    // 显式传入本次渲染确认过的契约，避免 statesRef 尚未同步时静默返回。
    const started = architect.generateFullstack(projectId, {
      tenantId: user?.tenantId || 'tenant_local',
      sandboxId,
      files,
      contract,
      contractId: contract.contractId,
    })
    console.info('[fullstack] generate dispatched', { ...diagnostics, started })
    if (started) {
      showToast('info', '已启动全栈代码生成，正在写入沙盒…')
    } else {
      console.warn('[fullstack] generateFullstack returned false', diagnostics)
      showToast('info', '全栈代码正在生成中，请稍候')
    }
  }

  /**
   * 契约确认后的前端初始化。
   *
   * 只运行 FrontendCoder / BuildVerifier / SandboxRestart，把契约中的
   * frontendRoutes / pageLayout / components 渲染为真实 React 骨架。
   * 新项目没有源码和沙盒，服务端会用内置 React/Vite 模板新建沙盒。
   */
  const handleFrontendBootstrap = (projectId, { manual = false } = {}) => {
    // 手动重试也必须等必答问题解决；自动触发只在 CONFIRMED 后发生。
    if (manual && blockGenerateUntilAnswered(projectId)) return
    const project = projects.find((item) => item.id === projectId)
    const architectState = architectStates[projectId]
    const contract = architectState?.contract
    const fullstack = architectState?.fullstack
    const contractId = contract?.contractId

    if (!project || !contractId) {
      showToast('error', '架构契约缺失，无法初始化前端页面')
      return
    }
    if (contract.status && contract.status !== 'CONFIRMED') {
      showToast('error', '架构契约尚未确认，无法初始化前端页面')
      return
    }
    if (contract.appShape === 'script_automation') {
      // 脚本形态没有 UI；契约确认后直接结束初始化，不创建前端沙盒。
      updateProject(projectId, {
        bootstrap: 'ready',
        canvasMode: 'blank',
        generated: false,
      })
      showToast('info', '脚本类项目不包含前端页面，已跳过页面初始化')
      return
    }
    const missing = [
      !Array.isArray(contract.frontendRoutes) ||
      contract.frontendRoutes.length === 0
        ? '页面路由（frontendRoutes）'
        : null,
      !Array.isArray(contract.components) || contract.components.length === 0
        ? '组件列表（components）'
        : null,
    ].filter(Boolean)
    if (missing.length > 0) {
      updateProject(projectId, { bootstrap: 'failed' })
      showToast('error', `契约缺少${missing.join(' 与 ')}，无法初始化前端`)
      return
    }
    if (
      fullstack?.starting ||
      fullstack?.status === 'RUNNING'
    ) {
      if (manual) showToast('info', '前端页面正在生成中，请稍候')
      return
    }

    const run = runStates[projectId] ?? EMPTY_RUN
    frontendBootstrapFailureRef.current.delete(projectId)
    updateProject(projectId, {
      bootstrap: 'bootstrapping',
      canvasMode: 'bootstrapping',
    })
    const started = architect.generateFullstack(projectId, {
      tenantId: user?.tenantId || 'tenant_local',
      sandboxId: run.result?.sandboxId,
      // 新项目 files 为空：服务端使用镜像内置的 React/Vite 模板建沙盒。
      files: {},
      contract,
      contractId,
      mode: 'frontend',
      options: { maxOutputChars: 20000 },
    })
    if (!started) {
      updateProject(projectId, { bootstrap: 'failed' })
      showToast('error', '前端初始化启动失败，请重试')
      return
    }
    if (manual) showToast('info', '正在根据契约生成初始前端页面…')
  }

  // 契约一旦 CONFIRMED，新建项目自动触发前端骨架生成。
  // 每个 contractId 只触发一次，避免 Architect 状态更新造成重复 Run。
  useEffect(() => {
    Object.entries(architectStates).forEach(([projectId, state]) => {
      if (state?.status !== 'CONFIRMED') return
      const project = projects.find((item) => item.id === projectId)
      if (!project || project.bootstrap !== 'pending') return
      const key = state.contract?.contractId || state.runId
      if (!key) return
      if (frontendBootstrapRef.current.get(projectId) === key) return
      frontendBootstrapRef.current.set(projectId, key)
      handleFrontendBootstrap(projectId)
    })
  }, [architectStates, projects])

  // 前端初始化失败：把画布置为失败态，展示重试入口（不重新跑架构对话）。
  useEffect(() => {
    Object.entries(architectStates).forEach(([projectId, state]) => {
      const fullstack = state?.fullstack
      if (!fullstack?.runId) return
      if (fullstack.status !== 'FAILED' || fullstack.mode !== 'frontend') return
      const project = projects.find((item) => item.id === projectId)
      if (!project || project.bootstrap !== 'bootstrapping') return
      if (frontendBootstrapFailureRef.current.get(projectId) === fullstack.runId)
        return
      frontendBootstrapFailureRef.current.set(projectId, fullstack.runId)
      updateProject(projectId, { bootstrap: 'failed' })
      showToast(
        'error',
        `初始前端页面生成失败：${fullstack.error || '请重试'}`,
      )
    })
  }, [architectStates, projects, updateProject])

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
    // 记录终态事件，供“静默拖拽同步”判断成功 / 失败；旧调用方忽略返回值。
    let terminalEvent = null

    // 同一时间窗内到达的多个 SSE 事件合并为**一次** runStates 更新：
    // 既减少状态对象拷贝，也保证 React 每批只渲染一次工作区。
    const emitEvent = createFrameBatcher((events) => {
      if (isStale()) return
      const applicable = []
      for (const event of events) {
        if (
          event.type === 'run.succeeded' ||
          event.type === 'run.failed' ||
          event.type === 'run.cancelled' ||
          event.type === 'run.rejected'
        ) {
          terminalEvent = event
        }
        if (closedProjectsRef.current.has(projectId)) {
          if (
            event.type === 'run.succeeded' ||
            event.type === 'run.failed' ||
            event.type === 'run.cancelled' ||
            event.type === 'run.rejected'
          ) {
            void destroySandbox(event.payload?.sandboxId, session?.token)
          }
          continue
        }
        applicable.push(event)
      }
      if (applicable.length === 0) return
      patchRun(projectId, (previous) =>
        applicable.reduce(
          (run, event) => applyStreamEvent(run, event, annotationId),
          previous,
        ),
      )
    })

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
        onEvent: emitEvent,
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
      // 流结束前把最后一批事件落进状态，避免 rAF 尚未执行就被清理。
      emitEvent.flush()
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
      // 供静默拖拽同步判断结果；常规调用方无需消费。
      return terminalEvent
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

  /**
   * 画布拖拽移动的最终出口：把像素位移翻译成自然语言指令，交给
   * Supervisor 识别为 ui_move，再调用标准 Tool `move_component`。
   *
   * 拖拽本身只负责“表达意图”，不会直接改写 DOM / style；真正的代码
   * 变更仍走 Agent + Patch + 构建校验，避免前后端状态分叉。
   */
  const handleAnnotationMove = async (projectId, annotationId, dx, dy) => {
    const project = projects.find((item) => item.id === projectId)
    const run = runStates[projectId] ?? EMPTY_RUN
    const annotation = run.annotations.find((item) => item.id === annotationId)
    if (
      !project ||
      !annotation ||
      run.submitting ||
      run.clarification ||
      run.approval ||
      inflightRef.current.has(projectId)
    ) {
      return
    }
    const round = (value) => Math.round(Number(value) || 0)
    const moveX = round(dx)
    const moveY = round(dy)
    // 小于 4px 视为手抖；避免生成毫无意义的 Agent Run。
    if (Math.abs(moveX) < 4 && Math.abs(moveY) < 4) return

    const parts = []
    if (moveX) parts.push(`${moveX > 0 ? '向右' : '向左'} ${Math.abs(moveX)}px`)
    if (moveY) parts.push(`${moveY > 0 ? '向下' : '向上'} ${Math.abs(moveY)}px`)
    const label = annotation.target?.label || annotation.label || '组件'
    const instruction = `把「${label}」${parts.join('，')}移动（只改位置，不要改宽高）`

    const userMessage = newMessage('user', instruction)
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

  /**
   * 直接拖拽的最终出口（业界标准的「直接操作 + 后台静默同步」）。
   *
   * SDK 只负责手势与临时 translate，这里负责：
   *   1. 计算相对组件原始位置的累计偏移（同一组件多次拖拽可叠加）；
   *   2. 立即 Toast 告知已接管，500ms 防抖只提交最后一次位置；
   *   3. 以标准 AnnotationEnvelope（action=MOVE, payload.move）静默提交，
   *      不占用聊天输入框，也不要求用户再点发送；
   *   4. 终态失败 / 超时由 dragRollbacks 还原 iframe 内的临时位移。
   */
  const triggerDragRollback = (projectId, items, message) => {
    if (!items?.length) return
    setDragRollbacks((previous) => ({
      ...previous,
      [projectId]: {
        nonce: (previous[projectId]?.nonce ?? 0) + 1,
        items,
      },
    }))
    showToast('error', message || '代码同步失败，位置已还原')
  }

  const moveOffsetText = (dx, dy) => {
    const parts = []
    if (dx) parts.push(`${dx > 0 ? '向右' : '向左'} ${Math.abs(dx)}px`)
    if (dy) parts.push(`${dy > 0 ? '向下' : '向上'} ${Math.abs(dy)}px`)
    return parts.join('、') || '原位'
  }

  const dragRect = (payload) => {
    const rect = payload?.rect
    if (!rect) return { x: 0, y: 0, width: 0.1, height: 0.1 }
    const clamp01 = (value) => Math.min(1, Math.max(0, Number(value) || 0))
    return {
      x: clamp01(rect.x / 1440),
      y: clamp01(rect.y / 900),
      width: Math.min(1, Math.max(0.001, Number(rect.width) / 1440 || 0.1)),
      height: Math.min(1, Math.max(0.001, Number(rect.height) / 900 || 0.1)),
    }
  }

  const buildMoveEntry = (project, item) => {
    const source = item.payload.source || {}
    const file = source.file || projectEntryFile(project)
    const selector =
      item.selector ||
      (source.file
        ? `[data-source="${source.file}:${source.line || 1}:${
            source.column || 1
          }"]`
        : '')
    const label = source.component || source.tag || '组件'
    dragSeqRef.current += 1
    return {
      id: `move_${Date.now().toString(36)}_${dragSeqRef.current}`,
      action: 'MOVE',
      type: 'drag',
      intent: `把「${label}」${moveOffsetText(
        Math.round(item.next.x),
        Math.round(item.next.y),
      )}移动（只改位置，不要改宽高）`,
      rect: dragRect(item.payload),
      target: {
        marker: source.marker || '',
        selector,
        label,
        role: source.tag || 'element',
        component: source.component || 'App',
        tag: source.tag || 'div',
      },
      inspectSource: {
        file,
        line: Number(source.line) || 1,
        column: Number(source.column) || 1,
        component: source.component || 'App',
        sourceKind: source.sourceKind || 'jsx',
        marker: source.marker || '',
      },
      moveOffset: { dx: Math.round(item.next.x), dy: Math.round(item.next.y) },
      moveToken: item.token,
      createdAt: new Date().toISOString(),
    }
  }

  /**
   * 等 runStates 真正落回 submitting=false 后再解除“静默同步”标记。
   *
   * 由 runStates effect 事件驱动；此处只登记 pending，不再 setInterval
   * / setTimeout 轮询。若当前快照已经是 false，nonce 会立即唤醒 effect。
   */
  const releaseDragSync = (projectId) => {
    dragSyncReleaseRef.current.add(projectId)
    setDragReleaseNonce((previous) => previous + 1)
  }

  /**
   * 失败 / 超时必须强制解除同步态。
   *
   * 正常成功路径要等 submitting 落回 false，避免中间帧退出拖拽模式；
   * 但超时后后端 Run 可能已经失去响应，若继续等待 submitting 会让
   * “代码同步中...”永久挂住，反而阻塞用户下一次拖拽。
   */
  const clearDragSync = (projectId) => {
    dragSyncReleaseRef.current.delete(projectId)
    setDragSyncStates((previous) => {
      if (!previous[projectId]) return previous
      const next = { ...previous }
      delete next[projectId]
      return next
    })
  }

  const flushComponentDrags = async (projectId) => {
    const pending = dragPendingRef.current.get(projectId)
    dragPendingRef.current.delete(projectId)
    if (!pending || pending.size === 0) return
    const project = projects.find((item) => item.id === projectId)
    if (!project) return

    const all = [...pending.values()]
    // 一次 Run 最多 3 个标注；超出的旧拖拽没有提交，必须还原。
    const overflow = all.slice(0, Math.max(0, all.length - MOVE_BATCH_LIMIT))
    const batch = all.slice(-MOVE_BATCH_LIMIT)
    overflow.forEach((item) => {
      dragCommittedRef.current.set(
        `${projectId}::${item.selector}`,
        item.base,
      )
    })
    if (overflow.length) {
      triggerDragRollback(
        projectId,
        overflow.map((item) => ({
          token: item.token,
          selector: item.selector,
        })),
        '一次最多同步 3 个组件，其余位置已还原',
      )
    }
    if (batch.length === 0) return

    const entries = batch.map((item) => buildMoveEntry(project, item))
    const message = entries.map((entry) => entry.intent).join(' / ')
    setDragSyncStates((previous) => ({
      ...previous,
      [projectId]: true,
    }))
    const submission = submitViaSupervisor(projectId, {
      message,
      annotations: entries,
      scopeHint: 'ui',
      // 拖拽是确定性的几何事实，直接交给 UI Agent，不再经过意图猜测。
      targetAgent: 'ui_graph',
      // 静默同步不能让人工审批打断；失败由前端回滚。
      requireManualApproval: false,
      // 不抢焦点：用户还在画布上拖拽，不能被切到预览面板。
      switchToPreview: false,
    })
    const outcome = await raceWithTimeout(submission, MOVE_SYNC_TIMEOUT_MS)
    // 必须等 submitting=false 真正渲染后再解除，避免中间帧退出拖拽模式。
    releaseDragSync(projectId)
    const restore = () => {
      batch.forEach((item) => {
        dragCommittedRef.current.set(
          `${projectId}::${item.selector}`,
          item.base,
        )
      })
    }
    const rollbackItems = batch.map((item) => ({
      token: item.token,
      selector: item.selector,
    }))

    if (outcome?.timedOut) {
      // 超时后 Run 可能仍在构建：尽力取消，避免稍后 HMR 把已回滚的位置
      // 再次改掉。
      const runId = runStatesRef.current?.[projectId]?.runId
      if (runId) cancelRunSilently(runId)
      restore()
      triggerDragRollback(projectId, rollbackItems, '代码同步超时，位置已还原')
      clearDragSync(projectId)
      return
    }
    if (outcome?.ok) {
      setDragCommits((previous) => ({
        ...previous,
        [projectId]: {
          nonce: (previous[projectId]?.nonce ?? 0) + 1,
          items: batch.map((item) => ({
            token: item.token,
            selector: item.selector,
          })),
        },
      }))
      showToast('success', '位置已同步到代码')
      return
    }
    restore()
    triggerDragRollback(projectId, rollbackItems)
    clearDragSync(projectId)
  }

  /** SDK 松手回调：先乐观记录，再 300ms 防抖提交最后一次位置。 */
  const handleComponentDragEnd = (projectId, payload) => {
    const project = projects.find((item) => item.id === projectId)
    const run = runStates[projectId] ?? EMPTY_RUN
    if (!project || !payload?.selector) return
    // 没有沙盒就没有可写入的代码，直接还原并提示。
    if (!run.result?.sandboxId) {
      triggerDragRollback(
        projectId,
        [{ token: payload.token, selector: payload.selector }],
        '当前页面尚未生成代码，无法同步位置',
      )
      return
    }
    const busy =
      run.submitting ||
      Boolean(run.clarification) ||
      Boolean(run.approval) ||
      inflightRef.current.has(projectId) ||
      gateIsActive(projectId)
    if (busy) {
      triggerDragRollback(
        projectId,
        [{ token: payload.token, selector: payload.selector }],
        '当前有任务执行中，位置未同步',
      )
      return
    }
    const dx = Math.round(Number(payload.dx) || 0)
    const dy = Math.round(Number(payload.dy) || 0)
    if (Math.abs(dx) < 2 && Math.abs(dy) < 2) return

    const selector = payload.selector
    const key = `${projectId}::${selector}`
    const base = dragCommittedRef.current.get(key) || { x: 0, y: 0 }
    const next = { x: base.x + dx, y: base.y + dy }
    dragCommittedRef.current.set(key, next)

    const pending =
      dragPendingRef.current.get(projectId) || new Map()
    pending.set(selector, { payload, selector, base, next, token: payload.token })
    dragPendingRef.current.set(projectId, pending)
    showToast('info', '位置已更新，Agent 正在同步代码...')

    const existing = dragTimersRef.current.get(projectId)
    if (existing) window.clearTimeout(existing)
    const timer = window.setTimeout(() => {
      dragTimersRef.current.delete(projectId)
      void flushComponentDrags(projectId)
    }, MOVE_SYNC_DEBOUNCE_MS)
    dragTimersRef.current.set(projectId, timer)
  }

  /** 用户取消拖拽（pointercancel）：SDK 已自行还原，无需提交。 */
  const handleComponentDragCancel = () => {
    // 取消手势不产生代码变更；保留已排队的其他组件拖拽。
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
   * 导出当前项目的 tar.gz。
   *
   * @param {string} projectId
   * @param {{sandboxId?: string, runId?: string}} [target]
   *   Architect 面板会显式传入全栈生成得到的沙盒；未传时回退到最近一次
   *   Agent Run 的结果，兼容顶部工具栏的原有入口。
   *
   * 路由策略：
   *  - 显式 sandboxId（Architect 全栈生成）直接走沙盒导出，不依赖 BFF
   *    内存里的 runId 映射；
   *  - 有普通 runId 时优先 /runs/{runId}/export 并带上 sandboxId；
   *  - 旧会话两者缺失时提示先执行 Agent。
   */
  const exportProject = async (projectId, target = {}) => {
    const run = runStates[projectId]
    const sandboxId = target.sandboxId || run?.result?.sandboxId
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
    const runId = target.runId || run?.result?.runId
    // sandboxId 显式带上：BFF 重启后进程内 runId 映射会丢失，前端仍能导出。
    const url = target.sandboxId
      ? `/api/v1/sandboxes/${encodeURIComponent(sandboxId)}/export`
      : runId
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

  const handleExport = (projectId) => exportProject(projectId)

  /**
   * Architect 成功卡片的一键导出：直接使用全栈 Run 的沙盒，避免等待
   * fullstack.completed 同步到 runStates 的时序差。
   */
  const handleArchitectExport = (projectId, target = {}) =>
    exportProject(projectId, target ?? {})

  /**
   * 预览沙盒失联后，用最近一次指令重新执行。
   * 仍复用原画布标注；没有历史指令（旧会话）时由 UI 引导用户手动重发。
   */
  const handleRetry = (projectId) => {
    const run = runStates[projectId]
    if (!run?.lastInstruction || run.annotations.length === 0) return
    void handleSubmit(projectId)
  }

  // ------------------------------------------------------------------
  // 项目快照（沙盒 Git checkpoint）
  // ------------------------------------------------------------------
  const sandboxIdOf = (projectId) =>
    runStatesRef.current[projectId]?.result?.sandboxId || null

  const showSnapshotToast = (kind, text) => {
    setToast({ id: Date.now(), kind, text })
  }

  /** 拉取版本历史；requestId 丢弃过期响应，避免快速切换项目时串数据。 */
  const refreshSnapshotList = async (projectId, sandboxId) => {
    const requestId = snapshotDrawerRequestRef.current + 1
    snapshotDrawerRequestRef.current = requestId
    setSnapshotDrawer((previous) =>
      previous.projectId === projectId
        ? { ...previous, loading: true, error: null }
        : previous,
    )
    try {
      const payload = await listSnapshots(sandboxId, session?.token)
      if (
        !workspaceMountedRef.current ||
        snapshotDrawerRequestRef.current !== requestId
      ) {
        return
      }
      setSnapshotDrawer((previous) =>
        previous.projectId === projectId
          ? {
              ...previous,
              loading: false,
              error: null,
              snapshots: Array.isArray(payload?.snapshots) ? payload.snapshots : [],
              headCommit: payload?.headCommit ?? null,
            }
          : previous,
      )
    } catch (cause) {
      if (
        !workspaceMountedRef.current ||
        snapshotDrawerRequestRef.current !== requestId
      ) {
        return
      }
      setSnapshotDrawer((previous) =>
        previous.projectId === projectId
          ? { ...previous, loading: false, error: cause?.message || String(cause) }
          : previous,
      )
    }
  }

  const openSnapshotDrawer = () => {
    if (!activeTabId) return
    const sandboxId = sandboxIdOf(activeTabId)
    setSnapshotDrawer({
      open: true,
      projectId: activeTabId,
      loading: Boolean(sandboxId),
      error: sandboxId
        ? null
        : '当前项目还没有沙盒，请先执行一次 Agent 生成页面。',
      snapshots: [],
      headCommit: null,
      restoring: null,
      confirmHash: null,
      confirmError: null,
    })
    if (sandboxId) void refreshSnapshotList(activeTabId, sandboxId)
  }

  const closeSnapshotDrawer = () => {
    snapshotDrawerRequestRef.current += 1
    setSnapshotDrawer((previous) => ({
      ...previous,
      open: false,
      loading: false,
      restoring: null,
      confirmHash: null,
      confirmError: null,
    }))
  }

  const openSnapshotSave = (projectId) => {
    if (!sandboxIdOf(projectId)) {
      showSnapshotToast('error', '请先执行一次 Agent 生成沙盒，再保存快照。')
      return
    }
    // 自动带一条可读备注，用户只改不改都可以直接保存；保存后自动提交
    // Git checkpoint 并刷新 global_summary.md。
    const run = runStates[projectId] ?? EMPTY_RUN
    const project = projects.find((item) => item.id === projectId)
    setSnapshotSave({
      open: true,
      projectId,
      message: `保存当前版本：${project?.name ?? projectId}`,
      saving: false,
      error: null,
      latestChanges: run.latestChanges || '',
    })
  }

  const closeSnapshotSave = () => {
    setSnapshotSave((previous) =>
      previous.saving ? previous : { ...previous, open: false },
    )
  }

  const submitSnapshotSave = async (event) => {
    event.preventDefault()
    if (snapshotSave.saving) return
    const message = snapshotSave.message.trim()
    if (!message) {
      setSnapshotSave((previous) => ({
        ...previous,
        error: '请输入快照备注，例如“完成了登录页的样式”',
      }))
      return
    }
    const projectId = snapshotSave.projectId
    const sandboxId = sandboxIdOf(projectId)
    if (!sandboxId) {
      setSnapshotSave((previous) => ({
        ...previous,
        error: '沙盒已失效，请重新执行一次 Agent。',
      }))
      return
    }

    setSnapshotSave((previous) => ({ ...previous, saving: true, error: null }))
    try {
      const run = runStates[projectId] ?? EMPTY_RUN
      // latestChanges 是本次快照涵盖的改动说明；为空时退化为快照备注，
      // 保证第一次保存也能生成 global_summary.md。
      const latestChanges = run.latestChanges || message
      const payload = await createSnapshot(sandboxId, message, session?.token, {
        projectId,
        tenantId: user?.tenantId || 'tenant_local',
        latestChanges,
      })
      if (!workspaceMountedRef.current) return
      setSnapshotSave((previous) => ({ ...previous, saving: false, open: false }))
      // 保存成功 = 已落盘：解除下一步门控，并缓存全局摘要供 Architect 使用。
      patchRun(projectId, {
        dirty: false,
        projectSummary: payload?.projectSummary || run.projectSummary || '',
        latestChanges: payload?.latestChanges || latestChanges,
      })
      showSnapshotToast(
        'success',
        `快照已保存：${message}（${payload?.shortHash ?? ''}）`,
      )
      if (snapshotDrawer.open && snapshotDrawer.projectId === projectId) {
        void refreshSnapshotList(projectId, sandboxId)
      }
    } catch (cause) {
      if (!workspaceMountedRef.current) return
      setSnapshotSave((previous) => ({
        ...previous,
        saving: false,
        error: cause?.message || String(cause),
      }))
    }
  }

  /**
   * 执行硬回滚。抽屉与快照标签页共用：
   *  - 成功后刷新版本历史；
   *  - 递增 previewRefresh，iframe 以新 key 重建并加载回滚后的构建；
   *  - 同步 run.result.commitHash，开发者详情里的锚点不指回旧版本。
   */
  const performRestoreSnapshot = async (projectId, commitHash) => {
    const sandboxId = sandboxIdOf(projectId)
    if (!sandboxId) {
      showSnapshotToast('error', '沙盒已失效，无法恢复快照。')
      return
    }
    setSnapshotDrawer((previous) =>
      previous.projectId === projectId
        ? { ...previous, restoring: commitHash, confirmError: null }
        : previous,
    )
    try {
      const payload = await restoreSnapshot(sandboxId, commitHash, session?.token)
      if (!workspaceMountedRef.current) return
      const label =
        payload?.message ||
        snapshotDrawer.snapshots.find((item) => item.commitHash === commitHash)
          ?.message ||
        commitHash.slice(0, 7)
      setSnapshotDrawer((previous) =>
        previous.projectId === projectId
          ? {
              ...previous,
              restoring: null,
              confirmHash: null,
              confirmError: null,
            }
          : previous,
      )
      showSnapshotToast('success', `已恢复到快照：${label}`)
      setPreviewRefresh((previous) => ({
        ...previous,
        [projectId]: (previous[projectId] ?? 0) + 1,
      }))
      setPreviewFocus((previous) => ({
        ...previous,
        [projectId]: (previous[projectId] ?? 0) + 1,
      }))
      setRunStates((previous) => {
        const run = previous[projectId]
        if (!run?.result) return previous
        return {
          ...previous,
          [projectId]: {
            ...run,
            result: { ...run.result, commitHash },
            // 回滚后工作区与该 commit 一致，视为已保存；拖拽定位层失效。
            dirty: false,
            placement: null,
            latestChanges: `恢复到快照：${label}`,
          },
        }
      })
      if (snapshotDrawer.projectId === projectId) {
        void refreshSnapshotList(projectId, sandboxId)
      }
    } catch (cause) {
      if (!workspaceMountedRef.current) return
      setSnapshotDrawer((previous) =>
        previous.projectId === projectId
          ? {
              ...previous,
              restoring: null,
              confirmError: cause?.message || String(cause),
            }
          : previous,
      )
    }
  }

  const requestRestoreSnapshot = (commitHash) => {
    setSnapshotDrawer((previous) => ({
      ...previous,
      confirmHash: commitHash,
      confirmError: null,
    }))
  }

  const cancelRestoreSnapshot = () => {
    setSnapshotDrawer((previous) =>
      previous.restoring
        ? previous
        : { ...previous, confirmHash: null, confirmError: null },
    )
  }

  const confirmRestoreSnapshot = () => {
    if (!snapshotDrawer.projectId || !snapshotDrawer.confirmHash) return
    void performRestoreSnapshot(
      snapshotDrawer.projectId,
      snapshotDrawer.confirmHash,
    )
  }

  /** 新建 / 激活一个只读快照标签，并按需加载该 commit 的 Diff。 */
  const handleViewSnapshot = (projectId, snapshot) => {
    const sandboxId = sandboxIdOf(projectId)
    if (!sandboxId) {
      showSnapshotToast('error', '沙盒已失效，无法查看该快照。')
      return
    }
    const id = snapshotTabId(projectId, snapshot.commitHash)
    const projectName =
      projects.find((project) => project.id === projectId)?.name ?? ''
    setSnapshotTabs((previous) => {
      const existing = previous.find((tab) => tab.id === id)
      if (existing) {
        return previous.map((tab) =>
          tab.id === id ? { ...tab, loading: !tab.diff, error: null } : tab,
        )
      }
      return [
        ...previous,
        {
          id,
          projectId,
          projectName,
          commitHash: snapshot.commitHash,
          shortHash: snapshot.shortHash,
          message: snapshot.message,
          committedAt: snapshot.committedAt,
          diff: '',
          truncated: false,
          loading: true,
          error: null,
        },
      ]
    })
    setActiveSnapshotId(id)
    closeSnapshotDrawer()

    snapshotDiffAbortRef.current.get(id)?.abort()
    const controller = new AbortController()
    snapshotDiffAbortRef.current.set(id, controller)
    fetchSnapshotDiff(
      sandboxId,
      snapshot.commitHash,
      session?.token,
      controller.signal,
    )
      .then((payload) => {
        if (!workspaceMountedRef.current) return
        setSnapshotTabs((previous) =>
          previous.map((tab) =>
            tab.id === id
              ? {
                  ...tab,
                  loading: false,
                  error: null,
                  diff: payload?.diff ?? '',
                  truncated: Boolean(payload?.truncated),
                }
              : tab,
          ),
        )
      })
      .catch((cause) => {
        if (cause?.name === 'AbortError' || !workspaceMountedRef.current) return
        setSnapshotTabs((previous) =>
          previous.map((tab) =>
            tab.id === id
              ? {
                  ...tab,
                  loading: false,
                  error: cause?.message || String(cause),
                }
              : tab,
          ),
        )
      })
      .finally(() => {
        snapshotDiffAbortRef.current.delete(id)
      })
  }

  const handleSelectSnapshotTab = (id) => {
    setActiveSnapshotId(id)
  }

  const handleSelectProjectTab = (projectId) => {
    setActiveSnapshotId(null)
    setActiveTab(projectId)
    setTabActivation((previous) => ({
      ...previous,
      [projectId]: (previous[projectId] ?? 0) + 1,
    }))
  }

  const handleCloseSnapshotTab = (id) => {
    snapshotDiffAbortRef.current.get(id)?.abort()
    snapshotDiffAbortRef.current.delete(id)
    setSnapshotTabs((previous) => previous.filter((tab) => tab.id !== id))
    setActiveSnapshotId((previous) => (previous === id ? null : previous))
  }

  const handleSnapshotTabRestore = (snapshot) => {
    if (!window.confirm('当前未保存的修改将丢失，是否继续？')) return
    void performRestoreSnapshot(snapshot.projectId, snapshot.commitHash)
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
    // 项目被彻底销毁后，其虚拟快照标签也必须移除，避免指向已销毁的沙盒。
    const closingSnapshotTabs = snapshotTabs.filter(
      (tab) => tab.projectId === projectId,
    )
    if (closingSnapshotTabs.length > 0) {
      closingSnapshotTabs.forEach((tab) => {
        snapshotDiffAbortRef.current.get(tab.id)?.abort()
        snapshotDiffAbortRef.current.delete(tab.id)
      })
      setSnapshotTabs((previous) =>
        previous.filter((tab) => tab.projectId !== projectId),
      )
      setActiveSnapshotId((current) =>
        closingSnapshotTabs.some((tab) => tab.id === current) ? null : current,
      )
    }
    if (snapshotDrawer.open && snapshotDrawer.projectId === projectId) {
      snapshotDrawerRequestRef.current += 1
      setSnapshotDrawer((previous) => ({
        ...previous,
        open: false,
        loading: false,
        restoring: null,
        confirmHash: null,
      }))
    }
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

  /**
   * 稳定的事件处理器集合。
   *
   * 页面级 runStates 每次 SSE 批次都会更新；如果每次渲染都向
   * ProjectWorkspace 传新箭头函数，React.memo 永远失效，所有 Tab
   * （包括隐藏的）都会跟着重渲染。
   *
   * 这里把处理器做成按 projectId 缓存的稳定对象，内部通过 ref 读取
   * 最新一次提交的函数闭包——既避免陈旧 state，又让 memo 真正生效。
   */
  const workspaceActionsRef = useRef(null)
  useLayoutEffect(() => {
    workspaceActionsRef.current = {
      handleAnnotationAdd,
      handleAnnotationIntentChange,
      handleAnnotationActionChange,
      handleAnnotationRemove,
      abortAnnotationScope,
      handleAnnotationSend,
      handleAnnotationMove,
      handleComponentDragEnd,
      handleComponentDragCancel,
      handleClarificationAnswer,
      handleAnnotationRollback,
      handleClearAnnotations,
      handleApprovalDecision,
      handleCancelRun,
      handleSubmit,
      handleRetry,
      handleExport,
      handleArchitectExport,
      openSnapshotSave,
      loadAgentCatalog,
      handleOpenArchitect,
      handleStartArchitect,
      handleRestartArchitect,
      handleFrontendBootstrap,
      handleFullstackGenerate,
      handleReleaseGate,
      architectClose: architect.closePanel,
      architectAnswer: architect.answer,
      architectDecide: architect.decide,
      architectAskHelper: architect.askHelper,
      architectCancel: architect.cancel,
    }
  })

  const projectActions = useMemo(() => {
    const map = new Map()
    openTabs.forEach((projectId) => {
      const call = (name, ...args) =>
        workspaceActionsRef.current?.[name]?.(projectId, ...args)
      map.set(projectId, {
        onModelProviderChange: setModelProvider,
        onAnnotationAdd: (payload) => call('handleAnnotationAdd', payload),
        onAnnotationIntentChange: (annotationId, intent) =>
          call('handleAnnotationIntentChange', annotationId, intent),
        onAnnotationActionChange: (annotationId, action) =>
          call('handleAnnotationActionChange', annotationId, action),
        onAnnotationRemove: (annotationId) =>
          call('handleAnnotationRemove', annotationId),
        onAnnotationUnmount: (annotationId) =>
          call('abortAnnotationScope', annotationId),
        onAnnotationSend: (annotationId) =>
          call('handleAnnotationSend', annotationId),
        onAnnotationMove: (annotationId, dx, dy) =>
          call('handleAnnotationMove', annotationId, dx, dy),
        onComponentDragEnd: (payload) =>
          call('handleComponentDragEnd', payload),
        onComponentDragCancel: (payload) =>
          call('handleComponentDragCancel', payload),
        onClarificationAnswer: (
          annotationId,
          clarification,
          answers,
          comment,
        ) =>
          call(
            'handleClarificationAnswer',
            annotationId,
            clarification,
            answers,
            comment,
          ),
        onAnnotationRollback: (annotationId, message) =>
          call('handleAnnotationRollback', annotationId, message),
        onClearAnnotations: () => call('handleClearAnnotations'),
        onApprovalDecision: (decision, comment) =>
          call('handleApprovalDecision', decision, comment),
        onCancelRun: () => call('handleCancelRun'),
        onSubmit: () => call('handleSubmit'),
        onRetry: () => call('handleRetry'),
        onExport: () => call('handleExport'),
        onArchitectExport: (target) =>
          call('handleArchitectExport', target),
        onSaveSnapshot: () => call('openSnapshotSave'),
        onRefreshAgents: () => workspaceActionsRef.current?.loadAgentCatalog(),
        onArchitectOpen: () => call('handleOpenArchitect'),
        onArchitectStart: (goal) => call('handleStartArchitect', goal),
        onArchitectRestart: () => call('handleRestartArchitect'),
        onStartCreate: () => call('handleOpenArchitect'),
        onFrontendBootstrap: () =>
          call('handleFrontendBootstrap', { manual: true }),
        onArchitectClose: () =>
          workspaceActionsRef.current?.architectClose(projectId),
        onArchitectAnswer: (answers, comment, options) =>
          workspaceActionsRef.current?.architectAnswer(
            projectId,
            answers,
            comment,
            options,
          ),
        onArchitectDecide: (decision, comment) =>
          workspaceActionsRef.current?.architectDecide(
            projectId,
            decision,
            comment,
          ),
        onArchitectAskHelper: (question) =>
          workspaceActionsRef.current?.architectAskHelper(projectId, question),
        onArchitectCancel: () =>
          workspaceActionsRef.current?.architectCancel(projectId),
        onFullstackGenerate: () => call('handleFullstackGenerate'),
        onReleaseGate: () => call('handleReleaseGate'),
      })
    })
    return map
  }, [openTabs])

  const activeSnapshotTab =
    snapshotTabs.find((tab) => tab.id === activeSnapshotId) ?? null

  /**
   * 单条快照的渲染函数。
   * 版本历史可能长达 200 条：普通列表一次性挂载会造成明显卡顿，
   * 因此超过阈值时交给 VirtualList 按可视区渲染。
   */
  const renderSnapshotItem = (item) => {
    const isHead = item.commitHash === snapshotDrawer.headCommit
    const confirming = snapshotDrawer.confirmHash === item.commitHash
    const restoring = snapshotDrawer.restoring === item.commitHash
    return (
      <li
        key={item.commitHash}
        className={`snapshot-item ${isHead ? 'is-head' : ''}`}
        data-snapshot-item
        data-commit-hash={item.commitHash}
      >
        <div className="snapshot-item-head">
          <span className="snapshot-item-message">
            {item.message || '（无备注）'}
          </span>
          {isHead && <span className="snapshot-head-tag">当前版本</span>}
        </div>
        <div className="snapshot-item-meta">
          <code>{item.shortHash}</code>
          <span>{formatSnapshotTime(item.committedAt)}</span>
          {item.author && <span>{item.author}</span>}
        </div>
        <div className="snapshot-item-actions">
          <button
            type="button"
            className="ghost-button"
            onClick={() => handleViewSnapshot(snapshotDrawer.projectId, item)}
          >
            查看
          </button>
          <button
            type="button"
            className="ghost-button is-danger"
            disabled={isHead || restoring}
            onClick={() => requestRestoreSnapshot(item.commitHash)}
          >
            {restoring ? '恢复中…' : isHead ? '已是当前版本' : '恢复'}
          </button>
        </div>
        {confirming && (
          <div
            className="snapshot-confirm"
            role="alertdialog"
            aria-label="确认恢复快照"
          >
            <p>当前未保存的修改将丢失，是否继续？</p>
            <div className="snapshot-confirm-actions">
              <button
                type="button"
                className="ghost-button"
                disabled={restoring}
                onClick={cancelRestoreSnapshot}
              >
                取消
              </button>
              <button
                type="button"
                className="danger-button"
                disabled={restoring}
                onClick={confirmRestoreSnapshot}
              >
                {restoring ? '恢复中…' : '继续恢复'}
              </button>
            </div>
            {snapshotDrawer.confirmError && (
              <p className="snapshot-error" role="alert">
                {snapshotDrawer.confirmError}
              </p>
            )}
          </div>
        )}
      </li>
    )
  }

  return (
    <div className="app-shell app-shell-wide app-shell-workspace">
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
          snapshotTabs={snapshotTabs}
          activeSnapshotId={activeSnapshotId}
          limitReached={limitReached}
          onSelect={handleSelectProjectTab}
          onSelectSnapshot={handleSelectSnapshotTab}
          onCloseSnapshot={handleCloseSnapshotTab}
          onClose={handleCloseTab}
          onReorder={reorderTabs}
          onNewProject={createProject}
          onOpenHistory={openSnapshotDrawer}
          historyAvailable={Boolean(
            activeProject && runStates[activeProject.id]?.result?.sandboxId,
          )}
        />

        <div className="workspace-body">
          {openProjects.map((project) => {
            const run = runStates[project.id] ?? EMPTY_RUN
            return (
              <ProjectWorkspace
                key={project.id}
                project={project}
                run={run}
                active={activeSnapshotId == null && project.id === activeTabId}
                modelProvider={modelProvider}
                canExport={Boolean(run.result?.sandboxId)}
                exporting={exportingProjectId === project.id}
                canSaveSnapshot={Boolean(run.result?.sandboxId)}
                previewRefreshNonce={previewRefresh[project.id] ?? 0}
                tabActivationNonce={tabActivation[project.id] ?? 0}
                architectState={
                  architectStates[project.id] ?? EMPTY_ARCHITECT
                }
                supervisorGate={gateStates[project.id] ?? null}
                dragRollback={dragRollbacks[project.id] ?? null}
                dragCommit={dragCommits[project.id] ?? null}
                dragSyncActive={Boolean(dragSyncStates[project.id])}
                agentCatalog={agentCatalog}
                agentToken={session?.token}
                previewFocusNonce={previewFocus[project.id] ?? 0}
                // 稳定回调：内部通过 ref 调用最新闭包，允许 memo 跳过无关重渲染。
                {...projectActions.get(project.id)}
              />
            )
          })}

          {activeSnapshotTab && (
            <SnapshotWorkspace
              key={activeSnapshotTab.id}
              snapshot={activeSnapshotTab}
              onClose={() => handleCloseSnapshotTab(activeSnapshotTab.id)}
              onRestore={handleSnapshotTabRestore}
            />
          )}

          {openProjects.length === 0 &&
            !activeSnapshotTab && <EmptyWorkspace onNewProject={createProject} />}
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

      {snapshotDrawer.open && (
        <div
          className="snapshot-drawer-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeSnapshotDrawer()
          }}
        >
          <aside
            className="snapshot-drawer"
            role="dialog"
            aria-modal="true"
            aria-label="版本历史"
            data-snapshot-drawer
          >
            <header className="snapshot-drawer-head">
              <div>
                <h2>版本历史</h2>
                <p className="muted">
                  {projects.find(
                    (project) => project.id === snapshotDrawer.projectId,
                  )?.name ?? ''}
                </p>
              </div>
              <button
                className="snapshot-drawer-close"
                type="button"
                aria-label="关闭版本历史"
                onClick={closeSnapshotDrawer}
              >
                ×
              </button>
            </header>

            <div className="snapshot-drawer-body">
              {snapshotDrawer.loading && (
                <p className="muted">正在读取快照历史…</p>
              )}
              {snapshotDrawer.error && (
                <p className="snapshot-error" role="alert">
                  {snapshotDrawer.error}
                </p>
              )}
              {!snapshotDrawer.loading &&
                !snapshotDrawer.error &&
                snapshotDrawer.snapshots.length === 0 && (
                  <p className="muted">
                    还没有快照。点击顶部 💾 保存快照，为当前版本存档。
                  </p>
                )}

              {snapshotDrawer.snapshots.length > 12 ? (
                <VirtualList
                  className="snapshot-list"
                  items={snapshotDrawer.snapshots}
                  getKey={(item) => item.commitHash}
                  height={Math.min(560, Math.max(320, window.innerHeight - 240))}
                  // 展开二次确认时该行更高；其余行使用估算值，渲染后自动修正。
                  itemHeight={(item) =>
                    snapshotDrawer.confirmHash === item.commitHash ? 238 : 118
                  }
                  renderItem={renderSnapshotItem}
                />
              ) : (
                <ol className="snapshot-list">
                  {snapshotDrawer.snapshots.map(renderSnapshotItem)}
                </ol>
              )}
            </div>
          </aside>
        </div>
      )}

      {snapshotSave.open && (
        <div
          className="snapshot-modal-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeSnapshotSave()
          }}
        >
          <form
            className="snapshot-modal"
            role="dialog"
            aria-modal="true"
            aria-label="保存快照"
            onSubmit={submitSnapshotSave}
          >
            <header className="snapshot-modal-head">
              <h2>💾 保存快照</h2>
              <button
                className="snapshot-drawer-close"
                type="button"
                aria-label="关闭保存快照"
                onClick={closeSnapshotSave}
              >
                ×
              </button>
            </header>
            <p className="muted">
              把当前沙盒代码提交为一个可随时回滚的版本。
            </p>
            <label className="snapshot-modal-label" htmlFor="snapshot-note">
              快照备注
            </label>
            <input
              id="snapshot-note"
              className="snapshot-note-input"
              autoFocus
              maxLength={200}
              placeholder="例如：完成了登录页的样式"
              value={snapshotSave.message}
              disabled={snapshotSave.saving}
              onChange={(event) =>
                setSnapshotSave((previous) => ({
                  ...previous,
                  message: event.target.value,
                  error: null,
                }))
              }
            />
            <div className="snapshot-modal-hint">
              {snapshotSave.message.trim().length}/200
            </div>
            {snapshotSave.error && (
              <p className="snapshot-error" role="alert">
                {snapshotSave.error}
              </p>
            )}
            <div className="snapshot-modal-actions">
              <button
                type="button"
                className="ghost-button"
                disabled={snapshotSave.saving}
                onClick={closeSnapshotSave}
              >
                取消
              </button>
              <button
                type="submit"
                className="primary-button"
                disabled={
                  snapshotSave.saving || !snapshotSave.message.trim()
                }
              >
                {snapshotSave.saving ? '保存中…' : '保存快照'}
              </button>
            </div>
          </form>
        </div>
      )}

      <AccountDialog open={accountOpen} onClose={() => setAccountOpen(false)} />
      <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </div>
  )
}
