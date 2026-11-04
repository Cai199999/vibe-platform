import { useEffect, useState } from 'react'
import DiffView from './DiffView.jsx'
import ScaledPreview from './ScaledPreview.jsx'

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
  const isSuccess = status === 'AWAITING_APPROVAL'
  return (
    <span className={`status-pill ${isSuccess ? 'is-success' : 'is-failed'}`}>
      <i />
      {isSuccess ? '已完成' : '执行失败'}
    </span>
  )
}

function LoadingState({ elapsed, timeline }) {
  return (
    <div className="loading-state runtime-loading is-timeline">
      <div className="loading-copy timeline-head">
        <h3 className="loading-title">Agent 正在执行</h3>
        <p className="muted">{elapsed < 1 ? '<1' : elapsed}s</p>
      </div>
      <Timeline items={timeline} />
    </div>
  )
}

function PausedState({ timeline, clarification, onGoToCanvas }) {
  return (
    <div className="loading-state runtime-loading is-paused">
      <div className="loading-copy timeline-head">
        <h3 className="loading-title">等待你确认</h3>
        <p className="muted">意图还不够明确，Agent 已暂停执行。</p>
      </div>
      <Timeline items={timeline} />
      {clarification?.questions?.length > 0 && (
        <ul className="paused-questions">
          {clarification.questions.map((question) => (
            <li key={question.questionId}>{question.prompt}</li>
          ))}
        </ul>
      )}
      {onGoToCanvas && (
        <button
          className="ghost-button paused-action"
          type="button"
          onClick={onGoToCanvas}
        >
          去画布回答
        </button>
      )}
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
        <strong>还没有预览</strong>
        <p>{hasAnnotation ? '填写修改意图后提交。' : '圈选一个组件开始。'}</p>
      </div>
    </div>
  )
}

function RunDetails({ result, onClose }) {
  const verify = result.verifyResult
  const diffLines = result.generatedPatch ? result.generatedPatch.split('\n').length : 0

  return (
    <div className="runtime-drawer" role="dialog" aria-label="Run 执行详情">
      <div className="runtime-drawer-head">
        <div>
          <span className="eyebrow">Run Detail</span>
          <strong>执行详情</strong>
        </div>
        <button className="ghost-button" type="button" onClick={onClose}>
          关闭
        </button>
      </div>

      <div className="runtime-drawer-body">
        <div className="runtime-meta">
          <span>
            Run <code>{result.runId}</code>
          </span>
          <span>
            构建 <code>{verify?.durationMs ? `${verify.durationMs} ms` : '—'}</code>
          </span>
          <span>
            退出码 <code>{verify ? verify.exitCode : '—'}</code>
          </span>
          <span>
            沙盒 <code>{result.sandboxId}</code>
          </span>
        </div>

        {result.error && <div className="error-box">{result.error}</div>}

        {diffLines > 0 && (
          <details className="runtime-fold" open>
            <summary>
              查看本次生成的代码 Diff
              <span>{diffLines} 行</span>
            </summary>
            <DiffView patch={result.generatedPatch} title="Generated Patch" />
          </details>
        )}

        {!result.status?.includes('APPROVAL') && verify?.errorLogs && (
          <details className="runtime-fold">
            <summary>最终构建日志</summary>
            <pre className="log-view">{verify.errorLogs}</pre>
          </details>
        )}

        {result.messages?.length > 0 && (
          <details className="runtime-fold">
            <summary>
              Agent 执行轨迹
              <span>{result.messages.length} 条</span>
            </summary>
            <ol className="trace-list">
              {result.messages.map((message, index) => (
                <li key={index} className={message.level === 'error' ? 'is-error' : ''}>
                  <span className="trace-node">{message.node}</span>
                  <p>{message.content}</p>
                </li>
              ))}
            </ol>
          </details>
        )}
      </div>
    </div>
  )
}

/**
 * 预览 URL 存活探测。
 *
 * 沙盒容器可能在页面刷新、服务重启或清理后被销毁，但 sessionStorage 里仍
 * 保留旧的 previewUrl。此时 iframe 只会显示浏览器自己的“连接失败”页面。
 * 用 no-cors 请求探活：能连上就返回 opaque response，连接拒绝会 reject。
 */
function usePreviewConnection(previewUrl, active) {
  const [status, setStatus] = useState('idle')

  useEffect(() => {
    if (!previewUrl || !active) {
      setStatus('idle')
      return undefined
    }

    let cancelled = false
    const controller = new AbortController()
    // 端口被防火墙丢包时 fetch 可能长时间挂起，4s 后强制判定失联。
    const timeout = window.setTimeout(() => controller.abort(), 4000)
    setStatus('checking')

    const probe = async () => {
      for (let attempt = 0; attempt < 2; attempt += 1) {
        try {
          await fetch(previewUrl, {
            mode: 'no-cors',
            cache: 'no-store',
            signal: controller.signal,
          })
          window.clearTimeout(timeout)
          if (!cancelled) setStatus('online')
          return
        } catch {
          if (cancelled) return
          if (attempt === 0) {
            // Vite 刚启动时可能有一瞬间不可达，重试一次再判定失败。
            await new Promise((resolve) => setTimeout(resolve, 400))
          }
        }
      }
      window.clearTimeout(timeout)
      if (!cancelled) setStatus('offline')
    }

    void probe()
    return () => {
      cancelled = true
      window.clearTimeout(timeout)
      controller.abort()
    }
  }, [previewUrl, active])

  return status
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
  expanded = false,
  visible = true,
  onRetry,
  onGoToCanvas,
}) {
  const [detailsOpen, setDetailsOpen] = useState(false)
  const {
    submitting,
    elapsed,
    result,
    error,
    annotations = [],
    timeline = [],
    clarification = null,
  } = run
  const previewUrl = result?.previewUrl
  const success = result?.status === 'AWAITING_APPROVAL'
  const diffLines = result?.generatedPatch ? result.generatedPatch.split('\n').length : 0
  const connection = usePreviewConnection(previewUrl, visible && !submitting)
  const previewOffline = connection === 'offline'

  let stateLabel = 'IDLE'
  let stateClass = 'is-idle'
  if (clarification) {
    stateLabel = 'PAUSED · 待确认'
    stateClass = 'is-paused'
  } else if (submitting) {
    stateLabel = `RUNNING · ${elapsed < 1 ? '<1' : elapsed}s`
    stateClass = 'is-running'
  } else if (error) {
    stateLabel = 'FAILED'
    stateClass = 'is-failed'
  } else if (result) {
    stateLabel = previewOffline ? 'OFFLINE' : success ? 'READY' : 'FAILED'
    stateClass = previewOffline ? 'is-failed' : success ? 'is-ready' : 'is-failed'
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
      </div>

      <div className="runtime-stage">
        {/* iframe 一旦挂载就保留到下一次 Run 或关闭 Tab，切换视图只改 display。 */}
        {previewUrl && (
          <ScaledPreview
            src={previewUrl}
            title={`${projectName} 沙盒预览`}
            visible={visible && !submitting}
          />
        )}

        {submitting && (
          <div className="runtime-overlay">
            <LoadingState elapsed={elapsed} timeline={timeline} />
          </div>
        )}

        {!submitting && clarification && (
          <div className="runtime-overlay">
            <PausedState
              timeline={timeline}
              clarification={clarification}
              onGoToCanvas={onGoToCanvas}
            />
          </div>
        )}

        {!submitting && !clarification && error && (
          <div className="runtime-overlay">
            <div className="error-state runtime-error">
              <span className="error-code">RUN_FAILED</span>
              <h3>执行失败</h3>
              <p>{error}</p>
              <p className="muted">请稍后重试。</p>
            </div>
          </div>
        )}

        {!submitting && !clarification && !error && !result && (
          <div className="runtime-overlay">
            <IdleState hasAnnotation={annotations.length > 0} />
          </div>
        )}

        {!submitting && !clarification && !error && result && !previewUrl && (
          <div className="runtime-overlay">
            <p className="muted">没有可用的预览地址。</p>
          </div>
        )}

        {!submitting && !clarification && !error && previewOffline && (
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

        {!submitting && !clarification && !error && result && !previewOffline && (
          <div className="runtime-result-overlay">
            <div className="runtime-result-status">
              <StatusPill status={result.status} />
            </div>

            <div className="runtime-result-actions">
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

        {detailsOpen && result && (
          <RunDetails result={result} onClose={() => setDetailsOpen(false)} />
        )}
      </div>
    </aside>
  )
}
