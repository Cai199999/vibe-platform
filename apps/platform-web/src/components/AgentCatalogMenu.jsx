/**
 * “我的专属 Agent”目录。
 *
 * 数据来自 Supervisor 的 AgentRegistry（GET /api/v1/supervisor/agents）：
 * 只展示 source=manifest / status=custom 的企业 Agent，内置 Agent 不在此列。
 * 当前 CustomAgent 只完成声明与路由，运行时尚在 M3，因此菜单明确标注
 * “仅路由”，避免用户误以为已经执行代码。
 */

import { useEffect, useRef, useState } from 'react'

export default function AgentCatalogMenu({
  agents = [],
  loading = false,
  error = null,
  onRefresh,
}) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    const handlePointerDown = (event) => {
      if (rootRef.current && !rootRef.current.contains(event.target)) {
        setOpen(false)
      }
    }
    const handleKeyDown = (event) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', handlePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [open])

  const customAgents = agents.filter(
    (agent) => agent.source === 'manifest' || agent.status === 'custom',
  )

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
        我的专属 Agent
        <span className="agent-catalog-count" data-custom-agent-count>
          {customAgents.length}
        </span>
      </button>

      {open && (
        <div className="agent-catalog-popover" role="dialog" aria-label="我的专属 Agent">
          <div className="agent-catalog-head">
            <div>
              <strong>我的专属 Agent</strong>
              <span>来自 config/custom_agents 的声明式 Manifest</span>
            </div>
            <button
              type="button"
              className="agent-catalog-refresh"
              onClick={() => onRefresh?.()}
              disabled={loading}
            >
              {loading ? '刷新中…' : '刷新'}
            </button>
          </div>

          {error && <p className="agent-catalog-state is-error">{error}</p>}
          {!error && loading && (
            <p className="agent-catalog-state">
              <span className="mini-spinner" aria-hidden="true" /> 正在读取 AgentRegistry…
            </p>
          )}
          {!error && !loading && customAgents.length === 0 && (
            <p className="agent-catalog-state">
              还没有专属 Agent。把一个 <code>*.json</code> Manifest 放进
              <code> config/custom_agents/</code> 后点击刷新即可秒级接入。
            </p>
          )}

          <div className="agent-catalog-list">
            {customAgents.map((agent) => (
              <article
                className="agent-catalog-item"
                key={agent.agentId}
                data-custom-agent-id={agent.agentId}
              >
                <div className="agent-catalog-item-head">
                  <strong>{agent.displayName}</strong>
                  <span className="agent-catalog-tag">仅路由 · M3 执行</span>
                </div>
                <p>{agent.description || '企业自定义能力'}</p>
                <div className="agent-catalog-meta">
                  <span>
                    能力 <code>{(agent.taskTypes ?? []).join(' · ') || '—'}</code>
                  </span>
                  <span>
                    关键词{' '}
                    <code>
                      {(agent.keywords ?? []).slice(0, 5).join(' / ') || '—'}
                    </code>
                  </span>
                </div>
              </article>
            ))}
          </div>

          <p className="agent-catalog-foot">
            Manifest 只描述能力，不执行代码。上传后由 Supervisor 自动扫描注册，
            无需修改任何 Python / Java 代码。
          </p>
        </div>
      )}
    </div>
  )
}
