import { useEffect, useMemo, useRef, useState } from 'react'
import ContractViewer from './ContractViewer.jsx'

const SHAPE_LABELS = {
  lightweight_web: '轻量网页工具',
  script_automation: '脚本自动化',
  full_stack_system: '全栈系统',
}

const SHAPE_DESCRIPTIONS = {
  lightweight_web: '纯前端、LocalStorage 或 Serverless，不引入后端与数据库。',
  script_automation: '后台定时 / 命令行运行，无 UI，只定义输入、处理与输出。',
  full_stack_system: '需要账号、持久化与 REST API 的完整业务系统。',
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
 * 应用形态是唯一一个必须“显式选择”的问题：不预选默认值，避免用户一路
 * 回车把全栈需求误判为轻量工具，或反之。
 */
function ClarificationForm({ clarification, submitting, onSubmit, onCancel }) {
  const questions = clarification.questions ?? []
  const shapeQuestion = questions.find((item) => item.slot === 'app.shape')
  const otherQuestions = questions.filter((item) => item.slot !== 'app.shape')

  const [values, setValues] = useState(() => {
    const initial = {}
    otherQuestions.forEach((question) => {
      initial[question.questionId] = initialValue(question)
    })
    return initial
  })
  const [shape, setShape] = useState('')
  const [comment, setComment] = useState('')

  const setValue = (questionId, value) => {
    setValues((previous) => ({ ...previous, [questionId]: value }))
  }

  const readyToSubmit = useMemo(() => {
    if (shapeQuestion && !shape) return false
    return otherQuestions.every(
      (question) => !question.required || !isEmpty(values[question.questionId]),
    )
  }, [otherQuestions, shape, shapeQuestion, values])

  const submit = (event) => {
    event.preventDefault()
    if (!readyToSubmit || submitting) return
    const answers = []
    if (shapeQuestion && shape) {
      answers.push({
        questionId: shapeQuestion.questionId,
        value: shape,
      })
    }
    otherQuestions.forEach((question) => {
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
          <h3>
            {shapeQuestion ? '先确认应用形态' : '补齐实施所需信息'}
          </h3>
        </div>
        <span className="architect-form-confidence">
          当前置信度 {clampPercent(clarification.confidenceScore)}%
        </span>
      </div>

      {shapeQuestion && (
        <fieldset className="shape-choice-group">
          <legend>{shapeQuestion.prompt}</legend>
          <div className="shape-choice-grid">
            {(shapeQuestion.options ?? []).map((option) => {
              const selected = shape === option.value
              return (
                <button
                  key={option.value}
                  type="button"
                  className={`shape-choice ${selected ? 'is-selected' : ''}`}
                  aria-pressed={selected}
                  onClick={() => setShape(option.value)}
                >
                  <span className="shape-choice-name">
                    {SHAPE_LABELS[option.value] ?? option.label}
                  </span>
                  <span className="shape-choice-desc">
                    {SHAPE_DESCRIPTIONS[option.value] ?? option.label}
                  </span>
                  <span className="shape-choice-check" aria-hidden="true">
                    {selected ? '✓' : ''}
                  </span>
                </button>
              )
            })}
          </div>
        </fieldset>
      )}

      {otherQuestions.map((question) => {
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
  gate,
  onReleaseGate,
}) {
  const scrollRef = useRef(null)
  const contractRef = useRef(null)
  const [confirmCancel, setConfirmCancel] = useState(false)
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
              <h3>从局部改 UI，升级为可实施的全栈方案</h3>
              <p>
                架构师会读取当前沙盒源码，先判断应用形态，再逐步澄清技术栈、
                业务流与数据实体，最后输出带置信度的《全栈实施契约》。
              </p>
              <ul>
                <li>轻量网页：只确认 UI 流程与前端数据模型，不强迫上后端。</li>
                <li>自动化脚本：只确认运行环境、输入输出与逻辑，不追问页面。</li>
                <li>全栈系统：明确 REST API、数据库 DDL 与前后端交互。</li>
              </ul>
              <button type="button" className="architect-primary" onClick={onStart}>
                开始全局架构澄清
              </button>
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
            <div className="architect-outcome is-success" role="status">
              <span aria-hidden="true">✓</span>
              <div>
                <strong>实施契约已确认</strong>
                <p>
                  下一阶段可直接依据该契约生成前后端代码与数据库迁移。契约状态已
                  持久化到本次 Architect Run。
                </p>
              </div>
            </div>
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
