/**
 * “管理 Agent”面板。
 *
 * 数据来自 Supervisor 的 AgentRegistry（GET /api/v1/supervisor/agents）：
 *  - 内置 Agent 由平台维护，面板只读展示并打上锁标记，不可删除；
 *  - 企业自定义 Agent 支持新建、删除，以及 .md / .txt 知识库上传；
 *  - 新建与知识上传都直接写入 AI Core 的 config/custom_agents 目录，
 *    落盘后重扫注册中心，前端刷新即可看到最新清单。
 *
 * 面板本身不执行任何 Agent；它只做注册中心的“管理控制台”。
 */

import { useEffect, useRef, useState } from 'react'
import {
  createSupervisorAgent,
  deleteAgentKnowledge,
  deleteSupervisorAgent,
  fetchAgentKnowledge,
  uploadAgentKnowledge,
} from '../workspace/supervisorClient.js'

const EMPTY_KNOWLEDGE = {
  open: false,
  loading: false,
  error: null,
  docs: [],
  uploading: false,
  uploadError: null,
}

function isCustomAgent(agent) {
  return agent.source === 'manifest' || agent.status === 'custom'
}

function formatBytes(value) {
  if (!Number.isFinite(value) || value <= 0) return '0 B'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

function statusLabel(status) {
  if (status === 'available') return '可用'
  if (status === 'reserved') return '预留'
  if (status === 'custom') return '自定义'
  return status || '未知'
}

export default function AgentCatalogMenu({
  agents = [],
  loading = false,
  error = null,
  onRefresh,
  token,
}) {
  const [open, setOpen] = useState(false)
  // 面板内部视图：list = 管理列表，create = 新建 Agent 对话框。
  const [view, setView] = useState('list')
  const [requirement, setRequirement] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [confirmingDelete, setConfirmingDelete] = useState(null)
  const [deletingId, setDeletingId] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [knowledge, setKnowledge] = useState({})
  const rootRef = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    const handlePointerDown = (event) => {
      if (rootRef.current && !rootRef.current.contains(event.target)) {
        setOpen(false)
      }
    }
    const handleKeyDown = (event) => {
      if (event.key !== 'Escape') return
      // 优先退出新建对话框；否则关闭整个面板。
      if (view === 'create') {
        setView('list')
        return
      }
      setOpen(false)
    }
    document.addEventListener('pointerdown', handlePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [open, view])

  const builtinAgents = agents.filter((agent) => !isCustomAgent(agent))
  const customAgents = agents.filter(isCustomAgent)

  const knowledgeOf = (agentId) => knowledge[agentId] ?? EMPTY_KNOWLEDGE

  const patchKnowledge = (agentId, patch) => {
    setKnowledge((previous) => ({
      ...previous,
      [agentId]: { ...EMPTY_KNOWLEDGE, ...(previous[agentId] ?? {}), ...patch },
    }))
  }

  /** 展开 / 收起知识库；首次展开时拉取文件清单。 */
  const toggleKnowledge = async (agentId) => {
    const current = knowledgeOf(agentId)
    if (current.open) {
      patchKnowledge(agentId, { open: false })
      return
    }
    patchKnowledge(agentId, { open: true })
    if (current.docs.length === 0 && !current.loading) {
      await loadKnowledge(agentId)
    }
  }

  const loadKnowledge = async (agentId) => {
    patchKnowledge(agentId, { loading: true, error: null })
    try {
      const payload = await fetchAgentKnowledge(agentId, token)
      patchKnowledge(agentId, {
        loading: false,
        error: null,
        docs: Array.isArray(payload?.documents) ? payload.documents : [],
      })
    } catch (cause) {
      patchKnowledge(agentId, {
        loading: false,
        error: `知识库读取失败：${cause?.message || cause}`,
      })
    }
  }

  const handleUpload = async (agentId, file) => {
    if (!file) return
    patchKnowledge(agentId, { uploading: true, uploadError: null })
    try {
      const document = await uploadAgentKnowledge(agentId, file, token)
      patchKnowledge(agentId, {
        uploading: false,
        uploadError: null,
        error: null,
        open: true,
      })
      setNotice(`已把「${document?.fileName || file.name}」加入知识库`)
      await loadKnowledge(agentId)
      // Manifest 的 knowledgeFiles 元数据同步更新，刷新一次注册中心视图。
      await onRefresh?.()
    } catch (cause) {
      patchKnowledge(agentId, {
        uploading: false,
        uploadError: `上传失败：${cause?.message || cause}`,
      })
    }
  }

  const handleDeleteKnowledge = async (agentId, fileName) => {
    patchKnowledge(agentId, { error: null })
    try {
      await deleteAgentKnowledge(agentId, fileName, token)
      setNotice(`已删除知识文件「${fileName}」`)
      await loadKnowledge(agentId)
    } catch (cause) {
      patchKnowledge(agentId, {
        error: `删除失败：${cause?.message || cause}`,
      })
    }
  }

  const resetCreateForm = () => {
    setRequirement('')
    setDisplayName('')
    setCreateError(null)
  }

  const handleCreate = async (event) => {
    event.preventDefault()
    const text = requirement.trim()
    if (text.length < 4) {
      setCreateError('请至少输入 4 个字的需求描述')
      return
    }
    setCreating(true)
    setCreateError(null)
    try {
      const created = await createSupervisorAgent(
        {
          requirement: text,
          displayName: displayName.trim() || undefined,
        },
        token,
      )
      resetCreateForm()
      setView('list')
      setNotice(
        `已创建 Agent「${created?.displayName || created?.agentId || '自定义 Agent'}」`,
      )
      // AgentBuilder 已重扫注册中心，这里再刷新前端清单。
      await onRefresh?.()
    } catch (cause) {
      setCreateError(`创建失败：${cause?.message || cause}`)
    } finally {
      setCreating(false)
    }
  }

  const handleDeleteAgent = async (agentId) => {
    setDeletingId(agentId)
    setActionError(null)
    try {
      await deleteSupervisorAgent(agentId, token)
      setConfirmingDelete(null)
      setNotice(`已删除 Agent「${agentId}」`)
      setKnowledge((previous) => {
        const next = { ...previous }
        delete next[agentId]
        return next
      })
      await onRefresh?.()
    } catch (cause) {
      setActionError(`删除失败：${cause?.message || cause}`)
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <div className="agent-catalog" ref={rootRef} data-agent-catalog>
      <button
        type="button"
        className={`agent-catalog-trigger ${open ? 'is-open' : ''}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span aria-hidden="true">✨</span>
        管理 Agent
        <span className="agent-catalog-count" data-agent-count>
          {agents.length}
        </span>
      </button>

      {open && (
        <div className="agent-catalog-popover" role="dialog" aria-label="管理 Agent">
          <div className="agent-catalog-head">
            <div>
              <strong>{view === 'create' ? '新建 Agent' : '管理 Agent'}</strong>
              <span>
                {view === 'create'
                  ? '描述职责，AgentBuilder 会生成 Manifest 与 System Prompt'
                  : '内置 Agent 锁定保护 · 自定义 Agent 可创建 / 删除'}
              </span>
            </div>
            {view === 'list' ? (
              <div className="agent-catalog-head-actions">
                <button
                  type="button"
                  className="agent-catalog-refresh"
                  onClick={() => onRefresh?.()}
                  disabled={loading}
                >
                  {loading ? '刷新中…' : '刷新'}
                </button>
                <button
                  type="button"
                  className="agent-catalog-create"
                  data-agent-create
                  onClick={() => {
                    resetCreateForm()
                    setNotice(null)
                    setView('create')
                  }}
                >
                  ＋ 新建 Agent
                </button>
              </div>
            ) : (
              <button
                type="button"
                className="agent-catalog-refresh"
                onClick={() => setView('list')}
              >
                返回列表
              </button>
            )}
          </div>

          {view === 'create' ? (
            <form className="agent-create-form" onSubmit={handleCreate}>
              <label>
                <span>Agent 名称（可选）</span>
                <input
                  type="text"
                  value={displayName}
                  maxLength={128}
                  placeholder="例如：合同审查助手"
                  onChange={(event) => setDisplayName(event.target.value)}
                  disabled={creating}
                />
              </label>
              <label>
                <span>职责与需求</span>
                <textarea
                  value={requirement}
                  maxLength={4000}
                  rows={6}
                  placeholder="描述这个 Agent 要做什么、处理哪些输入、输出什么结果。例如：审查采购合同中的付款条款与违约责任，输出风险清单与修改建议。"
                  onChange={(event) => setRequirement(event.target.value)}
                  disabled={creating}
                  autoFocus
                />
                <em>{requirement.trim().length} / 4000</em>
              </label>
              {createError && (
                <p className="agent-catalog-state is-error" role="alert">
                  {createError}
                </p>
              )}
              <div className="agent-create-actions">
                <button
                  type="submit"
                  className="agent-create-submit"
                  disabled={creating || requirement.trim().length < 4}
                >
                  {creating ? '生成中…' : '生成 Agent'}
                </button>
                <button
                  type="button"
                  className="agent-create-cancel"
                  onClick={() => {
                    resetCreateForm()
                    setView('list')
                  }}
                  disabled={creating}
                >
                  取消
                </button>
              </div>
            </form>
          ) : (
            <>
              {error && (
                <p className="agent-catalog-state is-error" role="alert">
                  {error}
                </p>
              )}
              {actionError && (
                <p className="agent-catalog-state is-error" role="alert">
                  {actionError}
                </p>
              )}
              {notice && (
                <p className="agent-catalog-state is-success" role="status">
                  {notice}
                </p>
              )}
              {!error && loading && (
                <p className="agent-catalog-state">
                  <span className="mini-spinner" aria-hidden="true" /> 正在读取
                  AgentRegistry…
                </p>
              )}

              <section className="agent-catalog-section">
                <h4>
                  内置 Agent
                  <span className="agent-catalog-section-count">
                    {builtinAgents.length}
                  </span>
                </h4>
                <div className="agent-catalog-list">
                  {builtinAgents.map((agent) => (
                    <article
                      className="agent-catalog-item is-locked"
                      key={agent.agentId}
                      data-builtin-agent-id={agent.agentId}
                    >
                      <div className="agent-catalog-item-head">
                        <strong>{agent.displayName}</strong>
                        <span
                          className="agent-catalog-lock"
                          title="内置 Agent 由平台维护，不可删除"
                        >
                          🔒 内置
                        </span>
                      </div>
                      <p>{agent.description || '平台内置能力'}</p>
                      <div className="agent-catalog-meta">
                        <span>
                          状态 <code>{statusLabel(agent.status)}</code>
                        </span>
                        <span>
                          能力{' '}
                          <code>
                            {(agent.taskTypes ?? []).join(' · ') || '—'}
                          </code>
                        </span>
                      </div>
                    </article>
                  ))}
                </div>
              </section>

              <section className="agent-catalog-section">
                <h4>
                  自定义 Agent
                  <span className="agent-catalog-section-count">
                    {customAgents.length}
                  </span>
                </h4>
                {customAgents.length === 0 ? (
                  <p className="agent-catalog-state">
                    还没有自定义 Agent。点击右上角「新建 Agent」，用一句需求即可生成。
                  </p>
                ) : (
                  <div className="agent-catalog-list">
                    {customAgents.map((agent) => {
                      const kb = knowledgeOf(agent.agentId)
                      return (
                        <article
                          className="agent-catalog-item is-custom"
                          key={agent.agentId}
                          data-custom-agent-id={agent.agentId}
                        >
                          <div className="agent-catalog-item-head">
                            <strong>{agent.displayName}</strong>
                            <span className="agent-catalog-tag">自定义</span>
                          </div>
                          <p>{agent.description || '企业自定义能力'}</p>
                          <div className="agent-catalog-meta">
                            <span>
                              能力{' '}
                              <code>
                                {(agent.taskTypes ?? []).join(' · ') || '—'}
                              </code>
                            </span>
                          </div>

                          <div className="agent-catalog-actions">
                            <button
                              type="button"
                              className="agent-knowledge-toggle"
                              onClick={() => toggleKnowledge(agent.agentId)}
                              aria-expanded={kb.open}
                            >
                              📚 知识库
                              {kb.docs.length > 0 ? ` (${kb.docs.length})` : ''}
                            </button>
                            {confirmingDelete === agent.agentId ? (
                              <>
                                <button
                                  type="button"
                                  className="agent-delete-confirm"
                                  onClick={() =>
                                    handleDeleteAgent(agent.agentId)
                                  }
                                  disabled={deletingId === agent.agentId}
                                >
                                  {deletingId === agent.agentId
                                    ? '删除中…'
                                    : '确认删除'}
                                </button>
                                <button
                                  type="button"
                                  className="agent-delete-cancel"
                                  onClick={() => setConfirmingDelete(null)}
                                  disabled={deletingId === agent.agentId}
                                >
                                  取消
                                </button>
                              </>
                            ) : (
                              <button
                                type="button"
                                className="agent-delete"
                                onClick={() =>
                                  setConfirmingDelete(agent.agentId)
                                }
                              >
                                删除 Agent
                              </button>
                            )}
                          </div>

                          {kb.open && (
                            <div className="agent-knowledge-panel">
                              <div className="agent-knowledge-head">
                                <span>
                                  上传 .md / .txt 后自动加入该 Agent 的 RAG 目录
                                </span>
                                <label
                                  className={`agent-knowledge-upload ${kb.uploading ? 'is-busy' : ''}`}
                                >
                                  {kb.uploading ? '上传中…' : '上传文件'}
                                  <input
                                    type="file"
                                    accept=".md,.txt,text/markdown,text/plain"
                                    disabled={kb.uploading}
                                    onChange={(event) => {
                                      const file = event.target.files?.[0]
                                      event.target.value = ''
                                      void handleUpload(agent.agentId, file)
                                    }}
                                  />
                                </label>
                              </div>
                              {kb.uploadError && (
                                <p
                                  className="agent-catalog-state is-error"
                                  role="alert"
                                >
                                  {kb.uploadError}
                                </p>
                              )}
                              {kb.loading && (
                                <p className="agent-catalog-state">
                                  <span
                                    className="mini-spinner"
                                    aria-hidden="true"
                                  />{' '}
                                  正在读取知识文件…
                                </p>
                              )}
                              {kb.error && (
                                <p
                                  className="agent-catalog-state is-error"
                                  role="alert"
                                >
                                  {kb.error}
                                </p>
                              )}
                              {!kb.loading && !kb.error && kb.docs.length === 0 && (
                                <p className="agent-catalog-state">
                                  暂无知识文件。支持 Markdown 与纯文本。
                                </p>
                              )}
                              {kb.docs.length > 0 && (
                                <ul className="agent-knowledge-list">
                                  {kb.docs.map((document) => (
                                    <li key={document.fileName}>
                                      <span
                                        className="agent-knowledge-name"
                                        title={document.fileName}
                                      >
                                        {document.fileName}
                                      </span>
                                      <span className="agent-knowledge-size">
                                        {formatBytes(document.sizeBytes)}
                                      </span>
                                      <button
                                        type="button"
                                        className="agent-knowledge-delete"
                                        onClick={() =>
                                          handleDeleteKnowledge(
                                            agent.agentId,
                                            document.fileName,
                                          )
                                        }
                                      >
                                        删除
                                      </button>
                                    </li>
                                  ))}
                                </ul>
                              )}
                            </div>
                          )}
                        </article>
                      )
                    })}
                  </div>
                )}
              </section>

              <p className="agent-catalog-foot">
                内置 Agent 由平台维护且不可删除；自定义 Agent 的
                Manifest、Prompt 与知识文件全部落盘在
                <code> config/custom_agents/</code>，随注册中心热更新。
              </p>
            </>
          )}
        </div>
      )}
    </div>
  )
}
