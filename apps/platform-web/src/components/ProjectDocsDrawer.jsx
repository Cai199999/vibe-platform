/**
 * 项目方案文档抽屉（模拟文档）。
 *
 * 只展示由当前契约 / VisualDocument 投影出的 01–05 文档。
 * 不写回、不让 Agent 直接编辑；后续正式文档生成器可复用同一结构。
 */

import { useEffect, useMemo, useState } from 'react'
import { docsToMarkdown } from '../workspace/projectDocs.js'

function downloadText(fileName, content) {
  const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = fileName
  anchor.click()
  URL.revokeObjectURL(url)
}

export default function ProjectDocsDrawer({ open, docs = [], onClose }) {
  const [activeKind, setActiveKind] = useState(docs[0]?.kind || '')
  const activeDoc =
    docs.find((doc) => doc.kind === activeKind) || docs[0] || null
  const markdown = useMemo(() => docsToMarkdown(docs), [docs])

  useEffect(() => {
    if (docs.length > 0 && !docs.some((doc) => doc.kind === activeKind)) {
      setActiveKind(docs[0].kind)
    }
  }, [activeKind, docs])

  if (!open) return null

  return (
    <div className="project-docs-backdrop" role="dialog" aria-modal="true">
      <div className="project-docs-shell">
        <header className="project-docs-head">
          <div>
            <span className="eyebrow">Project Documents</span>
            <h2>方案文档</h2>
          </div>
          <div className="project-docs-actions">
            <button
              type="button"
              className="ghost-button"
              disabled={!activeDoc}
              onClick={() => {
                if (!activeDoc) return
                downloadText(
                  `${activeDoc.kind}.md`,
                  docsToMarkdown([activeDoc]),
                )
              }}
            >
              导出当前文档
            </button>
            <button
              type="button"
              className="ghost-button"
              disabled={!markdown}
              onClick={() => downloadText('项目方案文档.md', markdown)}
            >
              导出全部
            </button>
            <button type="button" className="ghost-button" onClick={onClose}>
              关闭
            </button>
          </div>
        </header>

        <div className="project-docs-banner" role="status">
          模拟文档 · 由当前契约与 VisualDocument 自动生成，仅供查看；
          不作为第二事实源，也不会被 Agent 直接修改。
        </div>

        {docs.length === 0 ? (
          <div className="project-docs-empty">契约尚未生成，暂无方案文档。</div>
        ) : (
          <div className="project-docs-layout">
            <nav className="project-docs-tabs" aria-label="方案文档">
              {docs.map((doc) => (
                <button
                  key={doc.kind}
                  type="button"
                  aria-selected={doc.kind === activeDoc?.kind}
                  className={doc.kind === activeDoc?.kind ? 'is-selected' : ''}
                  onClick={() => setActiveKind(doc.kind)}
                >
                  <span>{doc.kind}</span>
                  <em>{doc.status}</em>
                </button>
              ))}
            </nav>
            <article className="project-docs-content">
              {activeDoc && (
                <>
                  <div className="project-docs-meta">
                    <span>{activeDoc.docId}</span>
                    <span>revision r{activeDoc.revision}</span>
                    <span>{activeDoc.status}</span>
                    {activeDoc.contentHash && (
                      <span title={activeDoc.contentHash}>
                        {String(activeDoc.contentHash).slice(0, 24)}…
                      </span>
                    )}
                  </div>
                  <h1>{activeDoc.title}</h1>
                  {activeDoc.sections.map((section) => (
                    <section key={section.title}>
                      <h3>{section.title}</h3>
                      <pre className="project-docs-body">{section.body}</pre>
                    </section>
                  ))}
                </>
              )}
            </article>
          </div>
        )}
      </div>
    </div>
  )
}
