/**
 * 本地文档中心。
 *
 * 上传时先做“长期复用 / 一次性”语义判断：
 *  - STORE   → 直接加入文档中心；
 *  - DISCARD → 默认不加入，用户仍可手动覆盖；
 *  - ASK     → 询问用户是否加入。
 *
 * 只有 active=true 的文档（最多 3 份）会进入 Architect 的本次 RAG。
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { useAuth } from '../auth/AuthContext.jsx'
import { classifyArchitectDocument } from '../workspace/architectStream.js'
import {
  MAX_ACTIVE_DOCUMENTS,
  MAX_DOCUMENT_BYTES,
  addProjectDocument,
  loadProjectDocuments,
  removeProjectDocument,
  toggleProjectDocument,
} from '../workspace/documentCenter.js'

const REUSE_LABELS = {
  FREQUENT: '长期复用',
  ONE_OFF: '一次性',
  UNCERTAIN: '待确认',
}

function formatSize(bytes) {
  const value = Number(bytes) || 0
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

export default function ProjectDocumentCenter({
  open,
  projectId,
  provider = 'auto',
  onClose,
  onChange,
}) {
  const { session } = useAuth()
  const [docs, setDocs] = useState([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [pending, setPending] = useState(null)
  const inputRef = useRef(null)

  const refresh = () => {
    const next = loadProjectDocuments(projectId)
    setDocs(next)
    onChange?.(next)
  }

  useEffect(() => {
    if (open) refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, projectId])

  const activeCount = useMemo(
    () => docs.filter((doc) => doc.active).length,
    [docs],
  )

  if (!open) return null

  const handleFiles = async (event) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file || busy) return
    setError('')
    setNotice('')
    setPending(null)
    if (!/\.(md|markdown|txt)$/i.test(file.name)) {
      setError('仅支持 .md / .markdown / .txt')
      return
    }
    if (file.size > MAX_DOCUMENT_BYTES) {
      setError('单份文档不能超过 200KB')
      return
    }
    try {
      const content = await file.text()
      if (!content.trim()) {
        setError('文档内容为空')
        return
      }
      setBusy(true)
      const analysis = await classifyArchitectDocument({
        name: file.name,
        content,
        llmProvider: provider,
        token: session?.token,
      })
      if (analysis.decision === 'STORE') {
        const result = addProjectDocument(projectId, {
          name: file.name,
          content,
          size: file.size,
          ...analysis,
        })
        if (!result.ok) setError(result.error)
        else {
          setDocs(result.docs)
          onChange?.(result.docs)
          setNotice(`「${file.name}」已加入文档中心（${REUSE_LABELS[analysis.reuse] || '待确认'}）`)
        }
      } else if (analysis.decision === 'DISCARD') {
        setPending({ name: file.name, content, size: file.size, analysis })
        setNotice(
          `检测到一次性 / 反向语义：${analysis.reason || '默认不加入文档中心'}`,
        )
      } else {
        setPending({ name: file.name, content, size: file.size, analysis })
        setNotice(
          `未检测到明确的复用语义：${analysis.reason || '请手动决定'}`,
        )
      }
    } catch (cause) {
      setError(`读取文档失败：${cause?.message || cause}`)
    } finally {
      setBusy(false)
    }
  }

  const confirmPending = (add) => {
    if (!pending) return
    if (add) {
      const result = addProjectDocument(projectId, {
        name: pending.name,
        content: pending.content,
        size: pending.size,
        decision: 'STORE',
        reuse: pending.analysis?.reuse || 'UNCERTAIN',
        reason: pending.analysis?.reason || '用户手动确认加入',
        source: pending.analysis?.source || 'rules',
      })
      if (!result.ok) setError(result.error)
      else {
        setDocs(result.docs)
        onChange?.(result.docs)
        setNotice(`「${pending.name}」已加入文档中心`)
      }
    } else {
      setNotice(`「${pending.name}」未加入文档中心`)
    }
    setPending(null)
  }

  const handleToggle = (doc) => {
    setError('')
    const result = toggleProjectDocument(projectId, doc.id, !doc.active)
    if (!result.ok) {
      setError(result.error)
      return
    }
    setDocs(result.docs)
    onChange?.(result.docs)
  }

  const handleRemove = (doc) => {
    setError('')
    const result = removeProjectDocument(projectId, doc.id)
    if (!result.ok) {
      setError(result.error)
      return
    }
    setDocs(result.docs)
    onChange?.(result.docs)
  }

  return (
    <div
      className="project-doc-center-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label="文档中心"
    >
      <section className="project-doc-center">
        <header className="project-doc-center-head">
          <div>
            <span className="eyebrow">DOCUMENT CENTER</span>
            <h2>文档中心</h2>
            <p>
              本地保存 · 只有启用的文档（最多 {MAX_ACTIVE_DOCUMENTS} 份）进入
              Architect 的本次 RAG
            </p>
          </div>
          <button type="button" className="ghost-button" onClick={onClose}>
            关闭
          </button>
        </header>

        <div className="project-doc-center-upload">
          <label className="architect-doc-picker">
            <input
              ref={inputRef}
              type="file"
              accept=".md,.markdown,.txt"
              onChange={handleFiles}
              disabled={busy}
            />
            {busy ? '正在判断文档用途…' : '上传文档'}
          </label>
          <span>
            {activeCount}/{MAX_ACTIVE_DOCUMENTS} 启用 · 已保存 {docs.length} 份
          </span>
        </div>

        {error && <p className="architect-doc-error">{error}</p>}
        {notice && <p className="project-doc-center-notice">{notice}</p>}

        {pending && (
          <div className="project-doc-center-pending">
            <div>
              <strong>{pending.name}</strong>
              <p>{pending.analysis?.reason || '未检测到明确语义'}</p>
              <span>
                复用判断：{REUSE_LABELS[pending.analysis?.reuse] || '待确认'} ·
                来源：{pending.analysis?.source === 'llm' ? '模型' : '规则'}
              </span>
            </div>
            <div className="project-doc-center-pending-actions">
              <button
                type="button"
                className="architect-primary"
                onClick={() => confirmPending(true)}
              >
                加入文档中心
              </button>
              <button
                type="button"
                className="ghost-button"
                onClick={() => confirmPending(false)}
              >
                不加入
              </button>
            </div>
          </div>
        )}

        {docs.length === 0 ? (
          <div className="project-doc-center-empty">
            还没有文档。上传规范、需求、术语表或数据字典，Architect 会在后续
            澄清与追加需求时优先使用它们。
          </div>
        ) : (
          <div className="project-doc-center-list">
            {docs.map((doc) => (
              <article
                key={doc.id}
                className={`project-doc-center-item ${doc.active ? 'is-active' : ''}`}
              >
                <div className="project-doc-center-item-main">
                  <strong>{doc.name}</strong>
                  <p>{doc.reason || '未记录判断理由'}</p>
                  <span>
                    {formatSize(doc.size)} · {REUSE_LABELS[doc.reuse] || '待确认'} ·
                    来源：{doc.source === 'llm' ? '模型' : '规则'}
                  </span>
                </div>
                <div className="project-doc-center-item-actions">
                  <label className="project-doc-center-toggle">
                    <input
                      type="checkbox"
                      checked={doc.active}
                      onChange={() => handleToggle(doc)}
                    />
                    启用
                  </label>
                  <button
                    type="button"
                    className="ghost-button"
                    onClick={() => handleRemove(doc)}
                  >
                    删除
                  </button>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
