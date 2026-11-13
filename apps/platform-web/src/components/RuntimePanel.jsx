import { useEffect, useState } from 'react'
import DeployGuideDialog from './DeployGuideDialog.jsx'
import DiffView from './DiffView.jsx'
import InspectDraftCard from './InspectDraftCard.jsx'
import ScaledPreview from './ScaledPreview.jsx'
import VirtualList from './VirtualList.jsx'
import { usePreviewConnection } from '../workspace/usePreviewConnection.js'
import {
  CREATE_NEW,
  MODIFY_EXISTING,
  isCreateAction,
  normalizeAction,
} from '../workspace/annotationActions.js'

/**
 * 时间轴：直接渲染后端 SSE 的 node.started / node.finished 事件。
 * 不再用固定 STAGES 假装进度——节点重跑、自动修复都按真实事件展示。
 */
const TIMELINE_STATUS_ICON = {
  running: null,
  ok: '✓',
  paused: 'Ⅱ',
  error: '!',
  skipped: '–',
}

const formatDuration = (ms) => {
  if (ms === null || ms === undefined) return ''
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`
}

// 成功终态：M2 的旧快照仍可能是 AWAITING_APPROVAL，统一按成功展示。
const SUCCESS_STATUSES = new Set(['SUCCEEDED', 'AWAITING_APPROVAL'])

// 与 DiffView 相同的行着色规则；审批面板按文件分别渲染。
const diffLineClass = (line) => {
  if (line.startsWith('+++') || line.startsWith('---')) return 'meta'
  if (line.startsWith('@@')) return 'hunk'
  if (line.startsWith('+')) return 'add'
  if (line.startsWith('-')) return 'del'
  return ''
}

const RISK_TEXT = { LOW: '低风险', MEDIUM: '中风险', HIGH: '高风险' }

/**
 * Diff 审批面板：逐个文件展示 unified diff，批准才会写入 Git checkpoint。
 *
 * 决策通过 POST /runs/{runId}/approvals/{approvalId}/decision 提交，
 * 服务端 202 后恢复同一个 Run，事件流继续推进。
 */
function ApprovalPanel({
  approval,
  busy,
  cancelRequested,
  onDecision,
  onCancel,
  onGoToCanvas,
}) {
  const [comment, setComment] = useState('')
  const evidence = approval.evidence ?? {}
  const files = evidence.files ?? []
  const build = evidence.build ?? {}
  const risk = approval.riskLevel || 'LOW'

  return (
    <div className="loading-state runtime-loading is-approval">
      <div className="loading-copy timeline-head">
        <h3 className="loading-title">
          {approval.isDraft ? '等待确认' : '等待审批'}
        </h3>
        <p className="muted">
          {approval.isDraft
            ? '预览中是隔离草稿，主工作区尚未修改。确认后才会合并并回写 Git。'
            : '构建已通过；确认 Diff 后才会写入 Git checkpoint。'}
        </p>
      </div>

      <div className="approval-summary">
        <span className={`approval-risk is-${risk.toLowerCase()}`}>
          {RISK_TEXT[risk] ?? risk}
        </span>
        <span className="approval-stat is-add">+{evidence.insertions ?? 0}</span>
        <span className="approval-stat is-del">−{evidence.deletions ?? 0}</span>
        <span className="muted">{files.length} 个文件</span>
        {build.status && (
          <span className={`approval-build is-${String(build.status).toLowerCase()}`}>
            构建 {build.status}
          </span>
        )}
      </div>

      {approval.summary && <p className="approval-lead">{approval.summary}</p>}

      <div className="approval-files">
        {files.length === 0 && <p className="muted">本次没有文件变更。</p>}
        {files.map((file) => (
          <details
            className="approval-file"
            key={file.path}
            open={files.length === 1}
          >
            <summary>
              <code>{file.path}</code>
              <span className="approval-file-stat">
                <em className="is-add">+{file.insertions}</em>
                <em className="is-del">−{file.deletions}</em>
              </span>
            </summary>
            <pre className="diff-view approval-diff">
              {(file.diff || '').split('\n').map((line, index) => (
                <span
                  key={`${index}-${line}`}
                  className={`diff-line ${diffLineClass(line)}`}
                >
                  <span className="diff-text">{line || ' '}</span>
                </span>
              ))}
            </pre>
            {file.truncated && (
              <p className="muted approval-truncated">Diff 过长，已截断。</p>
            )}
          </details>
        ))}
      </div>

      <input
        className="approval-comment"
        type="text"
        value={comment}
        maxLength={200}
        placeholder="审批备注（可选）"
        disabled={busy}
        onChange={(event) => setComment(event.target.value)}
      />

      <div className="approval-actions">
        <button
          className="primary-button"
          type="button"
          disabled={busy}
          onClick={() => onDecision?.('approve', comment)}
        >
          {busy ? (
            <span className="mini-spinner" aria-label="提交中" />
          ) : approval.isDraft ? (
            '确认并合并'
          ) : (
            '批准并应用'
          )}
        </button>
        <button
          className="ghost-button is-danger"
          type="button"
          disabled={busy}
          onClick={() => onDecision?.('reject', comment)}
        >
          {approval.isDraft ? '放弃草稿' : '拒绝并撤销'}
        </button>
        <button
          className="ghost-button"
          type="button"
          disabled={busy || cancelRequested}
          onClick={onCancel}
        >
          {cancelRequested ? '正在取消…' : '取消执行'}
        </button>
        {onGoToCanvas && (
          <button className="ghost-button" type="button" onClick={onGoToCanvas}>
            去画布查看
          </button>
        )}
      </div>
    </div>
  )
}

const DECISION_KIND_TEXT = {
  protected_component: '公共组件保护',
  target_ambiguous: '目标存在歧义',
}

/**
 * 安全路径决策面板。
 *
 * 选项全部由服务端生成：歧义候选、callSite 实例级修改、取消。
 * 前端只回传 optionId，不提供任何“强行修改公共组件”的入口。
 */
function DecisionPanel({
  decision,
  busy,
  cancelRequested,
  onAnswer,
  onCancel,
  onGoToCanvas,
}) {
  const [comment, setComment] = useState('')
  const options = decision.options ?? []
  const target = decision.target ?? {}
  const kind = DECISION_KIND_TEXT[decision.kind] ?? decision.kind

  return (
    <div className="loading-state runtime-loading is-decision">
      <div className="loading-copy timeline-head">
        <h3 className="loading-title">需要你选择安全路径</h3>
        <p className="muted">{decision.summary || decision.reason}</p>
        <p className="decision-meta">
          第 {decision.round}/{decision.maxRounds} 轮 · {kind}
        </p>
      </div>

      {target.file && (
        <p className="decision-target">
          <code>{target.file}</code>
          {target.range?.startLine ? (
            <span className="muted"> · 第 {target.range.startLine} 行</span>
          ) : null}
          {target.component ? (
            <span className="muted"> · {target.component}</span>
          ) : null}
        </p>
      )}
      {target.sharedReason && (
        <p className="decision-reason">{target.sharedReason}</p>
      )}

      <div className="decision-options">
        {options.map((option) => (
          <button
            key={option.optionId}
            type="button"
            className={`decision-option ${
              option.action === 'cancel' ? 'is-cancel' : ''
            } ${option.recommended ? 'is-recommended' : ''}`}
            disabled={busy}
            onClick={() => onAnswer?.(option.optionId, comment)}
          >
            <span className="decision-option-label">
              {option.label}
              {option.recommended && <em>推荐</em>}
            </span>
            {option.description && (
              <span className="decision-option-desc">{option.description}</span>
            )}
          </button>
        ))}
        {options.length === 0 && (
          <p className="muted">没有可执行的安全路径，请取消本次修改。</p>
        )}
      </div>

      <input
        className="approval-comment decision-comment"
        type="text"
        value={comment}
        maxLength={300}
        placeholder="补充说明（可选）"
        disabled={busy}
        onChange={(event) => setComment(event.target.value)}
      />

      <div className="approval-actions">
        <button
          className="ghost-button"
          type="button"
          disabled={busy || cancelRequested}
          onClick={onCancel}
        >
          {cancelRequested ? '正在取消…' : '取消执行'}
        </button>
        {onGoToCanvas && (
          <button className="ghost-button" type="button" onClick={onGoToCanvas}>
            去画布查看
          </button>
        )}
      </div>
    </div>
  )
}

function Timeline({ items }) {
  if (!items?.length) {
    return (
      <div className="timeline-empty">
        <span className="mini-spinner" aria-hidden="true" />
        <p>正在连接 Agent…</p>
      </div>
    )
  }
  return (
    <ol className="run-timeline" aria-label="Agent 执行时间轴">
      {items.map((item) => {
        const status = item.status || 'running'
        return (
          <li key={item.id} className={`timeline-item is-${status}`}>
            <span className="timeline-marker" aria-hidden="true">
              {status === 'running' ? (
                <span className="mini-spinner" />
              ) : (
                TIMELINE_STATUS_ICON[status] ?? '✓'
              )}
            </span>
            <div className="timeline-body">
              <div className="timeline-title">
                <strong>{item.label || item.node}</strong>
                {item.durationMs !== null && item.durationMs !== undefined && (
                  <span className="timeline-duration">
                    {formatDuration(item.durationMs)}
                  </span>
                )}
              </div>
              {(item.summary || item.message) && (
                <p>{item.summary || item.message}</p>
              )}
            </div>
          </li>
        )
      })}
    </ol>
  )
}

function StatusPill({ status }) {
  const isSuccess = SUCCESS_STATUSES.has(status)
  const label = isSuccess
    ? '已完成'
    : status === 'REJECTED'
      ? '已拒绝'
      : status === 'CANCELLED'
        ? '已取消'
        : '执行失败'
  return (
    <span className={`status-pill ${isSuccess ? 'is-success' : 'is-failed'}`}>
      <i />
      {label}
    </span>
  )
}

function CancelButton({ busy, cancelRequested, onCancel }) {
  if (!onCancel) return null
  return (
    <button
      className="ghost-button paused-action"
      type="button"
      disabled={busy || cancelRequested}
      onClick={onCancel}
    >
      {cancelRequested ? (
        <>
          <span className="mini-spinner" aria-label="取消中" /> 正在取消…
        </>
      ) : (
        '取消执行'
      )}
    </button>
  )
}

function LoadingState({ elapsed, timeline, cancelRequested, onCancel }) {
  return (
    <div className="loading-state runtime-loading is-timeline">
      <div className="loading-copy timeline-head">
        <h3 className="loading-title">
          {cancelRequested ? '正在取消…' : 'Agent 正在执行'}
        </h3>
        <p className="muted">{elapsed < 1 ? '<1' : elapsed}s</p>
      </div>
      <Timeline items={timeline} />
      <CancelButton
        busy={false}
        cancelRequested={cancelRequested}
        onCancel={onCancel}
      />
    </div>
  )
}

function PausedState({
  timeline,
  clarification,
  onGoToCanvas,
  onCancel,
  cancelRequested,
}) {
  return (
    <div className="loading-state runtime-loading is-paused">
      <div className="loading-copy timeline-head">
        <h3 className="loading-title">等待你确认</h3>
        <p className="muted">
          {['CRITICAL', 'CORE_BUSINESS'].includes(clarification?.decisionImpact)
            ? '这属于核心业务决策，Agent 已暂停执行，避免擅自改动架构或契约。'
            : 'Agent 已暂停执行，确认后继续。'}
        </p>
      </div>
      <Timeline items={timeline} />
      {clarification?.decisionSummary && (
        <p className="paused-decision-summary">
          {clarification.decisionSummary}
        </p>
      )}
      {clarification?.clarificationOptions?.length > 0 && (
        <ul className="paused-quick-options">
          {clarification.clarificationOptions.map((option) => (
            <li key={option.optionId}>
              <strong>
                {option.label}
                {option.recommended ? '（推荐）' : ''}
              </strong>
              {option.description && <span>{option.description}</span>}
            </li>
          ))}
        </ul>
      )}
      {clarification?.questions?.length > 0 && (
        <ul className="paused-questions">
          {clarification.questions.map((question) => (
            <li key={question.questionId}>{question.prompt}</li>
          ))}
        </ul>
      )}
      <div className="paused-actions">
        {onGoToCanvas && (
          <button
            className="ghost-button paused-action"
            type="button"
            onClick={onGoToCanvas}
          >
            去画布回答
          </button>
        )}
        <CancelButton
          busy={false}
          cancelRequested={cancelRequested}
          onCancel={onCancel}
        />
      </div>
    </div>
  )
}

function IdleState({ hasAnnotation }) {
  return (
    <div className="runtime-idle">
      <div className="runtime-placeholder">
        <div className="placeholder-bars" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
        <strong>IDLE · 等待 Run</strong>
        <p>
          {hasAnnotation
            ? '标注已就绪，填写意图并发送即可启动沙盒。'
            : '在画布圈选区域，或在预览中精准选择元素。'}
        </p>
      </div>
    </div>
  )
}

const MODEL_LABELS = {
  auto: 'DeepSeek V3',
  deepseek: 'DeepSeek V3',
  mock: 'Mock 引擎',
}

/** 标注对应的 AST 路径：Inspect 有精确 file:line:column；圈选模式回退到
 * 入口文件 + 标记行号（由发送时写入的 astPath 提供）。 */
function annotationAstPath(annotation, index) {
  if (annotation.astPath) return annotation.astPath
  const source = annotation.inspectSource
  if (source?.file) {
    return `${source.file}:${source.line ?? 1}:${source.column ?? 1}`
  }
  const target = isCreateAction(annotation.action)
    ? annotation.addTarget
    : annotation.target
  return target?.selector ? `${target.selector}（行号未解析）` : `标注 ${index + 1}`
}

/** 开发者详情抽屉：底部状态栏拉起，执行中也可查看。 */
function RunDetails({
  result,
  annotations = [],
  timeline = [],
  modelProvider = 'auto',
  sandboxLabel = '未启动',
  previewStatus = 'idle',
  onClose,
}) {
  const verify = result?.verifyResult
  const diffLines = result?.generatedPatch
    ? result.generatedPatch.split('\n').length
    : 0
  const modelLabel = MODEL_LABELS[modelProvider] ?? modelProvider

  return (
    <div className="runtime-drawer" role="dialog" aria-label="开发者执行详情">
      <div className="runtime-drawer-head">
        <div>
          <span className="eyebrow">Developer Detail</span>
          <strong>执行详情</strong>
        </div>
        <button className="ghost-button" type="button" onClick={onClose}>
          关闭
        </button>
      </div>

      <div className="runtime-drawer-body">
        <div className="runtime-meta">
          <span>
            模型 <code>{modelLabel}</code>
          </span>
          <span>
            预览 <code>{previewStatus}</code>
          </span>
          <span>
            沙盒 <code>{sandboxLabel}</code>
          </span>
          <span>
            Run <code>{result?.runId ?? '—'}</code>
          </span>
          <span>
            构建{' '}
            <code>{verify?.durationMs ? `${verify.durationMs} ms` : '—'}</code>
          </span>
          <span>
            退出码 <code>{verify ? verify.exitCode : '—'}</code>
          </span>
        </div>

        {result?.error && <div className="error-box">{result.error}</div>}

        <details className="runtime-fold" open>
          <summary>
            目标元素 AST 路径
            <span>{annotations.length} 个标注</span>
          </summary>
          {annotations.length === 0 ? (
            <p className="muted">当前没有标注。</p>
          ) : (
            <ul className="ast-path-list">
              {annotations.map((annotation, index) => {
                const path = annotationAstPath(annotation, index)
                const intent = annotation.intent?.trim()
                return (
                  <li key={annotation.id}>
                    <span
                      className={`ast-action is-${
                        annotation.action ? normalizeAction(annotation.action) : MODIFY_EXISTING
                      }`}
                    >
                      {isCreateAction(annotation.action) ? CREATE_NEW : MODIFY_EXISTING}
                    </span>
                    <button
                      className="ast-path"
                      type="button"
                      title="点击复制路径"
                      onClick={() => {
                        navigator.clipboard?.writeText(path)?.catch(() => {})
                      }}
                    >
                      {path}
                    </button>
                    {intent && <p>{intent}</p>}
                  </li>
                )
              })}
            </ul>
          )}
        </details>

        <details className="runtime-fold" open>
          <summary>
            LangGraph 节点流转
            <span>{timeline.length} 个节点</span>
          </summary>
          {timeline.length > 0 ? (
            <Timeline items={timeline} />
          ) : (
            <p className="muted">暂无节点事件；发起 Run 后会实时记录。</p>
          )}
        </details>

        {diffLines > 0 && (
          <details className="runtime-fold" open>
            <summary>
              原始代码 Diff
              <span>{diffLines} 行</span>
            </summary>
            <DiffView patch={result.generatedPatch} title="Generated Patch" />
          </details>
        )}

        {!result?.status?.includes('APPROVAL') && verify?.errorLogs && (
          <details className="runtime-fold">
            <summary>最终构建日志</summary>
            <pre className="log-view">{verify.errorLogs}</pre>
          </details>
        )}

        {result?.messages?.length > 0 && (
          <details className="runtime-fold">
            <summary>
              Agent 执行轨迹
              <span>{result.messages.length} 条</span>
            </summary>
            {result.messages.length > 30 ? (
              // 自动修复 / 多轮重试会产生大量轨迹消息；虚拟滚动只渲染
              // 可视区，避免开发者详情抽屉一次性操作上百个 DOM 节点。
              <VirtualList
                className="trace-list"
                items={result.messages}
                height={360}
                itemHeight={64}
                renderItem={(message) => (
                  <li className={message.level === 'error' ? 'is-error' : ''}>
                    <span className="trace-node">{message.node}</span>
                    <p>{message.content}</p>
                  </li>
                )}
              />
            ) : (
              <ol className="trace-list">
                {result.messages.map((message, index) => (
                  <li
                    key={index}
                    className={message.level === 'error' ? 'is-error' : ''}
                  >
                    <span className="trace-node">{message.node}</span>
                    <p>{message.content}</p>
                  </li>
                ))}
              </ol>
            )}
          </details>
        )}
      </div>
    </div>
  )
}

/**
 * 执行耗时只在 RuntimePanel 本地计时。
 *
 * 旧实现把 elapsed 写回页面级 runStates，每 400ms 触发一次全局重渲染；
 * 这里改成组件内 1s 心跳，计时成本只落在当前预览面板。
 */
function useElapsed(startedAt, running) {
  const [elapsed, setElapsed] = useState(0)

  useEffect(() => {
    if (!running || !startedAt) {
      setElapsed(0)
      return undefined
    }
    const tick = () =>
      setElapsed(Math.max(0, Math.floor((Date.now() - startedAt) / 1000)))
    tick()
    const timer = window.setInterval(tick, 1000)
    return () => window.clearInterval(timer)
  }, [startedAt, running])

  return elapsed
}

/**
 * 每个 Tab 独立的“沙盒屏幕”。
 *
 * 结构固定为「chrome 顶栏 + 全出血预览画布」：
 *  - chrome 与 CanvasStage 的浏览器栏等高，双屏模式左右严格对齐；
 *  - 预览内容使用 ScaledPreview 等比缩放，外层不产生滚动条；
 *  - 状态、详情与 Diff 以浮层 / 抽屉覆盖在画布上，不挤压 iframe 尺寸。
 */
export default function RuntimePanel({
  run,
  projectName,
  modelProvider = 'auto',
  expanded = false,
  // active = 当前 Tab 是否处于激活状态。非激活 Tab 不挂载 iframe，
  // 避免多个隐藏沙盒同时运行 HMR / 定时器。
  active = true,
  visible = true,
  onRetry,
  // 大型重构超时后的“转后台运行”：不阻塞画布，完成后 Toast 通知。
  onRunInBackground,
  onGoToCanvas,
  onApprovalDecision,
  onDecisionAnswer,
  onCancelRun,
  inspectMode = false,
  onInspectPick,
  // 预览 iframe 内部路由切换 / 重载：与画布共用同一套标注清理逻辑。
  onFrameRouteChange,
  inspectDraft = null,
  onInspectDraftChange,
  onInspectDraftCancel,
  onInspectDraftSubmit,
  // 可视化选型：等待用户在预览里点选参考组件时，暂停浮层让出 iframe。
  referencePick = null,
  onReferenceCancel,
  placement = null,
  onPlacementConfirm,
  onPlacementDismiss,
  refreshNonce = 0,
  // 双屏同步：画布滚动位置 / 点击反馈，仅展示模式传入。
  syncScroll = null,
  interactionFeedback = null,
  // 直接拖拽：预览实时跟随画布位移；失败时按 items 回滚。
  dragFeedback = null,
  dragRollback = null,
  dragCommit = null,
}) {
  const [detailsOpen, setDetailsOpen] = useState(false)
  const [deployOpen, setDeployOpen] = useState(false)
  const {
    submitting,
    result,
    error,
    annotations = [],
    timeline = [],
    clarification = null,
    approval = null,
    pendingDecision = null,
    cancelRequested = false,
    pendingAction = null,
    backgroundRun = null,
  } = run
  // 审批期间后端返回草稿预览地址：iframe 展示待确认版本，
  // 主工作树此时仍是上一轮内容，用户放弃时无需回滚。
  const previewUrl = approval?.previewUrl || result?.previewUrl
  // 本地秒表：与页面级状态解耦，执行期间不再每 400ms 重渲染整个工作区。
  const elapsed = useElapsed(run.startedAt, submitting)
  const success = SUCCESS_STATUSES.has(result?.status)
  const diffLines = result?.generatedPatch ? result.generatedPatch.split('\n').length : 0
  const connection = usePreviewConnection(previewUrl, visible && !submitting)
  const previewOffline = connection === 'offline'
  const modelLabel = MODEL_LABELS[modelProvider] ?? modelProvider
  // 沙盒实时状态：运行中 > 预览探活结果 > 是否有 sandboxId。
  // 后台 Run 不占用 submitting，但沙盒依然在跑，状态灯要显示“运行中”。
  const busy = submitting || Boolean(backgroundRun)
  const sandboxState = busy
    ? { label: '运行中', tone: 'busy' }
    : result?.sandboxId
      ? connection === 'offline'
        ? { label: '预览离线', tone: 'offline' }
        : connection === 'checking'
          ? { label: '探测中', tone: 'checking' }
          : { label: '在线', tone: 'online' }
      : { label: '未启动', tone: 'idle' }
  const sandboxLabel = result?.sandboxId
    ? `${sandboxState.label} · ${result.sandboxId.slice(0, 14)}`
    : sandboxState.label
  // 只有真正的“执行”可以取消；回滚 / 审批决策提交中不显示取消。
  const cancellable = Boolean(onCancelRun) && pendingAction !== 'rollback'
  // 可恢复超时：后端已精简上下文重试仍超时，允许一键转后台继续生成。
  // 必须具备原始提交快照（backgroundRequest），否则无从重放。
  const canRunInBackground =
    Boolean(onRunInBackground) &&
    Boolean(run.backgroundRequest) &&
    (Boolean(result?.backgroundAvailable) ||
      result?.errorCode === 'CODER_TIMEOUT_RECOVERABLE')

  let stateLabel = 'IDLE'
  let stateClass = 'is-idle'
  if (pendingDecision && !submitting) {
    stateLabel = 'DECISION · 待选择'
    stateClass = 'is-paused'
  } else if (approval && !submitting) {
    stateLabel = 'APPROVAL · 待审批'
    stateClass = 'is-paused'
  } else if (clarification && !submitting) {
    stateLabel = 'PAUSED · 待确认'
    stateClass = 'is-paused'
  } else if (submitting) {
    stateLabel = cancelRequested
      ? 'CANCELLING…'
      : `RUNNING · ${elapsed < 1 ? '<1' : elapsed}s`
    stateClass = 'is-running'
  } else if (backgroundRun) {
    stateLabel = 'BACKGROUND · 后台执行中'
    stateClass = 'is-running'
  } else if (error) {
    stateLabel = 'FAILED'
    stateClass = 'is-failed'
  } else if (result) {
    stateLabel = previewOffline
      ? 'OFFLINE'
      : success
        ? 'READY'
        : result.status === 'REJECTED'
          ? 'REJECTED'
          : result.status === 'CANCELLED'
            ? 'CANCELLED'
            : 'FAILED'
    stateClass = previewOffline || !success ? 'is-failed' : 'is-ready'
  }

  return (
    <aside
      className={`runtime-panel is-immersive ${expanded ? 'is-expanded' : ''}`}
      data-sandbox-id={result?.sandboxId ?? ''}
    >
      <div className="canvas-chrome runtime-chrome">
        <span className="chrome-dots" aria-hidden="true">
          <i className="dot-red" />
          <i className="dot-amber" />
          <i className="dot-green" />
        </span>
        <span className={`runtime-state ${stateClass}`}>
          <i />
          {stateLabel}
        </span>
        <div className="runtime-chrome-meta">
          <button
            type="button"
            className="runtime-deploy-button"
            data-deploy-button
            title="查看生产部署配置与上线指引"
            onClick={() => setDeployOpen(true)}
          >
            🚀 部署上线
          </button>
          <span className="runtime-chip" title="当前模型">
            {modelLabel}
          </span>
          <span className={`runtime-chip is-sandbox is-${sandboxState.tone}`}>
            <i />
            沙盒 {sandboxLabel}
          </span>
        </div>
      </div>

      <div className="runtime-stage">
        {/* 只有激活 Tab 才挂载 iframe；隐藏 Tab 的沙盒 JS / HMR 连接会被释放。
            切换视图（画布 <-> 预览）仍只改 display，不重建 iframe。 */}
        {previewUrl && active && (
          <ScaledPreview
            // 快照回滚后工作区文件已变化，但 URL 不变；用 nonce 重建 iframe，
            // 强制浏览器重新加载沙盒的最新构建结果。
            key={`${previewUrl}:${refreshNonce}`}
            src={previewUrl}
            title={`${projectName} 沙盒预览`}
            visible={visible && !submitting}
            inspectMode={inspectMode}
            onInspectPick={onInspectPick}
            syncScroll={syncScroll}
            interactionFeedback={interactionFeedback}
            dragFeedback={dragFeedback}
            dragRollback={dragRollback}
            dragCommit={dragCommit}
            onRouteChange={onFrameRouteChange}
          />
        )}

        {/* 精准选择命中后，在预览画布内直接弹出标注输入卡片。 */}
        {inspectDraft && !submitting && !approval && !pendingDecision && (
          <InspectDraftCard
            draft={inspectDraft}
            onChange={onInspectDraftChange}
            onCancel={onInspectDraftCancel}
            onSubmit={onInspectDraftSubmit}
          />
        )}

        {submitting && (
          <div className="runtime-overlay">
            <LoadingState
              elapsed={elapsed}
              timeline={timeline}
              cancelRequested={cancelRequested}
              onCancel={cancellable ? onCancelRun : undefined}
            />
          </div>
        )}

        {!submitting && approval && (
          <div className="runtime-overlay">
            <ApprovalPanel
              approval={approval}
              busy={submitting}
              cancelRequested={cancelRequested}
              onDecision={onApprovalDecision}
              onCancel={onCancelRun}
              onGoToCanvas={onGoToCanvas}
            />
          </div>
        )}

        {!submitting && pendingDecision && (
          <div className="runtime-overlay">
            <DecisionPanel
              decision={pendingDecision}
              busy={submitting}
              cancelRequested={cancelRequested}
              onAnswer={onDecisionAnswer}
              onCancel={onCancelRun}
              onGoToCanvas={onGoToCanvas}
            />
          </div>
        )}

        {/* 取色期间隐藏 PausedState，否则遮罩会挡住 iframe，用户点不到
            目标组件；改用顶部窄横幅提示操作，画布保持可点击。 */}
        {!submitting &&
          !approval &&
          !pendingDecision &&
          clarification &&
          !referencePick && (
          <div className="runtime-overlay">
            <PausedState
              timeline={timeline}
              clarification={clarification}
              onGoToCanvas={onGoToCanvas}
              cancelRequested={cancelRequested}
              onCancel={onCancelRun}
            />
          </div>
        )}

        {referencePick && !submitting && !approval && !pendingDecision && (
          <div
            className="runtime-reference-banner"
            role="status"
            aria-live="polite"
          >
            <span className="runtime-reference-dot" aria-hidden="true" />
            <div className="runtime-reference-copy">
              <strong>请点击页面上您想参考的组件</strong>
              <span>
                {referencePick.referenceText || referencePick.comment
                  ? `已收到「${referencePick.referenceText || referencePick.comment}」，点中后提取真实类名与样式。`
                  : '点中后提取真实 Tailwind 类名与 computed 样式。'}
              </span>
            </div>
            <button
              type="button"
              className="runtime-reference-cancel"
              onClick={() => onReferenceCancel?.()}
            >
              取消
            </button>
          </div>
        )}

        {backgroundRun && (
          <div
            className="runtime-background-banner"
            role="status"
            aria-live="polite"
          >
            <span className="runtime-background-dot" aria-hidden="true" />
            <div className="runtime-background-copy">
              <strong>后台生成中，画布可以继续操作</strong>
              <span>完成后会弹 Toast 通知，无需停留在预览页。</span>
            </div>
          </div>
        )}

        {!submitting && !approval && !pendingDecision && !clarification && error && (
          <div className="runtime-overlay">
            <div className="error-state runtime-error">
              <span className="error-code">
                {result?.errorCode || 'RUN_FAILED'}
              </span>
              <h3>执行失败</h3>
              <p>{error}</p>
              {canRunInBackground ? (
                <>
                  <p className="muted">
                    当前草稿与标注已保留，可转后台继续生成；完成后会通知你。
                  </p>
                  <button
                    className="ghost-button runtime-background-action"
                    type="button"
                    onClick={onRunInBackground}
                  >
                    转后台运行
                  </button>
                </>
              ) : (
                <p className="muted">请稍后重试。</p>
              )}
            </div>
          </div>
        )}

        {!submitting && !approval && !pendingDecision && !clarification && !error && !result && (
          <div className="runtime-overlay">
            <IdleState hasAnnotation={annotations.length > 0} />
          </div>
        )}

        {!submitting &&
          !approval &&
          !pendingDecision &&
          !clarification &&
          !error &&
          result &&
          !previewUrl && (
            <div className="runtime-overlay">
              <p className="muted">没有可用的预览地址。</p>
            </div>
          )}

        {!submitting &&
          !backgroundRun &&
          !approval &&
          !pendingDecision &&
          !clarification &&
          !error &&
          previewOffline && (
          <div className="runtime-overlay">
            <div className="error-state runtime-error">
              <span className="error-code">PREVIEW_OFFLINE</span>
              <h3>预览已失效</h3>
              <p>重新执行一次即可恢复。</p>
              {onRetry ? (
                <button className="ghost-button" type="button" onClick={onRetry}>
                  重新执行 Agent
                </button>
              ) : (
                <p className="muted">请切回画布，重新发送一次修改指令。</p>
              )}
            </div>
          </div>
        )}

        {!submitting &&
          !backgroundRun &&
          !approval &&
          !pendingDecision &&
          !clarification &&
          !error &&
          result &&
          !previewOffline && (
          <div className="runtime-result-overlay">
            <div className="runtime-result-status">
              <StatusPill status={result.status} />
            </div>

            <div className="runtime-result-actions">
              {canRunInBackground && (
                <button
                  className="runtime-pill-button is-emphasis"
                  type="button"
                  onClick={onRunInBackground}
                >
                  转后台运行
                </button>
              )}
              {(diffLines > 0 || result.messages?.length > 0) && (
                <button
                  className="runtime-pill-button"
                  type="button"
                  onClick={() => setDetailsOpen(true)}
                >
                  详情 / 代码 Diff
                </button>
              )}
              {previewUrl && (
                <a
                  className="runtime-pill-button"
                  href={previewUrl}
                  target="_blank"
                  rel="noreferrer"
                >
                  新窗口 ↗
                </a>
              )}
            </div>
          </div>
        )}

        {/* 开发者状态栏：始终可见，点击右侧拉起底部详情抽屉。执行中也能
            查看模型、预览探活、沙盒 ID 与节点时间轴。 */}
        <div className="runtime-statusbar" role="status">
          <div className="runtime-statusbar-left">
            <span className="runtime-status-chip">
              模型 <strong>{modelLabel}</strong>
            </span>
            <span className={`runtime-status-chip is-${sandboxState.tone}`}>
              沙盒 <strong>{sandboxState.label}</strong>
            </span>
            <span className="runtime-status-chip">
              预览 <strong>{connection}</strong>
            </span>
            <span className="runtime-status-chip">
              标注 <strong>{annotations.length}</strong>
            </span>
            {result?.sandboxId && (
              <code className="runtime-status-id">{result.sandboxId}</code>
            )}
          </div>
          <button
            className={`runtime-statusbar-toggle ${detailsOpen ? 'is-open' : ''}`}
            type="button"
            onClick={() => setDetailsOpen((previous) => !previous)}
            aria-expanded={detailsOpen}
          >
            {detailsOpen ? '收起详情' : '开发者详情'}
            <span aria-hidden="true">⌃</span>
          </button>
        </div>

        {detailsOpen && (
          <RunDetails
            result={result}
            annotations={annotations}
            timeline={timeline}
            modelProvider={modelProvider}
            sandboxLabel={sandboxLabel}
            previewStatus={connection}
            onClose={() => setDetailsOpen(false)}
          />
        )}
      </div>

      <DeployGuideDialog
        open={deployOpen}
        onClose={() => setDeployOpen(false)}
        sandboxId={result?.sandboxId ?? ''}
      />
    </aside>
  )
}
