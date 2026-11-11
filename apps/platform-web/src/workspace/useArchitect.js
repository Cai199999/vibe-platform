import { useCallback, useEffect, useRef, useState } from 'react'
import {
  fetchArchitectAnswers,
  fetchArchitectCancel,
  fetchArchitectDecision,
  fetchArchitectHelper,
  streamArchitect,
} from './architectStream.js'
import { streamFullstack } from './fullstackStream.js'

const STORAGE_KEY = 'vibe.architect.v1'
// Architect / Fullstack 事件合并写盘间隔：SSE 期间不再逐事件同步序列化。
const PERSIST_DELAY_MS = 800
const TERMINAL = new Set(['CONFIRMED', 'CANCELLED', 'FAILED'])
const ACTIVE = new Set([
  'RUNNING',
  'ANALYZING',
  'DRAFTING_CONTRACT',
  'REPAIRING_CONTRACT',
  'REVISING_CONTRACT',
  'CONFIRMING_CONTRACT',
  'CANCELLING',
])

/**
 * 全栈代码生成的默认状态。
 *
 * 与 Architect 状态同属一个项目快照（architect.fullstack），但事件流完全
 * 独立：契约确认后才启动，终态为 fullstack.completed / fullstack.failed。
 */
export const EMPTY_FULLSTACK = {
  runId: null,
  // IDLE -> RUNNING -> SUCCEEDED | FAILED
  status: 'IDLE',
  // fullstack | frontend。frontend 表示契约确认后的初始前端骨架生成。
  mode: null,
  lastSeq: 0,
  sandboxId: null,
  previewUrl: null,
  // 固定七个节点：DBACoder / BackendCoder / FrontendCoder / InfraCoder /
  // ConsistencyVerifier / BuildVerifier / SandboxRestart。
  steps: [],
  // 接口一致性检查的结构化报告（通过 / 差异列表）。
  consistency: null,
  activeNode: null,
  // 节点输出日志（文件级），只保留最近若干条，避免撑爆 sessionStorage。
  outputs: [],
  result: null,
  error: null,
  errorCode: null,
  // 失败归因：CODE（代码缺陷）/ CONTRACT（契约矛盾）/ ENVIRONMENT。
  // 契约矛盾时前端弹窗引导用户修改契约，而不是再次点击生成。
  failureKind: null,
  needsClarification: false,
  clarification: null,
  starting: false,
  updatedAt: null,
}

const FULLSTACK_NODES = [
  'DBACoder',
  'BackendCoder',
  'FrontendCoder',
  'InfraCoder',
  'ConsistencyVerifier',
  'BuildVerifier',
  'SandboxRestart',
]

// 与 Python MODE_NODE_ORDERS 保持一致：Supervisor 派发专属专家时，
// 面板只展示该子图真实执行的节点。
const MODE_FULLSTACK_NODES = {
  fullstack: FULLSTACK_NODES,
  frontend: ['FrontendCoder', 'BuildVerifier', 'SandboxRestart'],
  backend: ['DBACoder', 'BackendCoder', 'BuildVerifier', 'SandboxRestart'],
  dba: ['DBACoder', 'SandboxRestart'],
}

function nodesForMode(mode) {
  return MODE_FULLSTACK_NODES[mode] ?? FULLSTACK_NODES
}

const FULLSTACK_NODE_LABELS = {
  DBACoder: '数据库工程师',
  BackendCoder: '后端工程师',
  FrontendCoder: '前端工程师',
  InfraCoder: '部署配置工程师',
  ConsistencyVerifier: '接口一致性检查',
  BuildVerifier: '构建校验',
  SandboxRestart: '沙盒重启',
}

const FULLSTACK_OUTPUT_LIMIT = 40

/**
 * 影子助手（Helper Agent）的本地会话状态。
 *
 * 与主架构状态完全隔离：这里只保存“随意提问”的问答记录，
 * 既不写入 messages，也不会触发 Graph / SSE / 契约状态变更。
 */
export const EMPTY_HELPER = {
  messages: [],
  pending: false,
  error: null,
}

/**
 * 统计主流程中仍需用户确认的关键问题数量。
 *
 * 画布顶部 Banner 与“生成”入口拦截共用同一口径：
 *  - 只有 AWAITING_CLARIFICATION 才算“必须回答”；
 *  - clarification.questions 优先，缺失时回退到 missingSlots；
 *  - 无法解析具体数量时至少按 1 个处理，避免误放行。
 */
export function countPendingKeyQuestions(architectState) {
  if (!architectState?.runId) return 0
  if (architectState.status !== 'AWAITING_CLARIFICATION') return 0
  const clarification = architectState.clarification
  const questions = clarification?.questions
  if (Array.isArray(questions) && questions.length > 0) {
    const required = questions.filter((question) => question.required !== false)
    return Math.max(1, required.length)
  }
  const missing = clarification?.missingSlots
  if (Array.isArray(missing) && missing.length > 0) return missing.length
  return 1
}

export const EMPTY_ARCHITECT = {
  open: false,
  runId: null,
  // Supervisor 派发时由服务端提供；恢复连接时优先复用该路径。
  streamPath: null,
  status: 'IDLE',
  lastSeq: 0,
  appShape: null,
  confidenceScore: 0,
  threshold: 0.85,
  round: 0,
  maxRounds: 10,
  clarification: null,
  contract: null,
  citations: [],
  messages: [],
  sourceOrigin: null,
  error: null,
  submitting: false,
  answerSubmitting: false,
  decisionSubmitting: false,
  cancelRequested: false,
  updatedAt: null,
  // 影子助手的独立会话：只读问答，不影响主状态机。
  helper: EMPTY_HELPER,
  // 契约确认后的全栈生成状态；与 Architect Run 生命周期解耦。
  fullstack: EMPTY_FULLSTACK,
}

export const ARCHITECT_TERMINAL_STATUSES = TERMINAL

function loadStates() {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return {}
    const restored = {}
    Object.entries(parsed).forEach(([projectId, saved]) => {
      if (!saved || typeof saved !== 'object') return
      restored[projectId] = {
        ...EMPTY_ARCHITECT,
        ...saved,
        open: false,
        submitting: false,
        answerSubmitting: false,
        decisionSubmitting: false,
        cancelRequested: Boolean(saved.cancelRequested),
        messages: Array.isArray(saved.messages) ? saved.messages : [],
        helper: {
          messages: Array.isArray(saved.helper?.messages)
            ? saved.helper.messages.slice(-40)
            : [],
          // 刷新后不可能还有在途请求，统一收敛为可重试状态。
          pending: false,
          error: null,
        },
        fullstack: {
          ...EMPTY_FULLSTACK,
          ...(saved.fullstack && typeof saved.fullstack === 'object'
            ? saved.fullstack
            : {}),
          // 刷新页面后旧的 SSE 连接已断开；把“进行中”收敛为可重试失败，
          // 避免按钮永久禁用。服务端生成即使成功也会写入明确终态，重跑幂等。
          status:
            saved.fullstack?.status === 'RUNNING' ? 'FAILED' : saved.fullstack?.status ??
              'IDLE',
          error:
            saved.fullstack?.status === 'RUNNING'
              ? '页面刷新导致生成事件流中断，可重新生成。'
              : saved.fullstack?.error ?? null,
          starting: false,
          activeNode: null,
          outputs: [],
        },
      }
    })
    return restored
  } catch {
    return {}
  }
}

function appendMessage(messages, entry) {
  if (!entry?.content) return messages
  if (entry.id && messages.some((item) => item.id === entry.id)) {
    return messages
  }
  return [...messages, entry].slice(-80)
}

function questionSummary(payload) {
  const questions = Array.isArray(payload?.questions) ? payload.questions : []
  if (questions.length === 0) return '需要补充一些关键信息后才能继续。'
  const reason =
    payload?.reason === 'app_shape_required'
      ? '先确认本次要做成哪种应用形态。'
      : '还需要确认以下信息：'
  return `${reason}\n${questions.map((item) => `• ${item.prompt}`).join('\n')}`
}

function answerSummary(payload, questions) {
  const byId = new Map(
    (questions ?? []).map((item) => [item.questionId, item.prompt]),
  )
  const answers = Array.isArray(payload?.answers) ? payload.answers : []
  const lines = answers
    .map((item) => {
      const value = item.value ?? item.text
      if (value === undefined || value === null || value === '') return null
      const prompt = byId.get(item.questionId) || item.questionId
      return `• ${prompt}：${Array.isArray(value) ? value.join('、') : value}`
    })
    .filter(Boolean)
  if (payload?.comment) lines.push(`• 补充说明：${payload.comment}`)
  return lines.join('\n')
}

function reduceArchitect(state, event) {
  const payload = event.payload ?? {}
  const base = {
    ...state,
    lastSeq: Math.max(state.lastSeq ?? 0, event.seq ?? 0),
    updatedAt: new Date().toISOString(),
  }

  switch (event.type) {
    case 'architect.started':
      return {
        ...base,
        status: 'RUNNING',
        submitting: true,
        error: null,
      }
    case 'architect.context.ingested':
      return {
        ...base,
        sourceOrigin: payload.origin ?? state.sourceOrigin,
      }
    case 'architect.rag.retrieved':
      return {
        ...base,
        citations: Array.isArray(payload.citations) ? payload.citations : [],
      }
    case 'architect.app_shape.detected':
      return {
        ...base,
        appShape: payload.appShape ?? state.appShape,
        confidenceScore: payload.confidenceScore ?? state.confidenceScore,
      }
    // 轻量 token 流：只保留尾部文本做打字机，最终结构仍以 completed
    // 事件为准。不写入 messages，避免流式片段污染对话历史。
    case 'token': {
      const text = String(payload.text || '')
      if (!text) return base
      return {
        ...base,
        streamingText: `${state.streamingText || ''}${text}`.slice(-1200),
        streamingNode: payload.node ?? state.streamingNode ?? null,
      }
    }
    case 'architect.analysis.completed':
      return {
        ...base,
        appShape: payload.appShape ?? state.appShape,
        confidenceScore: payload.confidenceScore ?? state.confidenceScore,
        threshold: payload.threshold ?? state.threshold,
        round: payload.round ?? state.round,
      }
    case 'architect.clarification.required':
      return {
        ...base,
        status: 'AWAITING_CLARIFICATION',
        clarification: payload,
        appShape: payload.appShape ?? state.appShape,
        confidenceScore: payload.confidenceScore ?? state.confidenceScore,
        threshold: payload.threshold ?? state.threshold,
        round: payload.round ?? state.round,
        maxRounds: payload.maxRounds ?? state.maxRounds,
        streamingText: '',
        streamingNode: null,
        submitting: false,
        answerSubmitting: false,
        messages: appendMessage(state.messages, {
          id: `clarification:${payload.clarificationId}`,
          role: 'assistant',
          kind: 'clarification',
          content: questionSummary(payload),
          createdAt: new Date().toISOString(),
        }),
      }
    case 'architect.clarification.answered':
      return {
        ...base,
        status: 'ANALYZING',
        clarification: null,
        submitting: true,
        answerSubmitting: false,
        messages: appendMessage(state.messages, {
          id: `answered:${payload.clarificationId}`,
          role: 'user',
          content: answerSummary(payload, state.clarification?.questions),
          createdAt: new Date().toISOString(),
        }),
      }
    case 'architect.contract.drafting':
      return { ...base, status: 'DRAFTING_CONTRACT', submitting: true }
    case 'architect.contract.repair.requested':
      return {
        ...base,
        status: 'REPAIRING_CONTRACT',
        messages: appendMessage(state.messages, {
          id: `repair:${event.seq}`,
          role: 'system',
          content: `契约校验未通过，正在自动修复：${(payload.issues ?? []).join('；')}`,
          createdAt: new Date().toISOString(),
        }),
      }
    case 'architect.contract.ready':
      return {
        ...base,
        status: 'AWAITING_CONTRACT_CONFIRMATION',
        contract: payload.contract ?? state.contract,
        citations: payload.citations ?? state.citations,
        confidenceScore:
          payload.contract?.confidenceScore ?? state.confidenceScore,
        threshold: payload.contract?.threshold ?? state.threshold,
        submitting: false,
        decisionSubmitting: false,
        streamingText: '',
        streamingNode: null,
        messages: appendMessage(state.messages, {
          id: `contract:${payload.contract?.contractId}:${event.seq}`,
          role: 'assistant',
          kind: 'contract',
          content: `《全栈实施契约》已生成（应用形态：${
            payload.contract?.appShape ?? state.appShape
          }，置信度 ${Math.round(
            (payload.contract?.confidenceScore ?? 0) * 100,
          )}%）。请确认、要求修订或取消。`,
          createdAt: new Date().toISOString(),
        }),
      }
    case 'architect.contract.decision.received':
      return {
        ...base,
        status:
          payload.decision === 'REVISE'
            ? 'REVISING_CONTRACT'
            : payload.decision === 'CONFIRM'
              ? 'CONFIRMING_CONTRACT'
              : 'CANCELLING',
        decisionSubmitting: true,
      }
    case 'architect.contract.revision_requested':
      return {
        ...base,
        status: 'REVISING_CONTRACT',
        messages: appendMessage(state.messages, {
          id: `revision:${event.seq}`,
          role: 'user',
          content: `请修订契约：${payload.comment}`,
          createdAt: new Date().toISOString(),
        }),
      }
    case 'architect.contract.confirmed':
      return {
        ...base,
        status: 'CONFIRMED',
        contract: payload.contract ?? state.contract,
        submitting: false,
        decisionSubmitting: false,
        messages: appendMessage(state.messages, {
          id: `confirmed:${payload.contractId}:${event.seq}`,
          role: 'system',
          content: '实施契约已确认，后续可进入全栈代码生成阶段。',
          createdAt: new Date().toISOString(),
        }),
      }
    case 'architect.cancelled':
      return {
        ...base,
        status: 'CANCELLED',
        clarification: null,
        submitting: false,
        decisionSubmitting: false,
        cancelRequested: false,
        messages: appendMessage(state.messages, {
          id: `cancelled:${event.seq}`,
          role: 'system',
          content: '本次架构澄清已取消。',
          createdAt: new Date().toISOString(),
        }),
      }
    case 'architect.failed':
      return {
        ...base,
        status: 'FAILED',
        error: payload.error || '架构澄清失败',
        errorCode: payload.errorCode ?? null,
        clarification: null,
        streamingText: '',
        streamingNode: null,
        submitting: false,
        decisionSubmitting: false,
        messages: appendMessage(state.messages, {
          id: `failed:${event.seq}`,
          role: 'system',
          content: payload.error || '架构澄清失败',
          createdAt: new Date().toISOString(),
        }),
      }
    default:
      return base
  }
}

function fullstackRunId() {
  try {
    return `fsk_${crypto.randomUUID().replace(/-/g, '').slice(0, 20)}`
  } catch {
    return `fsk_${Math.random().toString(36).slice(2, 12)}${Date.now().toString(36)}`
  }
}

function createFullstackSteps(nodes = FULLSTACK_NODES) {
  return nodes.map((node, index) => ({
    node,
    label: FULLSTACK_NODE_LABELS[node] ?? node,
    index: index + 1,
    total: nodes.length,
    status: 'PENDING',
    summary: '',
    files: [],
    durationMs: 0,
    error: null,
  }))
}

function updateFullstackStep(steps, node, patch) {
  let matched = false
  const next = steps.map((step) => {
    if (step.node !== node) return step
    matched = true
    return { ...step, ...patch }
  })
  if (matched) return next
  // 服务端新增节点（模板演进）时补齐，避免事件被丢弃。
  return [
    ...next,
    {
      node,
      label: FULLSTACK_NODE_LABELS[node] ?? node,
      index: next.length + 1,
      total: Math.max(next.length + 1, FULLSTACK_NODES.length),
      status: 'PENDING',
      summary: '',
      files: [],
      durationMs: 0,
      error: null,
      ...patch,
    },
  ]
}

function appendFullstackOutput(outputs, entry) {
  // 同一文件重复输出（例如重试）只保留最后一条，避免日志重复刷屏。
  const key = `${entry.node}:${entry.path}`
  const next = outputs.filter((item) => `${item.node}:${item.path}` !== key)
  next.push(entry)
  return next.slice(-FULLSTACK_OUTPUT_LIMIT)
}

/**
 * 归约 FullstackGraph 事件。
 *
 * 事件类型：
 *   fullstack.started / node.started / node.output / node.completed /
 *   node.failed / sandbox.restarting / sandbox.ready / completed / failed
 */
function reduceFullstack(state, event) {
  const current = state ?? EMPTY_FULLSTACK
  const payload = event.payload ?? {}
  const base = {
    ...current,
    lastSeq: Math.max(current.lastSeq ?? 0, event.seq ?? 0),
    updatedAt: new Date().toISOString(),
  }

  switch (event.type) {
    case 'fullstack.started':
      return {
        ...base,
        runId: payload.runId ?? current.runId,
        status: 'RUNNING',
        mode: payload.mode ?? current.mode ?? 'fullstack',
        starting: false,
        sandboxId: payload.sandboxId ?? current.sandboxId,
        previewUrl: payload.previewUrl ?? current.previewUrl,
        steps: createFullstackSteps(
          Array.isArray(payload.nodes) && payload.nodes.length > 0
            ? payload.nodes
            : FULLSTACK_NODES,
        ),
        error: null,
        errorCode: null,
        failureKind: null,
        needsClarification: false,
        clarification: null,
      }
    case 'fullstack.node.started':
      return {
        ...base,
        status: 'RUNNING',
        starting: false,
        activeNode: payload.node ?? current.activeNode,
        steps: updateFullstackStep(current.steps, payload.node, {
          status: 'RUNNING',
          error: null,
        }),
      }
    case 'fullstack.node.output':
      return {
        ...base,
        outputs: appendFullstackOutput(current.outputs, {
          id: `${payload.node}:${payload.path}`,
          node: payload.node,
          path: payload.path,
          language: payload.language ?? 'text',
          content: payload.content ?? '',
          truncated: Boolean(payload.truncated),
          bytes: payload.bytes ?? 0,
        }),
      }
    case 'fullstack.node.completed':
      return {
        ...base,
        activeNode:
          current.activeNode === payload.node ? null : current.activeNode,
        steps: updateFullstackStep(current.steps, payload.node, {
          status: 'SUCCEEDED',
          summary: payload.summary ?? '',
          files: Array.isArray(payload.files) ? payload.files : [],
          durationMs: payload.durationMs ?? 0,
          error: null,
        }),
      }
    case 'fullstack.consistency.report':
      // 报告既保留在结构化字段里，也追加到节点日志，失败的差异可被直接阅读。
      return {
        ...base,
        consistency: payload,
        outputs: appendFullstackOutput(current.outputs, {
          id: `ConsistencyVerifier:report:${event.seq}`,
          node: 'ConsistencyVerifier',
          path: 'server/consistency-report.json',
          language: 'json',
          content: JSON.stringify(payload, null, 2),
          truncated: false,
          bytes: 0,
        }),
      }
    case 'fullstack.node.failed':
      return {
        ...base,
        activeNode: null,
        steps: updateFullstackStep(current.steps, payload.node, {
          status: 'FAILED',
          error: payload.error ?? '节点执行失败',
        }),
      }
    case 'fullstack.sandbox.restarting':
      return {
        ...base,
        outputs: appendFullstackOutput(current.outputs, {
          id: `SandboxRestart:restart:${event.seq}`,
          node: 'SandboxRestart',
          path: 'container.restart',
          language: 'text',
          content: `正在重启沙盒并等待 ${payload.healthPath ?? '/api/health'} 就绪…`,
          truncated: false,
          bytes: 0,
        }),
      }
    case 'fullstack.sandbox.ready':
      return {
        ...base,
        sandboxId: payload.sandboxId ?? current.sandboxId,
        previewUrl: payload.previewUrl ?? current.previewUrl,
      }
    case 'fullstack.completed':
      return {
        ...base,
        status: 'SUCCEEDED',
        mode: payload.mode ?? current.mode ?? 'fullstack',
        starting: false,
        activeNode: null,
        sandboxId: payload.sandboxId ?? current.sandboxId,
        previewUrl: payload.previewUrl ?? current.previewUrl,
        result: payload,
        // 成功后把仍在 RUNNING 的节点收口，避免 UI 永远转圈。
        steps: current.steps.map((step) =>
          step.status === 'RUNNING'
            ? { ...step, status: 'SUCCEEDED' }
            : step,
        ),
        error: null,
        errorCode: null,
        failureKind: null,
        needsClarification: false,
        clarification: null,
      }
    case 'fullstack.failed':
      return {
        ...base,
        status: 'FAILED',
        mode: payload.mode ?? current.mode ?? 'fullstack',
        starting: false,
        activeNode: null,
        steps: payload.node
          ? updateFullstackStep(current.steps, payload.node, {
              status: 'FAILED',
              error: payload.error ?? '生成失败',
            })
          : current.steps,
        error: payload.error || '全栈代码生成失败',
        errorCode: payload.errorCode ?? null,
        failureKind: payload.failureKind ?? null,
        needsClarification: Boolean(payload.needsClarification),
        clarification: payload.clarification ?? null,
      }
    default:
      return base
  }
}

export function useArchitect({ token } = {}) {
  const [states, setStates] = useState(loadStates)
  const statesRef = useRef(states)
  const controllersRef = useRef(new Map())
  // 全栈生成与 Architect 澄清是两条独立 SSE，各自维护 AbortController。
  const fullstackControllersRef = useRef(new Map())
  // 同步防重入：点击处理里会先 await 门控释放，这段时间 React 状态
  // 还未变成 RUNNING，仅靠 statesRef 无法阻止双击启动两个 Run。
  const fullstackStartingRef = useRef(new Set())
  // 影子助手单飞锁：状态更新是异步的，靠 pending 字段无法阻止双击双发。
  const helperPendingRef = useRef(new Set())

  useEffect(() => {
    statesRef.current = states
  }, [states])

  // 只持久化可恢复字段；open 永远重置，避免刷新后自动弹出面板。
  // 800ms 合并写盘：契约 / 全栈节点事件到达时不再逐条同步 JSON 序列化。
  useEffect(() => {
    const timer = window.setTimeout(() => {
      const snapshot = {}
      Object.entries(statesRef.current).forEach(([projectId, state]) => {
        if (!state.runId) return
        snapshot[projectId] = {
          runId: state.runId,
          streamPath: state.streamPath,
          status: state.status,
          lastSeq: state.lastSeq,
          appShape: state.appShape,
          confidenceScore: state.confidenceScore,
          threshold: state.threshold,
          round: state.round,
          maxRounds: state.maxRounds,
          clarification: state.clarification,
          contract: state.contract,
          citations: state.citations,
          messages: state.messages,
          sourceOrigin: state.sourceOrigin,
          error: state.error,
          errorCode: state.errorCode ?? null,
          cancelRequested: state.cancelRequested,
          updatedAt: state.updatedAt,
          // 影子助手只保留最近 20 条：足够恢复上下文，又不会撑爆 sessionStorage。
          helper: {
            messages: Array.isArray(state.helper?.messages)
              ? state.helper.messages.slice(-20)
              : [],
          },
          // 只持久化可恢复字段：节点输出体积大且刷新后无法继续追加，丢弃。
          fullstack: state.fullstack
            ? {
                runId: state.fullstack.runId,
                status: state.fullstack.status,
                mode: state.fullstack.mode,
                lastSeq: state.fullstack.lastSeq,
                sandboxId: state.fullstack.sandboxId,
                previewUrl: state.fullstack.previewUrl,
                steps: state.fullstack.steps,
                result: state.fullstack.result,
                error: state.fullstack.error,
                errorCode: state.fullstack.errorCode,
                failureKind: state.fullstack.failureKind,
                needsClarification: state.fullstack.needsClarification,
                clarification: state.fullstack.clarification,
                updatedAt: state.fullstack.updatedAt,
              }
            : undefined,
        }
      })
      try {
        window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(snapshot))
      } catch {
        // 快照过大 / 隐私模式：忽略。
      }
    }, PERSIST_DELAY_MS)
    return () => window.clearTimeout(timer)
  }, [states])

  useEffect(
    () => () => {
      controllersRef.current.forEach((controller) => controller.abort())
      controllersRef.current.clear()
      fullstackControllersRef.current.forEach((controller) => controller.abort())
      fullstackControllersRef.current.clear()
    },
    [],
  )

  const patch = useCallback((projectId, updater) => {
    setStates((previous) => {
      const base = previous[projectId] ?? EMPTY_ARCHITECT
      const next = typeof updater === 'function' ? updater(base) : { ...base, ...updater }
      if (next === base) return previous
      return { ...previous, [projectId]: next }
    })
  }, [])

  const connect = useCallback(
    async (
      projectId,
      {
        payload = null,
        runId = null,
        replay = false,
        attach = false,
        streamPath = null,
        afterSeq = 0,
      } = {},
    ) => {
      const controller = new AbortController()
      controllersRef.current.get(projectId)?.abort()
      controllersRef.current.set(projectId, controller)
      try {
        await streamArchitect({
          payload,
          runId,
          replay,
          attach,
          streamPath,
          afterSeq,
          token,
          signal: controller.signal,
          onEvent: (event) => {
            patch(projectId, (previous) => reduceArchitect(previous, event))
          },
        })
      } catch (cause) {
        if (cause?.name === 'AbortError') return
        patch(projectId, (previous) => ({
          ...previous,
          submitting: false,
          answerSubmitting: false,
          decisionSubmitting: false,
          error: `Architect 事件流失败：${cause?.message || cause}`,
          status:
            previous.status === 'IDLE' || !previous.runId
              ? 'FAILED'
              : previous.status,
        }))
      } finally {
        if (controllersRef.current.get(projectId) === controller) {
          controllersRef.current.delete(projectId)
        }
      }
    },
    [patch, token],
  )

  /**
   * 启动全栈生成并消费 SSE。
   *
   * 生成在服务端后台线程执行，面板关闭 / 重新渲染都不会中断；只有关闭
   * 项目窗口或卸载页面时才会 abort 本地连接，服务端仍会产出终态事件，
   * 重新打开后可通过同一个 runId 重放。
   */
  const connectFullstack = useCallback(
    async (projectId, { payload, runId, replay = false } = {}) => {
      const controller = new AbortController()
      fullstackControllersRef.current.get(projectId)?.abort()
      fullstackControllersRef.current.set(projectId, controller)
      try {
        await streamFullstack({
          payload,
          runId,
          replay,
          afterSeq: 0,
          token,
          signal: controller.signal,
          onEvent: (event) => {
            patch(projectId, (previous) => ({
              ...previous,
              fullstack: reduceFullstack(previous.fullstack, event),
            }))
          },
        })
      } catch (cause) {
        if (cause?.name === 'AbortError') return
        patch(projectId, (previous) => {
          const fullstack = previous.fullstack ?? EMPTY_FULLSTACK
          // 终态已到达时，连接关闭属于正常收尾，不改写结果。
          if (fullstack.status === 'SUCCEEDED' || fullstack.status === 'FAILED') {
            return previous
          }
          return {
            ...previous,
            fullstack: {
              ...fullstack,
              status: 'FAILED',
              starting: false,
              activeNode: null,
              error: `全栈生成事件流失败：${cause?.message || cause}`,
              errorCode: 'FULLSTACK_STREAM_ERROR',
            },
          }
        })
      } finally {
        if (fullstackControllersRef.current.get(projectId) === controller) {
          fullstackControllersRef.current.delete(projectId)
          // 事件流已到终态 / 已失败，允许用户重新生成。
          fullstackStartingRef.current.delete(projectId)
        }
      }
    },
    [patch, token],
  )

  /**
   * 点击“开始生成全栈代码”。
   *
   * @param {string} projectId
   * @param {object} options
   * @param {object} [options.contract] 已确认契约（默认取当前 Architect 状态）
   * @param {string} [options.sandboxId]
   * @param {Record<string,string>} [options.files] 无沙盒时的源码兜底
   * @returns {boolean} 是否成功启动（重复点击 / 契约缺失返回 false）
   */
  const generateFullstack = useCallback(
    (projectId, options = {}) => {
      const current = statesRef.current[projectId] ?? EMPTY_ARCHITECT
      const fullstack = current.fullstack ?? EMPTY_FULLSTACK
      // 所有“静默返回 false”的分支都必须留下日志：否则用户只会看到
      // 按钮点击后毫无反应，无法判断是锁未释放还是契约缺失。
      if (
        fullstack.starting ||
        fullstack.status === 'RUNNING' ||
        fullstackStartingRef.current.has(projectId)
      ) {
        console.warn('[fullstack] generate skipped: already running', {
          projectId,
          status: fullstack.status,
          starting: Boolean(fullstack.starting),
          inflight: fullstackStartingRef.current.has(projectId),
        })
        return false
      }
      const contract = options.contract ?? current.contract
      const contractId = options.contractId ?? contract?.contractId
      if (!contract || !contractId) {
        console.warn('[fullstack] generate skipped: contract/contractId missing', {
          projectId,
          hasContract: Boolean(contract),
          contractId: contractId ?? null,
          architectStatus: current.status,
        })
        return false
      }
      console.info('[fullstack] generate accepted', {
        projectId,
        contractId,
        mode: options.mode ?? 'fullstack',
        appShape: contract.appShape,
        apiRouteCount: Array.isArray(contract.apiRoutes)
          ? contract.apiRoutes.length
          : 0,
        sandboxId: options.sandboxId ?? null,
        hasSourceFiles: Object.keys(options.files ?? {}).length > 0,
      })

      const runId = options.runId || fullstackRunId()
      const mode = options.mode === 'frontend' ? 'frontend' : 'fullstack'
      // 先占位再写状态，确保双击 / 门控释放期间的重复点击被同步拦截。
      fullstackStartingRef.current.add(projectId)
      patch(projectId, (previous) => ({
        ...previous,
        fullstack: {
          ...EMPTY_FULLSTACK,
          runId,
          status: 'RUNNING',
          mode,
          starting: true,
          steps: createFullstackSteps(
            mode === 'frontend'
              ? ['FrontendCoder', 'BuildVerifier', 'SandboxRestart']
              : FULLSTACK_NODES,
          ),
          updatedAt: new Date().toISOString(),
        },
      }))

      const payload = {
        runId,
        tenantId: options.tenantId || undefined,
        projectId,
        contractId,
        sandboxId: options.sandboxId || undefined,
        contract,
        source: {
          sandboxId: options.sandboxId || undefined,
          files: options.files ?? {},
        },
        options: {
          mode,
          verifyBuild: options.verifyBuild ?? true,
          restartSandbox: options.restartSandbox ?? true,
          ...(options.options ?? {}),
        },
      }
      void connectFullstack(projectId, { payload, runId })
      return true
    },
    [connectFullstack, patch],
  )

  /**
   * 挂载 Supervisor 已启动的全栈/专家 Run。
   *
   * 与 generateFullstack 的区别：不 POST 启动接口，只按 runId 订阅已有
   * 事件流，避免“Supervisor 派发一次 + 前端再启动一次”重复生成。
   */
  const attachFullstack = useCallback(
    (projectId, { runId, mode = 'backend' } = {}) => {
      if (!runId) return false
      fullstackStartingRef.current.add(projectId)
      patch(projectId, (previous) => ({
        ...previous,
        fullstack: {
          ...EMPTY_FULLSTACK,
          runId,
          status: 'RUNNING',
          mode,
          starting: true,
          steps: createFullstackSteps(nodesForMode(mode)),
          updatedAt: new Date().toISOString(),
        },
      }))
      void connectFullstack(projectId, { runId, replay: true })
      return true
    },
    [connectFullstack, patch],
  )

  const start = useCallback(
    (projectId, payload) => {
      const runId = payload.runId
      patch(projectId, {
        ...EMPTY_ARCHITECT,
        open: true,
        runId,
        status: 'RUNNING',
        submitting: true,
      })
      void connect(projectId, { payload, runId })
    },
    [connect, patch],
  )

  /**
   * 挂载一个已由 Supervisor 启动的 Architect Run。
   *
   * 与 start 的区别：不 POST /architect/runs/stream，只订阅服务端返回的
   * streamPath，避免 both 模式重复启动架构子图。
   */
  const attach = useCallback(
    (projectId, { runId, streamPath }) => {
      patch(projectId, {
        ...EMPTY_ARCHITECT,
        open: true,
        runId,
        streamPath,
        status: 'RUNNING',
        submitting: true,
      })
      void connect(projectId, {
        runId,
        streamPath,
        attach: true,
        afterSeq: 0,
      })
    },
    [connect, patch],
  )

  const openPanel = useCallback(
    (projectId) => {
      const current = statesRef.current[projectId]
      patch(projectId, { open: true })
      if (!current?.runId) return
      if (ACTIVE.has(current.status)) {
        void connect(projectId, {
          runId: current.runId,
          streamPath: current.streamPath,
          replay: true,
          afterSeq: current.lastSeq,
        })
      }
    },
    [connect, patch],
  )

  const closePanel = useCallback(
    (projectId) => {
      patch(projectId, { open: false })
    },
    [patch],
  )

  const answer = useCallback(
    async (projectId, answers, comment = '', options = {}) => {
      const current = statesRef.current[projectId]
      const clarification = current?.clarification
      if (!current?.runId || !clarification?.clarificationId) return
      patch(projectId, { answerSubmitting: true, error: null })
      try {
        await fetchArchitectAnswers(
          current.runId,
          clarification.clarificationId,
          answers,
          comment,
          token,
          Boolean(options.adoptSuggestions),
        )
        // 服务端已写入 clarification.answered，重放继续同一条时间轴。
        await connect(projectId, {
          runId: current.runId,
          streamPath: current.streamPath,
          replay: true,
          afterSeq: current.lastSeq,
        })
      } catch (cause) {
        patch(projectId, {
          answerSubmitting: false,
          error: `提交澄清失败：${cause?.message || cause}`,
        })
      }
    },
    [connect, patch, token],
  )

  const decide = useCallback(
    async (projectId, decision, comment = '') => {
      const current = statesRef.current[projectId]
      const contract = current?.contract
      if (!current?.runId || !contract?.contractId) return
      patch(projectId, { decisionSubmitting: true, error: null })
      try {
        await fetchArchitectDecision(
          current.runId,
          contract.contractId,
          decision,
          comment,
          token,
        )
        await connect(projectId, {
          runId: current.runId,
          streamPath: current.streamPath,
          replay: true,
          afterSeq: current.lastSeq,
        })
      } catch (cause) {
        patch(projectId, {
          decisionSubmitting: false,
          error: `提交契约决策失败：${cause?.message || cause}`,
        })
      }
    },
    [connect, patch, token],
  )

  /**
   * 影子助手：只读提问。
   *
   * 设计约束：
   *  1. 绝不改写 state.messages / clarification / contract，主流程只能由
   *     answer / decide 推进；
   *  2. 复用当前 Run 的架构快照与知识库（服务端只读），前端只负责把
   *     “最近若干轮问答”作为 history 传过去；
   *  3. 请求失败只影响 helper.error，不污染主面板错误条。
   *
   * @returns {Promise<boolean>} 是否成功拿到回答
   */
  const askHelper = useCallback(
    async (projectId, question) => {
      const trimmed = String(question ?? '').trim()
      if (!trimmed) return false
      const current = statesRef.current[projectId] ?? EMPTY_ARCHITECT
      if (!current.runId) {
        patch(projectId, (previous) => ({
          ...previous,
          helper: {
            ...(previous.helper ?? EMPTY_HELPER),
            error: '影子助手需要先有一个正在进行的架构设计。',
          },
        }))
        return false
      }
      // 同步单飞：避免 Enter 与点击在同一帧内触发两次请求。
      if (helperPendingRef.current.has(projectId)) return false
      helperPendingRef.current.add(projectId)

      const history = (current.helper?.messages ?? [])
        .filter((message) => message.role === 'user' || message.role === 'assistant')
        .map((message) => ({ role: message.role, content: message.content }))

      const userMessage = {
        id: `helper-user:${Date.now()}`,
        role: 'user',
        content: trimmed,
        createdAt: new Date().toISOString(),
      }
      patch(projectId, (previous) => ({
        ...previous,
        helper: {
          ...(previous.helper ?? EMPTY_HELPER),
          messages: [...(previous.helper?.messages ?? []), userMessage].slice(-40),
          pending: true,
          error: null,
        },
      }))

      try {
        const response = await fetchArchitectHelper(
          current.runId,
          trimmed,
          history,
          token,
        )
        const answer = String(response?.answer ?? '').trim()
        patch(projectId, (previous) => ({
          ...previous,
          helper: {
            ...(previous.helper ?? EMPTY_HELPER),
            messages: [
              ...(previous.helper?.messages ?? []),
              {
                id: `helper-assistant:${Date.now()}`,
                role: 'assistant',
                content: answer || '助手没有返回内容，请换个问法再试。',
                citations: Array.isArray(response?.citations)
                  ? response.citations
                  : [],
                model: response?.model ?? '',
                createdAt: new Date().toISOString(),
              },
            ].slice(-40),
            pending: false,
            error: null,
          },
        }))
        return true
      } catch (cause) {
        patch(projectId, (previous) => ({
          ...previous,
          helper: {
            ...(previous.helper ?? EMPTY_HELPER),
            pending: false,
            error: `影子助手回答失败：${cause?.message || cause}`,
          },
        }))
        return false
      } finally {
        helperPendingRef.current.delete(projectId)
      }
    },
    [patch, token],
  )

  const cancel = useCallback(
    async (projectId) => {
      const current = statesRef.current[projectId]
      if (!current?.runId || TERMINAL.has(current.status)) return
      patch(projectId, { cancelRequested: true, error: null })
      try {
        await fetchArchitectCancel(current.runId, '用户取消架构澄清', token)
        await connect(projectId, {
          runId: current.runId,
          streamPath: current.streamPath,
          replay: true,
          afterSeq: current.lastSeq,
        })
      } catch (cause) {
        patch(projectId, {
          cancelRequested: false,
          error: `取消失败：${cause?.message || cause}`,
        })
      }
    },
    [connect, patch, token],
  )

  const abort = useCallback((projectId) => {
    controllersRef.current.get(projectId)?.abort()
    controllersRef.current.delete(projectId)
    fullstackControllersRef.current.get(projectId)?.abort()
    fullstackControllersRef.current.delete(projectId)
  }, [])

  const remove = useCallback((projectId) => {
    abort(projectId)
    setStates((previous) => {
      if (!(projectId in previous)) return previous
      const next = { ...previous }
      delete next[projectId]
      return next
    })
  }, [abort])

  /**
   * 重置到 IDLE 对话入口：用于架构澄清失败 / 用户主动重新发起。
   * 先中止所有 SSE，避免旧 Run 的迟到事件回写新状态。
   */
  const reset = useCallback(
    (projectId) => {
      abort(projectId)
      setStates((previous) => ({
        ...previous,
        [projectId]: { ...EMPTY_ARCHITECT, open: true },
      }))
    },
    [abort],
  )

  const toggleOpen = useCallback(
    (projectId, open) => {
      if (open) openPanel(projectId)
      else closePanel(projectId)
    },
    [closePanel, openPanel],
  )

  /**
   * 关闭项目窗口：先尽力取消服务端仍在运行的 Architect Run，再清空本地
   * 快照。取消失败（例如 AI Core 已重启）不阻塞前端关闭。
   */
  const dispose = useCallback(
    async (projectId) => {
      const current = statesRef.current[projectId]
      if (current?.runId && !TERMINAL.has(current.status)) {
        try {
          await fetchArchitectCancel(
            current.runId,
            '项目窗口已关闭',
            token,
          )
        } catch {
          // 服务端可能已不存在该 Run，本地清理继续。
        }
      }
      remove(projectId)
    },
    [remove, token],
  )

  return {
    states,
    start,
    attach,
    openPanel,
    closePanel,
    toggleOpen,
    answer,
    decide,
    askHelper,
    cancel,
    generateFullstack,
    attachFullstack,
    abort,
    reset,
    remove,
    dispose,
  }
}
