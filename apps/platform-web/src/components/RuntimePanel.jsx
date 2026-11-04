import { useState } from 'react'
import DiffView from './DiffView.jsx'
import ScaledPreview from './ScaledPreview.jsx'

/**
 * 同步链路没有真实节点事件，M1 用时间轴模拟进度，避免界面看起来卡死。
 * M2 接入事件流后，这里改为消费真实 Agent 状态。
 */
const STAGES = [
  { at: 0, title: '正在解析画布标注', detail: '归一化坐标 → 组件选区 → AnnotationEnvelope' },
  { at: 3, title: 'Planner 正在规划修改方案', detail: '理解“改颜色”的目标组件与验收条件' },
  { at: 6, title: 'Coder 正在生成代码', detail: '输出 Unified Diff / 全量文件' },
  { at: 9, title: 'Applier 正在写入沙盒', detail: 'git apply + Vite HMR 热更新' },
  { at: 12, title: 'Verifier 正在运行构建', detail: 'npm run build 收集错误日志' },
  { at: 16, title: '遇到报错，正在尝试自我修复（第 1 次）', detail: '错误日志回灌 Coder，重新生成补丁' },
  { at: 22, title: '再次验证中', detail: '等待沙盒返回最终结果' },
]

const stageIndexFor = (elapsed) => {
  let index = 0
  STAGES.forEach((stage, i) => {
    if (elapsed >= stage.at) index = i
  })
  return index
}

function StatusPill({ status }) {
  const isSuccess = status === 'AWAITING_APPROVAL'
  return (
    <span className={`status-pill ${isSuccess ? 'is-success' : 'is-failed'}`}>
      <i />
      {isSuccess ? '等待审批 · 构建通过' : '执行失败'}
    </span>
  )
}

function LoadingState({ elapsed }) {
  const stageIndex = stageIndexFor(elapsed)
  const stage = STAGES[stageIndex]

  return (
    <div className="loading-state runtime-loading">
      <div className="orbit" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
      <div className="loading-copy">
        <h3 className="loading-title">Agent 正在执行</h3>
        <p className="muted">
          同步链路已打通，正在等待沙盒返回结果 · {elapsed < 1 ? '<1' : elapsed}s
        </p>
        <p className="loading-step">
          <span>
            {stageIndex + 1} / {STAGES.length}
          </span>
          {stage.title}
        </p>
        <p className="loading-detail">{stage.detail}</p>
      </div>

      <div className="stage-dots" aria-hidden="true">
        {STAGES.map((item, index) => (
          <i
            key={item.title}
            className={index < stageIndex ? 'is-done' : index === stageIndex ? 'is-active' : ''}
          />
        ))}
      </div>

      <div className="progress-track">
        <span />
      </div>
    </div>
  )
}

function IdleState({ hasAnnotation, projectName }) {
  return (
    <div className="runtime-idle">
      <div className="runtime-placeholder">
        <div className="placeholder-bars" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
        <strong>沙盒预览将在这里出现</strong>
        <p>
          {hasAnnotation
            ? '标注已就绪，在画布浮层中写下修改意图并发送。'
            : `在「${projectName}」的画布上圈选组件，浮层会自动出现。`}
        </p>
      </div>

      <ol className="flow-list">
        <li>Planner 解析标注，生成修改计划</li>
        <li>Coder 输出 Diff / 全量文件</li>
        <li>Applier 写入沙盒，Verifier 执行构建</li>
        <li>失败自动回灌日志，最多修复 3 次</li>
      </ol>
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
 * 每个 Tab 独立的“沙盒屏幕”。
 *
 * 结构固定为「chrome 顶栏 + 全出血预览画布」：
 *  - chrome 与 CanvasStage 的浏览器栏等高，双屏模式左右严格对齐；
 *  - 预览内容使用 ScaledPreview 等比缩放，外层不产生滚动条；
 *  - 状态、详情与 Diff 以浮层 / 抽屉覆盖在画布上，不挤压 iframe 尺寸。
 */
export default function RuntimePanel({ run, projectName, expanded = false, visible = true }) {
  const [detailsOpen, setDetailsOpen] = useState(false)
  const { submitting, elapsed, result, error, annotation } = run
  const previewUrl = result?.previewUrl
  const success = result?.status === 'AWAITING_APPROVAL'
  const diffLines = result?.generatedPatch ? result.generatedPatch.split('\n').length : 0

  let stateLabel = 'IDLE'
  let stateClass = 'is-idle'
  if (submitting) {
    stateLabel = `RUNNING · ${elapsed < 1 ? '<1' : elapsed}s`
    stateClass = 'is-running'
  } else if (error) {
    stateLabel = 'FAILED'
    stateClass = 'is-failed'
  } else if (result) {
    stateLabel = success ? 'READY' : 'FAILED'
    stateClass = success ? 'is-ready' : 'is-failed'
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
        <span className="runtime-url" title={previewUrl ?? 'vibe://sandbox/preview'}>
          {previewUrl ?? 'vibe://sandbox/preview'}
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
            <LoadingState elapsed={elapsed} />
          </div>
        )}

        {!submitting && error && (
          <div className="runtime-overlay">
            <div className="error-state runtime-error">
              <span className="error-code">RUN_FAILED</span>
              <h3>链路执行失败</h3>
              <p>{error}</p>
              <p className="muted">检查 Java BFF / Python AI Core 容器状态后重试。</p>
            </div>
          </div>
        )}

        {!submitting && !error && !result && (
          <div className="runtime-overlay">
            <IdleState hasAnnotation={Boolean(annotation)} projectName={projectName} />
          </div>
        )}

        {!submitting && !error && result && !previewUrl && (
          <div className="runtime-overlay">
            <p className="muted">没有可用的预览地址。</p>
          </div>
        )}

        {!submitting && !error && result && (
          <div className="runtime-result-overlay">
            <div className="runtime-result-status">
              <StatusPill status={result.status} />
              <span className="elapsed">
                {result.retryCount > 0 ? `自动修复 ${result.retryCount} 次` : '一次通过'}
              </span>
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
