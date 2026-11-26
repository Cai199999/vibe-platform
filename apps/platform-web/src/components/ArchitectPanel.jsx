import { useEffect, useMemo, useRef, useState } from 'react'
import ProjectDocsDrawer from './ProjectDocsDrawer.jsx'
import ProjectDocumentCenter from './ProjectDocumentCenter.jsx'
import { buildProjectDocs } from '../workspace/projectDocs.js'
import {
  classifyArchitectDocument,
  discoverArchitectSolutions,
} from '../workspace/architectStream.js'
import {
  SOLUTION_KIND_LABELS,
  buildSolutionChoice,
  defaultSolutionChoice,
  solutionMeta,
  solutionSourceLabel,
} from '../workspace/solutionDiscovery.js'
import { useAuth } from '../auth/AuthContext.jsx'
import {
  activeKnowledgeDocuments,
  addProjectDocument,
  documentCount,
  mergeKnowledgeDocuments,
} from '../workspace/documentCenter.js'
import ContractViewer from './ContractViewer.jsx'
import { fetchCollaborationRecord } from '../workspace/collaborationClient.js'
import { generateMultiService } from '../workspace/generatedAppsClient.js'
import {
  collaborationFromSnapshot,
  mergeCollaboration,
} from '../workspace/collaborationTimeline.js'

const SHAPE_LABELS = {
  lightweight_web: '轻量网页工具',
  script_automation: '脚本自动化',
  full_stack_system: '全栈系统',
}

const STATUS_LABELS = {
  IDLE: '未开始',
  RUNNING: '读取源码',
  ANALYZING: '分析中',
  DRAFTING_CONTRACT: '起草契约',
  REPAIRING_CONTRACT: '修复契约',
  AWAITING_CLARIFICATION: '等待回答',
  AWAITING_CONTRACT_CONFIRMATION: '等待确认',
  AWAITING_VISUAL_EDIT: '等待编辑 v0',
  REVISING_CONTRACT: '修订中',
  CONFIRMING_CONTRACT: '确认中',
  CANCELLING: '取消中',
  CONFIRMED: '已确认',
  CANCELLED: '已取消',
  FAILED: '失败',
}

const STATUS_TONES = {
  IDLE: 'idle',
  RUNNING: 'working',
  ANALYZING: 'working',
  DRAFTING_CONTRACT: 'working',
  REPAIRING_CONTRACT: 'working',
  AWAITING_CLARIFICATION: 'attention',
  AWAITING_CONTRACT_CONFIRMATION: 'attention',
  AWAITING_VISUAL_EDIT: 'attention',
  REVISING_CONTRACT: 'working',
  CONFIRMING_CONTRACT: 'working',
  CANCELLING: 'working',
  CONFIRMED: 'success',
  CANCELLED: 'muted',
  FAILED: 'error',
}

const WORKING_STATUSES = new Set([
  'RUNNING',
  'ANALYZING',
  'DRAFTING_CONTRACT',
  'REPAIRING_CONTRACT',
  'REVISING_CONTRACT',
  'CONFIRMING_CONTRACT',
  'CANCELLING',
])

function clampPercent(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return 0
  return Math.max(0, Math.min(100, Math.round(number * 100)))
}

function isEmpty(value) {
  if (value === null || value === undefined) return true
  if (typeof value === 'string') return value.trim().length === 0
  if (Array.isArray(value)) return value.length === 0
  return false
}

function initialValue(question) {
  if (question.type === 'multi_choice') {
    return Array.isArray(question.defaultValue) ? question.defaultValue : []
  }
  return question.defaultValue ?? ''
}

/**
 * 全局澄清表单。
 *
 * 对话前置模式下不再提供“全栈 / 轻量 / 脚本”三选一卡片：
 * 应用形态由架构师从自然语言目标中推断，这里统一渲染缺失槽位问题。
 */
function ClarificationForm({ clarification, submitting, onSubmit, onCancel }) {
  const questions = clarification.questions ?? []

  const [values, setValues] = useState(() => {
    const initial = {}
    questions.forEach((question) => {
      initial[question.questionId] = initialValue(question)
    })
    return initial
  })
  const [comment, setComment] = useState('')

  const setValue = (questionId, value) => {
    setValues((previous) => ({ ...previous, [questionId]: value }))
  }

  const readyToSubmit = useMemo(
    () =>
      questions.every(
        (question) => !question.required || !isEmpty(values[question.questionId]),
      ),
    [questions, values],
  )

  const submit = (event) => {
    event.preventDefault()
    if (!readyToSubmit || submitting) return
    const answers = []
    questions.forEach((question) => {
      const value = values[question.questionId]
      if (isEmpty(value)) return
      const answer = { questionId: question.questionId, value }
      if (typeof value === 'string') answer.text = value
      answers.push(answer)
    })
    onSubmit(answers, comment)
  }

  // 只有用户点击该按钮才允许跳过剩余问题；AI 无权自行跳过。
  const adoptSuggestions = () => {
    if (submitting) return
    onSubmit([], '采用建议并跳过剩余提问', { adoptSuggestions: true })
  }

  return (
    <form className="architect-clarification" onSubmit={submit}>
      <div className="architect-form-head">
        <div>
          <span className="architect-form-eyebrow">
            第 {clarification.round}/{clarification.maxRounds} 轮
          </span>
          <h3>补齐实施所需信息</h3>
        </div>
        <span className="architect-form-confidence">
          当前置信度 {clampPercent(clarification.confidenceScore)}%
        </span>
      </div>

      {questions.map((question) => {
        const value = values[question.questionId]
        const label = question.prompt
        if (question.type === 'single_choice') {
          return (
            <fieldset key={question.questionId} className="architect-question">
              <legend>{label}</legend>
              <div className="choice-pills">
                {(question.options ?? []).map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    className={value === option.value ? 'is-selected' : ''}
                    aria-pressed={value === option.value}
                    onClick={() => setValue(question.questionId, option.value)}
                  >
                    {option.label}
                  </button>
                ))}
              </div>
            </fieldset>
          )
        }
        if (question.type === 'multi_choice') {
          const selected = Array.isArray(value) ? value : []
          return (
            <fieldset key={question.questionId} className="architect-question">
              <legend>{label}</legend>
              <div className="choice-pills">
                {(question.options ?? []).map((option) => {
                  const checked = selected.includes(option.value)
                  return (
                    <button
                      key={option.value}
                      type="button"
                      className={checked ? 'is-selected' : ''}
                      aria-pressed={checked}
                      onClick={() =>
                        setValue(
                          question.questionId,
                          checked
                            ? selected.filter((item) => item !== option.value)
                            : [...selected, option.value],
                        )
                      }
                    >
                      {checked ? '✓ ' : ''}
                      {option.label}
                    </button>
                  )
                })}
              </div>
            </fieldset>
          )
        }
        return (
          <label key={question.questionId} className="architect-question">
            <span>{label}</span>
            <textarea
              rows={3}
              value={typeof value === 'string' ? value : ''}
              placeholder={
                question.type === 'entity_fields' || question.type === 'schema_fields'
                  ? '实体名 + 字段 + 关系，例如：Order(id, userId, total, status)'
                  : '请填写具体、可执行的信息'
              }
              onChange={(event) =>
                setValue(question.questionId, event.target.value)
              }
            />
          </label>
        )
      })}

      <label className="architect-question architect-comment">
        <span>补充说明（可选）</span>
        <textarea
          rows={2}
          value={comment}
          placeholder="还有什么上下文需要架构师知道？"
          onChange={(event) => setComment(event.target.value)}
        />
      </label>

      <div className="architect-form-actions">
        <button
          className="ghost-button"
          type="button"
          disabled={submitting}
          onClick={onCancel}
        >
          取消澄清
        </button>
        <button
          className="ghost-button"
          type="button"
          disabled={submitting}
          onClick={adoptSuggestions}
        >
          一键采用建议并跳过剩余提问
        </button>
        <button
          className="architect-primary"
          type="submit"
          disabled={!readyToSubmit || submitting}
        >
          {submitting ? (
            <>
              <span className="mini-spinner" aria-label="提交中" />
              提交中…
            </>
          ) : (
            '提交回答并继续'
          )}
        </button>
      </div>
    </form>
  )
}

const FULLSTACK_STEP_TEXT = {
  PENDING: '等待执行',
  RUNNING: '执行中…',
  SUCCEEDED: '已完成',
  FAILED: '失败',
}

/**
 * 契约确认后的全栈生成卡片。
 *
 * 只在 full_stack_system 契约上展示可点击按钮：其它形态没有后端与 DDL，
 * 服务端会直接 422，前端提前禁用避免无意义请求。
 */
function FullstackGeneration({
  fullstack,
  contract,
  onGenerate,
  onExport,
  onReclarify,
  a2a = [],
  exporting = false,
  projectMode = 'NEW_PROJECT',
}) {
  const state = fullstack ?? {}
  const existingSource = projectMode === 'EXISTING_SOURCE'
  const status = state.status ?? 'IDLE'
  const running = Boolean(state.starting) || status === 'RUNNING'
  // 契约矛盾与代码缺陷必须分开处理：矛盾时重试生成只会重复失败，
  // 弹窗直接引导用户回到 Architect 修改契约。
  const needsClarification =
    status === 'FAILED' &&
    Boolean(state.needsClarification) &&
    (!state.contractId || state.contractId === contract?.contractId)
  const clarification = state.clarification || {}
  const clarificationProblems = Array.isArray(clarification.problems)
    ? clarification.problems
    : []
  const clarificationPrompt =
    clarification.prompt || state.error || '请修改契约后重新澄清'
  // A2A：合并 Architect 流（契约回执）与 Fullstack 流（诊断/修复请求）。
  const a2aFeedback = (() => {
    const seen = new Set()
    const merged = []
    ;[...(Array.isArray(a2a) ? a2a : []), ...(Array.isArray(state.a2a) ? state.a2a : [])]
      .forEach((item) => {
        if (!item || typeof item !== 'object') return
        const key = item.feedbackId || `${item.fromAgent}:${item.kind}:${item.summary}`
        if (seen.has(key)) return
        seen.add(key)
        merged.push(item)
      })
    return merged.slice(-6)
  })()
  const revisionRequest = [...a2aFeedback]
    .reverse()
    .find(
      (item) =>
        item?.toAgent === 'Architect' &&
        ['REVISE_CONTRACT', 'REVIEW_CONTRACT'].includes(item.requestedAction),
    )
  const revisionPrompt =
    revisionRequest?.payload?.prompt ||
    revisionRequest?.payload?.audit?.prompt ||
    [revisionRequest?.summary, ...(revisionRequest?.evidence || [])]
      .filter(Boolean)
      .join('\n')
  const collaboration = Array.isArray(state.collaboration)
    ? state.collaboration
    : []
  const [serverCollaboration, setServerCollaboration] = useState([])
  const [collaborationLoad, setCollaborationLoad] = useState({
    loading: false,
    error: '',
  })
  const visibleCollaboration = mergeCollaboration(
    collaboration,
    serverCollaboration,
  ).slice(-8)
  const [collaborationFilter, setCollaborationFilter] = useState('all')
  const [multiService, setMultiService] = useState({
    loading: false,
    error: '',
    result: null,
  })
  const runMultiService = async () => {
    if (multiService.loading) return
    setMultiService({ loading: true, error: '', result: null })
    try {
      const result = await generateMultiService({ name: 'ticket-copilot' })
      setMultiService({ loading: false, error: '', result })
    } catch (error) {
      setMultiService({
        loading: false,
        error: error?.message || '多服务生成失败',
        result: null,
      })
    }
  }
  const loadedRunRef = useRef('')
  const filteredCollaboration = visibleCollaboration.filter(
    (item) => collaborationFilter === 'all' || item.kind === collaborationFilter,
  )

  const loadCollaboration = async () => {
    if (!state.runId || collaborationLoad.loading) return
    setCollaborationLoad({ loading: true, error: '' })
    try {
      const record = await fetchCollaborationRecord(state.runId)
      setServerCollaboration(collaborationFromSnapshot(record))
      setCollaborationLoad({ loading: false, error: '' })
    } catch (error) {
      setCollaborationLoad({
        loading: false,
        error: error?.message || '协作记录加载失败',
      })
    }
  }

  // 刷新 / 切换 Run 后自动拉取一次服务端记录，后续仍可手动刷新。
  useEffect(() => {
    if (!state.runId || loadedRunRef.current === state.runId) return
    loadedRunRef.current = state.runId
    loadCollaboration()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.runId])

  const [clarifyOpen, setClarifyOpen] = useState(false)

  useEffect(() => {
    // 每个失败 Run 自动弹一次；用户关闭后仍可从错误卡片重新打开。
    setClarifyOpen(needsClarification)
  }, [needsClarification, state.runId])
  const steps = Array.isArray(state.steps) ? state.steps : []
  const outputs = Array.isArray(state.outputs) ? state.outputs : []
  const result = state.result
  const qualityTier = state.qualityTier ?? contract?.qualityTier ?? null
  const isPrototype = qualityTier === 'L1_PROTOTYPE'
  // 自愈闭环里每一次重检都会覆盖 consistency 快照；终态仍保留最后一次。
  // 没有报告（旧会话 / 极简契约）时按通过处理，导出入口不应被冻住。
  // L1 已由用户显式接受“跳过严格校验”，一致性问题只做告警，不阻断导出。
  const consistencyOk = isPrototype || state.consistency?.ok !== false
  const exportReady = status === 'SUCCEEDED' && consistencyOk
  const qualityBadge =
    state.qualityBadge ??
    (isPrototype
      ? {
          tone: 'warning',
          text: '⚠️ 当前为 L1 敏捷模式，代码未经过严格一致性校验，不建议直接用于生产环境。',
        }
      : qualityTier === 'L3_PRODUCTION' && status === 'SUCCEEDED'
        ? { tone: 'success', text: '✅ L3 生产级校验通过，具备部署条件。' }
        : null)
  const warnings = Array.isArray(state.warnings)
    ? state.warnings.filter((item) => item && item !== qualityBadge?.text)
    : []
  const exportSandboxId = state.sandboxId || result?.sandboxId || null
  const isFullstack = contract?.appShape === 'full_stack_system'
  const apiRouteCount = Array.isArray(contract?.apiRoutes)
    ? contract.apiRoutes.length
    : 0
  const hasDdl =
    typeof contract?.databaseDdl === 'string' &&
    contract.databaseDdl.trim().length > 0
  const contractConfirmed = contract?.status === 'CONFIRMED'
  const contractReady =
    isFullstack && apiRouteCount > 0 && (hasDdl || existingSource)

  // 禁用原因必须是“可操作的中文提示”：用原生 title 在 disabled 按钮上
  // 多数浏览器不会触发，因此同时渲染可见文案，方便用户与 E2E 断言。
  const disabledReason = !isFullstack
    ? `当前契约形态为「${
        SHAPE_LABELS[contract?.appShape] ?? contract?.appShape ?? '未知'
      }」，无需生成后端代码。若判断有误，请重新发起澄清并说明需要“全栈系统”。`
    : !contractConfirmed
      ? '契约尚未确认（contract.status 不是 CONFIRMED），暂时无法生成。'
      : apiRouteCount === 0
        ? '契约缺少 REST 接口（apiRoutes 为空），已阻止生成；请修订契约补充接口后重新确认。'
        : !hasDdl && !existingSource
          ? '契约缺少数据库 DDL（databaseDdl 为空），已阻止生成；请修订契约补充数据实体后重新确认。'
          : needsClarification
            ? '契约存在结构性矛盾，重试生成不会成功；请先回到契约修改并重新确认。'
            : running
              ? '全栈代码正在生成，按钮暂时锁定；完成后可重新生成。'
              : ''
  const tooltip = contractReady
    ? running
      ? '生成中，请稍候'
      : existingSource
        ? '依据已确认契约生成后端与数据库增量'
        : '依据已确认契约生成全栈代码'
    : disabledReason || '契约未就绪，无法生成'

  // 门控诊断：用户反馈“按钮点不动”时，控制台能直接看到每一项判定。
  useEffect(() => {
    console.info('[fullstack] gate state', {
      status,
      running,
      contractStatus: contract?.status ?? null,
      appShape: contract?.appShape ?? null,
      apiRouteCount,
      hasDdl,
      contractReady,
      disabledReason: disabledReason || null,
      starting: Boolean(state.starting),
      contractId: contract?.contractId ?? null,
      failureKind: state.failureKind ?? null,
      needsClarification,
    })
  }, [
    status,
    state.starting,
    state.failureKind,
    running,
    needsClarification,
    contract?.contractId,
    contract?.status,
    contract?.appShape,
    apiRouteCount,
    hasDdl,
    contractReady,
    disabledReason,
  ])

  // 日志容器自身可滚动；保留最近 24 个文件足以覆盖一次完整生成，
  // 既避免超长 DOM，也不会把早期生成的 schema.sql 挤出可视列表。
  const visibleOutputs = outputs.slice(-24)

  return (
    <section
      className="architect-fullstack"
      data-fullstack-status={status}
      data-fullstack-ready={contractReady ? 'true' : 'false'}
      data-project-mode={projectMode}
      aria-label={existingSource ? '后端增量生成' : '全栈代码生成'}
    >
      <div className="architect-fullstack-head">
        <div>
          <span className="architect-form-eyebrow">
            {existingSource ? 'CONTRACT → BACKEND' : 'CONTRACT → SANDBOX'}
          </span>
          <h3>
            {existingSource ? '生成后端与数据库增量' : '生成前后端与数据库代码'}
          </h3>
          <p>
            {existingSource
              ? '只运行 DBACoder 与 BackendCoder，为已有前端补充接口和数据表；不运行 FrontendCoder，不重写现有 React 源码。'
              : 'DBACoder 生成 Sequelize ORM，BackendCoder 提供 Node API，FrontendCoder 接入真实请求，InfraCoder 输出 Dockerfile / render.yaml / .env.example，最后重启沙盒完成联调。'}
          </p>
        </div>
        <div className="architect-fullstack-actions">
          <span
            className="architect-fullstack-trigger-wrap"
            data-tooltip={tooltip}
            title={tooltip}
          >
            <button
              type="button"
              className="architect-primary architect-fullstack-trigger"
              data-fullstack-generate
              disabled={!contractReady || running || needsClarification}
              aria-describedby={
                disabledReason ? 'fullstack-disabled-reason' : undefined
              }
              onClick={() => onGenerate?.()}
            >
              {running ? (
                <>
                  <span className="mini-spinner" aria-label="生成中" />
                  正在生成…
                </>
              ) : status === 'SUCCEEDED' ? (
                existingSource ? '↻ 重新生成后端增量' : '↻ 重新生成全栈代码'
              ) : status === 'FAILED' ? (
                needsClarification
                  ? '请先修改契约'
                  : existingSource
                    ? '↻ 重试后端增量'
                    : '↻ 重试生成全栈代码'
              ) : existingSource ? (
                '🚀 生成后端增量'
              ) : (
                '🚀 开始生成全栈代码'
              )}
            </button>
          </span>
          {disabledReason && (
            <p
              id="fullstack-disabled-reason"
              className="architect-fullstack-gate-hint"
              data-fullstack-disabled-reason={disabledReason}
              role="status"
            >
              {disabledReason}
            </p>
          )}
        </div>
      </div>

      {state.error && (
        <div
          className="architect-fullstack-error"
          role="alert"
          data-fullstack-error
          data-failure-kind={state.failureKind ?? 'CODE'}
        >
          <strong>
            {needsClarification
              ? '契约存在结构性矛盾'
              : state.errorCode === 'RUN_EXPIRED'
                ? '上一次生成记录已过期'
                : '全栈生成失败'}
          </strong>
          <span>{state.error}</span>
          {state.errorCode === 'RUN_EXPIRED' && (
            <span className="architect-fullstack-error-hint">
              服务重启或记录超出保留期会让旧 Run 无法续传；点击下方按钮会以新的
              Run 重新开始，不影响已确认的契约与源码。
            </span>
          )}
          {state.failureKind === 'CONTRACT' && (
            <span className="architect-fullstack-error-hint">
              这不是代码生成缺陷：重试不会修复契约矛盾，请修改契约后重新确认。
            </span>
          )}
          {state.errorCode && <code>{state.errorCode}</code>}
          {needsClarification && (
            <button
              type="button"
              className="architect-ghost"
              data-contract-reclarify-open
              onClick={() => setClarifyOpen(true)}
            >
              查看需要修改的契约内容
            </button>
          )}
        </div>
      )}

      {a2aFeedback.length > 0 && (
        <div className="architect-a2a" data-a2a-feedback>
          <span className="architect-a2a-title">A2A 交互</span>
          {a2aFeedback.map((item, index) => (
            <span
              className="architect-a2a-item"
              key={item.feedbackId || `${item.fromAgent}-${index}`}
            >
              <strong>
                {item.fromAgent} → {item.toAgent}
              </strong>
              {' · '}
              {item.kind}
              {item.summary ? ` · ${item.summary}` : ''}
            </span>
          ))}
          {revisionRequest && (
            <button
              type="button"
              className="architect-ghost"
              data-a2a-revise-contract
              onClick={() => onReclarify?.(revisionPrompt)}
            >
              让架构工程师修订契约
            </button>
          )}
        </div>
      )}

      {(visibleCollaboration.length > 0 || state.runId) && (
        <div className="architect-collab" data-agent-collaboration>
          <span className="architect-collab-title">协作时间线</span>
          <div className="architect-collab-filters">
            {[
              ['all', '全部'],
              ['report', '汇报'],
              ['action', '操作'],
              ['fastpath', '快速通道'],
            ].map(([value, label]) => (
              <button
                type="button"
                key={value}
                className={
                  collaborationFilter === value
                    ? 'architect-collab-filter is-active'
                    : 'architect-collab-filter'
                }
                data-collab-filter={value}
                onClick={() => setCollaborationFilter(value)}
              >
                {label}
              </button>
            ))}
          </div>
          {filteredCollaboration.map((item) => (
            <span
              className={`architect-collab-item is-${item.kind}`}
              key={item.id}
            >
              {item.kind === 'report' && (
                <>
                  <strong>{item.agent}</strong>
                  {' · '}
                  {item.deliveryMode === 'fast_path' ? '直连' : '经架构师'}
                  {item.summary ? ` · ${item.summary}` : ''}
                </>
              )}
              {item.kind === 'action' && (
                <>
                  <strong>{item.actor}</strong>
                  {' · '}
                  {item.tool}
                  {item.target ? ` · ${item.target}` : ''}
                </>
              )}
              {item.kind === 'fastpath' && (
                <>
                  <strong>快速通道</strong>
                  {' · '}
                  {item.action === 'granted'
                    ? '已授权'
                    : `已收回：${item.reason || '出现异常'}`}
                </>
              )}
            </span>
          ))}
          {filteredCollaboration.length === 0 && (
            <span className="architect-collab-empty">当前筛选没有记录</span>
          )}
          {state.runId && (
            <button
              type="button"
              className="architect-ghost"
              data-load-collaboration
              onClick={loadCollaboration}
              disabled={collaborationLoad.loading}
            >
              {collaborationLoad.loading
                ? '正在加载协作记录…'
                : '从服务端加载完整记录'}
            </button>
          )}
          {collaborationLoad.error && (
            <span className="architect-collab-error" role="alert">
              {collaborationLoad.error}
            </span>
          )}
        </div>
      )}

      <div className="architect-multiservice" data-multi-service>
        <span className="architect-multiservice-title">多服务生成（原型）</span>
        <span className="architect-multiservice-hint">
          固定工单蓝图：Kafka + 消费者 + LangGraph Agent + OTel
        </span>
        <button
          type="button"
          className="architect-ghost"
          data-generate-multi-service
          disabled={multiService.loading}
          onClick={runMultiService}
        >
          {multiService.loading ? '生成中…' : '生成多服务应用'}
        </button>
        {multiService.error && (
          <span className="architect-collab-error" role="alert">
            {multiService.error}
          </span>
        )}
        {multiService.result && (
          <span className="architect-multiservice-result">
            {multiService.result.status}
            {' · '}产物 {multiService.result.artifacts}
            {' · '}幂等{' '}
            {String(
              multiService.result.verification?.idempotency?.secondDuplicate,
            )}
            {multiService.result.delivery?.manifest?.imageDigest
              ? ` · ${multiService.result.delivery.manifest.imageDigest.slice(0, 19)}…`
              : ''}
          </span>
        )}
      </div>

      {needsClarification && clarifyOpen && (
        <div
          className="dialog-backdrop"
          role="presentation"
          onClick={() => setClarifyOpen(false)}
        >
          <div
            className="account-dialog architect-clarify-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="fullstack-clarify-title"
            data-contract-clarification
            onClick={(event) => event.stopPropagation()}
          >
            <div className="dialog-head">
              <h3 id="fullstack-clarify-title">契约需要重新澄清</h3>
              <button
                type="button"
                className="dialog-close"
                aria-label="关闭"
                onClick={() => setClarifyOpen(false)}
              >
                ×
              </button>
            </div>
            <div className="architect-clarify-body">
              <p>
                平台检测到契约本身存在矛盾，而不是生成代码违反了契约。
                继续点击“重试生成”只会重复同一处错误，不会产生可用系统。
              </p>
              {clarificationProblems.length > 0 ? (
                <ul>
                  {clarificationProblems.map((problem, index) => (
                    <li key={`${problem.code || 'problem'}-${index}`}>
                      <strong>{problem.message}</strong>
                      {problem.hint && <span>{problem.hint}</span>}
                    </li>
                  ))}
                </ul>
              ) : (
                <pre>{state.error}</pre>
              )}
              <p className="architect-clarify-note">
                点击下方按钮会把这份修正要求发给 Architect；请根据提示调整
                需求、数据表或接口，再重新确认契约。
              </p>
            </div>
            <div className="dialog-actions">
              <button
                type="button"
                className="architect-ghost"
                onClick={() => setClarifyOpen(false)}
              >
                稍后处理
              </button>
              <button
                type="button"
                className="architect-primary"
                data-contract-reclarify
                onClick={() => {
                  setClarifyOpen(false)
                  onReclarify?.(clarificationPrompt)
                }}
              >
                回到契约并重新澄清
              </button>
            </div>
          </div>
        </div>
      )}

      {steps.length > 0 && (
        <ol className="fullstack-steps" data-fullstack-progress>
          {steps.map((step) => (
            <li
              key={step.node}
              className={`fullstack-step is-${String(step.status).toLowerCase()}`}
              data-fullstack-step={step.node}
              data-status={step.status}
            >
              <span className="fullstack-step-mark" aria-hidden="true">
                {step.status === 'SUCCEEDED'
                  ? '✓'
                  : step.status === 'FAILED'
                    ? '!'
                    : step.status === 'RUNNING'
                      ? '◌'
                      : '·'}
              </span>
              <div className="fullstack-step-body">
                <div className="fullstack-step-title">
                  <strong>{step.label || step.node}</strong>
                  <span>
                    {step.summary || FULLSTACK_STEP_TEXT[step.status] || step.status}
                  </span>
                </div>
                {Array.isArray(step.files) && step.files.length > 0 && (
                  <code className="fullstack-step-files">
                    {step.files.slice(0, 4).join(' · ')}
                    {step.files.length > 4 ? ` +${step.files.length - 4}` : ''}
                  </code>
                )}
              </div>
            </li>
          ))}
        </ol>
      )}

      {result?.collectionPath && (
        <p className="architect-decision-hint" data-fullstack-result>
          数据表 <code>{result.table}</code> · 接口前缀{' '}
          <code>{result.collectionPath}</code>
        </p>
      )}

      {/* L3 真实 MySQL 迁移验收结果：只有 L3 会产出该快照，
          L1/L2 不执行真实迁移，保持 null。 */}
      {status === 'SUCCEEDED' && state.mysqlCheck?.verifiedBy && (
        <p
          className="architect-decision-hint"
          data-quality-mysql={
            state.mysqlCheck.skipped
              ? 'skipped'
              : state.mysqlCheck.ok
                ? 'passed'
                : 'failed'
          }
        >
          {state.mysqlCheck.skipped
            ? '真实 MySQL 迁移验收已跳过（环境开关关闭）。'
            : state.mysqlCheck.ok
              ? `真实 MySQL 迁移验收通过：${state.mysqlCheck.table}（${
                  state.mysqlCheck.actualColumns?.length ?? 0
                } 个字段）。`
              : `真实 MySQL 迁移验收失败：${
                  state.mysqlCheck.error || '字段不一致'
                }`}
        </p>
      )}

      {/* 风险自适应门禁：L1 黄色告警 / L3 绿色通过徽标。 */}
      {status === 'SUCCEEDED' && qualityBadge && (
        <div
          className={`architect-quality-badge is-${qualityBadge.tone}`}
          data-quality-badge={qualityBadge.tone}
          role="status"
        >
          {qualityBadge.text}
        </div>
      )}

      {status === 'SUCCEEDED' && warnings.length > 0 && (
        <details className="fullstack-quality-warnings" data-quality-warnings>
          <summary>质量门禁告警（{warnings.length}）</summary>
          <ul>
            {warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </details>
      )}

      {/* 生成成功 + 一致性通过后才出现：用户不必回到顶部工具栏，
          在成功卡片里即可直接下载完整源码包。 */}
      {exportReady && (
        <div className="architect-export-card" data-architect-export>
          <div className="architect-export-copy">
            <span className="architect-export-mark" aria-hidden="true">
              ✓
            </span>
            <div>
              <strong>
                {isPrototype
                  ? '全栈代码已生成（L1 敏捷模式）'
                  : '全栈代码已生成，接口一致性检查通过'}
              </strong>
              <p>
                {isPrototype
                  ? '已跳过严格一致性校验，导出产物仅建议用于 Demo / 内部验证，上线前请切换到 L2 或 L3 重新生成。'
                  : '导出内容包含前后端源码、数据库模型、Dockerfile 与 .env.example，可直接解压到本地继续开发。'}
              </p>
            </div>
          </div>
          <button
            type="button"
            className="architect-primary architect-export-button"
            data-architect-export-button
            disabled={exporting || !exportSandboxId}
            title={
              exportSandboxId
                ? '下载当前沙盒的完整源码 tar.gz'
                : '当前会话缺少沙盒 ID，请重新生成后再导出'
            }
            onClick={() =>
              onExport?.({
                sandboxId: exportSandboxId,
                runId: state.runId || null,
              })
            }
          >
            {exporting ? (
              <>
                <span className="mini-spinner" aria-label="打包中" />
                正在打包…
              </>
            ) : (
              '📥 导出完整代码'
            )}
          </button>
        </div>
      )}

      {visibleOutputs.length > 0 && (
        <details
          className="fullstack-log"
          data-fullstack-log
          open={status === 'RUNNING'}
        >
          <summary>生成日志（{outputs.length}）</summary>
          <div className="fullstack-log-body">
            {visibleOutputs.map((output) => (
              <article key={output.id} data-fullstack-output={output.path}>
                <header>
                  <code>{output.path}</code>
                  <span>
                    {output.bytes ? `${output.bytes} B` : output.node}
                    {output.truncated ? ' · 已截断' : ''}
                  </span>
                </header>
                <pre>{output.content}</pre>
              </article>
            ))}
          </div>
        </details>
      )}
    </section>
  )
}

/**
 * 对话前置模式：契约确认后的前端骨架初始化状态卡。
 *
 * 不显示“代码生成完毕”这种终态文案：这里只负责把 FrontendCoder /
 * BuildVerifier / SandboxRestart 三节点进度可视化，成功后画布自动切换。
 */
function FrontendBootstrap({ fullstack, contract, onRetry, onFullstackGenerate }) {
  const state = fullstack ?? {}
  const status = state.status ?? 'IDLE'
  const mode = state.mode
  const running = Boolean(state.starting) || status === 'RUNNING'
  const steps = Array.isArray(state.steps) ? state.steps : []
  const succeeded = status === 'SUCCEEDED'
  const failed = status === 'FAILED'
  const isFullstack = contract?.appShape === 'full_stack_system'

  return (
    <section
      className="architect-fullstack architect-frontend-bootstrap"
      data-frontend-bootstrap-status={status}
      aria-label="前端骨架初始化"
    >
      <div className="architect-fullstack-head">
        <div>
          <span className="architect-form-eyebrow">CONTRACT → FRONTEND</span>
          <h3>根据契约初始化前端页面</h3>
          <p>
            FrontendCoder 会读取契约中的页面路由、布局与组件列表，生成真实
            的 React 骨架；构建校验通过后自动重启沙盒，画布随即切换为实时页面。
          </p>
        </div>
      </div>

      {(!mode || running) && (
        <div className="architect-frontend-status is-running" role="status">
          <span className="mini-spinner" aria-hidden="true" />
          <div>
            <strong>正在生成初始前端页面…</strong>
            <p>这一步不需要再次点击，契约确认后会自动执行。</p>
          </div>
        </div>
      )}

      {failed && (
        <div className="architect-fullstack-error" role="alert">
          <strong>前端初始化失败</strong>
          <span>{state.error || '未知错误，请重试。'}</span>
          {state.errorCode && <code>{state.errorCode}</code>}
          <button
            type="button"
            className="architect-secondary"
            onClick={() => onRetry?.()}
          >
            重试初始化
          </button>
        </div>
      )}

      {succeeded && (
        <div className="architect-outcome is-success" role="status">
          <span aria-hidden="true">✓</span>
          <div>
            <strong>初始前端页面已生成</strong>
            <p>
              画布已切换为沙盒实时页面，可以直接圈选区域进行微调。
              {isFullstack
                ? ' 如需接入真实后端与数据库，可继续生成全栈代码。'
                : ''}
            </p>
            {isFullstack && (
              <button
                type="button"
                className="architect-secondary"
                onClick={() => onFullstackGenerate?.()}
              >
                继续生成后端与数据库
              </button>
            )}
          </div>
        </div>
      )}

      {steps.length > 0 && (
        <ol className="fullstack-steps" data-fullstack-progress>
          {steps.map((step) => (
            <li
              key={step.node}
              className={`fullstack-step is-${String(step.status).toLowerCase()}`}
              data-fullstack-step={step.node}
              data-status={step.status}
            >
              <span className="fullstack-step-mark" aria-hidden="true">
                {step.status === 'SUCCEEDED'
                  ? '✓'
                  : step.status === 'FAILED'
                    ? '!'
                    : step.status === 'RUNNING'
                      ? '◌'
                      : '·'}
              </span>
              <div className="fullstack-step-body">
                <div className="fullstack-step-title">
                  <strong>{step.label || step.node}</strong>
                  <span>
                    {step.summary || FULLSTACK_STEP_TEXT[step.status] || step.status}
                  </span>
                </div>
                {Array.isArray(step.files) && step.files.length > 0 && (
                  <code className="fullstack-step-files">
                    {step.files.slice(0, 4).join(' · ')}
                  </code>
                )}
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

/**
 * 冻结 VisualDocument → 确定性 CodeGen 状态卡。
 *
 * 生成入口复用 workspace 的 onFullstackGenerate；真正的事实源是浏览器
 * localStorage 中的冻结树，面板只展示进度，不参与源码拼装。
 */
function VisualCodegen({ fullstack, onGenerate }) {
  const state = fullstack ?? {}
  const status = state.status ?? 'IDLE'
  const running = Boolean(state.starting) || status === 'RUNNING'
  const succeeded = status === 'SUCCEEDED'
  const failed = status === 'FAILED'
  const steps = Array.isArray(state.steps) ? state.steps : []
  const result = state.result ?? {}
  const visualNodeCount = result.visualDocumentId
    ? result.visualNodeCount ?? 0
    : 0

  return (
    <section
      className="architect-fullstack architect-visual-codegen"
      data-visual-codegen-status={status}
      aria-label="虚拟界面代码生成"
    >
      <div className="architect-fullstack-head">
        <div>
          <span className="architect-form-eyebrow">VISUAL → REACT</span>
          <h3>生成应用代码</h3>
          <p>
            确定性 Compiler 会把已冻结的虚拟界面编译为 React + CSS，
            每个节点注入 data-node-id，再通过 Babel AST、构建与沙盒冒烟门禁。
            不猜布局、不做整页 LLM 生成。
          </p>
        </div>
      </div>

      {running && (
        <div className="architect-frontend-status is-running" role="status">
          <span className="mini-spinner" aria-hidden="true" />
          <div>
            <strong>正在编译虚拟界面…</strong>
            <p>冻结树 → JSX → 构建门禁 → 沙盒预览。</p>
          </div>
        </div>
      )}

      {failed && (
        <div className="architect-fullstack-error" role="alert">
          <strong>代码生成失败</strong>
          <span>{state.error || '未知错误，请重试。'}</span>
          {state.errorCode && <code>{state.errorCode}</code>}
        </div>
      )}

      {succeeded && result.visualDocumentId && (
        <div className="architect-outcome is-success" role="status">
          <span aria-hidden="true">✓</span>
          <div>
            <strong>虚拟界面已编译为真实源码</strong>
            <p>
              {visualNodeCount > 0 ? `${visualNodeCount} 个节点` : '冻结树'}已写入
              src/App.jsx；画布已切换为沙盒实时页面，可继续圈选微调。
            </p>
          </div>
        </div>
      )}

      {steps.length > 0 && (
        <ol className="fullstack-steps" data-visual-codegen-progress>
          {steps.map((step) => (
            <li
              key={step.node}
              className={`fullstack-step is-${String(step.status).toLowerCase()}`}
              data-fullstack-step={step.node}
              data-status={step.status}
            >
              <span className="fullstack-step-mark" aria-hidden="true">
                {step.status === 'SUCCEEDED'
                  ? '✓'
                  : step.status === 'FAILED'
                    ? '!'
                    : step.status === 'RUNNING'
                      ? '◌'
                      : '·'}
              </span>
              <div className="fullstack-step-body">
                <div className="fullstack-step-title">
                  <strong>{step.label || step.node}</strong>
                  <span>
                    {step.summary ||
                      FULLSTACK_STEP_TEXT[step.status] ||
                      step.status}
                  </span>
                </div>
              </div>
            </li>
          ))}
        </ol>
      )}

      <div className="architect-fullstack-actions">
        <button
          type="button"
          className="architect-primary architect-fullstack-trigger"
          data-visual-codegen-generate
          disabled={running}
          onClick={() => onGenerate?.()}
        >
          {running
            ? '正在编译…'
            : succeeded
              ? '↻ 重新编译冻结界面'
              : failed
                ? '↻ 重试编译'
                : '🚀 生成应用（CodeGen）'}
        </button>
        <p className="architect-fullstack-gate-hint">
          未冻结会拒绝生成；冻结后虚拟树只读。
        </p>
      </div>
    </section>
  )
}

/**
 * 契约确认后的持续对话入口。
 *
 * 确认不是终点：架构师面板保持在线，用户可随时把新需求追加到当前
 * Context。提交后由后端直接进入 revise_contract（不会重新澄清、不会
 * 丢掉已经确认的技术栈与业务槽位），新契约回到待确认状态。
 */
function ContractAppend({
  contract,
  submitting,
  onDecide,
  projectId,
  provider = 'auto',
  onOpenDocumentCenter,
  onDocumentsChanged,
}) {
  const { session } = useAuth()
  const [comment, setComment] = useState('')
  const [runDocs, setRunDocs] = useState([])
  const [docBusy, setDocBusy] = useState(false)
  const [docError, setDocError] = useState('')
  const [docNotice, setDocNotice] = useState('')
  const [pendingDoc, setPendingDoc] = useState(null)
  const ready = comment.trim().length > 0
  const centerCount = documentCount(projectId)

  const addRunDoc = (doc) => {
    setRunDocs((previous) => {
      const key = `${doc.name}\u0000${doc.content}`
      if (
        previous.some(
          (item) => `${item.name}\u0000${item.content}` === key,
        )
      ) {
        return previous
      }
      return [...previous, doc]
    })
  }

  const handleFiles = async (event) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file || docBusy) return
    setDocError('')
    setDocNotice('')
    setPendingDoc(null)
    if (!/\.(md|markdown|txt)$/i.test(file.name)) {
      setDocError('仅支持 .md / .markdown / .txt')
      return
    }
    if (file.size > 200 * 1024) {
      setDocError('单份文档不能超过 200KB')
      return
    }
    try {
      const content = await file.text()
      if (!content.trim()) {
        setDocError('文档内容为空')
        return
      }
      setDocBusy(true)
      const analysis = await classifyArchitectDocument({
        name: file.name,
        content,
        llmProvider: provider,
        token: session?.token,
      })
      const doc = { name: file.name, content, size: file.size, analysis }
      if (analysis.decision === 'STORE') {
        const result = addProjectDocument(projectId, { ...doc, ...analysis })
        if (!result.ok) setDocError(result.error)
        else {
          setDocNotice(`「${file.name}」已加入文档中心`)
          onDocumentsChanged?.()
        }
        addRunDoc(doc)
      } else if (analysis.decision === 'DISCARD') {
        addRunDoc(doc)
        setDocNotice(`「${file.name}」判定为一次性，本次使用、不保存`)
      } else {
        setPendingDoc(doc)
        setDocNotice(
          `未检测到明确的复用语义：${
            analysis.reason || '请选择是否加入文档中心'
          }`,
        )
      }
    } catch (cause) {
      setDocError(`读取文档失败：${cause?.message || cause}`)
    } finally {
      setDocBusy(false)
    }
  }

  const resolvePending = (addToCenter) => {
    if (!pendingDoc) return
    if (addToCenter) {
      const result = addProjectDocument(projectId, {
        ...pendingDoc,
        ...pendingDoc.analysis,
        decision: 'STORE',
      })
      if (!result.ok) {
        setDocError(result.error)
        return
      }
      onDocumentsChanged?.()
      setDocNotice(`「${pendingDoc.name}」已加入文档中心`)
    } else {
      setDocNotice(`「${pendingDoc.name}」仅本次使用，不保存`)
    }
    addRunDoc(pendingDoc)
    setPendingDoc(null)
  }

  const submit = () => {
    if (!ready || submitting || pendingDoc || docBusy) return
    const docs = mergeKnowledgeDocuments(
      runDocs,
      activeKnowledgeDocuments(projectId),
    )
    onDecide('REVISE', comment.trim(), undefined, docs)
    setComment('')
    setRunDocs([])
    setDocNotice('')
  }

  return (
    <section className="architect-contract-append" data-contract-append>
      <label className="architect-revision">
        <span>追加修改需求…</span>
        <textarea
          rows={3}
          value={comment}
          placeholder="例如：账单页再加一个导出按钮；接口拆成 /api/v1/bills 与 /api/v1/bills/:id"
          onChange={(event) => setComment(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
              event.preventDefault()
              submit()
            }
          }}
        />
      </label>
      <div className="architect-doc-upload">
        <div className="architect-doc-upload-head">
          <strong>追加文档（可选）</strong>
          <span>启用的文档会进入本次修订 RAG；一次性的只用于本次</span>
        </div>
        <div className="architect-doc-upload-row">
          <label className="architect-doc-picker">
            <input
              type="file"
              accept=".md,.markdown,.txt"
              onChange={handleFiles}
              disabled={docBusy}
            />
            {docBusy ? '正在判断文档用途…' : '选择文档'}
          </label>
          <button
            type="button"
            className="ghost-button"
            onClick={onOpenDocumentCenter}
          >
            文档中心 ({centerCount})
          </button>
        </div>
        {runDocs.length > 0 && (
          <div className="architect-doc-list">
            {runDocs.map((doc, index) => (
              <span
                key={`${doc.name}-${index}`}
                className="architect-doc-chip"
              >
                {doc.name}
                <button
                  type="button"
                  aria-label={`移除 ${doc.name}`}
                  onClick={() =>
                    setRunDocs((previous) =>
                      previous.filter((_, i) => i !== index),
                    )
                  }
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        )}
        {pendingDoc && (
          <div className="project-doc-center-pending">
            <div>
              <strong>{pendingDoc.name}</strong>
              <p>{pendingDoc.analysis?.reason || '未检测到明确语义'}</p>
            </div>
            <div className="project-doc-center-pending-actions">
              <button
                type="button"
                className="architect-primary"
                onClick={() => resolvePending(true)}
              >
                加入文档中心
              </button>
              <button
                type="button"
                className="ghost-button"
                onClick={() => resolvePending(false)}
              >
                仅本次使用
              </button>
            </div>
          </div>
        )}
        {docError && <p className="architect-doc-error">{docError}</p>}
        {docNotice && (
          <p className="project-doc-center-notice">{docNotice}</p>
        )}
      </div>
      <div className="architect-decision-row">
        <span className="architect-append-hint">
          会直接在当前契约上修订，无需重新澄清
        </span>
        <button
          type="button"
          className="architect-primary"
          disabled={!ready || submitting || Boolean(pendingDoc) || docBusy}
          title={ready ? '追加到当前 Context 并重新生成契约' : '请先写下新的需求'}
          onClick={submit}
        >
          {submitting ? (
            <>
              <span className="mini-spinner" aria-label="修订中" />
              修订中…
            </>
          ) : (
            '追加需求并修订'
          )}
        </button>
      </div>
      <p className="architect-decision-hint">
        契约 ID：<code>{contract.contractId}</code>
      </p>
    </section>
  )
}

// 风险自适应门禁：契约确认前的必选质量分级。与后端
// QUALITY_TIER_OPTIONS 保持一致，未选择时禁止确认。
const QUALITY_TIER_OPTIONS = [
  {
    value: 'L1_PROTOTYPE',
    label: 'A. L1 敏捷模式（极速生成，跳过严格校验，适合做 Demo）',
  },
  {
    value: 'L2_STANDARD',
    label: 'B. L2 标准模式（带 AST 代码校验与基础数据库，适合内部工具）',
  },
  {
    value: 'L3_PRODUCTION',
    label: 'C. L3 生产级（全量一致性校验与真实数据库迁移验证，适合上线交付）',
  },
]

function ContractActions({
  contract,
  submitting,
  onDecide,
  qualityTierRequired = true,
}) {
  const [comment, setComment] = useState('')
  const [cancelConfirm, setCancelConfirm] = useState(false)
  const [qualityTier, setQualityTier] = useState(
    qualityTierRequired ? '' : contract?.qualityTier || 'L2_STANDARD',
  )
  const policyEdges = Array.isArray(contract?.agentPolicy?.fastPathEdges)
    ? contract.agentPolicy.fastPathEdges
    : []
  const [selectedEdges, setSelectedEdges] = useState(() =>
    policyEdges.map((_, index) => index),
  )
  useEffect(() => {
    setSelectedEdges(
      (Array.isArray(contract?.agentPolicy?.fastPathEdges)
        ? contract.agentPolicy.fastPathEdges
        : []
      ).map((_, index) => index),
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contract?.contractId])
  const togglePolicyEdge = (index) => {
    setSelectedEdges((current) =>
      current.includes(index)
        ? current.filter((item) => item !== index)
        : [...current, index],
    )
  }
  const policyReady = policyEdges.length === 0 || selectedEdges.length > 0
  const revisionReady = comment.trim().length > 0
  const confirmReady =
    (qualityTierRequired
      ? Boolean(qualityTier)
      : Boolean(contract?.qualityTier || 'L2_STANDARD')) && policyReady
  const effectiveTier = qualityTier || contract?.qualityTier || 'L2_STANDARD'
  const effectiveAgentPolicy = contract?.agentPolicy
    ? {
        ...contract.agentPolicy,
        fastPathEdges: policyEdges.filter((_, index) =>
          selectedEdges.includes(index),
        ),
      }
    : undefined

  return (
    <div className="architect-contract-actions">
      {qualityTierRequired ? (
        <fieldset className="architect-quality-tier" data-quality-tier>
          <legend>请选择您对本次生成的质量要求：</legend>
          {QUALITY_TIER_OPTIONS.map((option) => (
            <label key={option.value} className="architect-quality-option">
              <input
                type="radio"
                name={`quality-tier-${contract.contractId}`}
                value={option.value}
                checked={qualityTier === option.value}
                disabled={submitting}
                onChange={() => setQualityTier(option.value)}
              />
              <span>{option.label}</span>
            </label>
          ))}
          {!confirmReady && (
            <p className="architect-quality-hint" data-quality-tier-required>
              该选择不可跳过：确认契约前必须指定质量分级。
            </p>
          )}
        </fieldset>
      ) : (
        <p className="architect-quality-hint" data-quality-tier-locked>
          质量分级已固定为 {contract?.qualityTier || 'L2_STANDARD'}，后续修订沿用该选择。
        </p>
      )}
      {policyEdges.length > 0 && (
        <fieldset className="architect-agent-policy" data-agent-policy-editor>
          <legend>快速通道白名单</legend>
          <p className="architect-quality-hint">
            勾选允许子 Agent 直连的标准链路；未勾选的边会回到 Architect 中转。
          </p>
          {policyEdges.map((edge, index) => (
            <label
              key={`${edge?.[0]}-${edge?.[1]}-${index}`}
              className="architect-policy-option"
            >
              <input
                type="checkbox"
                checked={selectedEdges.includes(index)}
                disabled={submitting}
                onChange={() => togglePolicyEdge(index)}
              />
              <span>
                <code>{edge?.[0]}</code>
                {' → '}
                <code>{edge?.[1]}</code>
                {' · '}
                {edge?.[2]}
              </span>
            </label>
          ))}
          {!policyReady && (
            <p className="architect-quality-hint" data-agent-policy-required>
              至少保留一条白名单，或取消确认后重新修订契约。
            </p>
          )}
        </fieldset>
      )}
      <label className="architect-revision">
        <span>修订意见</span>
        <textarea
          rows={2}
          value={comment}
          placeholder="如需调整接口、数据模型或技术栈，请写明修改点。"
          onChange={(event) => setComment(event.target.value)}
        />
      </label>
      <div className="architect-decision-row">
        <button
          type="button"
          className="ghost-button"
          disabled={submitting}
          onClick={() => {
            if (!cancelConfirm) {
              setCancelConfirm(true)
              return
            }
            onDecide('CANCEL', '用户取消实施契约')
          }}
        >
          {cancelConfirm ? '确认取消？' : '取消'}
        </button>
        <button
          type="button"
          className="architect-secondary"
          disabled={!revisionReady || submitting}
          title={revisionReady ? '带着修订意见重新生成契约' : '请先填写修订意见'}
          onClick={() =>
            onDecide(
              'REVISE',
              comment.trim(),
              qualityTierRequired ? qualityTier || undefined : effectiveTier,
            )
          }
        >
          要求修订
        </button>
        <button
          type="button"
          className="architect-primary"
          disabled={submitting || !confirmReady}
          title={
            confirmReady
              ? '确认契约并进入代码生成'
              : '请先选择本次生成的质量要求'
          }
          onClick={() =>
            onDecide(
              'CONFIRM',
              comment.trim(),
              effectiveTier,
              undefined,
              effectiveAgentPolicy,
            )
          }
        >
          {submitting ? (
            <>
              <span className="mini-spinner" aria-label="确认中" />
              确认中…
            </>
          ) : (
            '确认契约，进入生成'
          )}
        </button>
      </div>
      <p className="architect-decision-hint">
        契约 ID：<code>{contract.contractId}</code>
      </p>
    </div>
  )
}

const HELPER_SUGGESTIONS = [
  '为什么要选择当前技术栈？',
  '这个架构以后怎么扩展？',
  '数据库表和接口是怎么对应的？',
]

/**
 * 统一聊天流中的一条消息。
 *
 * 主状态机消息与影子助手问答共用同一条时间轴，但通过 source / 样式区分：
 *  - 架构师的正式提问（澄清 / 契约）高亮，并提示用户使用正文里的表单；
 *  - 影子助手消息保持只读的弱化样式，避免被误认为需要推进状态机。
 */
function FlowMessage({ item }) {
  const fromHelper = item.source === 'helper'
  const formal = !fromHelper && item.role === 'assistant'
  const roleLabel = fromHelper
    ? item.role === 'assistant'
      ? '影子助手 · 只读'
      : '你 → 影子助手'
    : item.role === 'assistant'
      ? '架构师'
      : item.role === 'user'
        ? '你 → 架构师'
        : '系统'
  const className = [
    'architect-message',
    `role-${item.role}`,
    item.kind ? `kind-${item.kind}` : '',
    fromHelper ? 'is-helper' : 'is-architect',
    formal ? 'is-formal' : '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <article
      className={className}
      data-flow-message={item.id}
      data-flow-source={fromHelper ? 'helper' : 'architect'}
      data-formal-question={formal ? 'true' : 'false'}
    >
      <span className="architect-message-role">
        {formal && (
          <span className="architect-formal-icon" aria-hidden="true">
            ◈
          </span>
        )}
        {roleLabel}
      </span>
      {formal && (
        <span className="architect-formal-tag">
          {item.kind === 'contract'
            ? '可在契约卡片直接确认，或提交修订意见'
            : '请在下方澄清表单中回答'}
        </span>
      )}
      <div className="architect-message-body">{item.content}</div>
      {Array.isArray(item.citations) && item.citations.length > 0 && (
        <ul className="architect-helper-citations">
          {item.citations.slice(0, 3).map((citation, index) => (
            <li
              key={`${citation.docId ?? citation.title}-${index}`}
              title={citation.section || citation.path || ''}
            >
              📚 {citation.title}
            </li>
          ))}
        </ul>
      )}
    </article>
  )
}

/**
 * 常驻影子助手输入区（只读）。
 *
 * 事件路由是硬边界：这里只调用 onAskHelper（只读 RAG），绝不推进
 * 澄清、契约或全栈状态机。需要推进流程时使用正文里的
 * “追加修改需求…”（契约确认后）或澄清 / 契约决策表单。
 *
 * 不渲染标题行：顶部已经有正式流程入口，重复标题只会增加噪音；
 * 保留快捷提问胶囊，降低首次使用成本。
 */
function ArchitectInputDock({
  helperMessages,
  helperPending,
  helperError,
  helperDisabled,
  onAskHelper,
}) {
  const [helperDraft, setHelperDraft] = useState('')
  const helperReady =
    helperDraft.trim().length > 0 && !helperPending && !helperDisabled

  const askHelper = (text) => {
    const value = String(text ?? helperDraft).trim()
    if (!value || helperPending || helperDisabled) return
    setHelperDraft('')
    onAskHelper?.(value)
  }

  return (
    <div className="architect-input-dock" data-architect-input-dock>
      <form
        className="architect-input-group is-helper"
        onSubmit={(event) => {
          event.preventDefault()
          askHelper()
        }}
      >
        {helperMessages.length === 0 && !helperPending && !helperError && (
          <div className="architect-helper-suggestions">
            {HELPER_SUGGESTIONS.map((suggestion) => (
              <button
                key={suggestion}
                type="button"
                onClick={() => askHelper(suggestion)}
              >
                {suggestion}
              </button>
            ))}
          </div>
        )}
        <div className="architect-input-row is-helper">
          <textarea
            rows={2}
            value={helperDraft}
            data-helper-input
            disabled={helperDisabled || helperPending}
            placeholder={
              helperDisabled
                ? '架构设计开始后，可在这里随时提问…'
                : '有什么不懂的随时问我...'
            }
            onChange={(event) => setHelperDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                event.preventDefault()
                askHelper()
              }
            }}
          />
          <button
            type="submit"
            className="architect-secondary"
            data-helper-send
            disabled={!helperReady}
          >
            {helperPending ? '思考中…' : '提问'}
          </button>
        </div>
        <p className="architect-input-hint">
          只读模式：不会修改契约、技术栈或任何架构状态。
        </p>
        {helperError && (
          <div className="architect-helper-error" role="alert">
            {helperError}
          </div>
        )}
      </form>
    </div>
  )
}

/**
 * 契约命名冲突决策弹窗。
 *
 * 服务端已自动修复过一次仍无法收敛，此时继续重试只会重复同一错误；
 * 必须让用户在“映射已有表 / 新建表 / 取消”之间显式选择。选项 ID 由
 * 服务端下发，前端只回传 optionId，不能自行改写契约。
 */
function ConflictDecisionDialog({ conflict, submitting, error, onDecide }) {
  const [comment, setComment] = useState('')
  if (!conflict) return null
  const options = Array.isArray(conflict.options) ? conflict.options : []
  return (
    <div className="dialog-backdrop" role="presentation">
      <div
        className="account-dialog architect-conflict-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="architect-conflict-title"
        data-contract-conflict
      >
        <div className="dialog-head">
          <h3 id="architect-conflict-title">⚠️ 架构师发现命名冲突</h3>
        </div>
        <div className="architect-conflict-body">
          <p>{conflict.message || 'API 集合与数据库表名不一致。'}</p>
          <p className="architect-conflict-meta">
            API 集合：<code>{conflict.apiCollection || '—'}</code>
            <span aria-hidden="true"> · </span>
            缺失表：<code>{conflict.missingTable || conflict.resource || '—'}</code>
          </p>
          {Array.isArray(conflict.tables) && conflict.tables.length > 0 && (
            <p className="architect-conflict-meta">
              现有表：<code>{conflict.tables.join('、')}</code>
            </p>
          )}
          <label className="architect-conflict-comment">
            <span>补充说明（可选）</span>
            <textarea
              value={comment}
              maxLength={2000}
              rows={2}
              placeholder="例如：认证接口要复用已有 users 表，不要新建冗余表。"
              onChange={(event) => setComment(event.target.value)}
            />
          </label>
          <div className="architect-conflict-options">
            {options.map((option) => (
              <button
                key={option.optionId}
                type="button"
                className={`architect-conflict-option ${
                  option.action === 'CANCEL' ? 'is-danger' : ''
                }`}
                data-conflict-option={option.optionId}
                disabled={submitting}
                onClick={() => onDecide?.(option.optionId, comment)}
              >
                <strong>{option.label}</strong>
                {option.description && <span>{option.description}</span>}
              </button>
            ))}
          </div>
          {submitting && (
            <p className="architect-conflict-pending" role="status">
              正在应用你的选择并重新校验契约…
            </p>
          )}
          {error && (
            <div className="architect-helper-error" role="alert">
              {error}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

/**
 * 澄清前的方案选择卡片。
 *
 * 数据全部来自 `/architect/solutions/discover`；这里只负责展示与回传
 * optionId。选择结果由父组件映射成 solutionChoice，随 start 一起提交。
 */
function SolutionDiscoveryPanel({
  discovery,
  selected,
  onSelect,
  onConfirm,
  onSkip,
  onRetry,
  onBack,
}) {
  const data = discovery?.data
  const options = data?.options ?? []
  const loading = Boolean(discovery?.loading)
  const error = discovery?.error
  return (
    <section
      className="architect-solution-discovery"
      data-architect-solution-discovery
    >
      <header className="architect-solution-header">
        <div>
          <h4>先确认方案，再进入架构澄清</h4>
          <p>
            平台先分析需求，检索可复用方案；你也可以跳过检索直接自研。
          </p>
        </div>
        <button
          type="button"
          className="architect-solution-back"
          onClick={onBack}
          disabled={loading}
        >
          返回修改需求
        </button>
      </header>

      {loading && (
        <div
          className="architect-solution-loading"
          data-architect-solution-loading
        >
          <span className="architect-solution-spinner" aria-hidden="true" />
          正在分析需求，并从 GitHub / 本地知识库检索可复用方案…
        </div>
      )}

      {!loading && error && (
        <div className="architect-solution-error" role="alert">
          <strong>方案侦察未完成</strong>
          <span>{error}</span>
        </div>
      )}

      {!loading && data && (
        <>
          <div className="architect-solution-summary">
            <p>{data.requirementSummary}</p>
            <div className="architect-solution-tags">
              {data.domain && <span>{data.domain}</span>}
              {data.provider && <span>检索：{data.provider}</span>}
              {data.degraded && (
                <span className="is-warn">已降级为本地/自研</span>
              )}
            </div>
            {(data.notes ?? []).length > 0 && (
              <ul className="architect-solution-notes">
                {data.notes.map((note) => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
            )}
          </div>

          <div className="architect-solution-list" data-architect-solutions>
            {options.map((option) => {
              const active = option.optionId === selected?.optionId
              const recommended =
                option.optionId === data.recommendedOptionId
              const cautions = [
                ...(option.cons ?? []),
                ...(option.risks ?? []),
              ]
              return (
                <label
                  key={option.optionId}
                  className={`architect-solution-card${
                    active ? ' is-selected' : ''
                  }`}
                  data-architect-solution-option={option.optionId}
                >
                  <input
                    type="radio"
                    name="architect-solution-choice"
                    checked={active}
                    onChange={() => onSelect?.(option.optionId)}
                  />
                  <div className="architect-solution-body">
                    <div className="architect-solution-head">
                      <strong>{option.title}</strong>
                      <span
                        className={`architect-solution-kind is-${String(
                          option.kind,
                        ).toLowerCase()}`}
                      >
                        {SOLUTION_KIND_LABELS[option.kind] ?? option.kind}
                      </span>
                      {recommended && (
                        <span className="architect-solution-recommended">
                          推荐
                        </span>
                      )}
                      {option.source && (
                        <span className="architect-solution-source">
                          {solutionSourceLabel(option)}
                        </span>
                      )}
                    </div>
                    {option.summary && <p>{option.summary}</p>}
                    {solutionMeta(option) && (
                      <div className="architect-solution-meta">
                        {solutionMeta(option)}
                      </div>
                    )}
                    {option.sourceUrl && (
                      <a
                        className="architect-solution-link"
                        href={option.sourceUrl}
                        target="_blank"
                        rel="noreferrer"
                        onClick={(event) => event.stopPropagation()}
                      >
                        查看仓库 ↗
                      </a>
                    )}
                    <div className="architect-solution-columns">
                      <div>
                        <b>优点</b>
                        <ul>
                          {(option.pros ?? []).map((item, index) => (
                            <li key={`pro-${index}`}>{item}</li>
                          ))}
                        </ul>
                      </div>
                      <div>
                        <b>注意</b>
                        <ul>
                          {cautions.map((item, index) => (
                            <li key={`con-${index}`}>{item}</li>
                          ))}
                        </ul>
                      </div>
                    </div>
                  </div>
                </label>
              )
            })}
          </div>

          <div className="architect-solution-actions">
            <button
              type="button"
              className="architect-primary"
              data-architect-solution-confirm
              disabled={!selected}
              onClick={onConfirm}
            >
              使用此方案并开始
            </button>
            <button
              type="button"
              data-architect-solution-skip
              onClick={onSkip}
            >
              跳过，直接自研
            </button>
            <button type="button" onClick={onRetry}>
              重新侦察
            </button>
          </div>
        </>
      )}

      {!loading && !data && error && (
        <div className="architect-solution-actions">
          <button
            type="button"
            className="architect-primary"
            data-architect-solution-skip
            onClick={onSkip}
          >
            仍要自研并开始
          </button>
          <button type="button" onClick={onRetry}>
            重试
          </button>
        </div>
      )}
    </section>
  )
}

export default function ArchitectPanel({
  projectName,
  projectId = '',
  state,
  onClose,
  onStart,
  onAnswer,
  onDecide,
  onDecideConflict,
  onAskHelper,
  onCancel,
  onRestart,
  onFullstackGenerate,
  onFrontendBootstrap,
  onExport,
  exporting = false,
  autoBootstrap = false,
  gate,
  onReleaseGate,
  projectMode = 'NEW_PROJECT',
  modelProvider = 'auto',
}) {
  const scrollRef = useRef(null)
  const contractRef = useRef(null)
  const { session } = useAuth()
  const [confirmCancel, setConfirmCancel] = useState(false)
  // 新建项目的第一条输入：纯自然语言目标，不再要求先做形态选择。
  const [goal, setGoal] = useState('')
  // 项目规模 / 质量分级：在用户输入需求后、多轮对话开始前必须选择。
  const [qualityTier, setQualityTier] = useState('')
  // 方案侦察：分析需求 -> 检索可复用方案 -> 用户选择 -> 再正式 start。
  const [solutionDiscovery, setSolutionDiscovery] = useState({
    open: false,
    loading: false,
    error: '',
    data: null,
    selectedId: '',
  })
  const [docsOpen, setDocsOpen] = useState(false)
  // 文档中心：本地面板，上传在“追加修改需求”里；这里只负责打开 / 刷新。
  const [docCenterOpen, setDocCenterOpen] = useState(false)
  const [docCenterVersion, setDocCenterVersion] = useState(0)
  const existingSource = projectMode === 'EXISTING_SOURCE'
  const status = state?.status ?? 'IDLE'
  const statusTone = STATUS_TONES[status] ?? 'idle'
  const shape = state?.appShape
  const confidence = clampPercent(state?.confidenceScore)
  const threshold = clampPercent(state?.threshold ?? 0.85)
  const messages = state?.messages ?? []
  const clarification = state?.clarification
  const contract = state?.contract
  const projectDocs = useMemo(
    () =>
      buildProjectDocs({
        contract,
        visualDocument: contract?.visualDocument,
        projectName,
      }),
    [contract, projectName],
  )
  const helper = state?.helper ?? { messages: [], pending: false, error: null }
  const docCenterCount = useMemo(
    () => documentCount(projectId),
    [projectId, docCenterVersion],
  )

  /**
   * 统一聊天流：主状态机消息与影子助手问答按时间合并。
   *
   * 影子消息只来自 helper.messages，绝不会反向写入主 messages；正式推进
   * 仍由正文里的“追加需求并修订”或澄清 / 契约决策表单负责。
   */
  const flowItems = useMemo(() => {
    const items = []
    ;(messages ?? []).forEach((message, index) => {
      items.push({ ...message, source: 'architect', order: index })
    })
    ;(helper.messages ?? []).forEach((message, index) => {
      items.push({ ...message, source: 'helper', order: index })
    })
    return items.sort((left, right) => {
      const leftAt = Date.parse(left.createdAt ?? '') || 0
      const rightAt = Date.parse(right.createdAt ?? '') || 0
      if (leftAt !== rightAt) return leftAt - rightAt
      // 时间戳相同（同一批次事件）时保持各自原始顺序。
      return left.order - right.order
    })
  }, [messages, helper.messages])

  const runSolutionDiscovery = async () => {
    const text = goal.trim()
    if (!text || !qualityTier || solutionDiscovery.loading) return
    setSolutionDiscovery({
      open: true,
      loading: true,
      error: '',
      data: null,
      selectedId: '',
    })
    try {
      const data = await discoverArchitectSolutions({
        message: text,
        projectId,
        projectName,
        // 文档中心里启用的文档；与正式 start 保持同一份事实源。
        knowledgeDocuments: mergeKnowledgeDocuments(
          activeKnowledgeDocuments(projectId),
        ),
        provider: modelProvider,
        token: session?.token,
      })
      setSolutionDiscovery({
        open: true,
        loading: false,
        error: '',
        data,
        selectedId:
          data?.recommendedOptionId ||
          data?.options?.[0]?.optionId ||
          'opt_build',
      })
    } catch (cause) {
      setSolutionDiscovery({
        open: true,
        loading: false,
        error: `方案侦察暂不可用：${cause?.message || cause}`,
        data: null,
        selectedId: 'opt_build',
      })
    }
  }

  const selectedSolution = useMemo(() => {
    const options = solutionDiscovery.data?.options ?? []
    return (
      options.find(
        (option) => option.optionId === solutionDiscovery.selectedId,
      ) ?? null
    )
  }, [solutionDiscovery.data, solutionDiscovery.selectedId])

  const startArchitectWithChoice = (choice) => {
    onStart?.(goal.trim(), qualityTier, choice)
  }

  const confirmSolutionChoice = () => {
    const choice =
      buildSolutionChoice(selectedSolution) || defaultSolutionChoice()
    startArchitectWithChoice(choice)
  }

  const skipSolutionDiscovery = () => {
    startArchitectWithChoice(defaultSolutionChoice())
  }

  const closeSolutionDiscovery = () => {
    setSolutionDiscovery({
      open: false,
      loading: false,
      error: '',
      data: null,
      selectedId: '',
    })
  }

  useEffect(() => {
    const onKeyDown = (event) => {
      if (event.key === 'Escape') onClose?.()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  useEffect(() => {
    setConfirmCancel(false)
  }, [status, clarification?.clarificationId, contract?.contractId])

  useEffect(() => {
    if (
      contract?.contractId &&
      (status === 'AWAITING_CONTRACT_CONFIRMATION' || status === 'CONFIRMED')
    ) {
      // 契约内容是长文档：定位到标题，而不是跳到底部只看决策按钮。
      contractRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      return
    }
    const node = scrollRef.current
    if (!node) return
    node.scrollTo({ top: node.scrollHeight, behavior: 'smooth' })
  }, [
    messages.length,
    helper.messages.length,
    helper.pending,
    clarification?.clarificationId,
    contract?.contractId,
    status,
  ])

  const workingLabel =
    status === 'REPAIRING_CONTRACT'
      ? '正在按校验结果修复契约…'
      : status === 'DRAFTING_CONTRACT'
        ? '正在结合知识库起草实施契约…'
        : status === 'REVISING_CONTRACT'
          ? '正在按你的意见修订契约…'
          : status === 'CONFIRMING_CONTRACT'
            ? '正在确认契约并落库…'
            : status === 'CANCELLING'
              ? '正在取消…'
              : '正在读取当前前端源码并分析需求…'

  return (
    <div
      className="architect-overlay"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose?.()
      }}
    >
      <aside
        className="architect-panel"
        role="dialog"
        aria-modal="true"
        aria-label={`${projectName} 全栈架构澄清`}
      >
        <header className="architect-panel-header">
          <div className="architect-title">
            <span className="architect-mark" aria-hidden="true">
              ◈
            </span>
            <div>
              <div className="architect-eyebrow">M2.5 · GLOBAL ARCHITECT</div>
              <h2>全栈 Path 架构师</h2>
              <p>{projectName}</p>
            </div>
          </div>
          <div className="architect-header-right">
            <button
              type="button"
              className="ghost-button"
              data-project-doc-center-open
              title="查看本地文档中心；只有启用的文档进入 Architect RAG"
              onClick={() => setDocCenterOpen(true)}
            >
              📚 文档中心 ({docCenterCount})
            </button>
            <button
              type="button"
              className="ghost-button"
              data-project-docs-open
              disabled={!contract}
              title={contract ? '查看 01–05 方案文档' : '契约生成后可查看方案文档'}
              onClick={() => setDocsOpen(true)}
            >
              📄 方案文档
            </button>
            <span className={`architect-status is-${statusTone}`}>
              {WORKING_STATUSES.has(status) && <span className="mini-spinner" />}
              {STATUS_LABELS[status] ?? status}
            </span>
            <button
              type="button"
              className="architect-close"
              aria-label="关闭架构师面板"
              onClick={onClose}
            >
              ×
            </button>
          </div>
        </header>

        {projectMode === 'EXISTING_SOURCE' && (
          <div
            className="architect-gate is-released"
            data-existing-source-mode
            role="status"
          >
            <span className="architect-gate-mark" aria-hidden="true">
              ◇
            </span>
            <div className="architect-gate-copy">
              <strong>存量源码模式</strong>
              <p>
                源码是唯一事实源。这里只做业务审查与后端增量绑定；
                不生成 VisualDocument、全量前端代码或前端 CodeGen。
              </p>
            </div>
          </div>
        )}

        {gate?.gateId && (
          <div
            className={`architect-gate is-${String(gate.status).toLowerCase()}`}
            data-supervisor-gate={gate.status}
            role={gate.status === 'FAILED' ? 'alert' : 'status'}
          >
            <span className="architect-gate-mark" aria-hidden="true">
              {gate.status === 'RELEASED'
                ? '✓'
                : gate.status === 'FAILED'
                  ? '!'
                  : '◈'}
            </span>
            <div className="architect-gate-copy">
              {(gate.status === 'AWAITING_CONTRACT' ||
                gate.status === 'RELEASING') && (
                <>
                  <strong>正在进行全局架构设计，请先确认契约</strong>
                  <p>
                    契约确认前不会执行界面修改，沙盒保持当前状态，避免门控乱序导致代码冲突。
                  </p>
                  {gate.status === 'AWAITING_CONTRACT' &&
                    state?.status === 'CONFIRMED' && (
                      <button
                        type="button"
                        className="architect-secondary"
                        disabled={gate.releasing}
                        onClick={onReleaseGate}
                      >
                        {gate.releasing ? (
                          <>
                            <span className="mini-spinner" aria-label="释放中" />
                            正在放行…
                          </>
                        ) : (
                          '继续执行界面修改'
                        )}
                      </button>
                    )}
                </>
              )}
              {gate.status === 'RELEASED' && (
                <>
                  <strong>契约已确认，界面修改已启动</strong>
                  <p>
                    UI Graph 已放行并在独立沙盒中执行，可关闭本面板查看右侧执行时间轴。
                  </p>
                </>
              )}
              {gate.status === 'FAILED' && (
                <>
                  <strong>both 门控已中止</strong>
                  <p>{gate.error || '架构澄清被取消或失败，界面修改未执行。'}</p>
                </>
              )}
              {gate.error && gate.status !== 'FAILED' && (
                <p className="architect-gate-error">{gate.error}</p>
              )}
            </div>
          </div>
        )}

        {state?.runId && (
          <div className="architect-progress">
            <div className="architect-progress-meta">
              {shape ? (
                <span className={`shape-badge shape-${shape}`}>
                  {SHAPE_LABELS[shape]}
                </span>
              ) : (
                <span className="shape-badge shape-unknown">形态待确认</span>
              )}
              <span className="architect-round">
                第 {Math.min(state.round || 1, state.maxRounds || 5)}/
                {state.maxRounds || 5} 轮
              </span>
              <span className="architect-threshold">阈值 {threshold}%</span>
            </div>
            <div
              className="confidence-track"
              role="progressbar"
              aria-label="架构置信度"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={confidence}
            >
              <span
                className={`confidence-fill ${confidence >= threshold ? 'is-good' : ''}`}
                style={{ width: `${confidence}%` }}
              />
              <span className="confidence-target" style={{ left: `${threshold}%` }} />
            </div>
          </div>
        )}

        <div className="architect-panel-scroll" ref={scrollRef}>
          {state?.error && (
            <div className="architect-error" role="alert">
              <strong>架构澄清未完成</strong>
              <span>{state.error}</span>
            </div>
          )}

          {status === 'IDLE' && (
            <div
              className="architect-intro"
              data-architect-intro={existingSource ? 'existing' : 'new'}
            >
              <span className="architect-intro-mark" aria-hidden="true">
                {existingSource ? '◇' : '✦'}
              </span>
              <h3>
                {existingSource
                  ? '描述要为现有前端补充的后端能力'
                  : '告诉我你要做什么，架构师来拆解'}
              </h3>
              <p>
                {existingSource
                  ? '源码是唯一事实源。这里不会新建 VisualDocument，也不会重写现有 React 页面；架构师只基于现有文件树、组件和路由做业务审查，并确认接口与数据表的增量。'
                  : '不需要先画原型或选择项目类型。用一句话描述目标，架构师会从对话中识别应用形态，并逐步追问用户群体、核心功能、技术栈、数据实体与页面结构。'}
              </p>
              <label className="architect-goal">
                <span>
                  {existingSource ? '本次后端增量目标是什么？' : '你想创建什么？'}
                </span>
                <textarea
                  rows={5}
                  value={goal}
                  data-architect-goal
                  placeholder={
                    existingSource
                      ? '例如：为现有的用户列表补充分页查询接口、users 表的索引，并增加角色字段；不要改动页面结构。'
                      : '例如：我要做一个团队任务看板，成员可以登录、创建任务、拖拽状态，并给任务指派负责人。'
                  }
                  onChange={(event) => setGoal(event.target.value)}
                  onKeyDown={(event) => {
                    // Cmd/Ctrl + Enter 直接提交，适合连续多轮对话。
                    if (
                      (event.metaKey || event.ctrlKey) &&
                      event.key === 'Enter' &&
                      goal.trim()
                    ) {
                      event.preventDefault()
                      if (qualityTier) void runSolutionDiscovery()
                    }
                  }}
                />
              </label>
              <fieldset
                className="architect-goal-tier"
                data-architect-quality-tier
              >
                <legend>项目规模 / 质量要求（不可跳过）</legend>
                {QUALITY_TIER_OPTIONS.map((option) => (
                  <label key={option.value}>
                    <input
                      type="radio"
                      name="architect-quality-tier"
                      value={option.value}
                      checked={qualityTier === option.value}
                      onChange={() => setQualityTier(option.value)}
                    />
                    <span>{option.label}</span>
                  </label>
                ))}
              </fieldset>
              {solutionDiscovery.open ? (
                <SolutionDiscoveryPanel
                  discovery={solutionDiscovery}
                  selected={selectedSolution}
                  onSelect={(optionId) =>
                    setSolutionDiscovery((previous) => ({
                      ...previous,
                      selectedId: optionId,
                    }))
                  }
                  onConfirm={confirmSolutionChoice}
                  onSkip={skipSolutionDiscovery}
                  onRetry={() => void runSolutionDiscovery()}
                  onBack={closeSolutionDiscovery}
                />
              ) : (
                <div className="architect-goal-actions">
                  <button
                    type="button"
                    className="architect-primary"
                    data-architect-start
                    disabled={!goal.trim() || !qualityTier}
                    title={
                      !goal.trim()
                        ? '请先写下需求'
                        : !qualityTier
                          ? '请先选择项目规模 / 质量要求'
                          : '先侦察可复用方案，再进入架构澄清'
                    }
                    onClick={() => void runSolutionDiscovery()}
                  >
                    {existingSource ? '侦察复用方案' : '侦察复用方案并开始'}
                  </button>
                  <span className="architect-goal-hint">
                    {existingSource
                      ? '先看能否复用，再确认接口 / 数据表增量'
                      : !qualityTier
                        ? '先选择项目规模，再侦察方案'
                        : '先确认复用 / 自研方案，再进入架构澄清'}
                  </span>
                </div>
              )}
            </div>
          )}

          {status !== 'IDLE' && (flowItems.length > 0 || helper.pending) && (
            <div
              className="architect-chat"
              aria-label="架构师与影子助手统一对话"
              data-architect-flow
            >
              {flowItems.map((item) => (
                <FlowMessage
                  key={`${item.source}:${item.id}`}
                  item={item}
                />
              ))}
              {helper.pending && (
                <div className="architect-helper-thinking" role="status">
                  <span className="mini-spinner" aria-hidden="true" />
                  影子助手正在查阅架构上下文…
                </div>
              )}
            </div>
          )}

          {WORKING_STATUSES.has(status) && (
            <div className="architect-working" role="status">
              <span className="architect-orbit" aria-hidden="true">
                <span />
                <span />
                <span />
              </span>
              <div>
                <strong>{workingLabel}</strong>
                <p className={state?.streamingText ? 'architect-stream-text' : undefined}>
                  {state?.streamingText
                    ? state.streamingText
                    : status === 'REPAIRING_CONTRACT'
                      ? '校验规则会阻止缺少接口或 DDL 的全栈契约进入确认。'
                      : '全局澄清与画布局部修改使用独立状态机和事件流。'}
                </p>
              </div>
            </div>
          )}

          {status === 'AWAITING_CLARIFICATION' && clarification && (
            <ClarificationForm
              key={clarification.clarificationId}
              clarification={clarification}
              submitting={Boolean(state.answerSubmitting)}
              onSubmit={(answers, comment, options) =>
                onAnswer(answers, comment, options)
              }
              onCancel={onCancel}
            />
          )}

          {status === 'AWAITING_DECISION' && state.conflict && (
            <ConflictDecisionDialog
              key={state.conflict.conflictId}
              conflict={state.conflict}
              submitting={Boolean(state.conflictSubmitting)}
              error={state.error}
              onDecide={onDecideConflict}
            />
          )}

          {contract && (
            <div ref={contractRef} className="architect-contract-anchor">
              <ContractViewer contract={contract} citations={state.citations} />
            </div>
          )}

          {status === 'AWAITING_CONTRACT_CONFIRMATION' && contract && (
            <ContractActions
              key={contract.contractId}
              contract={contract}
              submitting={Boolean(state.decisionSubmitting)}
              onDecide={onDecide}
              qualityTierRequired={state.qualityTierRequired !== false}
            />
          )}

          {status === 'AWAITING_VISUAL_EDIT' && (
            <div className="architect-outcome is-success" role="status">
              <span aria-hidden="true">✦</span>
              <div>
                <strong>v0 虚拟原型已生成</strong>
                <p>
                  请在画布中离线调整布局与组件。完成后点击“确认视觉”，
                  系统只把相对 v0 的增量组件交给架构师做业务确认与后端绑定。
                </p>
                <a
                  className="primary-button"
                  href={`/visual-canvas?project=${encodeURIComponent(
                    projectId,
                  )}&title=${encodeURIComponent(
                    contract?.visualDocument?.title ||
                      projectName ||
                      '未命名页面',
                  )}`}
                >
                  打开虚拟画布继续编辑
                </a>
              </div>
            </div>
          )}

          {status === 'CONFIRMED' && (
            <>
              {contract?.visualDocument ? (
                <>
                  <div className="architect-outcome is-success" role="status">
                    <span aria-hidden="true">✓</span>
                    <div>
                      <strong>虚拟骨架已生成</strong>
                      <p>
                        已写入离线画布，可直接增删、拖拽、调整布局。
                        确认视觉后再编译为 React 源码并启动沙盒。
                      </p>
                      <a
                        className="primary-button"
                        href={`/visual-canvas?project=${encodeURIComponent(
                          projectId,
                        )}&title=${encodeURIComponent(
                          contract.visualDocument.title ||
                            projectName ||
                            '未命名页面',
                        )}`}
                      >
                        打开虚拟画布
                      </a>
                    </div>
                  </div>
                  <VisualCodegen
                    fullstack={state.fullstack}
                    onGenerate={onFullstackGenerate}
                  />
                </>
              ) : (
                <>
                  <div className="architect-outcome is-success" role="status">
                    <span aria-hidden="true">✓</span>
                    <div>
                      <strong>实施契约已确认</strong>
                      <p>
                        {autoBootstrap || state.fullstack?.mode === 'frontend'
                          ? '系统正在依据契约初始化前端页面，完成后画布会自动刷新。'
                          : '下一阶段可直接依据该契约生成前后端代码与数据库迁移。'}
                      </p>
                    </div>
                  </div>
                  {autoBootstrap || state.fullstack?.mode === 'frontend' ? (
                    <FrontendBootstrap
                      fullstack={state.fullstack}
                      contract={contract}
                      onRetry={onFrontendBootstrap}
                      onFullstackGenerate={onFullstackGenerate}
                    />
                  ) : (
                    <FullstackGeneration
                      fullstack={state.fullstack}
                      contract={contract}
                      onGenerate={onFullstackGenerate}
                      onExport={onExport}
                      onReclarify={(prompt) => onDecide?.('REVISE', prompt)}
                      a2a={state.a2a}
                      exporting={exporting}
                      projectMode={projectMode}
                    />
                  )}
                </>
              )}
              {/* 契约确认后对话栏不关闭：底部持续提供追加需求入口。 */}
              <ContractAppend
                contract={contract}
                submitting={Boolean(state.decisionSubmitting)}
                onDecide={onDecide}
                projectId={projectId}
                provider={modelProvider}
                onOpenDocumentCenter={() => setDocCenterOpen(true)}
                onDocumentsChanged={() =>
                  setDocCenterVersion((value) => value + 1)
                }
              />
            </>
          )}

          {status === 'CANCELLED' && (
            <div className="architect-outcome is-muted" role="status">
              <span aria-hidden="true">—</span>
              <div>
                <strong>本次架构澄清已取消</strong>
                <p>画布与现有代码没有被修改，可随时重新发起。</p>
              </div>
            </div>
          )}

          {status === 'FAILED' && (
            <div className="architect-outcome is-error">
              <span aria-hidden="true">!</span>
              <div>
                <strong>架构澄清失败</strong>
                <p>{state.error || '请检查 AI Core 与模型配置后重试。'}</p>
                <button type="button" className="architect-secondary" onClick={onRestart}>
                  重新发起
                </button>
              </div>
            </div>
          )}
        </div>

        {(WORKING_STATUSES.has(status) ||
          status === 'AWAITING_CLARIFICATION' ||
          status === 'FAILED') &&
          !contract && (
            <footer className="architect-panel-footer">
              {WORKING_STATUSES.has(status) ? (
                <>
                  <span>执行中可随时取消；已产生的代码与画布不会受影响。</span>
                  <button
                    type="button"
                    className="ghost-button"
                    disabled={state.cancelRequested}
                    onClick={() => {
                      if (!confirmCancel) {
                        setConfirmCancel(true)
                        return
                      }
                      onCancel()
                    }}
                  >
                    {confirmCancel ? '确认取消？' : '取消执行'}
                  </button>
                </>
              ) : status === 'AWAITING_CLARIFICATION' ? (
                <>
                  <span>
                    第 {clarification?.round ?? state.round} 轮澄清 · 最多{' '}
                    {state.maxRounds} 轮，超时会标记 CONFIDENCE_EXHAUSTED。
                  </span>
                  <button
                    type="button"
                    className="ghost-button"
                    disabled={state.cancelRequested}
                    onClick={() => {
                      if (!confirmCancel) {
                        setConfirmCancel(true)
                        return
                      }
                      onCancel()
                    }}
                  >
                    {confirmCancel ? '确认取消？' : '取消本次澄清'}
                  </button>
                </>
              ) : (
                <span>失败不会影响当前画布、沙盒与已有代码。</span>
              )}
            </footer>
          )}

        {/* 底部只保留影子助手输入；正式推进统一走正文里的追加需求 / 决策表单。 */}
        {state?.runId && (
          <ArchitectInputDock
            helperMessages={helper.messages ?? []}
            helperPending={Boolean(helper.pending)}
            helperError={helper.error}
            helperDisabled={!state?.runId}
            onAskHelper={(question) => onAskHelper?.(question)}
          />
        )}
      </aside>
      <ProjectDocsDrawer
        open={docsOpen}
        docs={projectDocs}
        onClose={() => setDocsOpen(false)}
      />
      <ProjectDocumentCenter
        open={docCenterOpen}
        projectId={projectId}
        provider={modelProvider}
        onClose={() => setDocCenterOpen(false)}
        onChange={() => setDocCenterVersion((value) => value + 1)}
      />
    </div>
  )
}
