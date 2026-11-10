import { useEffect, useMemo, useRef, useState } from 'react'
import ContractViewer from './ContractViewer.jsx'

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
function FullstackGeneration({ fullstack, contract, onGenerate }) {
  const state = fullstack ?? {}
  const status = state.status ?? 'IDLE'
  const running = Boolean(state.starting) || status === 'RUNNING'
  const steps = Array.isArray(state.steps) ? state.steps : []
  const outputs = Array.isArray(state.outputs) ? state.outputs : []
  const result = state.result
  const isFullstack = contract?.appShape === 'full_stack_system'
  const apiRouteCount = Array.isArray(contract?.apiRoutes)
    ? contract.apiRoutes.length
    : 0
  const hasDdl =
    typeof contract?.databaseDdl === 'string' &&
    contract.databaseDdl.trim().length > 0
  const contractConfirmed = contract?.status === 'CONFIRMED'
  const contractReady = isFullstack && apiRouteCount > 0 && hasDdl

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
        : !hasDdl
          ? '契约缺少数据库 DDL（databaseDdl 为空），已阻止生成；请修订契约补充数据实体后重新确认。'
          : running
            ? '全栈代码正在生成，按钮暂时锁定；完成后可重新生成。'
            : ''
  const tooltip = contractReady
    ? running
      ? '生成中，请稍候'
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
    })
  }, [
    status,
    state.starting,
    running,
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
      aria-label="全栈代码生成"
    >
      <div className="architect-fullstack-head">
        <div>
          <span className="architect-form-eyebrow">CONTRACT → SANDBOX</span>
          <h3>生成前后端与数据库代码</h3>
          <p>
            DBACoder 生成 Sequelize ORM，BackendCoder 提供 Node API，
            FrontendCoder 接入真实请求，InfraCoder 输出 Dockerfile /
            render.yaml / .env.example，最后重启沙盒完成联调。
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
              disabled={!contractReady || running}
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
                '↻ 重新生成全栈代码'
              ) : status === 'FAILED' ? (
                '↻ 重试生成全栈代码'
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
        <div className="architect-fullstack-error" role="alert" data-fullstack-error>
          <strong>全栈生成失败</strong>
          <span>{state.error}</span>
          {state.errorCode && <code>{state.errorCode}</code>}
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
 * 契约确认后的持续对话入口。
 *
 * 确认不是终点：架构师面板保持在线，用户可随时把新需求追加到当前
 * Context。提交后由后端直接进入 revise_contract（不会重新澄清、不会
 * 丢掉已经确认的技术栈与业务槽位），新契约回到待确认状态。
 */
function ContractAppend({ contract, submitting, onDecide }) {
  const [comment, setComment] = useState('')
  const ready = comment.trim().length > 0

  const submit = () => {
    if (!ready || submitting) return
    onDecide('REVISE', comment.trim())
    setComment('')
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
      <div className="architect-decision-row">
        <span className="architect-append-hint">
          会直接在当前契约上修订，无需重新澄清
        </span>
        <button
          type="button"
          className="architect-primary"
          disabled={!ready || submitting}
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

function ContractActions({ contract, submitting, onDecide }) {
  const [comment, setComment] = useState('')
  const [cancelConfirm, setCancelConfirm] = useState(false)
  const revisionReady = comment.trim().length > 0

  return (
    <div className="architect-contract-actions">
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
          onClick={() => onDecide('REVISE', comment.trim())}
        >
          要求修订
        </button>
        <button
          type="button"
          className="architect-primary"
          disabled={submitting}
          onClick={() => onDecide('CONFIRM', comment.trim())}
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

export default function ArchitectPanel({
  projectName,
  state,
  onClose,
  onStart,
  onAnswer,
  onDecide,
  onCancel,
  onRestart,
  onFullstackGenerate,
  onFrontendBootstrap,
  autoBootstrap = false,
  gate,
  onReleaseGate,
}) {
  const scrollRef = useRef(null)
  const contractRef = useRef(null)
  const [confirmCancel, setConfirmCancel] = useState(false)
  // 新建项目的第一条输入：纯自然语言目标，不再要求先做形态选择。
  const [goal, setGoal] = useState('')
  const status = state?.status ?? 'IDLE'
  const statusTone = STATUS_TONES[status] ?? 'idle'
  const shape = state?.appShape
  const confidence = clampPercent(state?.confidenceScore)
  const threshold = clampPercent(state?.threshold ?? 0.85)
  const messages = state?.messages ?? []
  const clarification = state?.clarification
  const contract = state?.contract

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
  }, [messages.length, clarification?.clarificationId, contract?.contractId, status])

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
            <div className="architect-intro">
              <span className="architect-intro-mark" aria-hidden="true">
                ✦
              </span>
              <h3>告诉我你要做什么，架构师来拆解</h3>
              <p>
                不需要先画原型或选择项目类型。用一句话描述目标，架构师会从
                对话中识别应用形态，并逐步追问用户群体、核心功能、技术栈、
                数据实体与页面结构。
              </p>
              <label className="architect-goal">
                <span>你想创建什么？</span>
                <textarea
                  rows={5}
                  value={goal}
                  data-architect-goal
                  placeholder="例如：我要做一个团队任务看板，成员可以登录、创建任务、拖拽状态，并给任务指派负责人。"
                  onChange={(event) => setGoal(event.target.value)}
                  onKeyDown={(event) => {
                    // Cmd/Ctrl + Enter 直接提交，适合连续多轮对话。
                    if (
                      (event.metaKey || event.ctrlKey) &&
                      event.key === 'Enter' &&
                      goal.trim()
                    ) {
                      event.preventDefault()
                      onStart?.(goal.trim())
                    }
                  }}
                />
              </label>
              <div className="architect-goal-actions">
                <button
                  type="button"
                  className="architect-primary"
                  data-architect-start
                  disabled={!goal.trim()}
                  onClick={() => onStart?.(goal.trim())}
                >
                  开始架构设计
                </button>
                <span className="architect-goal-hint">
                  架构师会连续追问，直到契约置信度达到阈值
                </span>
              </div>
            </div>
          )}

          {status !== 'IDLE' && messages.length > 0 && (
            <div className="architect-chat" aria-label="全局澄清对话">
              {messages.map((message) => (
                <article
                  key={message.id}
                  className={`architect-message role-${message.role} ${
                    message.kind ? `kind-${message.kind}` : ''
                  }`}
                >
                  <span className="architect-message-role">
                    {message.role === 'assistant'
                      ? '架构师'
                      : message.role === 'user'
                        ? '你'
                        : '系统'}
                  </span>
                  <div className="architect-message-body">{message.content}</div>
                </article>
              ))}
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
                <p>
                  {status === 'REPAIRING_CONTRACT'
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
              onSubmit={(answers, comment) => onAnswer(answers, comment)}
              onCancel={onCancel}
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
            />
          )}

          {status === 'CONFIRMED' && (
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
                />
              )}
              {/* 契约确认后对话栏不关闭：底部持续提供追加需求入口。 */}
              <ContractAppend
                contract={contract}
                submitting={Boolean(state.decisionSubmitting)}
                onDecide={onDecide}
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
      </aside>
    </div>
  )
}
