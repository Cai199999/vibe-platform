import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { Link, useNavigate } from 'react-router-dom'
import AppHeader from '../components/AppHeader.jsx'
import AccountDialog from '../components/AccountDialog.jsx'
import DirectOpsDecisionDialog from '../components/DirectOpsDecisionDialog.jsx'
import DraftLeaveGuard from '../components/DraftLeaveGuard.jsx'
import NewProjectDialog from '../components/NewProjectDialog.jsx'
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
import {
  analyzeDirectDelete,
  fetchDirectRevision,
  nextDirectActionId,
  submitDirectAction,
  submitDirectDecision,
  undoLastDirectAction,
} from '../workspace/directOps.js'
import { importProjectFile } from '../workspace/importClient.js'
import {
  loadProjectSource,
  saveProjectSource,
} from '../workspace/projectSourceClient.js'
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
import {
  draftKeyFor,
  resolveSaveToast,
  useDraftStore,
} from '../workspace/draftStore.js'
import { writeDraftTombstone } from '../workspace/draftStorage.js'
import { formatPlacementAcceptance } from '../workspace/stagedPlacement.js'
import { INITIAL_APP } from '../mockProject.js'
import {
  validateVisualDocument,
  visualDocumentFromContract,
} from '../visual/visualDocument.js'
import {
  VISUAL_DOCUMENT_STORAGE_KEY,
  loadFrozenVisualDocument,
  useVisualDocumentStore,
} from '../visual/visualDocumentStore.js'
import {
  PROJECT_MODE,
  resolveProjectMode,
  storeImportedSource,
} from '../workspace/projectMode.js'
import {
  buildVisualDelta,
  loadVisualV0Baseline,
  storeVisualV0Baseline,
} from '../visual/visualDelta.js'
import {
  buildSemanticReviewContext,
  peekSemanticReviewPacket,
  takeSemanticReviewPacket,
} from '../visual/visualReview.js'

const TARGET_FILE = 'src/App.jsx'
const MAX_ANNOTATIONS = 3
// 批量布局提交超时：Coder 已在后端 30s 熔断，这里留出合并补丁、
// 构建与 HMR 的余量；超时后保留用户草稿，不能回滚视觉位置。
const LAYOUT_SYNC_TIMEOUT_MS = 60000

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
  // 安全路径决策：decision.required 的 payload，非空时展示决策悬浮框。
  pendingDecision: null,
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
  // “转后台运行”：Run 在服务端继续执行，前端只保留轻量进度 / 终态通知，
  // 用户可以继续圈选；backgroundRequest 保存原始指令供一键重试。
  backgroundRun: null,
  backgroundRequest: null,
}

// 成功终态：M2 的旧快照仍可能是 AWAITING_APPROVAL，统一按成功渲染。
const SUCCESS_STATUSES = new Set(['SUCCEEDED', 'AWAITING_APPROVAL'])
const isSuccessStatus = (status) => SUCCESS_STATUSES.has(status)
const TERMINAL_EVENT_TYPES = new Set([
  'run.succeeded',
  'run.failed',
  'run.cancelled',
  'run.rejected',
])

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
 * 从自然语言粗判新增组件类型，仅用于前端“幽灵预览”的视觉占位。
 * 后端仍然以完整 AnnotationEnvelope 为准，不依赖这个标签。
 */
function inferPendingComponentType(intent) {
  const text = String(intent || '')
  const rules = [
    [/输入框|输入|表单|搜索框|textarea/i, 'input', '输入框'],
    [/卡片|card|区块/i, 'card', '卡片'],
    [/图片|图标|头像|banner|image|logo/i, 'image', '图片'],
    [/标题|文字|文本|段落|说明/i, 'text', '文本'],
    [/导航|菜单|tab|标签页/i, 'nav', '导航'],
    [/按钮|button|btn|提交|登录|注册/i, 'button', '按钮'],
  ]
  for (const [pattern, key, label] of rules) {
    if (pattern.test(text)) return { key, label }
  }
  return { key: 'component', label: '组件' }
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
  const isBatchMove = rawAction === 'BATCH_MOVE'
  const isLayoutBatch = rawAction === 'BATCH_LAYOUT'
  const isMove = rawAction === 'MOVE'
  // 旧 localStorage 里的标注没有 action：按指令与几何判定即时推断。
  // 有 action 时统一归一化，旧 ADD / MODIFY 也在这里升级。
  const action = isBatchMove
    ? 'BATCH_MOVE'
    : isLayoutBatch
      ? 'BATCH_LAYOUT'
      : isMove
        ? 'MOVE'
        : entry.action
          ? normalizeAction(entry.action)
          : inferAnnotationAction(entry.target, instruction, entry.geometryAction)
  // 批量布局：一个 Envelope 携带 N 条选择器 + 累计偏移，Coder 一次合并。
  const moveBatch =
    isBatchMove || isLayoutBatch
      ? (entry.moveBatch ?? [])
          .map((move) => ({
            source: move.source || move.selector || '',
            file: move.file || targetFile,
            selector: move.selector || '',
            label: move.label || '组件',
            dx: Math.round(Number(move.dx) || 0),
            dy: Math.round(Number(move.dy) || 0),
            token: move.token ?? null,
          }))
          .filter((move) => move.selector)
      : []
  // 统一提交的新增组件：每一项都携带完整 CREATE_NEW 信封，后端直接复用
  // 现有 UI 上下文裁剪与 data-vibe-created 校验，避免再做一套协议。
  const additionBatch = isLayoutBatch
    ? (entry.additionBatch ?? [])
        .map((addition) => {
          const nested = addition.annotation
          if (!nested) return null
          const annotationId =
            addition.annotationId || nested.annotationId || ''
          if (!annotationId) return null
          const intentText =
            addition.intent ||
            nested.intent?.ui?.summary ||
            nested.payload?.text ||
            ''
          if (!String(intentText).trim()) return null
          // 聊天历史 / 截图与生成无关，剥离以缩小 Prompt。
          const { chatHistory, screenshot, ...compact } = nested
          return {
            annotationId,
            intent: String(intentText).trim(),
            componentType: addition.componentType || 'component',
            componentLabel: addition.componentLabel || '组件',
            annotation: {
              ...compact,
              annotationId,
              action: CREATE_NEW,
              type: compact.type || 'visual-draw',
            },
          }
        })
        .filter(Boolean)
    : []
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
  // CREATE_NEW 拖拽落位后，entry.source 是父容器描述；它必须优先于
  // 创建时记录的叶子 inspectSource，否则 Resolver 会把插入锚点定位到
  // 子元素而不是用户高亮的盒子。
  const precise =
    (isCreateAction(action) && entry.source) ||
    entry.inspectSource ||
    entry.source ||
    null
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
  const acceptance = isLayoutBatch
    ? [
        `统一提交 ${additionBatch.length} 个新增组件与 ${moveBatch.length} 个位置调整`,
        '新增组件必须在 target.selector 指向的父容器内插入完整 JSX，并带 data-vibe-created 标记',
        ...(moveBatch.length
          ? [
              '位移只允许追加 transform: translate3d(dx,dy,0)，禁止修改 width / height / left / top',
            ]
          : []),
        '所有变更必须合并为同一份写盘操作，禁止分多次写文件或触发多次 HMR',
        '其余布局、文案与交互保持不变',
      ]
    : isBatchMove
      ? [
          `按 payload.moves 一次调整 ${moveBatch.length} 个组件的累计偏移`,
          '每个组件只允许追加 transform: translate3d(dx,dy,0)，禁止修改 width / height / left / top',
          '必须合并为同一份全量文件补丁，禁止分多次写文件',
          '其余布局、文案与交互保持不变',
        ]
      : isMove
    ? [
        `把${target?.label || '目标组件'}移动到累计偏移 (${Math.round(Number(moveOffset?.dx) || 0)}, ${Math.round(Number(moveOffset?.dy) || 0)}) px`,
        '只允许追加 transform: translate3d(dx,dy,0)，禁止修改 width / height / left / top',
        '其余布局、文案与交互保持不变',
      ]
    : isCreateAction(action)
      ? [
          `在${target?.label || '圈选区域'}（${target?.selector || '父容器'}）内部新增组件：${instruction}`,
          '只生成组件主体与 data-vibe-created 审计标记，不写死绝对坐标；按父容器自然文档流插入',
          // 拖拽落位后的确定性插入约束：显式告诉 Coder 兄弟锚点与顺序，
          // 不让模型再从自然语言里猜“放第几个”。
          ...(entry.placement
            ? [
                formatPlacementAcceptance(
                  entry.placement,
                  target?.label || '父容器',
                ),
              ]
            : []),
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
    type:
      !isMove && !isBatchMove && !isLayoutBatch && precise
        ? 'visual-draw'
        : 'drag',
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
            // 实例作用域：file:line 只能定位到组件源码，无法区分同一
            // 组件的第几个实例；instance / callSite 必须随 Envelope 进入
            // 后端 Prompt，否则 Coder 会改基础组件造成全局污染。
            instance: Number(precise?.instance ?? target?.instance) || undefined,
            instanceCount:
              Number(precise?.instanceCount ?? target?.instanceCount) ||
              undefined,
            instanceId:
              precise?.instanceId ?? target?.instanceId ?? undefined,
            // React fiber 上的稳定身份：key 或列表项基本类型 props。
            // Coder 用它写出 item.id === 'xxx' 之类的局部守卫。
            instanceKey:
              precise?.instanceKey ?? target?.instanceKey ?? undefined,
            instanceProps:
              precise?.instanceProps ??
              target?.instanceProps ??
              undefined,
            callSite: precise?.callSite ?? target?.callSite ?? undefined,
            // 绝对视觉指纹：SDK 从真实 DOM 提取的类名 + computed CSS。
            // Coder 修改 / 参考组件时必须基于这些事实，而不是猜样式。
            styleSignature:
              precise?.styleSignature ??
              target?.styleSignature ??
              undefined,
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
      // 可视化选型：用户点选的参考组件及其真实类名 / computed CSS。
      // 即使后续重新提交（并非走澄清 checkpoint），也必须保留事实来源。
      ...(entry.reference ? { reference: entry.reference } : {}),
      // 暂存组件拖拽落位结果：父容器 instanceId + 插入锚点 + 兄弟顺序。
      // 后端 Resolver / Coder 只读这份结构化事实，不解析自然语言。
      ...(isCreateAction(action) && entry.placement
        ? { placement: entry.placement }
        : {}),
      // 直接拖拽：dx / dy 是相对组件原始位置的累计偏移（不是本次手势
      // 增量）。后端 _try_move_tool 直接读取它调用 move_component，
      // 不再让模型从自然语言里猜方向与距离。
      ...(isLayoutBatch && (moveBatch.length || additionBatch.length)
        ? {
            // 视觉暂存统一提交：位移走确定性 Tool，新增组件走一次 Coder。
            ...(moveBatch.length ? { moves: moveBatch } : {}),
            additions: additionBatch,
          }
        : isBatchMove && moveBatch.length
        ? {
            // 任务三协议：多个组件一次性提交；Coder 只生成一份合并补丁。
            // 每项都带 selector（Tool 入参）与 source（审计标识）。
            moves: moveBatch,
          }
        : isMove && moveOffset
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

/**
 * 把澄清问题 / 用户回答写进对应标注的迷你聊天记录。
 *
 * HITL 暂停不是一次性的表单弹窗：问题和回答必须进入同一 chatHistory，
 * 用户才能看到完整上下文，并在同一浮层里继续多轮对话。
 */
function appendClarificationQuestion(run, payload, fallbackAnnotationId) {
  const targetId = payload?.annotationId || fallbackAnnotationId
  if (!targetId) return run
  const clarificationId = payload?.clarificationId
  const questionText = (payload?.questions ?? [])
    .map((question) => String(question.prompt || '').trim())
    .filter(Boolean)
    .join('\n')
  const content =
    questionText || payload?.decisionSummary || '需要确认一些信息后继续。'
  let changed = false
  const annotations = (run.annotations ?? []).map((annotation) => {
    if (
      annotation.id !== targetId &&
      annotation.threadId !== targetId
    ) {
      return annotation
    }
    const history = annotation.chatHistory ?? []
    // 重放 / 重连同一澄清时不能重复插入问题气泡。
    if (
      clarificationId &&
      history.some(
        (message) =>
          message.clarificationId === clarificationId &&
          message.role === 'assistant',
      )
    ) {
      return annotation
    }
    changed = true
    return {
      ...annotation,
      chatHistory: [
        ...history,
        newMessage('assistant', content, {
          clarificationId,
          kind: 'clarification_question',
        }),
      ],
    }
  })
  return changed ? { ...run, annotations } : run
}

function _formatAnswerValue(value) {
  if (Array.isArray(value)) {
    return value.map(_formatAnswerValue).filter(Boolean).join('、')
  }
  if (value && typeof value === 'object') {
    return Object.values(value)
      .map(_formatAnswerValue)
      .filter(Boolean)
      .join('；')
  }
  return String(value ?? '').trim()
}

/**
 * 把可视化选型结果挂到标注上。
 *
 * 澄清恢复依赖 LangGraph checkpoint；但若恢复后的 Run 失败、用户再次发送，
 * 前端必须仍能带上同一份 reference，否则 Coder 又只能猜样式。
 */
function attachAnnotationReference(run, annotationId, reference) {
  if (!reference || !annotationId) return run
  let changed = false
  const annotations = (run.annotations ?? []).map((annotation) => {
    if (
      annotation.id !== annotationId &&
      annotation.threadId !== annotationId
    ) {
      return annotation
    }
    changed = true
    return { ...annotation, reference }
  })
  return changed ? { ...run, annotations } : run
}

function appendClarificationReply(
  run,
  annotationId,
  clarification,
  answers = [],
  comment = '',
) {
  if (!annotationId) return run
  const parts = [
    ...(answers ?? []).map((item) => _formatAnswerValue(item?.value)),
    String(comment || '').trim(),
  ].filter(Boolean)
  if (parts.length === 0) return run
  const clarificationId = clarification?.clarificationId
  let changed = false
  const annotations = (run.annotations ?? []).map((annotation) => {
    if (
      annotation.id !== annotationId &&
      annotation.threadId !== annotationId
    ) {
      return annotation
    }
    const history = annotation.chatHistory ?? []
    if (
      clarificationId &&
      history.some(
        (message) =>
          message.clarificationId === clarificationId &&
          message.role === 'user',
      )
    ) {
      return annotation
    }
    changed = true
    return {
      ...annotation,
      chatHistory: [
        ...history,
        newMessage('user', parts.join('；'), {
          clarificationId,
          kind: 'clarification_answer',
        }),
      ],
    }
  })
  return changed ? { ...run, annotations } : run
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
    // 可恢复超时（CODER_TIMEOUT_RECOVERABLE）会展示“转后台运行”，
    // 而不是把用户钉死在普通失败页。
    errorCode: event.type === 'run.failed' ? payload.code ?? null : null,
    backgroundAvailable:
      event.type === 'run.failed' ? Boolean(payload.backgroundAvailable) : false,
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
    pendingDecision: null,
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
    // 问题同时进入时间轴与对应标注的迷你聊天框：用户看到的是一段
    // 连续对话，而不是一个与历史割裂的表单。
    const withQuestion = appendClarificationQuestion(
      base,
      payload,
      annotationId,
    )
    return {
      ...withQuestion,
      clarification: payload,
      submitting: false,
      pendingAnnotationId: null,
      pendingAction: null,
    }
  }
  if (event.type === 'clarification.answered') {
    return { ...base, clarification: null }
  }
  if (event.type === 'decision.required') {
    // 安全路径决策：payload 与 AWAITING_DECISION 快照同构，悬浮框据此
    // 渲染选项；用户只能回传服务端生成的 optionId。
    return {
      ...base,
      pendingDecision: payload,
      clarification: null,
      approval: null,
      submitting: false,
      pendingAnnotationId: null,
      pendingAction: null,
      cancelRequested: false,
    }
  }
  if (event.type === 'decision.answered') {
    return { ...base, pendingDecision: null }
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

function EmptyWorkspace({ onNewProject, onImportSource, busy = false }) {
  return (
    <div className="workspace-empty">
      <div className="empty-mark" aria-hidden="true">
        ▦
      </div>
      <h2>工作区里还没有打开的窗口</h2>
      <p>
        新建一个项目，或直接导入已有 React 源码；也可以从左上角项目下拉栏
        重新打开已有项目。
      </p>
      <div className="workspace-empty-actions">
        <button className="primary-button" type="button" onClick={onNewProject}>
          ＋ 新建项目
        </button>
        <button
          className="ghost-button"
          type="button"
          data-direct-import
          disabled={busy}
          onClick={onImportSource}
        >
          {busy ? '正在分析…' : '导入源码分析'}
        </button>
      </div>
    </div>
  )
}

export default function WorkspacePage() {
  const { user, session } = useAuth()
  const { requireManualApproval } = useSettings()
  const navigate = useNavigate()
  // 草稿隔离维度：租户 + 用户。projectId 在每次读写时补入，避免新建项目
  // 继承上一个项目的序号 / 操作队列。
  const draftScope = useMemo(
    () => ({
      tenantId: user?.tenantId || 'tenant_local',
      userId: user?.id || 'anonymous',
    }),
    [user?.tenantId, user?.id],
  )
  // 已从 IndexedDB 恢复完成的项目 key；恢复前禁止 publish 镜像写库。
  const draftReadyRef = useRef(new Set())
  const draftHydratingRef = useRef(new Set())
  // 进行中的 hydration Promise：关闭 Tab 时先等它结束，避免把落盘草稿误判为空。
  const draftHydrationRef = useRef(new Map())
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

  // 导入源码的服务端回灌：localStorage 只是缓存，BFF 持久化快照才是
  // 跨刷新 / 跨浏览器的事实源。每个项目每次页面加载最多请求一次。
  const projectSourceHydrationRef = useRef(new Map())
  useEffect(() => {
    if (!session?.token) return
    const tenantId = user?.tenantId || 'tenant_local'
    projects
      .filter(
        (project) =>
          project?.imported &&
          (project.kind === 'react' ||
            String(project.template || '').startsWith('imported-react')),
      )
      .forEach((project) => {
        if (projectSourceHydrationRef.current.has(project.id)) return
        projectSourceHydrationRef.current.set(project.id, 'settled')
        void loadProjectSource(project.id, {
          tenantId,
          token: session.token,
        })
          .then((remote) => {
            if (!remote?.files || Object.keys(remote.files).length === 0) {
              return
            }
            const remoteRevision = Number(remote.revision) || 0
            const localRevision = Number(project.sourceRevision) || 0
            const localFiles = project.files || {}
            const hasLocalSource = Object.values(localFiles).some(
              (value) => typeof value === 'string' && value.trim().length > 0,
            )
            // 本地已有同一修订的源码时不覆盖，避免把工作区最新内容回滚成
            // 上一次导入快照；跨浏览器 / 清缓存时本地缺失，必然回灌。
            if (
              hasLocalSource &&
              remoteRevision > 0 &&
              remoteRevision <= localRevision
            ) {
              return
            }
            const entryFile =
              remote.entryFile || project.entryFile || TARGET_FILE
            updateProject(project.id, {
              files: remote.files,
              entryFile,
              sourceName: remote.sourceName || project.sourceName,
              sourceRevision: remoteRevision,
              imported: true,
              generated: false,
              bootstrap: 'ready',
            })
            const entrySource =
              remote.files[entryFile] ||
              Object.values(remote.files)[0] ||
              ''
            storeImportedSource(
              project.id,
              remote.sourceName || entryFile,
              entrySource,
            )
          })
          .catch((cause) => {
            // 404 表示该导入项目早于持久化功能；本地文件仍可继续使用。
            if (cause?.status !== 404) {
              console.warn(
                '[project-source] 服务端源码回灌失败',
                project.id,
                cause?.message || cause,
              )
            }
          })
      })
  }, [projects, session?.token, user?.tenantId, updateProject])

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
  // Direct Ops HITL：ScopePolicy 返回 options 时，复用决策面板让用户选择。
  const [directDecision, setDirectDecision] = useState(null)
  const [directDecisionBusy, setDirectDecisionBusy] = useState(false)
  const directDecisionSourceRef = useRef(null)
  // 关闭 Tab 的未保存草稿守卫；确认后才真正销毁项目 / 沙盒。
  const [draftGuard, setDraftGuard] = useState(null)
  const [draftGuardBusy, setDraftGuardBusy] = useState(false)
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
  const [newProjectOpen, setNewProjectOpen] = useState(false)
  const [sourceImportBusy, setSourceImportBusy] = useState(false)
  const [sourceImportError, setSourceImportError] = useState('')
  // 空工作区 / 存量入口的直达文件选择：不需要先经过“新建项目”对话。
  const directImportInputRef = useRef(null)
  // 导出状态只保留一个：同时下载多个项目没有意义，且提示条全局唯一。
  const [exportingProjectId, setExportingProjectId] = useState(null)
  const [exportNotice, setExportNotice] = useState(null)
  // 导入是“上传 + 容器启动 + npm install”的长请求，同一时间只允许一个。
  const [importing, setImporting] = useState(false)
  const inflightRef = useRef(new Set())
  // 每个项目最多一个 SSE 连接；关闭 Tab / 重新提交时主动 abort。
  const streamControllersRef = useRef(new Map())
  // 后台 Run 使用独立 SSE 连接：不占用项目当前 epoch，用户可继续圈选。
  // key = `${projectId}:${runId}`。
  const backgroundControllersRef = useRef(new Map())
  // 记录已关闭项目，防止关闭瞬间仍在执行的 Run 回写状态或泄漏沙盒。
  const closedProjectsRef = useRef(new Set())
  // 门控释放只允许一个在途请求；自动释放每个 gate 只尝试一次，失败后由用户
  // 在架构面板中手动重试，避免 effect 死循环。
  const gateReleaseInflightRef = useRef(new Set())
  const gateAutoReleaseAttemptedRef = useRef(new Set())
  // 每个项目只处理一次 fullstack.completed：刷新预览 / 聚焦 / Toast 不能因
  // Architect 状态的其他字段更新而重复触发。
  const fullstackHandledRef = useRef(new Map())
  // 自动 checkpoint 去重：同一项目的生成完成事件只提交一次。
  const autoSnapshotInflightRef = useRef(new Set())
  // 前端骨架初始化只允许每个契约触发一次；失败重试时由 handler 清除。
  const frontendBootstrapRef = useRef(new Map())
  // Architect → VisualDocument 同步：同一 contract/revision 只覆盖一次，
  // 避免用户画布编辑被后续 Architect 状态刷新冲掉。
  const visualDocumentSyncRef = useRef(new Map())
  const visualDocumentNoticeRef = useRef(new Set())
  const visualDocumentAutoOpenRef = useRef(new Set())
  const visualReviewStartedRef = useRef(new Set())
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
  // 直接拖拽：松手只更新草稿，不自动发起模型调用。
  // pendingLayoutChangesRef: projectId -> Map<selector, {
  //   selector, source, dx, dy, next, token, payload, ...
  // }>
  // dx / dy 是相对组件原始位置的累计偏移；同一组件多次拖动会覆盖同一条。
  const pendingLayoutChangesRef = useRef(new Map())
  // 渲染层快照：projectId -> 草稿数组，悬浮操作条据此显示数量。
  const [pendingLayoutChanges, setPendingLayoutChanges] = useState({})
  // 视觉暂存的新增组件：projectId -> Map<annotationId, annotation>。
  // 前端只画半透明预览，不写代码；用户点【保存布局】后才与位移一起提交。
  const pendingAdditionsRef = useRef(new Map())
  const [pendingAdditions, setPendingAdditions] = useState({})
  // 防止【保存布局】在 React 下一次渲染前被双击触发两次 Run。
  const layoutSaveInflightRef = useRef(new Set())
  // 布局提交可能被样式澄清打断：保存 snapshot，等恢复后的 Run 成功再
  // 统一清空 pendingAdditions / 位移草稿，避免“代码已生成、幽灵预览还在”。
  const layoutFlushRef = useRef(new Map())
  // 已成功写入代码的累计位移；保存成功前绝不更新，失败才能保留草稿。
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
      backgroundControllersRef.current.forEach((controller) => controller.abort())
      backgroundControllersRef.current.clear()
      annotationAbortRef.current.forEach((controller) => controller.abort())
      annotationAbortRef.current.clear()
      snapshotDiffAbortRef.current.forEach((controller) => controller.abort())
      snapshotDiffAbortRef.current.clear()
      // 组件卸载后丢弃所有未保存的布局 / 新增组件草稿。
      pendingLayoutChangesRef.current.clear()
      pendingAdditionsRef.current.clear()
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
      pendingDecision: null,
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

  /** DELETE_NODE 前置静态分析：只分析，不执行删除。 */
  const handleAnalyzeDelete = async (projectId, request = {}) => {
    const run = runStatesRef.current?.[projectId]
    const sandboxId = run?.result?.sandboxId
    const source = request.target ?? {}
    if (!sandboxId) {
      showToast('error', '请先生成或导入一个可预览项目')
      throw new Error('缺少沙盒，无法分析删除')
    }
    const directActionId = nextDirectActionId()
    const payload = {
      directActionId,
      projectId,
      sandboxId,
      tenantId: user?.tenantId || 'tenant_local',
      action: 'DELETE_NODE',
      baseRevision: 'analysis-only',
      target: {
        anchorId: source.anchorId,
        oid: source.oid,
        sourceRange: source.sourceRange,
        selector: source.selector,
        file: source.file,
        line: source.line || undefined,
        column: source.column || undefined,
        component: source.component,
        instance: source.instance,
        instanceCount: source.instanceCount,
        callSiteFile: source.callSite?.file,
        identity: source.identity,
        placement: source.placement,
      },
      payload: {},
      options: { source: 'FLOATING_TOOLBAR', dryRun: true },
    }
    try {
      return await analyzeDirectDelete(payload, session?.token)
    } catch (cause) {
      showToast('error', `删除分析失败：${cause?.message || cause}`)
      throw cause
    }
  }

  /** 提交 Direct Ops HITL 选择；仅 VISUAL_ONLY 会通知沙盒隐藏 DOM。 */
  const resolveDirectDecision = async (optionId) => {
    const current = directDecision
    if (!current?.decisionId || directDecisionBusy) return
    setDirectDecisionBusy(true)
    try {
      const result = await submitDirectDecision(
        { decisionId: current.decisionId, optionId },
        session?.token,
      )
      const status = result?.status
      if (status === 'VISUAL_ONLY') {
        directDecisionSourceRef.current?.postMessage?.(
          {
            channel: 'vibe-inspect',
            type: 'direct-visual-hide',
            payload: { source: current.source },
          },
          '*',
        )
        showToast('success', result?.message || '已隐藏当前项（仅视觉）')
      } else if (status === 'HANDOFF_TO_AI') {
        showToast('info', result?.message || '该操作需要 AI 修改数据源')
      } else {
        showToast('info', result?.message || '已取消')
      }
      directDecisionSourceRef.current = null
      setDirectDecision(null)
    } catch (cause) {
      showToast('error', `决策提交失败：${cause?.message || cause}`)
    } finally {
      setDirectDecisionBusy(false)
    }
  }

  /**
   * 接收沙盒内联 Direct Ops 的消息：
   *  - direct-add：内联 ➕ 新增基础组件
   *  - direct-delete-request / confirmed：删除分析 / 确认删除
   *  - direct-text：双击文本原位修改
   * 全部直接调用确定性接口，不进入 Agent / LLM。
   */
  useEffect(() => {
    if (!activeTabId) return undefined
    const reply = (source, type, payload) => {
      source?.postMessage?.(
        { channel: 'vibe-inspect', type, payload: payload || {} },
        '*',
      )
    }
    const handler = (event) => {
      const data = event.data
      if (!data || data.channel !== 'vibe-inspect') return
      if (data.type === 'direct-add') {
        const payload = data.payload || {}
        const targetProjectId = payload.projectId || activeTabId
        void handleDirectAction(targetProjectId, {
          action: 'ADD_COMPONENT',
          dryRun: false,
          target: payload.source,
          payload: {
            componentType: payload.componentType,
            styleContext: payload.styleContext || {},
          },
        }).catch((cause) => {
          reply(event.source, 'direct-result', {
            ok: false,
            message: `新增失败：${cause?.message || cause}`,
          })
        })
        return
      }
      if (data.type === 'direct-delete-request') {
        const payload = data.payload || {}
        const targetProjectId = payload.projectId || activeTabId
        void handleAnalyzeDelete(targetProjectId, { target: payload.source })
          .then((result) => {
            if (result?.decisionId && result?.options?.length) {
              // 需要用户决策：父页面弹出面板，SDK 只解锁 pending，不重复提示。
              directDecisionSourceRef.current = event.source
              setDirectDecision({
                ...result,
                projectId: targetProjectId,
                source: payload.source,
              })
              reply(event.source, 'direct-delete-result', {
                ...result,
                handled: true,
              })
              return
            }
            reply(event.source, 'direct-delete-result', result)
          })
          .catch((cause) => {
            reply(event.source, 'direct-delete-result', {
              safe: false,
              reasons: [String(cause?.message || cause)],
            })
          })
        return
      }
      if (data.type === 'direct-delete-confirmed') {
        const payload = data.payload || {}
        const targetProjectId = payload.projectId || activeTabId
        void handleDirectAction(targetProjectId, {
          action: 'DELETE_NODE',
          dryRun: false,
          target: payload.source,
        }).catch((cause) => {
          reply(event.source, 'direct-result', {
            ok: false,
            message: `删除失败：${cause?.message || cause}`,
          })
        })
        return
      }
      if (data.type === 'direct-undo') {
        const payload = data.payload || {}
        const targetProjectId = payload.projectId || activeTabId
        void handleUndoDirect(targetProjectId).catch((cause) => {
          reply(event.source, 'direct-result', {
            ok: false,
            message: `撤回失败：${cause?.message || cause}`,
          })
        })
        return
      }
      if (data.type === 'direct-text') {
        const payload = data.payload || {}
        const targetProjectId = payload.projectId || activeTabId
        void handleDirectAction(targetProjectId, {
          action: 'UPDATE_TEXT',
          dryRun: false,
          target: payload.source,
          payload: { text: payload.text },
        }).catch((cause) => {
          reply(event.source, 'direct-text-rollback', {
            originalText: payload.originalText,
          })
          reply(event.source, 'direct-result', {
            ok: false,
            message: `修改失败：${cause?.message || cause}`,
          })
        })
      }
    }
    window.addEventListener('message', handler)
    return () => window.removeEventListener('message', handler)
  }, [activeTabId, session?.token])

  /**
   * Direct Ops：悬浮工具栏 ➕ ❌ ✏️。
   *
   * - dryRun 不写代码，只验证目标解析与契约；
   * - 真正写入前先读取主工作树 revision，服务端做写锁 + 乐观并发校验；
   * - Phase 1 仅 UPDATE_TEXT 会落盘，其余动作返回 dryRun 结果。
   */
  const handleUndoDirect = async (projectId) => {
    const run = runStatesRef.current?.[projectId]
    const sandboxId = run?.result?.sandboxId
    if (!sandboxId) {
      showToast('error', '没有可撤回的沙盒')
      throw new Error('缺少沙盒')
    }
    const result = await undoLastDirectAction(
      projectId,
      sandboxId,
      session?.token,
    )
    showToast('success', `已撤回：${result.summary || '最近一次修改'}`)
    setPreviewRefresh((previous) => ({
      ...previous,
      [projectId]: (previous[projectId] ?? 0) + 1,
    }))
    return result
  }

  const handleDirectAction = async (projectId, request = {}) => {
    const project = projects.find((item) => item.id === projectId)
    const run = runStatesRef.current?.[projectId]
    const sandboxId = run?.result?.sandboxId
    if (!project || !sandboxId) {
      showToast('error', '请先生成或导入一个可预览项目')
      throw new Error('缺少沙盒，无法执行直接操作')
    }
    const dryRun = Boolean(request.dryRun)
    let revision = ''
    if (!dryRun) {
      const revisionPayload = await fetchDirectRevision(
        projectId,
        sandboxId,
        session?.token,
      )
      revision = revisionPayload.revision
    }
    const source = request.target ?? {}
    const directActionId = nextDirectActionId()
    const payload = {
      directActionId,
      idempotencyKey: `${projectId}:${directActionId}`,
      projectId,
      sandboxId,
      tenantId: user?.tenantId || 'tenant_local',
      baseRevision: revision || undefined,
      action: request.action,
      dryRun,
      target: {
        anchorId: source.anchorId,
        oid: source.oid,
        sourceRange: source.sourceRange,
        selector: source.selector,
        file: source.file,
        line: source.line || undefined,
        column: source.column || undefined,
        component: source.component,
        instance: source.instance,
        instanceCount: source.instanceCount,
        callSiteFile: source.callSite?.file,
        identity: source.identity,
        placement: source.placement,
      },
      payload: request.payload ?? {},
      options: {
        source: 'FLOATING_TOOLBAR',
        dryRun,
      },
    }
    try {
      const result = await submitDirectAction(payload, session?.token)
      if (result.status === 'SUCCEEDED') {
        showToast(
          'success',
          `直接修改已保存（${result.latencyMs}ms）`,
        )
        setPreviewRefresh((previous) => ({
          ...previous,
          [projectId]: (previous[projectId] ?? 0) + 1,
        }))
      } else if (result.status === 'DRY_RUN') {
        showToast(
          'info',
          `dryRun 通过：${result.risk}${
            result.resolvedTarget?.wouldChange === false
              ? '，内容无变化'
              : ''
          }`,
        )
      }
      return result
    } catch (cause) {
      const raw = String(cause?.message || cause)
      const friendly = raw.includes('DIRECT_REQUIRES_LLM')
        ? '该组件需要 AI 生成，请使用 Agent 添加'
        : raw.includes('DIRECT_WRITE_CONFLICT')
          ? '代码已被其它操作更新，请刷新后重试'
          : raw.includes('DIRECT_PROJECT_BUSY')
            ? '项目正在写入，请稍后再试'
            : raw.includes('DIRECT_TARGET_AMBIGUOUS')
              ? '该位置有多个实例，请更精确地选择'
              : `直接操作失败：${raw}`
      showToast('error', friendly)
      throw cause
    }
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

      const currentRun = runStatesRef.current?.[projectId] ?? EMPTY_RUN
      const sandboxId =
        fullstack.sandboxId || currentRun.result?.sandboxId || null
      const isFrontendBootstrap = fullstack.mode === 'frontend'
      const isVisualCodegen = Boolean(fullstack.result?.visualDocumentId)
      const isBootstrapLike = isFrontendBootstrap || isVisualCodegen
      const checkpointMessage = isVisualCodegen
        ? 'v0: Visual CodeGen'
        : isFrontendBootstrap
          ? 'v0: Architect Skeleton'
          : '自动存档：全栈代码已生成'

      // 先应用生成结果，再执行一次“无感 checkpoint”：
      // 骨架 / 全栈生成都会直接写主工作树，必须在用户发起下一次标注 Run
      // 之前把它提交掉，否则 begin_draft 会因 “主工作树存在未提交修改” 拒绝。
      // 这里复用现有 snapshotClient.createSnapshot，不新增任何接口或按钮。
      const applyGeneratedResult = (markReady = true) => {
        if (!workspaceMountedRef.current) return
        patchRun(projectId, (previous) => {
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

        if (isBootstrapLike) {
          // 初始骨架 / Visual CodeGen：把真实 App.jsx 写回项目，并把画布
          // 从虚拟树切换为 live。后续标注修改都基于这份源码。
          const entrySource = fullstack.result?.frontendEntry || ''
          if (!entrySource) {
            console.warn('[visual-codegen] 完成事件缺少 frontendEntry', {
              projectId,
              runId: fullstack.runId,
              mode: fullstack.mode,
            })
          }
          updateProject(projectId, (project) => ({
            ...project,
            // v0 快照是进入画布交互的前置条件：只有基线成功建立，
            // 才把 bootstrap 切到 ready。
            bootstrap: markReady ? 'ready' : 'failed',
            canvasMode: markReady ? 'live' : 'blank',
            generated: markReady ? true : project.generated,
            entryFile: 'src/App.jsx',
            files: entrySource
              ? { ...project.files, 'src/App.jsx': entrySource }
              : project.files,
          }))
          showToast(
            markReady ? 'success' : 'error',
            markReady
              ? isVisualCodegen
                ? '虚拟界面已编译为 React 源码，画布已切换为实时预览'
                : 'v0 基线已建立，画布已切换为实时预览'
              : 'v0 快照保存失败，请重试后再进入画布编辑',
          )
        } else {
          focusPreview(projectId)
          showToast('success', '全栈代码已生成，沙盒前后端已重启')
        }
      }

      if (!sandboxId) {
        applyGeneratedResult()
        return
      }

      // 同一项目同一时刻只允许一次自动存档；完成后才放开画布交互。
      autoSnapshotInflightRef.current.add(projectId)
      let snapshotOk = true
      void (async () => {
        try {
          const payload = await createSnapshot(
            sandboxId,
            checkpointMessage,
            session?.token,
            {
              projectId,
              tenantId: user?.tenantId || 'tenant_local',
              latestChanges: checkpointMessage,
            },
          )
          if (workspaceMountedRef.current) {
            patchRun(projectId, (previous) => ({
              ...previous,
              // 代码已进入 Git，下一步架构 / 标注 Run 可以安全创建 worktree。
              dirty: false,
              projectSummary:
                payload?.projectSummary || currentRun.projectSummary || '',
              latestChanges:
                payload?.latestChanges || currentRun.latestChanges || '',
              result: previous.result
                ? {
                    ...previous.result,
                    commitHash:
                      payload?.commitHash || previous.result.commitHash || null,
                    baselineCommit:
                      previous.result.baselineCommit ||
                      payload?.commitHash ||
                      null,
                  }
                : previous.result,
            }))
          }
        } catch (cause) {
          snapshotOk = false
          // 自动存档失败不阻断预览，但必须显式标脏并提示用户手动保存，
          // 否则下一次标注 Run 仍会撞上“主工作树存在未提交修改”。
          if (workspaceMountedRef.current) {
            patchRun(projectId, { dirty: true })
            showToast(
              'error',
              `自动存档失败，请点击【保存快照】后继续：${
                cause?.message || cause
              }`,
            )
          }
        } finally {
          autoSnapshotInflightRef.current.delete(projectId)
          // v0 基线失败时不得把骨架标记为 ready，画布保持 blank，
          // 用户可重试 bootstrap；全栈生成不阻塞预览。
          applyGeneratedResult(!isBootstrapLike || snapshotOk)
        }
      })()
    })
  }, [architectStates, updateProject, session?.token, user?.tenantId])

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
      // live 画布：iframe 文档坐标 + 圈选时滚动快照。滚动时 CanvasStage
      // 据此把圈重新投影到视口；页面切换 / 重载时整组标注会被清空。
      documentRect = null,
      frameScroll = null,
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

    const autoAction = geometryAction
      ? normalizeAction(geometryAction)
      : inferAnnotationAction(target, presetIntent, geometryAction)
    const annotationId = newId('ann')
    const annotation = {
      id: annotationId,
      rect,
      // live 画布专用的文档坐标（不受 iframe 滚动影响）。
      documentRect,
      frameScroll,
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
      // CREATE_NEW 是视觉暂存：不立刻调 Agent，等用户点【保存布局】
      // 或进入架构流程时，和位移一起统一提交。
      staged: isCreateAction(autoAction),
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
      baseCommitHash: runStatesRef.current[projectId]?.result?.commitHash ?? null,
      lastCommitHash: null,
      lastTurnCount: 0,
    }
    const count =
      runStatesRef.current[projectId]?.annotations.length ?? 0
    patchRun(projectId, (previous) => {
      if (previous.annotations.length >= MAX_ANNOTATIONS) return previous
      return {
        ...previous,
        annotations: [...previous.annotations, annotation],
      }
    })
    if (count < MAX_ANNOTATIONS && annotation.staged) {
      upsertPendingAddition(projectId, annotation)
    }
  }

  const handleAnnotationIntentChange = (projectId, annotationId, intent) => {
    const current = (runStatesRef.current[projectId] ?? EMPTY_RUN).annotations.find(
      (annotation) => annotation.id === annotationId,
    )
    const nextAction = current
      ? current.actionSource === 'manual'
        ? current.action
        : inferAnnotationAction(current.target, intent, current.geometryAction)
      : null
    patchRun(projectId, (previous) => ({
      ...previous,
      annotations: previous.annotations.map((annotation) =>
        annotation.id === annotationId
          ? {
              ...annotation,
              intent,
              // 自动模式随文案实时切换；手动选择过类型后尊重用户选择。
              action: nextAction ?? annotation.action,
              // 只有 CREATE_NEW 才是视觉暂存；改成 MODIFY 后恢复普通提交。
              staged: isCreateAction(nextAction ?? annotation.action),
            }
          : annotation,
      ),
    }))
    if (current) {
      upsertPendingAddition(projectId, {
        ...current,
        intent,
        action: nextAction ?? current.action,
        staged: isCreateAction(nextAction ?? current.action),
      })
    }
  }

  const handleAnnotationActionChange = (projectId, annotationId, action) => {
    const nextAction = normalizeAction(action)
    const current = (runStatesRef.current[projectId] ?? EMPTY_RUN).annotations.find(
      (annotation) => annotation.id === annotationId,
    )
    patchRun(projectId, (previous) => ({
      ...previous,
      annotations: previous.annotations.map((annotation) =>
        annotation.id === annotationId
          ? {
              ...annotation,
              action: nextAction,
              actionSource: 'manual',
              staged: isCreateAction(nextAction),
            }
          : annotation,
      ),
    }))
    if (current) {
      upsertPendingAddition(projectId, {
        ...current,
        action: nextAction,
        actionSource: 'manual',
        staged: isCreateAction(nextAction),
      })
    }
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
    // 视觉暂存还没有写进代码，删除时只清本地草稿，绝不能触发 Git 回滚。
    removePendingAddition(projectId, annotationId)

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

  /**
   * iframe 内部路由切换 / 整页重载：当前页面已经不是圈选时那一个。
   *
   * 只清前端标注与拖拽草稿，**绝不回滚代码**——用户可能只是切了个 Tab，
   * 已生成的修改仍然有效。与“清空标注”按钮（会 reset 到基线）严格区分。
   */
  const handleFrameRouteChange = (projectId, payload) => {
    const run = runStates[projectId] ?? EMPTY_RUN
    const hadAnnotations =
      run.annotations.length > 0 || Boolean(run.clarification)
    pendingLayoutChangesRef.current.delete(projectId)
    setPendingLayoutChanges((previous) => {
      if (!previous[projectId]) return previous
      const next = { ...previous }
      delete next[projectId]
      return next
    })
    // 页面已经切换，暂存的新增组件也失去了落点；只清前端草稿，不回滚代码。
    clearPendingAdditions(projectId)
    patchRun(projectId, (previous) => {
      if (
        previous.annotations.length === 0 &&
        !previous.clarification &&
        !previous.pendingAnnotationId
      ) {
        return previous
      }
      return {
        ...previous,
        annotations: [],
        clarification: null,
        pendingAnnotationId: null,
        pendingAction: null,
      }
    })
    if (hadAnnotations) {
      showToast(
        'info',
        `页面已切换（${payload?.reason === 'reload' ? '重新加载' : '路由变化'}），已清除旧标注，请重新圈选`,
      )
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
      clearPendingAdditions(projectId)
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
      clearPendingAdditions(projectId)
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
      background = false,
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
          // 后台模式使用更长的 Coder 超时；普通模式仍严格 30s 熔断。
          background: Boolean(background),
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
   * 后台 Run：dispatch 后立即释放前端，SSE 在独立连接里继续。
   *
   * 与普通 attach 的区别：
   *  - 不改 `submitting`，用户可以继续圈选 / 浏览；
   *  - 不占用项目 epoch，后续普通提交不会中止后台任务；
   *  - 只有终态才回写 result / 标注聊天，并弹 Toast。
   */
  const startBackgroundUiRun = (
    projectId,
    { runId, streamPath, annotationId },
  ) => {
    const key = `${projectId}:${runId}`
    backgroundControllersRef.current.get(key)?.abort()
    const controller = new AbortController()
    backgroundControllersRef.current.set(key, controller)
    patchRun(projectId, (previous) => ({
      ...previous,
      submitting: false,
      pendingAnnotationId: null,
      pendingAction: null,
      error: null,
      runId,
      lastSeq: 0,
      timeline: [],
      patchPreview: '',
      clarification: null,
      approval: null,
      cancelRequested: false,
      targetAnnotationId: annotationId,
      supervisorHandoff: 'handoff_ui',
      supervisorPhase: 'background_running',
      backgroundRun: {
        runId,
        streamPath,
        annotationId,
        startedAt: Date.now(),
      },
    }))

    void (async () => {
      let terminal = null
      try {
        await streamRun({
          runId,
          attach: true,
          streamPath,
          token: session?.token,
          signal: controller.signal,
          onEvent: (event) => {
            if (TERMINAL_EVENT_TYPES.has(event.type)) terminal = event
          },
        })
      } catch (cause) {
        if (
          cause?.name !== 'AbortError' &&
          workspaceMountedRef.current &&
          !closedProjectsRef.current.has(projectId)
        ) {
          const message = `后台任务事件流中断：${cause?.message || cause}`
          patchRun(projectId, (previous) => ({
            ...previous,
            backgroundRun: null,
            error: message,
          }))
          showToast('error', message)
        }
        return
      } finally {
        backgroundControllersRef.current.delete(key)
      }
      if (
        !workspaceMountedRef.current ||
        closedProjectsRef.current.has(projectId)
      ) {
        return
      }
      if (!terminal) {
        patchRun(projectId, (previous) => ({
          ...previous,
          backgroundRun: null,
          error: '后台任务事件流已结束，但未收到终态，请刷新查看结果',
        }))
        return
      }
      const current = runStatesRef.current?.[projectId] ?? EMPTY_RUN
      const result = resultFromTerminalEvent(current, terminal)
      patchRun(projectId, (previous) => ({
        ...finalizeFromResult(previous, result, annotationId),
        backgroundRun: null,
        // 成功即清空重试快照；失败保留，便于再次转后台。
        backgroundRequest: isSuccessStatus(result.status)
          ? null
          : previous.backgroundRequest,
      }))
      if (isSuccessStatus(result.status)) {
        showToast('success', '后台任务已完成，预览已更新')
        focusPreview(projectId)
      } else {
        showToast(
          'error',
          `后台任务失败：${result.error || '构建验证未通过'}`,
        )
      }
    })()
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
      // 后台模式：dispatch 后立即释放前端，不阻塞用户继续圈选。
      background = false,
    },
  ) => {
    const project = projects.find((item) => item.id === projectId)
    const run = runStates[projectId] ?? EMPTY_RUN
    if (!project || run.submitting || inflightRef.current.has(projectId)) {
      return { ok: false, reason: 'busy' }
    }
    if (run.backgroundRun) {
      showToast('info', '后台任务执行中，完成后会自动通知你')
      return { ok: false, reason: 'background' }
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
      background,
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
      backgroundRun: null,
      // 失败后“转后台运行”必须能拿回原始指令；sendEntries 在成功提交时
      // 会被清空 intent，因此这里先保存一份不可变快照。
      // 转后台的重放不携带 sendEntries（避免重复写用户气泡），此时保留
      // 上一次快照，确保再次失败仍能继续转后台。
      backgroundRequest:
        annotations.length > 0 && sendEntries.length > 0
          ? {
              message,
              sendEntries,
              annotationId,
              createdAt: new Date().toISOString(),
            }
          : previous.backgroundRequest,
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
        if (background) {
          // 后台模式：立即释放 submitting，用户可继续圈选；SSE 独立订阅。
          releaseInflight()
          startBackgroundUiRun(projectId, {
            runId: ui.runId,
            streamPath: ui.streamPath,
            annotationId,
          })
          showToast('info', '已转后台运行，完成后会通知你')
          return {
            ok: true,
            reason: 'background',
            runId: ui.runId,
            decision: decisionView,
          }
        }
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

  /** 新建项目强制分流：先选择从零开始或导入源码。 */
  const openNewProjectDialog = () => {
    if (limitReached) {
      showToast('error', `最多只能同时打开 ${maxTabs} 个项目，请关闭其他项目`)
      return
    }
    setSourceImportError('')
    setNewProjectOpen(true)
  }

  const handleCreateBlankProject = () => {
    setNewProjectOpen(false)
    createProject()
  }

  const handleImportSourceFile = async (file) => {
    if (!file || sourceImportBusy) return
    setSourceImportError('')
    if (limitReached) {
      setSourceImportError(`最多只能同时打开 ${maxTabs} 个项目，请先关闭其他项目`)
      return
    }
    if (!/\.(jsx?|tsx?)$/i.test(file.name)) {
      setSourceImportError('仅支持 .jsx / .tsx / .js / .ts 文件')
      return
    }
    if (file.size > 500 * 1024) {
      setSourceImportError('文件超过 500KB，请拆分后再导入')
      return
    }
    setSourceImportBusy(true)
    try {
      const source = await file.text()
      const projectName =
        file.name.replace(/\.(jsx?|tsx?)$/i, '') || '存量项目'
      const files = { [TARGET_FILE]: source }
      const projectId = newId('proj')
      // 先落库再建本地项目：刷新 / 换浏览器后仍能从 BFF 回灌源码事实。
      const saved = await saveProjectSource(
        {
          projectId,
          tenantId: user?.tenantId || 'tenant_local',
          projectName,
          entryFile: TARGET_FILE,
          sourceName: file.name,
          files,
        },
        session?.token,
      )
      // 工作区项目统一落到 Vite 入口，保证后续后端增量绑定有可运行的前端；
      // 只读投影仍使用原始文件名展示分析来源。
      importProject({
        id: projectId,
        name: projectName,
        files,
        entryFile: TARGET_FILE,
        kind: 'react',
        sourceName: file.name,
      })
      updateProject(projectId, {
        sourceRevision: Number(saved?.revision) || 1,
      })
      storeImportedSource(projectId, file.name, source)
      setNewProjectOpen(false)
      navigate(
        `/visual-canvas?project=${encodeURIComponent(
          projectId,
        )}&mode=existing`,
      )
    } catch (cause) {
      const message = `导入失败：${cause?.message || cause}`
      setSourceImportError(message)
      // 直达导入没有弹窗承载错误，必须用 toast 保证用户能看到失败原因。
      if (!newProjectOpen) showToast('error', message)
    } finally {
      setSourceImportBusy(false)
    }
  }

  /**
   * 直达导入：空工作区不需要先打开“新建项目”对话。
   * 选择文件后仍复用 handleImportSourceFile 的落盘 + 只读投影跳转。
   */
  const openDirectImportPicker = () => {
    if (sourceImportBusy) return
    setSourceImportError('')
    directImportInputRef.current?.click()
  }

  /**
   * 工具栏主 CTA：已有 Run 时恢复同一面板；否则也走 Supervisor，
   * 由后端决定是纯 Architect 还是 UI + Architect 的 both。
   */
  const handleStartArchitect = async (projectId, goal = '') => {
    const project = projects.find((item) => item.id === projectId)
    const run = runStates[projectId] ?? EMPTY_RUN
    const projectMode = resolveProjectMode(project, {
      sandboxId: run.result?.sandboxId,
    })
    // 保存门控：未保存的生成 / 拖拽 / 样式修改绝不能进入全栈澄清。
    if (run.dirty) {
      showToast('info', '请先保存当前修改')
      return
    }

    // 存量项目：禁止走虚拟树 / 新建契约。Architect 只做现有源码的
    // 业务审查与后端增量绑定，System Prompt 由后端按 projectMode 切换。
    if (projectMode === PROJECT_MODE.EXISTING_SOURCE) {
      const text = goal.trim()
      if (!text) {
        architect.openPanel(projectId)
        return
      }
      const sourceFiles = project?.files || {}
      if (
        Object.keys(sourceFiles).length === 0 &&
        !run.result?.sandboxId
      ) {
        showToast('error', '没有找到可分析的源码，请重新导入已有 React 文件')
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
        projectName: project?.name || '存量项目',
        source: run.result?.sandboxId
          ? { sandboxId: run.result.sandboxId, files: sourceFiles }
          : { files: sourceFiles },
        projectSummary: run.projectSummary || '',
        latestChanges: run.latestChanges || '',
        uiHistory: [
          {
            role: 'system',
            content:
              '【EXISTING_SOURCE】当前项目已有 React 源码，源码是唯一事实源。' +
              '禁止生成 VisualDocument、整页 JSX 或全量前端代码；' +
              '只基于现有文件树、组件、路由与交互做业务审查和后端增量绑定。',
          },
          ...history,
          { role: 'user', content: text },
        ],
        projectMode,
        options: { llmProvider: modelProvider },
      })
      return
    }

    // 【统一提交】进入架构澄清前，先把画布上所有视觉暂存一次性写进代码。
    // 保存失败则停留在当前页面，草稿不会丢，避免 Architect 基于旧界面推演。
    const pendingSummary = describePendingChanges(projectId)
    if (pendingSummary) {
      const saved = await handleSaveLayout(projectId)
      if (!saved) {
        showToast('info', '画布变更尚未保存成功，已暂停进入架构澄清')
        return
      }
    }
    // 用 system 角色注入，只作为 Architect 的上下文事实，不污染用户目标。
    const pendingContext = pendingSummary
      ? [{ role: 'system', content: pendingSummary }]
      : []

    // v0 → 当前快照 Diff：Architect 必须能看到用户后续直接改了什么。
    let snapshotDiff = ''
    if (run.result?.sandboxId && run.result.commitHash) {
      try {
        const diffPayload = await fetchSnapshotDiff(
          run.result.sandboxId,
          run.result.commitHash,
          session?.token,
          undefined,
          'baseline',
        )
        snapshotDiff = String(diffPayload?.diff || '')
      } catch (cause) {
        console.warn('[architect] snapshot diff unavailable', cause)
      }
    }
    const snapshotContext = snapshotDiff
      ? [
          {
            role: 'system',
            content:
              '最近快照 Diff（v0 → 当前）。请结合 ChangeLog 主动识别用户删除/新增/' +
              '修改的业务组件；在生成后端 API 或契约前，针对新的数据实体主动询问' +
              '数据源与建表需求。\n' +
              snapshotDiff.slice(0, 6000),
          },
        ]
      : []

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
      const frozenDocument = loadFrozenVisualDocument(projectId)
      const v0Baseline = loadVisualV0Baseline(projectId)
      architect.start(projectId, {
        runId: newId('arc'),
        tenantId: user?.tenantId || 'tenant_local',
        projectId,
        projectName: project?.name || '未命名项目',
        // 新项目没有源码；Architect 仅依据自然语言目标推演架构。
        source: run.result?.sandboxId
          ? { sandboxId: run.result.sandboxId, files: {} }
          : { files: {} },
        projectSummary: run.projectSummary || '',
        latestChanges: run.latestChanges || '',
        uiHistory: [
          ...history,
          ...pendingContext,
          ...snapshotContext,
          { role: 'user', content: text },
        ],
        projectMode: PROJECT_MODE.NEW_PROJECT,
        // 首次澄清只产出 v0 虚拟树；冻结后再走 BACKEND_BINDING。
        outputPhase: frozenDocument ? 'BACKEND_BINDING' : 'VISUAL_V0',
        visualDocument: frozenDocument || undefined,
        visualDelta:
          frozenDocument && v0Baseline
            ? buildVisualDelta(v0Baseline, frozenDocument)
            : undefined,
        options: { llmProvider: modelProvider },
      })
      return
    }

    // 旧项目 / 演示项目：保留 Supervisor 统一入口，兼容既有画布标注流程。
    const annotations = run.annotations.filter((item) => item.intent.trim())
    const message = [
      '下一步：构建全栈方案',
      ...annotations.map((item) => item.intent.trim()),
      ...(pendingSummary ? [pendingSummary] : []),
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
   * 存量项目：只做后端 / 数据库增量绑定。
   *
   * 绝不运行 FrontendCoder、绝不触发 Visual CodeGen；复用 FullstackGraph
   * 的 backend 子图（DBACoder → BackendCoder → BuildVerifier → Restart）。
   */
  const handleExistingBackendGenerate = async (
    projectId,
    { project, contract },
  ) => {
    if (!contract?.contractId) {
      showToast('error', '契约尚未确认，无法生成后端增量')
      return false
    }
    if (contract.status && contract.status !== 'CONFIRMED') {
      showToast('error', `契约状态为 ${contract.status}，确认后才能生成`)
      return false
    }
    if (contract.visualDocument) {
      showToast(
        'error',
        '存量项目禁止前端 CodeGen；请使用源码直改或仅生成后端增量',
      )
      return false
    }
    if (contract.appShape !== 'full_stack_system') {
      showToast('info', '存量项目后端增量需要 full_stack_system 契约')
      return false
    }
    const apiRouteCount = Array.isArray(contract.apiRoutes)
      ? contract.apiRoutes.length
      : 0
    if (apiRouteCount === 0) {
      showToast('error', '契约缺少 REST 接口（apiRoutes），无法绑定后端')
      return false
    }

    const gate = gateStates[projectId]
    if (
      gate?.gateId &&
      (gate.status === 'AWAITING_CONTRACT' || gate.status === 'RELEASING')
    ) {
      await releaseGateFor(projectId, gate.gateId, { awaitUi: false })
    }
    const run = runStates[projectId] ?? EMPTY_RUN
    const files = run.result?.sandboxId
      ? {}
      : { ...(project?.files || {}) }
    const started = architect.generateFullstack(projectId, {
      tenantId: user?.tenantId || 'tenant_local',
      sandboxId: run.result?.sandboxId,
      files,
      contract,
      contractId: contract.contractId,
      mode: 'backend',
      projectMode: PROJECT_MODE.EXISTING_SOURCE,
      options: { maxOutputChars: 20000 },
    })
    if (started) {
      showToast(
        'info',
        '正在基于现有前端生成后端与数据库增量，不会重写前端代码…',
      )
    } else {
      showToast('info', '后端增量正在生成中，请稍候')
    }
    return started
  }

  /**
   * 冻结 VisualDocument -> CodeGen。
   *
   * 只接受浏览器里已冻结（FROZEN + contentHash）的虚拟树；确定性 Compiler
   * 会注入 data-node-id，然后复用 FullstackGraph 的构建 / 重启门禁。
   * 全栈契约同时具备 API 与 DDL 时走 fullstack，否则只编译前端。
   */
  const handleVisualCodegen = (projectId, { project, contract }) => {
    if (resolveProjectMode(project) === PROJECT_MODE.EXISTING_SOURCE) {
      showToast(
        'error',
        '存量项目不生成虚拟树 / 前端 CodeGen；请走源码直改或仅生成后端增量',
      )
      return false
    }
    const document = loadFrozenVisualDocument(projectId)
    if (!document) {
      showToast(
        'error',
        '没有找到已冻结的虚拟界面：请先在虚拟画布中点击“确认视觉，进入业务审查”',
      )
      return false
    }
    if (
      contract?.visualDocument?.docId &&
      document.docId !== contract.visualDocument.docId
    ) {
      showToast('error', '冻结界面与当前契约不匹配，请重新打开虚拟画布确认')
      return false
    }
    const report = validateVisualDocument(document)
    if (!report.ok) {
      showToast('error', `冻结界面校验失败：${report.errors[0] || '未知错误'}`)
      return false
    }
    if (!contract?.contractId) {
      showToast('error', '契约尚未确认，无法编译冻结界面')
      return false
    }
    if (contract.status && contract.status !== 'CONFIRMED') {
      showToast('error', `契约状态为 ${contract.status}，确认后才能生成代码`)
      return false
    }
    if (contract.appShape === 'script_automation') {
      showToast('info', '脚本类项目不包含前端页面，无法编译虚拟界面')
      return false
    }
    const apiRouteCount = Array.isArray(contract.apiRoutes)
      ? contract.apiRoutes.length
      : 0
    const hasDdl =
      typeof contract.databaseDdl === 'string' &&
      contract.databaseDdl.trim().length > 0
    const mode =
      contract.appShape === 'full_stack_system' &&
      apiRouteCount > 0 &&
      hasDdl
        ? 'fullstack'
        : 'frontend'
    const run = runStates[projectId] ?? EMPTY_RUN
    // 没有沙盒时至少携带当前入口源码，服务端会在模板沙盒上覆盖它；
    // fullstack 模式也依赖这份兜底源码通过 _ensure_sandbox 校验。
    const files = run.result?.sandboxId
      ? {}
      : { [projectEntryFile(project)]: projectSource(project) }

    console.info('[visual-codegen] start', {
      projectId,
      contractId: contract.contractId,
      documentId: document.docId,
      contentHash: document.metadata?.contentHash,
      nodeCount: report.nodeCount,
      mode,
    })
    updateProject(projectId, {
      bootstrap: 'bootstrapping',
      canvasMode: 'bootstrapping',
    })
    const started = architect.generateFullstack(projectId, {
      tenantId: user?.tenantId || 'tenant_local',
      sandboxId: run.result?.sandboxId,
      files,
      contract,
      contractId: contract.contractId,
      mode,
      // handleVisualCodegen 已前置拒绝存量项目，这里显式传 NEW_PROJECT，
      // 避免服务端被旧契约 / 模板源码误判。
      projectMode: PROJECT_MODE.NEW_PROJECT,
      visualDocument: document,
      options: { maxOutputChars: 20000 },
    })
    if (!started) {
      updateProject(projectId, {
        bootstrap: 'failed',
        canvasMode: 'visual',
      })
      showToast('info', '代码生成正在进行中，请稍候')
      return false
    }
    showToast(
      'info',
      `正在把 ${report.nodeCount} 个虚拟节点编译为 React 源码…`,
    )
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
    // 存量项目：不走 FrontendCoder / CodeGen，只做后端与数据库增量。
    if (resolveProjectMode(project) === PROJECT_MODE.EXISTING_SOURCE) {
      return handleExistingBackendGenerate(projectId, { project, contract })
    }
    // 离线优先契约：冻结虚拟树才是前端事实源，不能再走源码暂存 / 旧骨架。
    if (contract?.visualDocument) {
      handleVisualCodegen(projectId, { project, contract })
      return
    }
    // 全栈生成会以当前前端为基准推演后端；先统一提交画布暂存，
    // 否则用户刚加的组件不会出现在生成结果里。
    if (describePendingChanges(projectId)) {
      const saved = await handleSaveLayout(projectId)
      if (!saved) {
        showToast('info', '画布变更尚未保存成功，已暂停全栈生成')
        return
      }
    }
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
      // 与上方分流使用同一判定，不在请求层引入第二套依据。
      projectMode: resolveProjectMode(project),
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
    if (resolveProjectMode(project) === PROJECT_MODE.EXISTING_SOURCE) {
      showToast('info', '存量项目不初始化前端骨架；请走源码直改')
      return
    }
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
      projectMode: PROJECT_MODE.NEW_PROJECT,
      options: { maxOutputChars: 20000 },
    })
    if (!started) {
      updateProject(projectId, { bootstrap: 'failed' })
      showToast('error', '前端初始化启动失败，请重试')
      return
    }
    if (manual) showToast('info', '正在根据契约生成初始前端页面…')
  }

  /**
   * Architect → 离线画布。
   *
   * contract.visualDocument 是第一阶段唯一前端产物：
   *  - 校验通过后直接写入 visualDocumentStore；
   *  - 不启动 FrontendCoder / 沙盒 / 任何真实源码生成；
   *  - 契约确认后只自动打开一次虚拟画布，返回工作台不会再次跳转。
   */
  useEffect(() => {
    if (!activeTabId) return
    const state = architectStates[activeTabId]
    const contract = state?.contract
    const visual = contract?.visualDocument
    if (!visual) return

    const key = [
      contract.contractId || '',
      visual.revision ?? 0,
      visual.metadata?.contentHash || '',
    ].join(':')
    const syncKeyStorage = `vibe.visual.sync.${activeTabId}`
    let storedSyncKey = ''
    let localDocument = null
    try {
      storedSyncKey = window.localStorage.getItem(syncKeyStorage) || ''
      localDocument = JSON.parse(
        window.localStorage.getItem(VISUAL_DOCUMENT_STORAGE_KEY) || 'null',
      )
    } catch {
      // 隐私模式 / 损坏 JSON：按未同步处理。
    }
    const currentDocument = useVisualDocumentStore.getState().doc
    const hasLocalDocument =
      currentDocument?.projectId === activeTabId ||
      localDocument?.projectId === activeTabId
    const alreadySynced = storedSyncKey === key && hasLocalDocument
    const shouldLoad =
      !alreadySynced &&
      visualDocumentSyncRef.current.get(activeTabId) !== key
    if (shouldLoad) {
      const project = projects.find((item) => item.id === activeTabId)
      const document = visualDocumentFromContract(contract, {
        projectId: activeTabId,
        title: project?.name || '未命名页面',
      })
      const report = validateVisualDocument(document)
      if (!report.ok) {
        console.warn('[visual-document] invalid contract payload', {
          projectId: activeTabId,
          contractId: contract.contractId,
          errors: report.errors,
        })
        if (!visualDocumentNoticeRef.current.has(`invalid:${key}`)) {
          visualDocumentNoticeRef.current.add(`invalid:${key}`)
          showToast(
            'error',
            `虚拟骨架校验失败：${report.errors[0] || 'Schema 不合法'}`,
          )
        }
        return
      }
      useVisualDocumentStore.getState().replaceDocument(document, {
        resetHistory: true,
      })
      // V0 是增量审查的基线；后续 BACKEND_BINDING 只传 delta。
      if ((contract.outputPhase || state.outputPhase) === 'VISUAL_V0') {
        storeVisualV0Baseline(activeTabId, document)
      }
      visualDocumentSyncRef.current.set(activeTabId, key)
      try {
        window.localStorage.setItem(syncKeyStorage, key)
      } catch {
        // 隐私模式下至少当前页面内由 ref 防重复覆盖。
      }
      updateProject(activeTabId, {
        bootstrap: 'ready',
        canvasMode: 'visual',
        generated: false,
      })
      if (!visualDocumentNoticeRef.current.has(`loaded:${key}`)) {
        visualDocumentNoticeRef.current.add(`loaded:${key}`)
        showToast('success', '虚拟骨架已生成，可进入画布自由调整')
      }
    }

    if (
      state.status !== 'CONFIRMED' &&
      state.status !== 'AWAITING_VISUAL_EDIT'
    ) {
      return
    }
    // 最终后端绑定契约不再自动跳回画布；用户已经完成视觉编辑。
    if (contract.outputPhase === 'BACKEND_BINDING') return
    const activeDocument = useVisualDocumentStore.getState().doc
    if (activeDocument?.docId !== visual.docId) return
    const autoOpenKey = `vibe.visual.autoOpened.${activeTabId}.${contract.contractId}`
    if (visualDocumentAutoOpenRef.current.has(autoOpenKey)) return
    visualDocumentAutoOpenRef.current.add(autoOpenKey)
    try {
      if (window.sessionStorage.getItem(autoOpenKey)) return
      window.sessionStorage.setItem(autoOpenKey, '1')
    } catch {
      // 隐私模式无法写 sessionStorage 时，本页仍只跳转一次。
    }
    const project = projects.find((item) => item.id === activeTabId)
    navigate(
      `/visual-canvas?project=${encodeURIComponent(
        activeTabId,
      )}&title=${encodeURIComponent(
        activeDocument.title || project?.name || '未命名页面',
      )}`,
    )
  }, [
    activeTabId,
    architectStates,
    navigate,
    projects,
    updateProject,
  ])

  /**
   * 冻结画布 → Architect 业务审查。
   *
   * VisualCanvasPage 只会把语义审查包写入 sessionStorage 并跳回工作台；
   * 这里消费一次，启动 Architect Run，复用现有多轮澄清与契约面板。
   */
  useEffect(() => {
    if (!activeTabId) return undefined
    const pending = peekSemanticReviewPacket(activeTabId)
    if (!pending) return undefined
    // StrictMode 会先执行一次 effect 再模拟卸载；延迟到稳定挂载后消费，
    // 避免刚刚 POST 的 Architect SSE 被第一轮 cleanup abort。
    const timer = window.setTimeout(() => {
      const packet = takeSemanticReviewPacket(activeTabId)
      if (!packet) return
      const key = `${activeTabId}:${packet.contentHash || packet.documentId}`
      if (visualReviewStartedRef.current.has(key)) return
      visualReviewStartedRef.current.add(key)
      const project = projects.find((item) => item.id === activeTabId)
      const frozenDocument = loadFrozenVisualDocument(activeTabId)
      architect.openPanel(activeTabId)
      architect.start(activeTabId, {
        runId: newId('arc'),
        tenantId: user?.tenantId || 'tenant_local',
        projectId: activeTabId,
        projectName: project?.name || packet.title || '未命名项目',
        source: { files: {} },
        projectSummary: packet.summary,
        latestChanges: packet.summary,
        projectMode: PROJECT_MODE.NEW_PROJECT,
        outputPhase: 'BACKEND_BINDING',
        visualDocument: frozenDocument || undefined,
        visualDelta: packet.visualDelta || undefined,
        uiHistory: [
          {
            role: 'system',
            content: buildSemanticReviewContext(packet),
          },
          {
            role: 'user',
            content:
              '请开始业务审查：只针对这份已冻结虚拟界面的交互和数据绑定缺口发起澄清，' +
              '不要重新询问页面布局或视觉风格。',
          },
        ],
        options: { llmProvider: modelProvider },
      })
    }, 300)
    return () => window.clearTimeout(timer)
  }, [
    activeTabId,
    architect.openPanel,
    architect.start,
    modelProvider,
    projects,
    user?.tenantId,
  ])

  // 契约一旦 CONFIRMED，旧版无 visualDocument 契约才触发前端骨架生成。
  // 新链路必须等用户在虚拟画布确认视觉后，由阶段三 CodeGen 统一生成。
  useEffect(() => {
    Object.entries(architectStates).forEach(([projectId, state]) => {
      if (state?.status !== 'CONFIRMED') return
      if (state?.contract?.visualDocument) return
      const project = projects.find((item) => item.id === projectId)
      if (!project || project.bootstrap !== 'pending') return
      const key = state.contract?.contractId || state.runId
      if (!key) return
      if (frontendBootstrapRef.current.get(projectId) === key) return
      frontendBootstrapRef.current.set(projectId, key)
      handleFrontendBootstrap(projectId)
    })
  }, [architectStates, projects])

  // 前端初始化 / Visual CodeGen 失败：把画布置为失败态并保留重试入口。
  useEffect(() => {
    Object.entries(architectStates).forEach(([projectId, state]) => {
      const fullstack = state?.fullstack
      if (!fullstack?.runId) return
      const isVisual = Boolean(state?.contract?.visualDocument)
      if (
        fullstack.status !== 'FAILED' ||
        (fullstack.mode !== 'frontend' && !isVisual)
      ) {
        return
      }
      const project = projects.find((item) => item.id === projectId)
      if (!project || project.bootstrap !== 'bootstrapping') return
      if (frontendBootstrapFailureRef.current.get(projectId) === fullstack.runId)
        return
      frontendBootstrapFailureRef.current.set(projectId, fullstack.runId)
      updateProject(projectId, {
        bootstrap: 'failed',
        canvasMode: isVisual ? 'visual' : 'blank',
      })
      showToast(
        'error',
        `${isVisual ? '虚拟界面编译' : '初始前端页面生成'}失败：${
          fullstack.error || '请重试'
        }`,
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
    // 暂停事件同样需要返回给调用方：布局保存要靠它区分“失败”与
    // “等待样式澄清”，否则会误删恢复成功后仍需使用的草稿快照。
    let clarificationEvent = null

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
        if (event.type === 'clarification.required') {
          clarificationEvent = event
        }
        // 代码已经写入沙盒：视觉暂存必须在这一帧立刻退场。SSE 终态可能
        // 早于 handleSaveLayout / handleClarificationAnswer 的 await 返回，
        // 如果等调用方收尾，就会出现“真实按钮 + 幽灵按钮”并存的重复帧。
        if (event.type === 'run.succeeded') {
          clearPendingAdditionsForRun(projectId, {
            annotationId,
            runId: event.runId ?? event.payload?.runId ?? null,
          })
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
      // 供静默拖拽同步 / 布局暂存判断结果；常规调用方无需消费。
      // 没有终态时返回澄清事件，调用方可据此区分“暂停”和“失败”。
      return terminalEvent ?? clarificationEvent
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
    reference = null,
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
    patchRun(projectId, (previous) => {
      // 先挂 reference，再 append 用户回复；两份状态都不会因网络失败丢失。
      const withReference = attachAnnotationReference(
        previous,
        annotationId,
        reference,
      )
      return {
        ...appendClarificationReply(
          withReference,
          annotationId,
          clarification,
          answers,
          comment,
        ),
        submitting: true,
        pendingAnnotationId: annotationId,
        pendingAction: 'run',
        clarification: null,
        error: null,
      }
    })
    // 多轮澄清期间幽灵预览必须原地更新：同一个 annotationId 只刷新
    // 意图文案，不新增视觉占位符。参考组件的真实类名也写进幽灵提示，
    // 让用户在 Coder 执行前就能确认“抄的是不是这个”。
    updatePendingAdditionClarification(
      projectId,
      annotationId,
      [
        ...(answers ?? []).map((item) => _formatAnswerValue(item?.value)),
        comment,
        reference
          ? `参考「${reference.label || reference.tag || '组件'}」${
              reference.classes?.length
                ? `：${reference.classes.slice(0, 12).join(' ')}`
                : ''
            }`
          : '',
      ]
        .filter(Boolean)
        .join('；'),
      reference,
    )

    // reference 随第一条答案提交；即使问题答案已存在，也要让后端拿到
    // 结构化视觉事实。answers 为空时补一条伪答案承载 reference。
    const payloadAnswers = reference
      ? answers?.length
        ? answers.map((item, index) =>
            index === 0 ? { ...item, reference } : item,
          )
        : [
            {
              questionId: '__visual_reference__',
              value: reference.label || 'visual-reference',
              reference,
            },
          ]
      : answers

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
            answers: payloadAnswers,
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
      const terminal = await startRunStream(projectId, null, annotationId, {
        replay: true,
        runId: run.runId,
        afterSeq: run.lastSeq,
      })
      // 布局提交被样式澄清打断时，真正的落盘发生在恢复后的 Run；只有
      // run.succeeded 才清空幽灵预览与草稿，失败 / 再次暂停都保留。
      const snapshot = layoutFlushRef.current.get(projectId)
      if (snapshot) {
        const belongsToLayout =
          (snapshot.runId === null || snapshot.runId === run.runId) &&
          (snapshot.additionIds.includes(annotationId) ||
            snapshot.moves.length === 0)
        if (belongsToLayout && terminal?.type === 'run.succeeded') {
          const committed = commitLayoutSnapshot(projectId, snapshot)
          layoutFlushRef.current.delete(projectId)
          showToast(
            'success',
            committed.additions
              ? `已同步 ${committed.additions} 个新增组件${
                  committed.moves ? `与 ${committed.moves} 处位置调整` : ''
                }`
              : '布局已同步',
          )
        } else if (
          terminal?.type === 'run.failed' ||
          terminal?.type === 'run.cancelled' ||
          terminal?.type === 'run.rejected'
        ) {
          // 终态失败：保留画布草稿供重试，但删除过期快照，避免后续
          // 无关的澄清成功误清用户草稿。
          layoutFlushRef.current.delete(projectId)
        } else if (snapshot.runId === null) {
          snapshot.runId = run.runId
        }
      }
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
   * 安全路径决策：POST 202 后用 lastSeq 重放事件流。
   *
   * 只提交服务端生成的 optionId；cancel 由服务端直接回滚并发送
   * run.cancelled，其余选项恢复 decision_gate 后重新进入 Resolver。
   */
  const handleDecisionAnswer = async (projectId, optionId, comment = '') => {
    const run = runStates[projectId] ?? EMPTY_RUN
    const decision = run.pendingDecision
    if (
      !run.runId ||
      !decision?.decisionId ||
      !optionId ||
      run.submitting ||
      inflightRef.current.has(projectId)
    ) {
      return
    }
    const option = (decision.options ?? []).find(
      (item) => item.optionId === optionId,
    )
    inflightRef.current.add(projectId)
    patchRun(projectId, {
      submitting: true,
      pendingAnnotationId: run.targetAnnotationId,
      pendingAction: 'run',
      // 立即收起悬浮框，避免重复点击；失败时恢复原决策。
      pendingDecision: null,
      error: null,
    })

    try {
      const response = await fetch(
        `/api/v1/runs/${encodeURIComponent(run.runId)}/decisions/${encodeURIComponent(decision.decisionId)}`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${session?.token ?? ''}`,
          },
          body: JSON.stringify({
            optionId,
            comment: String(comment || '').trim() || undefined,
            expectedVersion: decision.round,
          }),
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
      if (option?.action === 'cancel') {
        showToast('info', '已取消本次修改，草稿已清理')
      }
    } catch (cause) {
      patchRun(projectId, {
        submitting: false,
        pendingAnnotationId: null,
        pendingAction: null,
        pendingDecision: decision,
        error: `提交决策失败：${cause?.message || cause}`,
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
      (!run.submitting && !run.approval && !run.pendingDecision) ||
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

    // 视觉暂存：新增组件不立刻写代码，等【保存布局】统一提交。
    if (annotation.staged && isCreateAction(annotation.action)) {
      upsertPendingAddition(projectId, { ...annotation, intent: instruction })
      showToast(
        'info',
        '已加入未保存变更，点击右下角【保存布局】一次性生成，不会触发白屏',
      )
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
   * 批量拖拽 = 「本地草稿 + 用户统一提交」。
   *
   * SDK 只负责手势与临时 translate；松手不再触发任何 API。用户连续
   * 调整多个组件后点【保存布局】，前端把全部累计位移合并成一个
   * action=BATCH_MOVE 的 AnnotationEnvelope：
   *   - 后端只跑一次 Coder，生成一份合并补丁，避免多线程写同一文件；
   *   - 失败 / 超时保留草稿，用户可以直接重试；
   *   - 只有成功落盘后才清空草稿并更新已提交基线。
   */
  const triggerDragRollback = (
    projectId,
    items,
    message,
    kind = 'error',
  ) => {
    if (!items?.length) return
    setDragRollbacks((previous) => ({
      ...previous,
      [projectId]: {
        nonce: (previous[projectId]?.nonce ?? 0) + 1,
        items,
      },
    }))
    showToast(kind, message || '代码同步失败，位置已还原')
  }

  /** 把 ref 中的草稿同步到 React state，供悬浮操作条计数。 */
  const publishPendingLayoutChanges = (projectId) => {
    if (!workspaceMountedRef.current) return
    const pending = pendingLayoutChangesRef.current.get(projectId)
    setPendingLayoutChanges((previous) => {
      const next = { ...previous }
      if (pending?.size) {
        next[projectId] = [...pending.values()]
      } else {
        delete next[projectId]
      }
      return next
    })
    syncDraftMirror(projectId)
  }

  /** 把新增组件草稿同步到 React state（幽灵预览与悬浮列表都读它）。 */
  const publishPendingAdditions = (projectId) => {
    if (!workspaceMountedRef.current) return
    const pending = pendingAdditionsRef.current.get(projectId)
    setPendingAdditions((previous) => {
      const next = { ...previous }
      if (pending?.size) {
        next[projectId] = [...pending.values()]
      } else {
        delete next[projectId]
      }
      return next
    })
    syncDraftMirror(projectId)
  }

  const draftKeyOf = (projectId) =>
    draftKeyFor({ ...draftScope, projectId })

  /**
   * 把 ref 草稿镜像到 Zustand / IndexedDB。
   *
   * 恢复完成前绝不写库：否则首次渲染时 ref 还是空 Map，会把上一次会话的
   * 草稿覆盖成空记录。恢复完成后内容相同的操作不会产生新 seq。
   */
  const syncDraftMirror = (projectId) => {
    const key = draftKeyOf(projectId)
    if (!draftReadyRef.current.has(key)) return
    const layoutOps = [
      ...(pendingLayoutChangesRef.current.get(projectId)?.values() ?? []),
    ]
    const additions = [
      ...(pendingAdditionsRef.current.get(projectId)?.values() ?? []),
    ]
    useDraftStore
      .getState()
      .syncDraft(
        { ...draftScope, projectId },
        { layoutOps, additions },
      )
  }

  /**
   * 启动 / 打开项目时从 IndexedDB 恢复草稿。
   *
   * 恢复顺序：先读记录 → 合并进 ref（保留本次渲染中用户刚产生的新操作）
   * → 标记 ready → publish 到 React state。这样刷新 / 崩溃后画布上的位移、
   * 新增组件与悬浮操作条都能原样回来。
   */
  useEffect(() => {
    if (!user?.id) return
    openTabs.forEach((projectId) => {
      const key = draftKeyOf(projectId)
      if (
        draftReadyRef.current.has(key) ||
        draftHydratingRef.current.has(key)
      ) {
        return
      }
      draftHydratingRef.current.add(key)
      const hydration = (async () => {
        try {
          const record = await useDraftStore
            .getState()
            .hydrateProjectDraft({ ...draftScope, projectId })
          if (
            !workspaceMountedRef.current ||
            closedProjectsRef.current.has(projectId)
          ) {
            // 读取期间 Tab 已被关闭：清掉可能刚写回内存 Store 的记录。
            await useDraftStore
              .getState()
              .purgeProjectDraft({ ...draftScope, projectId })
            return
          }
          if (record?.ops?.length) {
            const layoutMap =
              pendingLayoutChangesRef.current.get(projectId) ?? new Map()
            const additionMap =
              pendingAdditionsRef.current.get(projectId) ?? new Map()
            record.ops.forEach((op) => {
              if (op.kind === 'layout' && op.payload?.selector) {
                if (!layoutMap.has(op.payload.selector)) {
                  layoutMap.set(op.payload.selector, op.payload)
                }
              }
              if (op.kind === 'addition') {
                const annotationId =
                  op.payload?.annotationId || op.payload?.id
                if (annotationId && !additionMap.has(annotationId)) {
                  additionMap.set(annotationId, op.payload)
                }
              }
            })
            if (layoutMap.size) {
              pendingLayoutChangesRef.current.set(projectId, layoutMap)
            }
            if (additionMap.size) {
              pendingAdditionsRef.current.set(projectId, additionMap)
            }
          }
          draftReadyRef.current.add(key)
          publishPendingLayoutChanges(projectId)
          publishPendingAdditions(projectId)
        } catch (cause) {
          // IndexedDB 损坏 / 配额异常：不阻断工作区，按“无草稿”继续。
          console.warn('[draft] 恢复本地草稿失败', projectId, cause)
        } finally {
          draftHydratingRef.current.delete(key)
        }
      })()
      draftHydrationRef.current.set(key, hydration)
      void hydration
        .finally(() => {
          if (draftHydrationRef.current.get(key) === hydration) {
            draftHydrationRef.current.delete(key)
          }
        })
        .catch(() => {})
    })
  }, [openTabs, draftScope, user?.id])

  /**
   * 刷新 / 关闭页面保护。
   *
   * 浏览器不允许自定义 beforeunload 文案，这里只负责触发原生确认；
   * 用户确认离开后 pagehide 同步写 localStorage 墓碑，下次进入工作区时
   * 由 draftStore 清理 IndexedDB。崩溃（没有 pagehide）不写墓碑，草稿保留。
   */
  useEffect(() => {
    if (!user?.id) return undefined
    const unsavedProjectIds = () =>
      openTabs.filter(
        (projectId) =>
          (useDraftStore.getState().records[draftKeyOf(projectId)]?.ops
            ?.length ?? 0) > 0,
      )

    const handleBeforeUnload = (event) => {
      if (unsavedProjectIds().length === 0) return
      event.preventDefault()
      // 兼容旧浏览器：必须给 returnValue 赋非空值才会弹确认框。
      event.returnValue = ''
    }
    const handlePageHide = (event) => {
      // bfcache（前进 / 后退）不是真正离开：此时写墓碑会在用户返回后
      // 误删仍然存活的草稿。只有真正卸载才同步落墓碑。
      if (event.persisted) return
      const projectIds = unsavedProjectIds()
      if (projectIds.length > 0) {
        writeDraftTombstone(draftScope, projectIds)
      }
    }

    window.addEventListener('beforeunload', handleBeforeUnload)
    window.addEventListener('pagehide', handlePageHide)
    return () => {
      window.removeEventListener('beforeunload', handleBeforeUnload)
      window.removeEventListener('pagehide', handlePageHide)
    }
  }, [openTabs, draftScope, user?.id])

  /**
   * 新增 / 更新一条视觉暂存。
   *
   * 只有 CREATE_NEW 且用户已经写下非空意图时才算“待保存变更”；
   * 空圈只是选区位，不应出现在未保存列表里。
   */
  const upsertPendingAddition = (projectId, annotation) => {
    if (!annotation?.id) return
    const intent = annotation.intent?.trim()
    const staged = Boolean(annotation.staged) && isCreateAction(annotation.action)
    const map = pendingAdditionsRef.current.get(projectId) || new Map()
    if (!staged || !intent) {
      map.delete(annotation.id)
    } else {
      const component = inferPendingComponentType(intent)
      map.set(annotation.id, {
        ...annotation,
        annotationId: annotation.id,
        intent,
        componentType: component.key,
        componentLabel: component.label,
      })
    }
    if (map.size) {
      pendingAdditionsRef.current.set(projectId, map)
    } else {
      pendingAdditionsRef.current.delete(projectId)
    }
    publishPendingAdditions(projectId)
  }

  /**
   * 暂存组件拖拽落位回写。
   *
   * 前端只记录“落在哪个父容器 + 相对偏移 + 兄弟锚点”，绝不写代码；
   * 真正插入发生在【保存布局】时，由后端 Resolver 按 placement 定位。
   * 这里同时更新 run.annotations 与 pendingAdditionsRef，保证浮层、
   * 幽灵节点与最终 Envelope 读到的是同一份事实。
   */
  const applyPendingAdditionPlacement = (projectId, annotationId, patch) => {
    if (!annotationId || !patch?.placement) return
    const map = pendingAdditionsRef.current.get(projectId)
    const staged = map?.get(annotationId)
    if (!map || !staged) return
    const placementFields = {
      rect: patch.rect ?? staged.rect,
      documentRect: patch.documentRect ?? staged.documentRect ?? null,
      frameScroll: patch.frameScroll ?? staged.frameScroll ?? null,
      addTarget: patch.addTarget ?? staged.addTarget ?? null,
      source: patch.source ?? staged.source ?? null,
      relativeRect: patch.relativeRect ?? staged.relativeRect ?? null,
      placement: patch.placement,
      placementUpdatedAt: new Date().toISOString(),
    }
    map.set(annotationId, { ...staged, ...placementFields })
    patchRun(projectId, (previous) => ({
      ...previous,
      annotations: previous.annotations.map((annotation) =>
        annotation.id === annotationId
          ? { ...annotation, ...placementFields }
          : annotation,
      ),
    }))
    publishPendingAdditions(projectId)
  }

  const removePendingAddition = (projectId, annotationId) => {
    const map = pendingAdditionsRef.current.get(projectId)
    if (!map?.delete(annotationId)) return
    if (map.size === 0) pendingAdditionsRef.current.delete(projectId)
    publishPendingAdditions(projectId)
  }

  const clearPendingAdditions = (projectId) => {
    if (!pendingAdditionsRef.current.delete(projectId)) return
    publishPendingAdditions(projectId)
  }

  /**
   * run.succeeded 到达时立即撤掉视觉幽灵节点。
   *
   * 这里只处理“看得见的假节点”，不删除 layoutFlushRef 快照：成功后的
   * 标注清理、位移基线更新与 Toast 仍由 handleSaveLayout /
   * handleClarificationAnswer 统一收尾，避免重复提交或丢失提示。
   */
  const clearPendingAdditionsForRun = (
    projectId,
    { annotationId = null, runId = null } = {},
  ) => {
    const snapshot = layoutFlushRef.current.get(projectId)
    if (snapshot) {
      const belongsToRun =
        snapshot.runId === null ||
        runId === null ||
        snapshot.runId === runId
      if (belongsToRun) {
        const map = pendingAdditionsRef.current.get(projectId)
        if (map?.size) {
          snapshot.additionIds.forEach((id) => map.delete(id))
          if (map.size === 0) pendingAdditionsRef.current.delete(projectId)
          publishPendingAdditions(projectId)
        }
        snapshot.additionsCleared = true
        return true
      }
    }
    // 非布局提交（单标注对话）没有 snapshot：只清理该标注的占位符。
    if (annotationId) removePendingAddition(projectId, annotationId)
    return false
  }

  /**
   * 澄清回答期间原地更新同一条视觉暂存记录。
   *
   * Map 的 key 始终是 annotationId，绝不新建占位符；这里只刷新最终意图
   * 文案，避免多轮问答后出现第二个幽灵按钮。
   */
  const updatePendingAdditionClarification = (
    projectId,
    annotationId,
    answerText,
    reference = null,
  ) => {
    const map = pendingAdditionsRef.current.get(projectId)
    const item = map?.get(annotationId)
    const text = String(answerText || '').trim()
    if (!item || (!text && !reference)) return
    map.set(annotationId, {
      ...item,
      ...(reference ? { reference } : {}),
      clarificationText: text,
      // 最终意图 = 原始指令 + 最新澄清答案；历史轮次不累积。
      finalIntent: [item.intent, text].filter(Boolean).join('；'),
    })
    publishPendingAdditions(projectId)
  }

  /**
   * iframe 文档重建（首次加载 / Vite HMR）。
   *
   * HMR 说明沙盒源码已变、真实 DOM 已重新渲染；此时仍挂着的幽灵节点
   * 一定是过期草稿。只在“确实有一次布局提交”时清理，避免误删用户
   * 尚未提交的新增组件。
   */
  const handleFrameReady = (projectId) => {
    const run = runStatesRef.current[projectId] ?? EMPTY_RUN
    const hasLayoutSnapshot = layoutFlushRef.current.has(projectId)
    if (!hasLayoutSnapshot && !run.submitting) return
    const snapshot = layoutFlushRef.current.get(projectId)
    if (snapshot) snapshot.additionsCleared = true
    if (pendingAdditionsRef.current.get(projectId)?.size) {
      pendingAdditionsRef.current.delete(projectId)
      publishPendingAdditions(projectId)
    }
  }

  /** 悬浮条文案：把“新增组件”和“位置调整”合并成一句人类可读摘要。 */
  const describePendingChanges = (projectId, overrides = {}) => {
    // 优先读 ref：点击瞬间 state 可能还落后一帧，不能漏掉刚画的组件。
    const moveMap = pendingLayoutChangesRef.current.get(projectId)
    const additionMap = pendingAdditionsRef.current.get(projectId)
    const moves =
      overrides.moves ??
      (moveMap?.size
        ? [...moveMap.values()]
        : pendingLayoutChanges[projectId] ?? [])
    const additions =
      overrides.additions ??
      (additionMap?.size
        ? [...additionMap.values()]
        : pendingAdditions[projectId] ?? [])
    if (moves.length === 0 && additions.length === 0) return ''
    const parts = []
    if (additions.length) {
      const names = additions
        .slice(0, 5)
        .map((item) => `「${item.intent || item.componentLabel}」`)
        .join('、')
      parts.push(
        `新增 ${additions.length} 个组件${names ? `：${names}` : ''}`,
      )
    }
    if (moves.length) {
      parts.push(`调整 ${moves.length} 个组件位置`)
    }
    return `【画布中尚未保存的变更】${parts.join('；')}。请在后续生成中一并实现，不要遗漏。`
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

  /** 根据 SDK 回传的 source 信息生成稳定选择器兜底。 */
  const moveSelector = (payload) => {
    const source = payload?.source || {}
    if (source.file) {
      return `[data-source="${source.file}:${source.line || 1}:${
        source.column || 1
      }"]`
    }
    return payload?.selector || ''
  }

  /**
   * 把一组草稿打包成单个 BATCH_MOVE 标注。
   *
   * 后端只接收一个 AnnotationEnvelope，因此不受“一次最多 3 个标注”的
   * 限制；payload.moves 中每条 record 都携带选择器与累计偏移，Coder
   * 可以一次生成包含全部 CSS 规则的合并补丁。
   */
  const buildBatchMoveEntry = (project, items) => {
    const first = items[0]
    const firstSource = first.payload.source || {}
    const file = firstSource.file || projectEntryFile(project)
    const selector = first.selector || moveSelector(first.payload)
    const label = firstSource.component || firstSource.tag || '组件'
    const moves = items.map((item) => {
      const source = item.payload.source || {}
      const itemFile = source.file || file
      const itemSelector = item.selector || moveSelector(item.payload)
      return {
        // source 是审计用的稳定标识；selector 才是 Tool 的真实入参。
        source: source.file
          ? `${source.file}:${source.line || 1}`
          : itemSelector,
        file: itemFile,
        selector: itemSelector,
        label: source.component || source.tag || '组件',
        dx: Math.round(item.next.x),
        dy: Math.round(item.next.y),
        token: item.token,
      }
    })
    dragSeqRef.current += 1
    return {
      id: `layout_${Date.now().toString(36)}_${dragSeqRef.current}`,
      action: 'BATCH_MOVE',
      type: 'drag',
      // 人类可读摘要仍保留，便于 Run 时间轴与聊天记录审计。
      intent: `统一调整 ${moves.length} 个组件位置：${items
        .map((item) => {
          const source = item.payload.source || {}
          const name = source.component || source.tag || '组件'
          return `「${name}」${moveOffsetText(
            Math.round(item.next.x),
            Math.round(item.next.y),
          )}`
        })
        .join('；')}（只改位置，不要改宽高）`,
      rect: dragRect(first.payload),
      target: {
        marker: firstSource.marker || '',
        selector,
        label,
        role: firstSource.tag || 'element',
        component: firstSource.component || 'App',
        tag: firstSource.tag || 'div',
        instance: Number(firstSource.instance) || undefined,
        instanceCount: Number(firstSource.instanceCount) || undefined,
        instanceId: firstSource.instanceId || undefined,
        instanceKey: firstSource.instanceKey || undefined,
        instanceProps: firstSource.instanceProps || undefined,
        callSite: firstSource.callSite || null,
        styleSignature: firstSource.styleSignature || null,
      },
      inspectSource: {
        file,
        line: Number(firstSource.line) || 1,
        column: Number(firstSource.column) || 1,
        component: firstSource.component || 'App',
        sourceKind: firstSource.sourceKind || 'jsx',
        marker: firstSource.marker || '',
        instance: Number(firstSource.instance) || undefined,
        instanceCount: Number(firstSource.instanceCount) || undefined,
        instanceId: firstSource.instanceId || undefined,
        instanceKey: firstSource.instanceKey || undefined,
        instanceProps: firstSource.instanceProps || undefined,
        callSite: firstSource.callSite || null,
        styleSignature: firstSource.styleSignature || null,
      },
      // buildAnnotation 会原样转成 payload.moves；不要塞进 payload.move，
      // 那是单组件 MOVE 的旧协议。
      moveBatch: moves,
      createdAt: new Date().toISOString(),
    }
  }

  /**
   * 把“位移 + 新增组件”打包成一个 BATCH_LAYOUT 标注。
   *
   * 新增组件必须携带完整 AnnotationEnvelope：后端 Coder 会直接复用
   * 现有 UI 上下文裁剪 / CREATE_NEW 标记校验，不再维护第二套协议。
   */
  const buildBatchLayoutEntry = (project, items, additions) => {
    const run = runStatesRef.current[project.id] ?? EMPTY_RUN
    const targetFile = projectEntryFile(project)
    const sourceCode = projectSource(project)
    const files = { ...(project.files ?? {}), [targetFile]: sourceCode }
    const findAnnotation = (annotationId) =>
      run.annotations.find((annotation) => annotation.id === annotationId)

    const additionBatch = additions
      .map((item) => {
        const raw = findAnnotation(item.annotationId) || item
        // 澄清后的重试必须只用最终意图：finalIntent 已覆盖被否定过的
        // 旧颜色 / 旧方案，不能再回退到 raw.intent。
        const intentText = String(
          item.finalIntent || item.intent || raw.intent || '',
        ).trim()
        const envelope = buildAnnotation(
          {
            ...raw,
            intent: intentText,
            // 视觉选型结果可能存储在幽灵草稿上（澄清期间未能写回
            // run.annotations）；重新提交时必须随信封一起带走。
            reference: item.reference || raw.reference || null,
            // 拖拽落位结果以草稿为准：run.annotations 可能尚未同步。
            placement: item.placement || raw.placement || null,
            addTarget: item.addTarget || raw.addTarget || null,
            source: item.source || raw.source || null,
            relativeRect: item.relativeRect || raw.relativeRect || null,
          },
          sourceCode,
          targetFile,
          files,
        )
        if (!envelope?.annotationId) return null
        const component = inferPendingComponentType(intentText)
        const { chatHistory, screenshot, ...compact } = envelope
        return {
          annotationId: envelope.annotationId,
          intent: intentText,
          componentType: component.key,
          componentLabel: component.label,
          annotation: compact,
        }
      })
      .filter((item) => item?.intent)

    const moveEntry = items.length ? buildBatchMoveEntry(project, items) : null
    const firstAddition = additionBatch[0]?.annotation
    const firstAdditionRaw = additions[0]
      ? findAnnotation(additions[0].annotationId) || additions[0]
      : null
    const rect =
      firstAdditionRaw?.rect ??
      moveEntry?.rect ?? { x: 0, y: 0, width: 0.1, height: 0.1 }
    const target = firstAddition?.target ??
      moveEntry?.target ?? {
        marker: '',
        selector: '',
        label: '画布',
        role: 'container',
        component: 'App',
        tag: 'div',
      }
    const inspectSource = firstAddition?.target?.source
      ? {
          file: firstAddition.target.source.file,
          line: firstAddition.target.source.line || 1,
          column: firstAddition.target.source.column || 1,
          component: firstAddition.target.source.component || 'App',
          sourceKind: firstAddition.target.source.sourceKind || 'jsx',
          marker: firstAddition.target.source.marker || '',
          instance: firstAddition.target.source.instance,
          instanceCount: firstAddition.target.source.instanceCount,
          instanceId: firstAddition.target.source.instanceId,
          instanceKey: firstAddition.target.source.instanceKey,
          instanceProps: firstAddition.target.source.instanceProps,
          callSite: firstAddition.target.source.callSite,
          styleSignature: firstAddition.target.source.styleSignature,
        }
      : moveEntry?.inspectSource ?? null

    const parts = []
    if (additionBatch.length) {
      parts.push(
        `新增 ${additionBatch.length} 个组件：${additionBatch
          .map((item) => `「${item.intent}」`)
          .join('、')}`,
      )
    }
    if (moveEntry?.moveBatch?.length) {
      parts.push(`调整 ${moveEntry.moveBatch.length} 个组件位置`)
    }
    dragSeqRef.current += 1
    return {
      id: `layout_${Date.now().toString(36)}_${dragSeqRef.current}`,
      action: 'BATCH_LAYOUT',
      type: 'drag',
      // 人类可读摘要：Run 时间轴 / 助手气泡直接展示。
      intent: `统一提交画布变更：${parts.join('；')}（新增组件必须带 data-vibe-created，位移只改 transform）`,
      rect,
      target,
      inspectSource,
      // 两个批次都由 buildAnnotation 归一化后进入 payload。
      moveBatch: moveEntry?.moveBatch ?? [],
      additionBatch,
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

  /** 【撤销】清空位移与新增组件草稿，并恢复 DOM 内联位移。 */
  const handleUndoLayout = (projectId) => {
    const pending = pendingLayoutChangesRef.current.get(projectId)
    const additions = pendingAdditionsRef.current.get(projectId)
    if (!pending?.size && !additions?.size) return
    if (dragSyncStates[projectId]) return
    const items = pending?.size
      ? [...pending.values()].map((item) => ({
          token: item.token,
          selector: item.selector,
        }))
      : []
    if (pending?.size) {
      pendingLayoutChangesRef.current.delete(projectId)
      publishPendingLayoutChanges(projectId)
    }
    const addedIds = new Set(additions ? additions.keys() : [])
    if (addedIds.size) {
      // 暂存新增组件从未写进代码，撤销只需删除本地标注与草稿。
      pendingAdditionsRef.current.delete(projectId)
      publishPendingAdditions(projectId)
      patchRun(projectId, (previous) => ({
        ...previous,
        annotations: previous.annotations.filter(
          (annotation) => !addedIds.has(annotation.id),
        ),
      }))
    }
    if (items.length) {
      triggerDragRollback(
        projectId,
        items,
        `已撤销 ${items.length} 个组件的位置调整`,
        'info',
      )
    }
    if (addedIds.size) {
      showToast('info', `已撤销 ${addedIds.size} 个暂存的新增组件`)
    }
  }

  /**
   * 布局提交成功（含澄清恢复后成功）的统一收尾。
   *
   * 只移除快照里的条目：保存 / 澄清期间用户若又拖了新组件，新草稿
   * 必须保留。新增组件真正落盘后才删除暂存标注与幽灵预览。
   */
  const commitLayoutSnapshot = (projectId, snapshot) => {
    const batch = snapshot?.moves ?? []
    const addedIds = snapshot?.additionIds ?? []
    batch.forEach((item) => {
      dragCommittedRef.current.set(
        `${projectId}::${item.selector}`,
        item.next,
      )
    })
    const current = pendingLayoutChangesRef.current.get(projectId)
    if (current) {
      batch.forEach((item) => {
        if (current.get(item.selector) === item) {
          current.delete(item.selector)
        }
      })
      if (current.size === 0) {
        pendingLayoutChangesRef.current.delete(projectId)
      }
    }
    publishPendingLayoutChanges(projectId)
    if (addedIds.length) {
      const addedSet = new Set(addedIds)
      const addedMap = pendingAdditionsRef.current.get(projectId)
      if (addedMap) {
        addedIds.forEach((annotationId) => addedMap.delete(annotationId))
        if (addedMap.size === 0) {
          pendingAdditionsRef.current.delete(projectId)
        }
      }
      publishPendingAdditions(projectId)
      patchRun(projectId, (previous) => ({
        ...previous,
        annotations: previous.annotations.filter(
          (annotation) => !addedSet.has(annotation.id),
        ),
      }))
    }
    if (batch.length) {
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
    }
    return { moves: batch.length, additions: addedIds.length }
  }

  /**
   * 【保存布局】一次 Run 提交所有暂存变更（位移 + 新增组件）。
   *
   * 只有后端成功（补丁已应用且构建通过）才清空 pending；超时 / 失败
   * 一律保留草稿和内联视觉位置，用户可以直接重试。
   *
   * 返回值用于“进入架构澄清 / 重新生成”前的自动统一提交。
   */
  const handleSaveLayout = async (projectId) => {
    const pending = pendingLayoutChangesRef.current.get(projectId)
    const additionMap = pendingAdditionsRef.current.get(projectId)
    const batch = pending?.size ? [...pending.values()] : []
    const additions = additionMap?.size ? [...additionMap.values()] : []
    if (batch.length === 0 && additions.length === 0) return true
    if (layoutSaveInflightRef.current.has(projectId)) return false
    const project = projects.find((item) => item.id === projectId)
    const run = runStates[projectId] ?? EMPTY_RUN
    if (!project) return false
    if (!run.result?.sandboxId) {
      showToast('error', '当前页面尚未生成代码，画布变更已保留')
      return false
    }
    if (
      run.submitting ||
      run.clarification ||
      run.approval ||
      inflightRef.current.has(projectId) ||
      gateIsActive(projectId)
    ) {
      showToast('info', '当前有任务执行中，画布变更已保留，请稍后重试')
      return false
    }

    const entry = additions.length
      ? buildBatchLayoutEntry(project, batch, additions)
      : buildBatchMoveEntry(project, batch)
    const addedIds = additions.map((item) => item.annotationId)
    // 快照必须基于本次提交的条目，而不是提交完成时的 ref 内容。
    const snapshot = { moves: batch, additionIds: addedIds, runId: null }
    layoutFlushRef.current.set(projectId, snapshot)
    layoutSaveInflightRef.current.add(projectId)
    setDragSyncStates((previous) => ({
      ...previous,
      [projectId]: true,
    }))
    try {
      const submission = submitViaSupervisor(projectId, {
        message: entry.intent,
        annotations: [entry],
        scopeHint: 'ui',
        // 画布变更都是确定性事实，直接交给 UI Agent，不再经过意图猜测。
        targetAgent: 'ui_graph',
        // 统一提交不能让人工审批打断；失败由前端保留草稿。
        requireManualApproval: false,
        // 不抢焦点：用户仍在画布上调整布局。
        switchToPreview: false,
      })
      const outcome = await raceWithTimeout(
        submission,
        LAYOUT_SYNC_TIMEOUT_MS,
      )
      // 成功路径等 submitting=false 真正渲染后再解除，避免中间帧退出拖拽模式。
      releaseDragSync(projectId)

      if (outcome?.timedOut) {
        // 超时后后端 Run 可能仍在写代码：尽力取消，避免稍后 HMR 覆盖草稿。
        const runId = runStatesRef.current?.[projectId]?.runId
        if (runId) cancelRunSilently(runId)
        layoutFlushRef.current.delete(projectId)
        clearDragSync(projectId)
        showToast('error', '画布变更同步超时，草稿已保留，可稍后重试')
        return false
      }

      if (outcome?.ok) {
        commitLayoutSnapshot(projectId, snapshot)
        layoutFlushRef.current.delete(projectId)
        showToast(
          'success',
          additions.length
            ? `已同步 ${additions.length} 个新增组件${
                batch.length ? `与 ${batch.length} 处位置调整` : ''
              }`
            : '布局已同步',
        )
        return true
      }

      // 样式澄清：Run 暂停而不是失败。保留 snapshot 与全部草稿，等用户
      // 回答后的恢复 Run 成功，再由 handleClarificationAnswer 统一收尾。
      // 以事件流返回的 clarification 为准，不依赖 React 状态刷新时序。
      if (outcome?.event?.type === 'clarification.required') {
        snapshot.runId = outcome.event.runId ?? run.runId
        // 暂停不是失败：解除“同步中”遮罩，让用户能回答迷你聊天框。
        releaseDragSync(projectId)
        return false
      }

      // 失败：不 rollback、不清空 pending，保留用户的全部画布草稿。
      layoutFlushRef.current.delete(projectId)
      clearDragSync(projectId)
      showToast('error', '画布变更同步失败，草稿已保留，可稍后重试')
      return false
    } finally {
      layoutSaveInflightRef.current.delete(projectId)
    }
  }

  /**
   * SDK 松手回调：只更新本地草稿，不发任何请求。
   *
   * 同一组件再次拖动时，在 pending 的累计偏移上叠加；这样 SDK 的内联
   * translate 与草稿中的 dx/dy 始终一致。已提交基线只在保存成功后更新。
   */
  const handleComponentDragEnd = (projectId, payload) => {
    const project = projects.find((item) => item.id === projectId)
    const run = runStates[projectId] ?? EMPTY_RUN
    if (!project || !payload?.selector) return
    // 没有沙盒就没有可写入的代码，直接还原本次手势并提示。
    if (!run.result?.sandboxId) {
      triggerDragRollback(
        projectId,
        [{ token: payload.token, selector: payload.selector }],
        '当前页面尚未生成代码，无法调整布局',
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
        '当前有任务执行中，本次拖拽已还原',
      )
      return
    }
    const selector = payload.selector
    const source = payload.source || {}
    const placement = payload.placement
    if (!placement?.parentSelector || !placement?.parentFile) {
      triggerDragRollback(
        projectId,
        [{ token: payload.token, selector }],
        '没有找到可落位的父容器，本次拖拽已还原',
      )
      return
    }
    console.info('[direct-drag] move request', {
      projectId,
      selector,
      source: {
        file: source.file,
        line: source.line || undefined,
        column: source.column || undefined,
        instance: source.instance,
        instanceCount: source.instanceCount,
      },
      placement,
    })
    // 拖拽不走 Agent：松手后直接调用确定性 MOVE_NODE。
    void handleDirectAction(projectId, {
      action: 'MOVE_NODE',
      dryRun: false,
      target: { ...source, placement },
      payload: {},
    })
      .then(() => {
        // 通知 SDK 丢弃临时 translate，并刷新预览等待 HMR。
        setDragCommits((previous) => ({
          ...previous,
          [projectId]: {
            nonce: (previous[projectId]?.nonce ?? 0) + 1,
            items: [{ token: payload.token, selector }],
          },
        }))
      })
      .catch((cause) => {
        console.warn('[direct-drag] move failed', {
          projectId,
          selector,
          source,
          placement,
          error: cause,
        })
        triggerDragRollback(
          projectId,
          [{ token: payload.token, selector }],
          `移动失败：${cause?.message || cause}`,
        )
      })
  }

  /** 用户取消拖拽（pointercancel）：SDK 已自行还原，不产生草稿。 */
  const handleComponentDragCancel = () => {
    // 取消手势不产生代码变更；已记录的其他组件草稿保持不变。
  }

  const handleSubmit = async (projectId) => {
    const project = projects.find((item) => item.id === projectId)
    const run = runStates[projectId] ?? EMPTY_RUN
    // CREATE_NEW 视觉暂存不进入普通 Run：它们必须与位移一起统一提交，
    // 否则“边画边写”会重新引入白屏与多线程写文件问题。
    const annotations = run.annotations.filter(
      (annotation) => !(annotation.staged && isCreateAction(annotation.action)),
    )
    const instructions = annotations.map((annotation) => annotation.intent.trim())
    if (
      !project ||
      instructions.some((text) => !text) ||
      run.submitting ||
      run.clarification ||
      run.approval ||
      inflightRef.current.has(projectId)
    ) {
      return
    }
    if (annotations.length === 0) {
      // 只有暂存变更时，提交动作等价于【保存布局】。
      await handleSaveLayout(projectId)
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

  /**
   * 大型重构失败后的“转后台运行”。
   *
   * 复用失败前保存的 sendEntries（包含用户原始指令），以 background=true
   * 重新派发；后端使用 240s 超时，前端不显示阻塞浮层。
   */
  const handleRunInBackground = (projectId) => {
    const run = runStates[projectId] ?? EMPTY_RUN
    const request = run.backgroundRequest
    if (!request?.sendEntries?.length) {
      showToast('info', '没有可转后台的修改，请在画布重新标注')
      return
    }
    if (run.submitting || run.backgroundRun) {
      showToast('info', '当前已有任务在运行，请稍候')
      return
    }
    showToast('info', '已提交后台任务，你可以继续圈选其他内容')
    void submitViaSupervisor(projectId, {
      message: request.message,
      annotations: request.sendEntries,
      annotationId: request.annotationId ?? null,
      // 首次提交已经写过用户气泡并清空 intent；重放只派发 Run，
      // 不再重复提交聊天记录。preload 快照仍通过 previous 保留。
      sendEntries: [],
      commitOnSuccess: false,
      background: true,
      switchToPreview: false,
    })
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
    const scope = { ...draftScope, projectId }
    // 关键：在点击瞬间记录 revision barrier。随后即使用户继续拖拽 /
    // 新增组件，这些新操作会拿到更大的 seq，保存成功后不会被误删。
    const saveTicket = useDraftStore.getState().beginSave(scope)
    try {
      // 快照提交的是主工作树代码，而画布草稿还只存在于本地。先把草稿
      // 通过现有【保存布局】链路落进代码，再做 Git checkpoint，否则
      // “保存快照”会丢掉用户眼前的修改。
      const hasPendingLayout =
        (pendingLayoutChangesRef.current.get(projectId)?.size ?? 0) > 0 ||
        (pendingAdditionsRef.current.get(projectId)?.size ?? 0) > 0
      if (hasPendingLayout) {
        const flushed = await handleSaveLayout(projectId)
        if (!flushed) {
          useDraftStore.getState().cancelSave(scope)
          if (!workspaceMountedRef.current) return
          setSnapshotSave((previous) => ({
            ...previous,
            saving: false,
            error: '画布草稿尚未同步，快照未保存；草稿已保留，可直接重试。',
          }))
          return
        }
      }

      const run = runStates[projectId] ?? EMPTY_RUN
      // latestChanges 是本次快照涵盖的改动说明；为空时退化为快照备注，
      // 保证第一次保存也能生成 global_summary.md。
      const latestChanges = run.latestChanges || message
      const payload = await createSnapshot(sandboxId, message, session?.token, {
        projectId,
        tenantId: user?.tenantId || 'tenant_local',
        latestChanges,
      })
      // 只删除 barrier 之前已提交的操作；保存期间产生的新修改继续留在
      // IndexedDB，并提示用户下次保存。
      const committed = await useDraftStore
        .getState()
        .commitSave(scope, { barrier: saveTicket.barrier })
      if (!workspaceMountedRef.current) return
      setSnapshotSave((previous) => ({ ...previous, saving: false, open: false }))
      // 保存成功 = 已落盘：解除下一步门控，并缓存全局摘要供 Architect 使用。
      patchRun(projectId, {
        dirty: false,
        projectSummary: payload?.projectSummary || run.projectSummary || '',
        latestChanges: payload?.latestChanges || latestChanges,
      })
      const notice = resolveSaveToast(committed)
      showSnapshotToast(notice.kind, notice.text)
      if (snapshotDrawer.open && snapshotDrawer.projectId === projectId) {
        void refreshSnapshotList(projectId, sandboxId)
      }
    } catch (cause) {
      useDraftStore.getState().cancelSave(scope)
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
  const closeProjectNow = (projectId) => {
    if (closedProjectsRef.current.has(projectId)) return
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
    // 项目销毁后丢弃未保存的布局草稿，避免悬浮条残留在其他 Tab。
    pendingLayoutChangesRef.current.delete(projectId)
    pendingAdditionsRef.current.delete(projectId)
    layoutSaveInflightRef.current.delete(projectId)
    layoutFlushRef.current.delete(projectId)
    draftReadyRef.current.delete(draftKeyOf(projectId))
    // 无草稿直接关闭时也清掉 clean 记录，避免 IndexedDB 留下孤儿。
    void useDraftStore
      .getState()
      .purgeProjectDraft({ ...draftScope, projectId })
    setPendingLayoutChanges((previous) => {
      if (!previous[projectId]) return previous
      const next = { ...previous }
      delete next[projectId]
      return next
    })
    setPendingAdditions((previous) => {
      if (!previous[projectId]) return previous
      const next = { ...previous }
      delete next[projectId]
      return next
    })
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

  /**
   * 关闭 Tab 前检查本地草稿：
   *  - 有未保存操作：弹自定义 Modal，取消则画布 / 草稿 / 沙盒原样保留；
   *  - 确认：先清 IndexedDB，再执行原有 closeProjectNow 彻底销毁。
   */
  const handleCloseTab = (projectId) => {
    const key = draftKeyOf(projectId)
    const hydration = draftHydrationRef.current.get(key)
    if (hydration) {
      // 草稿尚未从 IndexedDB 读完：先等恢复完成，再判断是否需要守卫。
      void hydration.then(
        () => {
          draftHydrationRef.current.delete(key)
          draftHydratingRef.current.delete(key)
          handleCloseTab(projectId)
        },
        () => handleCloseTab(projectId),
      )
      return
    }
    const hasDraft = useDraftStore
      .getState()
      .hasDraftForProject({ ...draftScope, projectId })
    if (hasDraft) {
      setDraftGuard({ kind: 'close', projectId })
      return
    }
    closeProjectNow(projectId)
  }

  const confirmDraftGuard = async () => {
    if (!draftGuard || draftGuardBusy) return
    setDraftGuardBusy(true)
    try {
      if (draftGuard.kind === 'close') {
        const projectId = draftGuard.projectId
        await useDraftStore
          .getState()
          .purgeProjectDraft({ ...draftScope, projectId })
        draftReadyRef.current.delete(draftKeyOf(projectId))
        setDraftGuard(null)
        closeProjectNow(projectId)
      }
    } finally {
      setDraftGuardBusy(false)
    }
  }

  const cancelDraftGuard = () => {
    if (draftGuardBusy) return
    setDraftGuard(null)
  }

  const statuses = useMemo(() => {
    const map = {}
    openTabs.forEach((projectId) => {
      const run = runStates[projectId]
      if (!run) {
        map[projectId] = 'idle'
      } else if (run.submitting) {
        map[projectId] = 'running'
      } else if (run.approval || run.pendingDecision) {
        // 待审批 / 待决策：Tab 上用警示色提示需要人工处理。
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
      applyPendingAdditionPlacement,
      handleSaveLayout,
      handleUndoLayout,
      handleClarificationAnswer,
      handleAnnotationRollback,
      handleClearAnnotations,
      // 路由切换：CanvasStage 只清自己的拖拽会话，标注 / 暂存草稿必须
      // 由页面级统一清空；漏掉这个键会让幽灵组件一直留在画布上。
      handleFrameRouteChange,
      handleFrameReady,
      handleApprovalDecision,
      handleDecisionAnswer,
      handleCancelRun,
      handleSubmit,
      handleRetry,
      handleRunInBackground,
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
      handleDirectAction,
      handleAnalyzeDelete,
      architectAnswer: architect.answer,
      architectDecide: architect.decide,
      architectDecideConflict: architect.decideConflict,
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
        // 移除暂存组件同时删除前端标注与草稿，不触碰沙盒代码。
        onRemovePendingAddition: (annotationId) =>
          call('handleAnnotationRemove', annotationId),
        // 拖拽松手：只回写 placement，不触发任何 Agent Run / 代码修改。
        onPendingAdditionPlacement: (annotationId, patch) =>
          call('applyPendingAdditionPlacement', annotationId, patch),
        onSaveLayout: () => call('handleSaveLayout'),
        onUndoLayout: () => call('handleUndoLayout'),
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
        onFrameRouteChange: (payload) =>
          call('handleFrameRouteChange', payload),
        onFrameReady: (payload) => call('handleFrameReady', payload),
        onApprovalDecision: (decision, comment) =>
          call('handleApprovalDecision', decision, comment),
        onDecisionAnswer: (optionId, comment) =>
          call('handleDecisionAnswer', optionId, comment),
        onCancelRun: () => call('handleCancelRun'),
        onSubmit: () => call('handleSubmit'),
        onRetry: () => call('handleRetry'),
        onRunInBackground: () => call('handleRunInBackground'),
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
        onArchitectDecide: (decision, comment, qualityTier) =>
          workspaceActionsRef.current?.architectDecide(
            projectId,
            decision,
            comment,
            qualityTier,
          ),
        onArchitectDecideConflict: (optionId, comment) =>
          workspaceActionsRef.current?.architectDecideConflict(
            projectId,
            optionId,
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
            onNewProject={openNewProjectDialog}
            onImportFile={handleImportFile}
            onExport={handleExport}
          />

          <div className="workspace-stats">
            <span className="chip chip-muted">
              {openTabs.length} / {maxTabs} 个项目
            </span>
            {anyRunning && <span className="chip chip-live">Agent 执行中</span>}
            <Link
              className="chip chip-muted"
              to={`/visual-canvas?project=${encodeURIComponent(
                activeProject?.id || '',
              )}&title=${encodeURIComponent(activeProject?.name || '未命名页面')}`}
            >
              ✨ 虚拟画布
            </Link>
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
          onNewProject={openNewProjectDialog}
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
                projectMode={resolveProjectMode(project, {
                  sandboxId: run.result?.sandboxId,
                })}
                supervisorGate={gateStates[project.id] ?? null}
                dragRollback={dragRollbacks[project.id] ?? null}
                dragCommit={dragCommits[project.id] ?? null}
                dragSyncActive={Boolean(dragSyncStates[project.id])}
                layoutDraftCount={
                  pendingLayoutChanges[project.id]?.length ?? 0
                }
                pendingAdditions={pendingAdditions[project.id] ?? []}
                layoutSaving={
                  Boolean(dragSyncStates[project.id]) || run.submitting
                }
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
            !activeSnapshotTab && (
              <EmptyWorkspace
                onNewProject={openNewProjectDialog}
                onImportSource={openDirectImportPicker}
                busy={sourceImportBusy}
              />
            )}
        </div>
      </main>

      <NewProjectDialog
        open={newProjectOpen}
        busy={sourceImportBusy}
        error={sourceImportError}
        onClose={() => {
          if (sourceImportBusy) return
          setNewProjectOpen(false)
        }}
        onCreateBlank={handleCreateBlankProject}
        onImportSource={handleImportSourceFile}
      />

      {/* 空工作区直达导入：不弹“新建项目”对话，选择文件后直接进入只读投影。 */}
      <input
        ref={directImportInputRef}
        type="file"
        accept=".jsx,.tsx,.js,.ts"
        hidden
        data-direct-import-input
        onChange={(event) => {
          const file = event.target.files?.[0]
          event.target.value = ''
          if (file) void handleImportSourceFile(file)
        }}
      />

      <DirectOpsDecisionDialog
        decision={directDecision}
        busy={directDecisionBusy}
        onChoose={(optionId) => void resolveDirectDecision(optionId)}
        onClose={() => {
          directDecisionSourceRef.current = null
          setDirectDecision(null)
        }}
      />

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

      <DraftLeaveGuard
        open={Boolean(draftGuard)}
        actionText="关闭"
        busy={draftGuardBusy}
        onCancel={cancelDraftGuard}
        onConfirm={confirmDraftGuard}
      />

      <AccountDialog open={accountOpen} onClose={() => setAccountOpen(false)} />
      <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </div>
  )
}
