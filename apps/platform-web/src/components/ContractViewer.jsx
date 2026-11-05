import { useMemo, useState } from 'react'

const SHAPE_LABELS = {
  lightweight_web: '轻量网页工具',
  script_automation: '脚本自动化',
  full_stack_system: '全栈系统',
}

function textValue(value) {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) {
    return value.map((item) => textValue(item)).join('、')
  }
  return JSON.stringify(value, null, 2)
}

/** 轻量 Markdown 渲染：只生成 React 节点，不注入 HTML。 */
function Markdown({ text }) {
  const nodes = useMemo(() => {
    const source = String(text ?? '').replace(/\r\n/g, '\n')
    const lines = source.split('\n')
    const result = []
    let codeLines = null
    let codeLang = ''
    let listItems = []
    let ordered = false
    let key = 0

    const flushList = () => {
      if (listItems.length === 0) return
      const Tag = ordered ? 'ol' : 'ul'
      result.push(
        <Tag key={`list-${key++}`} className="contract-md-list">
          {listItems.map((item, index) => (
            <li key={index}>{item}</li>
          ))}
        </Tag>,
      )
      listItems = []
    }

    for (const line of lines) {
      const fence = line.match(/^\s*```(\S*)\s*$/)
      if (fence) {
        if (codeLines !== null) {
          result.push(
            <pre key={`code-${key++}`} className="contract-md-code">
              <code data-lang={codeLang || undefined}>{codeLines.join('\n')}</code>
            </pre>,
          )
          codeLines = null
          codeLang = ''
        } else {
          flushList()
          codeLines = []
          codeLang = fence[1] || ''
        }
        continue
      }
      if (codeLines !== null) {
        codeLines.push(line)
        continue
      }
      if (!line.trim()) {
        flushList()
        continue
      }
      const heading = line.match(/^(#{1,4})\s+(.*)$/)
      if (heading) {
        flushList()
        const level = Math.min(4, heading[1].length)
        const Tag = `h${level}`
        result.push(
          <Tag key={`h-${key++}`} className={`contract-md-h contract-md-h${level}`}>
            {heading[2]}
          </Tag>,
        )
        continue
      }
      if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
        flushList()
        result.push(<hr key={`hr-${key++}`} className="contract-md-rule" />)
        continue
      }
      const bullet = line.match(/^\s*[-*+]\s+(.*)$/)
      if (bullet) {
        if (ordered) flushList()
        ordered = false
        listItems.push(bullet[1])
        continue
      }
      const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/)
      if (numbered) {
        if (!ordered) flushList()
        ordered = true
        listItems.push(numbered[1])
        continue
      }
      if (line.startsWith('>')) {
        flushList()
        result.push(
          <blockquote key={`quote-${key++}`} className="contract-md-quote">
            {line.replace(/^>\s?/, '')}
          </blockquote>,
        )
        continue
      }
      flushList()
      result.push(
        <p key={`p-${key++}`} className="contract-md-p">
          {line}
        </p>,
      )
    }
    if (codeLines !== null) {
      result.push(
        <pre key={`code-${key++}`} className="contract-md-code">
          <code>{codeLines.join('\n')}</code>
        </pre>,
      )
    }
    flushList()
    return result
  }, [text])

  return <div className="contract-markdown">{nodes}</div>
}

function EmptyState({ children }) {
  return <p className="contract-empty">{children}</p>
}

function KeyValueGrid({ data }) {
  const entries = Object.entries(data ?? {})
  if (entries.length === 0) return <EmptyState>暂无内容</EmptyState>
  return (
    <dl className="contract-kv">
      {entries.map(([key, value]) => (
        <div key={key} className="contract-kv-row">
          <dt>{key}</dt>
          <dd>{textValue(value)}</dd>
        </div>
      ))}
    </dl>
  )
}

export default function ContractViewer({ contract, citations = [] }) {
  const [tab, setTab] = useState('overview')
  if (!contract) return null

  const shape = contract.appShape ?? 'full_stack_system'
  const routes = contract.apiRoutes ?? []
  const interactions = contract.componentInteractions ?? []
  const uiLogic = contract.uiLogic ?? []
  const dataModel = contract.frontendDataModel ?? []
  const phases = contract.implementationPath ?? []
  const scriptSpec = contract.scriptSpec ?? {}
  const assumptions = contract.assumptions ?? []
  const contractCitations = contract.citations ?? citations
  const confidence = Math.round((contract.confidenceScore ?? 0) * 100)

  const tabs = [
    { id: 'overview', label: '概览' },
    ...(routes.length > 0 ? [{ id: 'api', label: `接口 ${routes.length}` }] : []),
    ...(contract.databaseDdl ? [{ id: 'database', label: '数据库 DDL' }] : []),
    ...(uiLogic.length > 0 || interactions.length > 0 || dataModel.length > 0
      ? [{ id: 'frontend', label: '前端设计' }]
      : []),
    ...(Object.keys(scriptSpec).length > 0 ? [{ id: 'script', label: '脚本规格' }] : []),
    ...(phases.length > 0 ? [{ id: 'path', label: '实施路径' }] : []),
    { id: 'markdown', label: 'Markdown 原文' },
  ]
  const activeTab = tabs.some((item) => item.id === tab) ? tab : 'overview'

  return (
    <section className="contract-viewer" aria-label="全栈实施契约">
      <header className="contract-header">
        <div>
          <div className="contract-eyebrow">IMPLEMENTATION CONTRACT</div>
          <h3>实施契约</h3>
        </div>
        <div className="contract-header-meta">
          <span className={`shape-badge shape-${shape}`}>{SHAPE_LABELS[shape]}</span>
          <span
            className={`confidence-pill ${confidence >= 85 ? 'is-good' : 'is-low'}`}
            title={`阈值 ${Math.round((contract.threshold ?? 0.85) * 100)}%`}
          >
            置信度 {confidence}%
          </span>
        </div>
      </header>

      <div className="contract-tabs" role="tablist" aria-label="契约视图">
        {tabs.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={activeTab === item.id}
            className={activeTab === item.id ? 'is-active' : ''}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div className="contract-body" role="tabpanel">
        {activeTab === 'overview' && (
          <div className="contract-overview">
            <h4>技术栈</h4>
            <KeyValueGrid data={contract.techStack} />

            <h4>关键假设</h4>
            {assumptions.length > 0 ? (
              <ul className="contract-assumptions">
                {assumptions.map((item, index) => (
                  <li key={index}>{item}</li>
                ))}
              </ul>
            ) : (
              <EmptyState>当前方案没有额外假设。</EmptyState>
            )}

            <h4>知识库依据</h4>
            {contractCitations.length > 0 ? (
              <div className="contract-citations">
                {contractCitations.map((item, index) => (
                  <span
                    key={`${item.docId ?? item.doc_id}-${index}`}
                    className="citation-chip"
                    title={item.path || item.section || ''}
                  >
                    {item.title || item.docId || item.doc_id || '知识库条目'}
                    {item.section ? ` · ${item.section}` : ''}
                  </span>
                ))}
              </div>
            ) : (
              <EmptyState>本次生成未引用知识库条目。</EmptyState>
            )}
          </div>
        )}

        {activeTab === 'api' && (
          <div className="contract-table-wrap">
            <table className="contract-table">
              <thead>
                <tr>
                  <th>方法</th>
                  <th>路径</th>
                  <th>请求</th>
                  <th>响应</th>
                  <th>说明</th>
                </tr>
              </thead>
              <tbody>
                {routes.map((route, index) => (
                  <tr key={`${route.method}-${route.path}-${index}`}>
                    <td>
                      <code className={`method method-${String(route.method).toLowerCase()}`}>
                        {route.method}
                      </code>
                    </td>
                    <td>
                      <code>{route.path}</code>
                    </td>
                    <td>{route.request || '—'}</td>
                    <td>{route.response || '—'}</td>
                    <td>{route.description || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {activeTab === 'database' && (
          <pre className="contract-ddl">
            <code>{contract.databaseDdl}</code>
          </pre>
        )}

        {activeTab === 'frontend' && (
          <div className="contract-frontend">
            <h4>UI 交互逻辑</h4>
            {uiLogic.length > 0 ? (
              <div className="contract-card-list">
                {uiLogic.map((item, index) => (
                  <article key={index} className="contract-mini-card">
                    <KeyValueGrid data={item} />
                  </article>
                ))}
              </div>
            ) : (
              <EmptyState>该契约没有单独的 UI 交互条目。</EmptyState>
            )}

            <h4>组件 / 事件交互</h4>
            {interactions.length > 0 ? (
              <div className="contract-table-wrap">
                <table className="contract-table">
                  <thead>
                    <tr>
                      <th>组件</th>
                      <th>事件</th>
                      <th>接口</th>
                      <th>逻辑</th>
                    </tr>
                  </thead>
                  <tbody>
                    {interactions.map((item, index) => (
                      <tr key={index}>
                        <td>{item.component}</td>
                        <td>{item.event}</td>
                        <td>
                          <code>{item.apiRoute || '—'}</code>
                        </td>
                        <td>{item.logic || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState>没有跨组件调用。</EmptyState>
            )}

            <h4>前端数据模型</h4>
            {dataModel.length > 0 ? (
              <div className="contract-card-list">
                {dataModel.map((item, index) => (
                  <article key={index} className="contract-mini-card">
                    <KeyValueGrid data={item} />
                  </article>
                ))}
              </div>
            ) : (
              <EmptyState>该契约没有独立的前端数据模型。</EmptyState>
            )}
          </div>
        )}

        {activeTab === 'script' && (
          <div className="contract-script">
            <h4>脚本运行规格</h4>
            <KeyValueGrid data={scriptSpec} />
          </div>
        )}

        {activeTab === 'path' && (
          <ol className="contract-phases">
            {phases.map((phase, index) => (
              <li key={`${phase.phase}-${index}`}>
                <div className="phase-title">
                  <span className="phase-index">{phase.phase || index + 1}</span>
                  {phase.title}
                </div>
                <ul>
                  {(phase.tasks ?? []).map((task, taskIndex) => (
                    <li key={taskIndex}>{task}</li>
                  ))}
                </ul>
              </li>
            ))}
          </ol>
        )}

        {activeTab === 'markdown' && (
          <Markdown text={contract.contractMarkdown ?? ''} />
        )}
      </div>
    </section>
  )
}
