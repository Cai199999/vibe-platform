/**
 * 存量源码只读投影画布。
 *
 * 只消费 sourceProjection.js 生成的只读文档：
 *   - 不读取 visualDocumentStore；
 *   - 不提供任何编辑命令；
 *   - hover 展示组件层级 / className / 源码位置；
 *   - 固定提示“已识别为存量源码，需转为虚拟树才能自由编辑”。
 */

import { useMemo, useState } from 'react'
import { NODE_LABELS } from './visualDocument.js'
import {
  SOURCE_PROJECTION_TIP,
  flattenSourceProjection,
  projectionClassName,
  projectionHierarchyText,
  projectionSourceText,
} from './sourceProjection.js'

function badgeFor(node) {
  if (node.projection?.cycle) return '循环引用'
  if (node.projection?.external) return '外部组件'
  if (node.projection?.inlined) return '已展开'
  if (node.projection?.kind === 'component') return '组件'
  return ''
}

function SourceNode({
  node,
  depth,
  selectedId,
  onSelect,
  onHover,
  onLeave,
}) {
  const [open, setOpen] = useState(true)
  const hasChildren = Array.isArray(node.children) && node.children.length > 0
  const selected = selectedId === node.id
  const badge = badgeFor(node)
  const classes = node.projection?.classes || []
  const className = projectionClassName(node)

  return (
    <div
      className={`src-projection-node is-${node.projection?.kind || 'element'}${
        selected ? ' is-selected' : ''
      }`}
      data-source-projection-node={node.id}
      data-projection-tag={node.projection?.tag}
      style={{ '--src-depth': depth }}
      onClick={(event) => {
        event.stopPropagation()
        onSelect(node.id)
      }}
      onMouseEnter={(event) => {
        // mouseenter 会被 React 合成到祖先节点；只让真正命中的节点更新
        // hover，避免子节点的 tooltip 被父节点覆盖。
        if (event.target.closest?.('[data-source-projection-node]') !== event.currentTarget) {
          return
        }
        onHover(node, event)
      }}
      onMouseMove={(event) => {
        if (event.target.closest?.('[data-source-projection-node]') !== event.currentTarget) {
          return
        }
        onHover(node, event)
      }}
      onMouseLeave={onLeave}
      role="treeitem"
      aria-selected={selected}
      tabIndex={0}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onSelect(node.id)
        }
      }}
    >
      <div className="src-projection-node-head">
        {hasChildren && (
          <button
            type="button"
            className="src-projection-toggle"
            aria-label={open ? '折叠' : '展开'}
            onClick={(event) => {
              event.stopPropagation()
              setOpen((value) => !value)
            }}
          >
            {open ? '▾' : '▸'}
          </button>
        )}
        <span className={`src-projection-tag is-${node.type}`}>
          {node.projection?.kind === 'text' ? 'text' : node.projection?.tag}
        </span>
        <span className="src-projection-label">
          {NODE_LABELS[node.type] || node.type}
          {node.name && node.projection?.kind !== 'text'
            ? ` · ${node.name}`
            : ''}
        </span>
        {badge && <span className="src-projection-badge">{badge}</span>}
      </div>

      {node.props?.text && (
        <p className="src-projection-text">{node.props.text}</p>
      )}
      {node.props?.placeholder && (
        <p className="src-projection-placeholder">
          placeholder: {node.props.placeholder}
        </p>
      )}
      {className && (
        <code className="src-projection-class">{className}</code>
      )}
      {classes.length > 0 && classes.join(' ') !== className && (
        <code className="src-projection-class is-muted">
          {classes.join(' ')}
        </code>
      )}

      {hasChildren && open && (
        <div className="src-projection-children" role="group">
          {node.children.map((child) => (
            <SourceNode
              key={child.id}
              node={child}
              depth={depth + 1}
              selectedId={selectedId}
              onSelect={onSelect}
              onHover={onHover}
              onLeave={onLeave}
            />
          ))}
        </div>
      )}
    </div>
  )
}

export default function ReadOnlySourceCanvas({ projection, onBack }) {
  const [selectedId, setSelectedId] = useState(null)
  const [hover, setHover] = useState(null)
  const nodes = useMemo(
    () => flattenSourceProjection(projection?.root),
    [projection],
  )
  const selected = useMemo(
    () => nodes.find((node) => node.id === selectedId) || null,
    [nodes, selectedId],
  )

  if (!projection?.root) {
    return (
      <div className="vc-shell src-projection-shell">
        <div className="vc-empty-canvas">
          <div>
            <strong>没有可渲染的源码投影</strong>
            <p>请返回画布重新导入 React 文件。</p>
            <button type="button" className="primary-button" onClick={onBack}>
              返回虚拟画布
            </button>
          </div>
        </div>
      </div>
    )
  }

  const hoverNode = hover?.node
  const hoverPath = hoverNode ? projectionHierarchyText(hoverNode) : ''
  const hoverClass = hoverNode ? projectionClassName(hoverNode) : ''
  const hoverSource = hoverNode ? projectionSourceText(hoverNode) : ''
  const tooltipLeft = hover
    ? Math.min(hover.x + 16, (window.innerWidth || 1280) - 380)
    : 0
  const tooltipTop = hover
    ? Math.min(hover.y + 16, (window.innerHeight || 800) - 180)
    : 0

  return (
    <div className="vc-shell src-projection-shell" data-source-projection>
      <header className="vc-topbar">
        <div className="vc-title-block">
          <span className="eyebrow">Source Projection · Read Only</span>
          <h1>{projection.title}</h1>
          <p>
            存量源码只读投影 · 不写回源码 · 不生成虚拟树 · 入口{' '}
            {projection.entryComponent || '未识别'}
          </p>
        </div>
        <div className="vc-topbar-actions">
          <span className="vc-validate is-ok">只读分析</span>
          <button type="button" className="ghost-button" onClick={onBack}>
            返回虚拟画布
          </button>
        </div>
      </header>

      <div className="src-projection-banner" role="status">
        <strong>{SOURCE_PROJECTION_TIP}</strong>
        <span>
          hover 节点查看组件层级、className 与源码位置；当前阶段不提供写回。
        </span>
      </div>

      <div className="src-projection-layout">
        <main className="src-projection-canvas" aria-label="源码组件树">
          <div className="src-projection-canvas-head">
            <span>
              组件 {projection.stats?.components ?? 0} · 元素{' '}
              {projection.stats?.elements ?? 0} · 文本{' '}
              {projection.stats?.texts ?? 0} · 投影节点{' '}
              {projection.stats?.projectionNodes ?? nodes.length}
            </span>
            <span>{projection.metadata?.sourceFile}</span>
          </div>
          <div className="src-projection-tree" role="tree">
            <SourceNode
              node={projection.root}
              depth={0}
              selectedId={selectedId}
              onSelect={setSelectedId}
              onHover={(node, event) =>
                setHover({
                  node,
                  x: event.clientX || 0,
                  y: event.clientY || 0,
                })
              }
              onLeave={() => setHover(null)}
            />
          </div>
        </main>

        <aside className="src-projection-inspector" aria-label="源码投影详情">
          <div className="vc-panel-head">
            <h2>节点详情</h2>
            <span>{selected ? selected.projection?.tag : '未选择'}</span>
          </div>
          {selected ? (
            <dl className="src-projection-detail">
              <dt>层级</dt>
              <dd>{projectionHierarchyText(selected)}</dd>
              <dt>标签</dt>
              <dd>
                <code>{selected.projection?.tag}</code>
              </dd>
              <dt>className</dt>
              <dd>
                <code>{projectionClassName(selected) || '—'}</code>
              </dd>
              <dt>源码位置</dt>
              <dd>
                <code>{projectionSourceText(selected) || '—'}</code>
              </dd>
              <dt>类型</dt>
              <dd>
                {selected.projection?.kind} · {selected.type}
              </dd>
              {selected.projection?.external && (
                <>
                  <dt>说明</dt>
                  <dd>该组件定义不在当前文件中，未展开。</dd>
                </>
              )}
              {selected.projection?.cycle && (
                <>
                  <dt>说明</dt>
                  <dd>检测到循环引用，已停止展开。</dd>
                </>
              )}
            </dl>
          ) : (
            <p className="src-projection-empty">
              在左侧树中点击或 hover 一个节点，查看它的只读元数据。
            </p>
          )}
          <div className="src-projection-hint">
            <strong>{SOURCE_PROJECTION_TIP}</strong>
          </div>
        </aside>
      </div>

      {hoverNode && (
        <div
          className="src-projection-tooltip"
          data-projection-tip
          style={{ left: tooltipLeft, top: tooltipTop }}
          role="tooltip"
        >
          <strong>{hoverPath || hoverNode.projection?.tag}</strong>
          {hoverClass && <code>{hoverClass}</code>}
          {hoverSource && <span>{hoverSource}</span>}
          <em>{SOURCE_PROJECTION_TIP}</em>
        </div>
      )}
    </div>
  )
}
