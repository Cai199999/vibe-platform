import { useState } from 'react'

function StatusPill({ status }) {
  const isSuccess = status === 'AWAITING_APPROVAL'
  return (
    <span className={`status-pill ${isSuccess ? 'is-success' : 'is-failed'}`}>
      <i />
      {isSuccess ? '等待审批 · 构建通过' : '执行失败'}
    </span>
  )
}

function DiffView({ patch }) {
  const [copied, setCopied] = useState(false)
  const lines = patch ? patch.split('\n') : []

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(patch)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1600)
    } catch {
      setCopied(false)
    }
  }

  const lineClass = (line) => {
    if (line.startsWith('+++') || line.startsWith('---')) return 'meta'
    if (line.startsWith('@@')) return 'hunk'
    if (line.startsWith('+')) return 'add'
    if (line.startsWith('-')) return 'del'
    return ''
  }

  return (
    <section className="panel-section">
      <div className="section-head">
        <h3>生成的代码 Diff</h3>
        {patch && (
          <button className="ghost-button" type="button" onClick={copy}>
            {copied ? '已复制' : '复制'}
          </button>
        )}
      </div>
      {lines.length ? (
        <pre className="diff-view">
          {lines.map((line, index) => (
            <span key={`${index}-${line}`} className={`diff-line ${lineClass(line)}`}>
              <span className="diff-no">{index + 1}</span>
              <span className="diff-text">{line || ' '}</span>
            </span>
          ))}
        </pre>
      ) : (
        <p className="muted">本次没有生成代码补丁。</p>
      )}
    </section>
  )
}

function LoadingState({ stages, stageIndex, elapsed }) {
  return (
    <div className="loading-state">
      <div className="orbit" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
      <div>
        <h3 className="loading-title">Agent 正在执行</h3>
        <p className="muted">
          同步链路已打通，正在等待沙盒返回结果 · {elapsed < 1 ? '<1' : elapsed}s
        </p>
      </div>

      <ol className="timeline">
        {stages.map((stage, index) => {
          const state = index < stageIndex ? 'done' : index === stageIndex ? 'active' : ''
          return (
            <li key={stage.title} className={state}>
              <span className="timeline-dot" />
              <div>
                <strong>{stage.title}</strong>
                <small>{stage.detail}</small>
              </div>
            </li>
          )
        })}
      </ol>

      <div className="progress-track">
        <span />
      </div>
    </div>
  )
}

function IdleState({ hasAnnotation }) {
  return (
    <div className="idle-state">
      <div className="idle-mark">◎</div>
      <h3>{hasAnnotation ? '标注已就绪' : '先在画布上圈一个组件'}</h3>
      <p>
        {hasAnnotation
          ? '在画布浮层内写下修改意图，Enter 发送。Agent 会创建隔离沙盒、生成代码并运行构建。'
          : '在画布上拖拽圈出组件，浮层会自动出现。输入“改颜色”并发送给 Agent。'}
      </p>
      <ol className="flow-list">
        <li>Planner 解析标注，生成修改计划</li>
        <li>Coder 输出 Diff / 全量文件</li>
        <li>Applier 写入沙盒，Verifier 执行构建</li>
        <li>失败自动回灌日志，最多修复 3 次</li>
      </ol>
    </div>
  )
}

function ResultState({ result }) {
  const success = result.status === 'AWAITING_APPROVAL'
  const verify = result.verifyResult

  return (
    <div className="result-state">
      <div className="result-head">
        <StatusPill status={result.status} />
        <span className="elapsed">{result.retryCount > 0 ? `自动修复 ${result.retryCount} 次` : '一次通过'}</span>
      </div>

      <div className="meta-grid">
        <div>
          <span>Run</span>
          <code>{result.runId}</code>
        </div>
        <div>
          <span>构建耗时</span>
          <code>{verify?.durationMs ? `${verify.durationMs} ms` : '—'}</code>
        </div>
        <div>
          <span>退出码</span>
          <code>{verify ? verify.exitCode : '—'}</code>
        </div>
        <div>
          <span>沙盒</span>
          <code>{result.sandboxId}</code>
        </div>
      </div>

      {result.error && <div className="error-box">{result.error}</div>}

      <section className="panel-section">
        <div className="section-head">
          <h3>沙盒实时预览</h3>
          {result.previewUrl && (
            <a className="ghost-button" href={result.previewUrl} target="_blank" rel="noreferrer">
              新窗口打开
            </a>
          )}
        </div>
        {result.previewUrl ? (
          <div className="preview-frame-wrap">
            <iframe
              className="preview-frame"
              src={result.previewUrl}
              title="Sandbox preview"
              sandbox="allow-scripts allow-same-origin allow-forms allow-modals"
            />
            <span className="preview-url">{result.previewUrl}</span>
          </div>
        ) : (
          <p className="muted">没有可用的预览地址。</p>
        )}
      </section>

      <DiffView patch={result.generatedPatch} />

      {!success && verify?.errorLogs && (
        <section className="panel-section">
          <div className="section-head">
            <h3>最终构建日志</h3>
          </div>
          <pre className="log-view">{verify.errorLogs}</pre>
        </section>
      )}

      {result.messages?.length > 0 && (
        <details className="trace-details">
          <summary>Agent 执行轨迹（{result.messages.length} 条）</summary>
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
  )
}

/**
 * 右侧 Agent 执行面板：空闲 / Loading / 结果三态。
 */
export default function RunPanel({ submitting, stages, stageIndex, elapsed, result, error, hasAnnotation }) {
  return (
    <aside className="run-panel">
      <div className="panel-head">
        <span className="eyebrow">Agent Runtime</span>
        <h2>执行状态</h2>
        <p>画布标注 → Java BFF → Python AI Core → Docker 沙盒</p>
      </div>

      {submitting && <LoadingState stages={stages} stageIndex={stageIndex} elapsed={elapsed} />}

      {!submitting && error && (
        <div className="error-state">
          <span className="error-code">RUN_FAILED</span>
          <h3>链路执行失败</h3>
          <p>{error}</p>
          <p className="muted">检查 Java BFF / Python AI Core 容器状态后重试。</p>
        </div>
      )}

      {!submitting && !error && result && <ResultState result={result} />}

      {!submitting && !error && !result && <IdleState hasAnnotation={hasAnnotation} />}
    </aside>
  )
}
