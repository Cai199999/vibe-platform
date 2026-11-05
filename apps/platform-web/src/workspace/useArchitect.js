import { useCallback, useEffect, useRef, useState } from 'react'
import {
  fetchArchitectAnswers,
  fetchArchitectCancel,
  fetchArchitectDecision,
  streamArchitect,
} from './architectStream.js'

const STORAGE_KEY = 'vibe.architect.v1'
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

export const EMPTY_ARCHITECT = {
  open: false,
  runId: null,
  status: 'IDLE',
  lastSeq: 0,
  appShape: null,
  confidenceScore: 0,
  threshold: 0.85,
  round: 0,
  maxRounds: 5,
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

export function useArchitect({ token } = {}) {
  const [states, setStates] = useState(loadStates)
  const statesRef = useRef(states)
  const controllersRef = useRef(new Map())

  useEffect(() => {
    statesRef.current = states
  }, [states])

  // 只持久化可恢复字段；open 永远重置，避免刷新后自动弹出面板。
  useEffect(() => {
    const snapshot = {}
    Object.entries(states).forEach(([projectId, state]) => {
      if (!state.runId) return
      snapshot[projectId] = {
        runId: state.runId,
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
      }
    })
    try {
      window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(snapshot))
    } catch {
      // 快照过大 / 隐私模式：忽略。
    }
  }, [states])

  useEffect(
    () => () => {
      controllersRef.current.forEach((controller) => controller.abort())
      controllersRef.current.clear()
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
    async (projectId, { payload = null, runId = null, replay = false, afterSeq = 0 } = {}) => {
      const controller = new AbortController()
      controllersRef.current.get(projectId)?.abort()
      controllersRef.current.set(projectId, controller)
      try {
        await streamArchitect({
          payload,
          runId,
          replay,
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

  const openPanel = useCallback(
    (projectId) => {
      const current = statesRef.current[projectId]
      patch(projectId, { open: true })
      if (!current?.runId) return
      if (ACTIVE.has(current.status)) {
        void connect(projectId, {
          runId: current.runId,
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
    async (projectId, answers, comment = '') => {
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
        )
        // 服务端已写入 clarification.answered，重放继续同一条时间轴。
        await connect(projectId, {
          runId: current.runId,
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

  const cancel = useCallback(
    async (projectId) => {
      const current = statesRef.current[projectId]
      if (!current?.runId || TERMINAL.has(current.status)) return
      patch(projectId, { cancelRequested: true, error: null })
      try {
        await fetchArchitectCancel(current.runId, '用户取消架构澄清', token)
        await connect(projectId, {
          runId: current.runId,
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
    openPanel,
    closePanel,
    toggleOpen,
    answer,
    decide,
    cancel,
    abort,
    remove,
    dispose,
  }
}
