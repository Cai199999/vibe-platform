/**
 * VisualDocument 高保真只读预览。
 *
 * 与可编辑画布的线框不同，这里直接渲染真实 HTML/CSS：
 *   - 页面 Tab 可点击切换；
 *   - navigate 交互可切换目标页面；
 *   - 使用 document.designTokens 的 CSS 变量；
 *   - 不写 store、不启动沙盒，只做视觉验收。
 */

import { useMemo } from 'react'
import {
  activePageIdOf,
  ensureVisualPages,
} from './visualDocument.js'

function tokensStyle(document) {
  const tokens = document?.designTokens || {}
  const colors = tokens.colors || {}
  const spacing = tokens.spacing || {}
  const radius = tokens.radius || {}
  const typography = tokens.typography || {}
  const style = {}
  Object.entries(colors).forEach(([key, value]) => {
    const name = key.replace(/[A-Z]/g, (char) => `-${char.toLowerCase()}`)
    style[`--color-${name}`] = value
  })
  Object.entries(spacing).forEach(([key, value]) => {
    style[`--spacing-${key}`] = value
  })
  Object.entries(radius).forEach(([key, value]) => {
    style[`--radius-${key}`] = value
  })
  Object.entries(typography).forEach(([key, value]) => {
    style[`--typography-${key}`] = value
  })
  return style
}

function layoutStyle(node) {
  const layout = node.layout || {}
  const style = {
    display: layout.display || 'block',
    gap: layout.gap || undefined,
    padding: layout.padding || undefined,
    minWidth: layout.minWidth || undefined,
    maxWidth: layout.maxWidth || undefined,
    boxSizing: 'border-box',
  }
  if (layout.display === 'flex' || layout.display === 'inline-flex') {
    style.flexDirection = layout.direction === 'row' ? 'row' : 'column'
    style.alignItems = {
      start: 'flex-start',
      center: 'center',
      end: 'flex-end',
      stretch: 'stretch',
    }[layout.align || 'stretch']
    style.justifyContent = {
      start: 'flex-start',
      center: 'center',
      between: 'space-between',
      end: 'flex-end',
    }[layout.justify || 'start']
    style.flexWrap = layout.wrap ? 'wrap' : 'nowrap'
  }
  if (layout.display === 'grid') {
    style.gridTemplateColumns = `repeat(${Math.max(
      1,
      Math.min(12, layout.columns || 1),
    )}, minmax(0, 1fr))`
  }
  if (layout.maxWidth) style.margin = '0 auto'
  return style
}

function typeStyle(node) {
  if (node.type === 'page') {
    return {
      minHeight: '100vh',
      width: '100%',
      padding: '32px',
      backgroundColor: 'var(--color-surface, #fff)',
      color: 'var(--color-text, #0f172a)',
    }
  }
  if (node.type === 'text') {
    return {
      margin: 0,
      color: 'var(--color-text, #0f172a)',
      fontSize: 'var(--typography-body, 14px)',
      lineHeight: 1.6,
    }
  }
  if (node.type === 'button') {
    return {
      padding: '10px 16px',
      border: '1px solid transparent',
      borderRadius: 'var(--radius-sm, 8px)',
      backgroundColor: 'var(--color-primary, #2563eb)',
      color: '#fff',
      fontSize: 'var(--typography-body, 14px)',
      fontWeight: 600,
      cursor: 'pointer',
    }
  }
  if (node.type === 'input') {
    return {
      width: '100%',
      padding: '10px 12px',
      border: '1px solid var(--color-border, #cbd5e1)',
      borderRadius: 'var(--radius-sm, 8px)',
      backgroundColor: 'var(--color-surface, #fff)',
      fontSize: 'var(--typography-body, 14px)',
    }
  }
  if (node.type === 'image') {
    return {
      display: 'block',
      width: '100%',
      height: 'auto',
      borderRadius: 'var(--radius-card, 12px)',
    }
  }
  if (node.type === 'list') {
    return { listStyle: 'none', margin: 0, padding: 0 }
  }
  if (node.type === 'table') {
    return { width: '100%', borderCollapse: 'collapse' }
  }
  if (node.type === 'section' || node.type === 'container') {
    return {
      border: '1px solid var(--color-border, #cbd5e1)',
      borderRadius: 'var(--radius-card, 12px)',
      backgroundColor: 'var(--color-surface, #fff)',
    }
  }
  return {}
}

function interactionFor(document, nodeId) {
  return (document.interactions || []).find(
    (item) => item.nodeId === nodeId && item.status !== 'CANCELLED',
  )
}

function PreviewNode({ node, document, onSwitchPage, depth = 0 }) {
  if (!node) return null
  const interaction = interactionFor(document, node.id)
  const role = node.semantics?.role || ''
  const roleStyle =
    role === 'page'
      ? {
          width: '100%',
          minHeight: '100vh',
          padding: 0,
          margin: 0,
          backgroundColor: 'var(--color-surface, #fff)',
          color: 'var(--color-text, #0f172a)',
          boxSizing: 'border-box',
        }
      : role === 'header'
      ? {
          display: 'flex',
          flexDirection: 'row',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: '16px',
          padding: '16px 24px',
          width: '100%',
          border: 'none',
          borderBottom: '1px solid var(--color-border, #cbd5e1)',
          borderRadius: 0,
          backgroundColor: 'var(--color-surface, #fff)',
          boxSizing: 'border-box',
        }
      : role === 'main'
        ? {
            width: '100%',
            padding: '24px',
            backgroundColor: 'var(--color-canvas, #f1f5f9)',
            border: 'none',
            borderRadius: 0,
            boxSizing: 'border-box',
          }
        : {}
  const style = {
    ...typeStyle(node),
    ...layoutStyle(node),
    ...roleStyle,
  }
  const common = {
    'data-preview-node-id': node.id,
    style,
  }

  if (node.type === 'text') {
    return (
      <p {...common}>
        {String(node.props?.text || node.name || '')}
      </p>
    )
  }
  if (node.type === 'button') {
    return (
      <button
        {...common}
        type="button"
        onClick={() => {
          if (
            interaction?.action === 'navigate' &&
            interaction?.targetPageId
          ) {
            onSwitchPage?.(interaction.targetPageId)
          }
        }}
      >
        {String(node.props?.text || node.name || '按钮')}
      </button>
    )
  }
  if (node.type === 'input') {
    return (
      <input
        {...common}
        type={String(node.props?.type || 'text')}
        placeholder={String(node.props?.placeholder || '')}
        disabled={Boolean(node.state?.disabled)}
      />
    )
  }
  if (node.type === 'image') {
    return (
      <img
        {...common}
        src={String(node.props?.src || '')}
        alt={String(node.props?.alt || '')}
      />
    )
  }
  if (node.type === 'table') {
    const columns = Array.isArray(node.props?.columns)
      ? node.props.columns
      : []
    const count = node.repeat ? Math.max(1, node.repeat.sampleCount || 3) : 1
    return (
      <table {...common}>
        {columns.length > 0 && (
          <thead>
            <tr>
              {columns.map((column, index) => (
                <th
                  key={index}
                  style={{
                    border: '1px solid var(--color-border, #cbd5e1)',
                    padding: '8px 12px',
                    textAlign: 'left',
                  }}
                >
                  {String(column)}
                </th>
              ))}
            </tr>
          </thead>
        )}
        <tbody>
          {(node.children || []).length > 0
            ? Array.from({ length: count }).map((_, index) => (
                <tr key={index}>
                  {(node.children || []).map((child) => (
                    <td
                      key={child.id}
                      style={{
                        border: '1px solid var(--color-border, #cbd5e1)',
                        padding: '8px 12px',
                      }}
                    >
                      <PreviewNode
                        node={child}
                        document={document}
                        onSwitchPage={onSwitchPage}
                        depth={depth + 1}
                      />
                    </td>
                  ))}
                </tr>
              ))
            : Array.from({ length: count }).map((_, index) => (
                <tr key={index}>
                  {columns.map((_, columnIndex) => (
                    <td
                      key={columnIndex}
                      style={{
                        border: '1px solid var(--color-border, #cbd5e1)',
                        padding: '8px 12px',
                      }}
                    >
                      &nbsp;
                    </td>
                  ))}
                </tr>
              ))}
        </tbody>
      </table>
    )
  }
  if (node.type === 'list') {
    const count = node.repeat ? Math.max(1, node.repeat.sampleCount || 3) : 1
    return (
      <ul {...common}>
        {node.repeat
          ? Array.from({ length: count }).map((_, index) => (
              <li key={index} style={{ listStyle: 'none' }}>
                {(node.children || []).map((child) => (
                  <PreviewNode
                    key={child.id}
                    node={child}
                    document={document}
                    onSwitchPage={onSwitchPage}
                    depth={depth + 1}
                  />
                ))}
              </li>
            ))
          : (node.children || []).map((child) => (
              <PreviewNode
                key={child.id}
                node={child}
                document={document}
                onSwitchPage={onSwitchPage}
                depth={depth + 1}
              />
            ))}
      </ul>
    )
  }

  const tag =
    node.type === 'page'
      ? 'main'
      : node.type === 'section'
        ? 'section'
        : node.type === 'form'
          ? 'form'
          : node.type === 'modal'
            ? 'div'
            : 'div'
  const Tag = tag
  return (
    <Tag
      {...common}
      onSubmit={
        node.type === 'form'
          ? (event) => event.preventDefault()
          : undefined
      }
    >
      {(node.children || []).map((child) => (
        <PreviewNode
          key={child.id}
          node={child}
          document={document}
          onSwitchPage={onSwitchPage}
          depth={depth + 1}
        />
      ))}
    </Tag>
  )
}

export default function VisualDocumentPreview({
  document,
  onClose,
  onSwitchPage,
}) {
  const pages = useMemo(() => ensureVisualPages(document), [document])
  const activePageId = activePageIdOf(document)
  const activePage =
    pages.find((page) => page.pageId === activePageId) || pages[0]
  const tokenStyle = useMemo(() => tokensStyle(document), [document])

  return (
    <div
      className="visual-preview-backdrop"
      role="dialog"
      aria-modal="true"
      data-visual-preview
    >
      <div className="visual-preview-shell" style={tokenStyle}>
        <header className="visual-preview-head">
          <div>
            <span className="eyebrow">Visual Preview</span>
            <h2>{document?.title || '页面预览'}</h2>
          </div>
          <div className="visual-preview-pages" role="tablist">
            {pages.map((page) => (
              <button
                key={page.pageId}
                type="button"
                role="tab"
                aria-selected={page.pageId === activePageId}
                className={
                  page.pageId === activePageId ? 'is-selected' : ''
                }
                onClick={() => onSwitchPage?.(page.pageId)}
              >
                {page.title || page.path}
              </button>
            ))}
          </div>
          <button type="button" className="ghost-button" onClick={onClose}>
            关闭预览
          </button>
        </header>
        <div className="visual-preview-body">
          <PreviewNode
            node={activePage?.root}
            document={document}
            onSwitchPage={onSwitchPage}
          />
        </div>
      </div>
    </div>
  )
}
