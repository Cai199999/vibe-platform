import { useState } from 'react'

/**
 * 代码 Diff 展示。抽出为独立组件，供每个 Tab 的 Runtime 面板复用。
 */
export default function DiffView({ patch, title = '生成的代码 Diff' }) {
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
        <h3>{title}</h3>
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
