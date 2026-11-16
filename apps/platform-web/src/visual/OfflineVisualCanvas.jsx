/**
 * 离线优先可视化画布。
 *
 * 60fps 关键点：
 *  - 拖拽中的 pointermove 只直接改 ghost 的 transform 和高亮 DOM；
 *  - 不 setState、不写 Zustand、不调用后端；
 *  - 只有 pointerup 落点确定后才提交一次 moveNode 命令。
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import {
  CONTAINER_TYPES,
  NODE_LABELS,
  countVisualNodes,
  createEmptyVisualDocument,
  findVisualNode,
  isVisualDescendant,
  validateVisualDocument,
} from './visualDocument.js'
import { useVisualDocumentStore } from './visualDocumentStore.js'

const PALETTE = [
  ['section', '区块', '▭'],
  ['container', '容器', '▢'],
  ['grid', '网格', '▦'],
  ['stack', '纵向栈', '↕'],
  ['text', '文本', 'T'],
  ['button', '按钮', '▣'],
  ['input', '输入框', '⌨'],
  ['image', '图片', '▧'],
  ['list', '列表', '≣'],
  ['table', '表格', '▤'],
  ['form', '表单', '⛶'],
  ['modal', '弹窗', '◫'],
]

const DROP_TARGET_CLASS = 'is-drop-target'
const DROP_INVALID_CLASS = 'is-drop-invalid'

function layoutStyle(node) {
  const layout = node.layout || {}
  const style = {
    display: layout.display || 'block',
    gap: layout.gap || undefined,
    padding: layout.padding || undefined,
    alignItems: layout.align || undefined,
    justifyContent:
      layout.justify === 'between'
        ? 'space-between'
        : layout.justify || undefined,
    minWidth: layout.minWidth || undefined,
    maxWidth: layout.maxWidth || undefined,
  }
  if (layout.display === 'flex') {
    style.flexDirection = layout.direction || 'column'
    if (layout.wrap) style.flexWrap = 'wrap'
  }
  if (layout.display === 'grid') {
    style.gridTemplateColumns = `repeat(${layout.columns || 1}, minmax(0, 1fr))`
  }
  return style
}

function shellStyle(node) {
  return {
    minWidth: node.layout?.minWidth || undefined,
    maxWidth: node.layout?.maxWidth || undefined,
  }
}

function nodeClassNames(node, selected, dragging) {
  return [
    'vc-node',
    `vc-node-${node.type}`,
    selected ? 'is-selected' : '',
    dragging ? 'is-dragging' : '',
    node.locked ? 'is-locked' : '',
    node.style?.className || '',
  ]
    .filter(Boolean)
    .join(' ')
}

function VisualNodeView({
  node,
  depth,
  selectedId,
  draggingId,
  onSelect,
  onStartDrag,
  onDelete,
  onDuplicate,
  renderNodeList,
}) {
  const isContainer = CONTAINER_TYPES.has(node.type)
  const isDragging = draggingId === node.id
  const selected = selectedId === node.id
  const handlePointerDown = (event) => {
    event.stopPropagation()
    onStartDrag(event, node)
  }

  const common = {
    className: nodeClassNames(node, selected, isDragging),
    style: layoutStyle(node),
    'data-visual-node-id': node.id,
    'data-visual-depth': depth,
    onClick: (event) => {
      event.stopPropagation()
      onSelect(node.id)
    },
  }

  if (node.type === 'page') {
    return (
      <div
        {...common}
        data-visual-drop-id={node.locked ? undefined : node.id}
        className={`${common.className} vc-page`}
      >
        <NodeChrome
          node={node}
          selected={selected}
          onStartDrag={handlePointerDown}
          onDelete={onDelete}
          onDuplicate={onDuplicate}
        />
        <div className="vc-page-body" data-visual-children>
          {node.children.length === 0 ? (
            <div className="vc-empty-page">
              <strong>空白页面</strong>
              <span>从左侧添加组件，或直接拖入这里</span>
            </div>
          ) : (
            renderNodeList(node.children)
          )}
        </div>
      </div>
    )
  }

  if (node.type === 'text') {
    return (
      <div {...common} className={`${common.className} vc-text`}>
        <NodeChrome
          node={node}
          selected={selected}
          onStartDrag={handlePointerDown}
          onDelete={onDelete}
          onDuplicate={onDuplicate}
        />
        <span>{node.props?.text || '文本'}</span>
      </div>
    )
  }

  if (node.type === 'button') {
    return (
      <div {...common} className={`${common.className} vc-button-wrap`}>
        <NodeChrome
          node={node}
          selected={selected}
          onStartDrag={handlePointerDown}
          onDelete={onDelete}
          onDuplicate={onDuplicate}
        />
        <button
          type="button"
          className={`vc-button is-${node.props?.variant || 'primary'}`}
          onClick={(event) => {
            event.preventDefault()
            event.stopPropagation()
          }}
        >
          {node.props?.text || '按钮'}
        </button>
      </div>
    )
  }

  if (node.type === 'input') {
    return (
      <div {...common} className={`${common.className} vc-input-wrap`}>
        <NodeChrome
          node={node}
          selected={selected}
          onStartDrag={handlePointerDown}
          onDelete={onDelete}
          onDuplicate={onDuplicate}
        />
        <input
          className="vc-input"
          placeholder={node.props?.placeholder || '请输入内容'}
          readOnly
          aria-label={node.a11y?.label || '输入框'}
        />
      </div>
    )
  }

  if (node.type === 'image') {
    return (
      <div {...common} className={`${common.className} vc-image-wrap`}>
        <NodeChrome
          node={node}
          selected={selected}
          onStartDrag={handlePointerDown}
          onDelete={onDelete}
          onDuplicate={onDuplicate}
        />
        <img
          className="vc-image"
          src={node.props?.src}
          alt={node.props?.alt || node.a11y?.label || '图片'}
          draggable={false}
        />
      </div>
    )
  }

  if (node.type === 'table') {
    const columns = node.props?.columns || ['列 1', '列 2', '操作']
    return (
      <div
        {...common}
        style={shellStyle(node)}
        data-visual-drop-id={node.locked ? undefined : node.id}
        className={`${common.className} vc-table-wrap`}
      >
        <NodeChrome
          node={node}
          selected={selected}
          onStartDrag={handlePointerDown}
          onDelete={onDelete}
          onDuplicate={onDuplicate}
        />
        <table className="vc-table">
          <thead>
            <tr>
              {columns.map((column, index) => (
                <th key={`${column}-${index}`}>{column}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {Array.from({ length: node.repeat?.sampleCount || 2 }).map(
              (_, rowIndex) => (
                <tr key={`row-${rowIndex}`}>
                  {columns.map((column, columnIndex) => (
                    <td key={`${column}-${columnIndex}`}>
                      {columnIndex === columns.length - 1
                        ? '查看'
                        : `示例 ${rowIndex + 1}`}
                    </td>
                  ))}
                </tr>
              ),
            )}
          </tbody>
        </table>
        <div className="vc-repeat-hint">
          {node.repeat
            ? `数据源：${node.repeat.source}`
            : '表格数据将在业务审查阶段绑定'}
        </div>
        <div
          className="vc-children"
          data-visual-children
          style={layoutStyle(node)}
        >
          {node.children.length === 0 ? (
            <div className="vc-empty-container">拖入表格工具栏或附加组件</div>
          ) : (
            renderNodeList(node.children)
          )}
        </div>
      </div>
    )
  }

  if (node.type === 'list') {
    return (
      <div
        {...common}
        style={shellStyle(node)}
        data-visual-drop-id={node.locked ? undefined : node.id}
        className={`${common.className} vc-list-wrap`}
      >
        <NodeChrome
          node={node}
          selected={selected}
          onStartDrag={handlePointerDown}
          onDelete={onDelete}
          onDuplicate={onDuplicate}
        />
        <div
          className="vc-list-body"
          data-visual-children
          style={layoutStyle(node)}
        >
          {node.children.length === 0 ? (
            <div className="vc-empty-list">列表项容器 · 拖入卡片或添加文本</div>
          ) : (
            renderNodeList(node.children)
          )}
        </div>
        {node.repeat && (
          <span className="vc-repeat-hint">
            循环 {node.repeat.sampleCount} 项 · {node.repeat.source}
          </span>
        )}
      </div>
    )
  }

  if (node.type === 'modal') {
    return (
      <div
        {...common}
        style={shellStyle(node)}
        data-visual-drop-id={node.locked ? undefined : node.id}
        className={`${common.className} vc-modal-wrap`}
      >
        <NodeChrome
          node={node}
          selected={selected}
          onStartDrag={handlePointerDown}
          onDelete={onDelete}
          onDuplicate={onDuplicate}
        />
        <div className="vc-modal-card">
          <strong>弹窗</strong>
          <div
            data-visual-children
            className="vc-children"
            style={layoutStyle(node)}
          >
            {renderNodeList(node.children)}
          </div>
        </div>
      </div>
    )
  }

  return (
    <div
      {...common}
      style={shellStyle(node)}
      data-visual-drop-id={isContainer && !node.locked ? node.id : undefined}
      className={`${common.className} vc-container`}
    >
      <NodeChrome
        node={node}
        selected={selected}
        onStartDrag={handlePointerDown}
        onDelete={onDelete}
        onDuplicate={onDuplicate}
      />
      <div className="vc-container-label">
        <span>{node.name || NODE_LABELS[node.type] || node.type}</span>
        {node.locked && <em>已锁定</em>}
      </div>
      <div
        className="vc-children"
        data-visual-children
        style={layoutStyle(node)}
      >
        {isContainer && node.children.length === 0 ? (
          <div className="vc-empty-container">拖入组件到这里</div>
        ) : (
          renderNodeList(node.children)
        )}
      </div>
    </div>
  )
}

function NodeChrome({ node, selected, onStartDrag, onDelete, onDuplicate }) {
  return (
    <div className="vc-node-chrome" data-visual-chrome>
      <button
        type="button"
        className="vc-drag-handle"
        title="拖拽移动 / 换父容器"
        aria-label={`拖拽 ${node.name || node.type}`}
        onPointerDown={onStartDrag}
        onClick={(event) => event.stopPropagation()}
      >
        ⠿
      </button>
      <span className={`vc-node-badge vc-badge-${node.type}`}>
        {NODE_LABELS[node.type] || node.type}
      </span>
      {node.locked && <span className="vc-locked-badge">锁</span>}
      {selected && (
        <span className="vc-node-actions">
          <button
            type="button"
            title="复制节点"
            onClick={(event) => {
              event.stopPropagation()
              onDuplicate(node.id)
            }}
          >
            ⧉
          </button>
          <button
            type="button"
            title="删除节点"
            className="is-danger"
            onClick={(event) => {
              event.stopPropagation()
              onDelete(node.id)
            }}
          >
            ×
          </button>
        </span>
      )}
    </div>
  )
}

export default function OfflineVisualCanvas({ projectId = '', title = '未命名页面' }) {
  const store = useVisualDocumentStore()
  const {
    doc,
    selectedId,
    history,
    future,
    lastError,
    selectNode,
    addNode,
    deleteNode,
    duplicateNode,
    moveNode,
    updateNode,
    undo,
    redo,
    resetDocument,
    replaceDocument,
    clearError,
  } = store
  const [draggingId, setDraggingId] = useState(null)
  const dragRef = useRef(null)
  const canvasRef = useRef(null)
  const [dropHint, setDropHint] = useState('')

  useEffect(() => {
    if (!projectId) return
    if (doc.projectId && doc.projectId === projectId) return
    replaceDocument(createEmptyVisualDocument({ projectId, title }), {
      resetHistory: true,
    })
  }, [doc.projectId, projectId, replaceDocument, title])

  const report = useMemo(() => validateVisualDocument(doc), [doc])
  const selected = useMemo(
    () => findVisualNode(doc.root, selectedId)?.node || null,
    [doc.root, selectedId],
  )
  const selectedParent = useMemo(
    () => findVisualNode(doc.root, selectedId)?.parent || null,
    [doc.root, selectedId],
  )

  const addToSelection = (type) => {
    let parentId = doc.root?.id || null
    if (selected && CONTAINER_TYPES.has(selected.type)) {
      parentId = selected.id
    } else if (selectedParent) {
      parentId = selectedParent.id
    }
    addNode(type, parentId)
  }

  const selectFromCanvas = (nodeId) => {
    selectNode(nodeId)
  }

  const removeSelected = (nodeId = selectedId) => {
    if (!nodeId) return
    deleteNode(nodeId)
  }

  const startDrag = (event, node) => {
    if (event.button !== 0 || node.locked) return
    event.preventDefault()
    event.stopPropagation()
    if (dragRef.current) return
    const ghost = document.createElement('div')
    ghost.className = 'vc-drag-ghost'
    ghost.textContent = node.name || NODE_LABELS[node.type] || node.type
    ghost.style.transform = `translate3d(${event.clientX + 12}px, ${
      event.clientY + 12
    }px, 0)`
    document.body.appendChild(ghost)
    canvasRef.current?.classList.add('is-dragging-node')
    setDraggingId(node.id)
    setDropHint('')
    dragRef.current = {
      nodeId: node.id,
      ghost,
      frame: 0,
      x: event.clientX,
      y: event.clientY,
      targetId: '',
      targetIndex: -1,
      highlighted: null,
      listeners: null,
    }

    const highlight = (targetEl, invalid) => {
      const current = dragRef.current
      if (!current) return
      if (current.highlighted && current.highlighted !== targetEl) {
        current.highlighted.classList.remove(
          DROP_TARGET_CLASS,
          DROP_INVALID_CLASS,
        )
      }
      current.highlighted = targetEl
      if (targetEl) {
        targetEl.classList.toggle(DROP_TARGET_CLASS, !invalid)
        targetEl.classList.toggle(DROP_INVALID_CLASS, invalid)
      }
    }

    const resolveDrop = () => {
      const current = dragRef.current
      if (!current) return
      const point = document.elementFromPoint(current.x, current.y)
      if (!point || !canvasRef.current?.contains(point)) {
        current.targetId = ''
        current.targetIndex = -1
        highlight(null, false)
        return
      }
      const parentEl = point.closest('[data-visual-drop-id]')
      if (!parentEl) {
        current.targetId = ''
        current.targetIndex = -1
        highlight(null, false)
        return
      }
      const parentId = parentEl.getAttribute('data-visual-drop-id')
      if (!parentId) return
      const source = findVisualNode(doc.root, current.nodeId)
      const target = findVisualNode(doc.root, parentId)
      if (!source || !target) return
      const invalid =
        source.node.id === parentId ||
        isVisualDescendant(source.node, parentId) ||
        target.node.locked
      highlight(parentEl, invalid)
      if (invalid) {
        current.targetId = ''
        current.targetIndex = -1
        return
      }
      // 只选择“属于当前父容器”的子节点区域，避免 querySelector
      // 命中更深层嵌套容器的 children。
      const childArea = Array.from(
        parentEl.querySelectorAll('[data-visual-children], .vc-page-body'),
      ).find(
        (element) =>
          element.closest('[data-visual-drop-id]') === parentEl,
      )
      let index = childArea ? childArea.children.length : 0
      if (childArea) {
        const children = Array.from(childArea.children).filter((element) =>
          element.hasAttribute('data-visual-node-id'),
        )
        for (let childIndex = 0; childIndex < children.length; childIndex += 1) {
          const rect = children[childIndex].getBoundingClientRect()
          if (current.y >= rect.top && current.y <= rect.bottom) {
            index = childIndex + (current.y > rect.top + rect.height / 2 ? 1 : 0)
            break
          }
        }
      }
      current.targetId = parentId
      current.targetIndex = index
    }

    const frame = () => {
      const current = dragRef.current
      if (!current) return
      current.frame = 0
      current.ghost.style.transform = `translate3d(${current.x + 12}px, ${
        current.y + 12
      }px, 0)`
      resolveDrop()
    }

    const onMove = (moveEvent) => {
      const current = dragRef.current
      if (!current) return
      current.x = moveEvent.clientX
      current.y = moveEvent.clientY
      if (!current.frame) current.frame = window.requestAnimationFrame(frame)
    }

    const onUp = () => {
      const current = dragRef.current
      if (!current) return
      // 最后一帧可能尚未执行 rAF；落手时同步再解析一次，保证极快点击
      // / 自动化操作也能得到确定落点，而不是依赖上一帧缓存。
      resolveDrop()
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onUp)
      if (current.frame) window.cancelAnimationFrame(current.frame)
      current.highlighted?.classList.remove(
        DROP_TARGET_CLASS,
        DROP_INVALID_CLASS,
      )
      current.ghost.remove()
      dragRef.current = null
      canvasRef.current?.classList.remove('is-dragging-node')
      setDraggingId(null)
      setDropHint('')
      if (current.targetId && current.targetIndex >= 0) {
        moveNode(current.nodeId, current.targetId, current.targetIndex)
      }
    }

    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onUp)
  }

  useEffect(() => {
    return () => {
      const current = dragRef.current
      if (current) {
        window.cancelAnimationFrame(current.frame)
        current.ghost?.remove()
        current.highlighted?.classList.remove(
          DROP_TARGET_CLASS,
          DROP_INVALID_CLASS,
        )
        dragRef.current = null
      }
    }
  }, [])

  const renderNodeList = (children) =>
    children.map((child) => (
      <VisualNodeView
        key={child.id}
        node={child}
        depth={1}
        selectedId={selectedId}
        draggingId={draggingId}
        onSelect={selectFromCanvas}
        onStartDrag={startDrag}
        onDelete={removeSelected}
        onDuplicate={duplicateNode}
        renderNodeList={renderNodeList}
      />
    ))

  return (
    <div className="vc-shell">
      <header className="vc-topbar">
        <div className="vc-title-block">
          <span className="eyebrow">Offline Visual Canvas</span>
          <h1>{doc.title}</h1>
          <p>
            离线编辑 · 不写源码 · 不启动沙盒 · 节点 {countVisualNodes(doc.root)} 个
          </p>
        </div>
        <div className="vc-topbar-actions">
          <span className={`vc-validate ${report.ok ? 'is-ok' : 'is-error'}`}>
            {report.ok ? 'Schema 通过' : `${report.errors.length} 个校验错误`}
          </span>
          <span className="vc-revision">r{doc.revision}</span>
          <button
            type="button"
            className="ghost-button"
            disabled={!history.length}
            onClick={undo}
          >
            ↶ 撤销
          </button>
          <button
            type="button"
            className="ghost-button"
            disabled={!future.length}
            onClick={redo}
          >
            ↷ 重做
          </button>
          <button
            type="button"
            className="ghost-button"
            onClick={() => resetDocument({ withRoot: true })}
          >
            清空画布
          </button>
          <a className="primary-button" href="/workspace">
            返回工作台
          </a>
        </div>
      </header>

      {!report.ok && (
        <div className="vc-validation-banner" role="alert">
          <strong>VisualDocument 校验未通过</strong>
          <span>{report.errors.slice(0, 3).join('；')}</span>
        </div>
      )}
      {lastError && (
        <div className="vc-validation-banner is-warning" role="status">
          <span>{lastError}</span>
          <button type="button" onClick={clearError}>
            关闭
          </button>
        </div>
      )}

      <div className="vc-layout">
        <aside className="vc-palette" aria-label="组件库">
          <div className="vc-panel-head">
            <h2>组件库</h2>
            <span>点击添加</span>
          </div>
          <div className="vc-palette-grid">
            {PALETTE.map(([type, label, icon]) => (
              <button
                key={type}
                type="button"
                className="vc-palette-item"
                onClick={() => addToSelection(type)}
              >
                <span className={`vc-palette-icon vc-badge-${type}`}>
                  {icon}
                </span>
                <span>{label}</span>
              </button>
            ))}
          </div>
          <div className="vc-hint-box">
            <strong>离线优先</strong>
            <p>
              增删、换父容器、文本和布局都只改本地 JSON 树。
              代码会在你确认并点击“生成全栈”后才一次性编译。
            </p>
          </div>
        </aside>

        <main className="vc-canvas-area">
          <div className="vc-canvas-toolbar">
            <span>拖拽左上角 ⠿ 换父容器</span>
            <span>{dropHint}</span>
            <span>{doc.status}</span>
          </div>
          <div className="vc-canvas-scroll" ref={canvasRef}>
            {doc.root ? (
              <div className="vc-canvas-frame">
                <VisualNodeView
                  node={doc.root}
                  depth={0}
                  selectedId={selectedId}
                  draggingId={draggingId}
                  onSelect={selectFromCanvas}
                  onStartDrag={startDrag}
                  onDelete={removeSelected}
                  onDuplicate={duplicateNode}
                  renderNodeList={renderNodeList}
                />
              </div>
            ) : (
              <div className="vc-empty-canvas">
                <div>
                  <strong>画布已清空</strong>
                  <p>从左侧添加一个组件，会自动创建页面根节点。</p>
                  <button
                    type="button"
                    className="primary-button"
                    onClick={() => addNode('section')}
                  >
                    ＋ 添加区块
                  </button>
                </div>
              </div>
            )}
          </div>
        </main>

        <aside className="vc-inspector" aria-label="属性面板">
          <div className="vc-panel-head">
            <h2>属性</h2>
            <span>{selected ? selected.type : '未选择'}</span>
          </div>
          {selected ? (
            <div className="vc-inspector-body">
              <label>
                节点名称
                <input
                  value={selected.name || ''}
                  onChange={(event) =>
                    updateNode(selected.id, { name: event.target.value })
                  }
                />
              </label>
              {selected.type === 'text' || selected.type === 'button' ? (
                <label>
                  文案
                  <input
                    value={selected.props?.text || ''}
                    onChange={(event) =>
                      updateNode(selected.id, {
                        props: { text: event.target.value },
                      })
                    }
                  />
                </label>
              ) : null}
              {selected.type === 'input' ? (
                <label>
                  占位提示
                  <input
                    value={selected.props?.placeholder || ''}
                    onChange={(event) =>
                      updateNode(selected.id, {
                        props: { placeholder: event.target.value },
                      })
                    }
                  />
                </label>
              ) : null}
              {selected.type === 'image' ? (
                <label>
                  图片地址
                  <input
                    value={selected.props?.src || ''}
                    onChange={(event) =>
                      updateNode(selected.id, {
                        props: { src: event.target.value },
                      })
                    }
                  />
                </label>
              ) : null}
              {CONTAINER_TYPES.has(selected.type) ? (
                <>
                  <div className="vc-control-row">
                    <label>
                      方向
                      <select
                        value={selected.layout?.direction || 'column'}
                        onChange={(event) =>
                          updateNode(selected.id, {
                            layout: { direction: event.target.value },
                          })
                        }
                      >
                        <option value="column">纵向</option>
                        <option value="row">横向</option>
                      </select>
                    </label>
                    <label>
                      对齐
                      <select
                        value={selected.layout?.align || 'stretch'}
                        onChange={(event) =>
                          updateNode(selected.id, {
                            layout: { align: event.target.value },
                          })
                        }
                      >
                        <option value="stretch">拉伸</option>
                        <option value="start">起始</option>
                        <option value="center">居中</option>
                        <option value="end">末尾</option>
                      </select>
                    </label>
                  </div>
                  <div className="vc-control-row">
                    <label>
                      间距
                      <input
                        value={selected.layout?.gap || ''}
                        placeholder="16px"
                        onChange={(event) =>
                          updateNode(selected.id, {
                            layout: { gap: event.target.value },
                          })
                        }
                      />
                    </label>
                    <label>
                      内边距
                      <input
                        value={selected.layout?.padding || ''}
                        placeholder="16px"
                        onChange={(event) =>
                          updateNode(selected.id, {
                            layout: { padding: event.target.value },
                          })
                        }
                      />
                    </label>
                  </div>
                  {selected.layout?.display === 'grid' ? (
                    <label>
                      列数
                      <input
                        type="number"
                        min="1"
                        max="12"
                        value={selected.layout?.columns || 1}
                        onChange={(event) =>
                          updateNode(selected.id, {
                            layout: {
                              columns: Number(event.target.value) || 1,
                            },
                          })
                        }
                      />
                    </label>
                  ) : null}
                  <label>
                    最大宽度
                    <input
                      value={selected.layout?.maxWidth || ''}
                      placeholder="1180px"
                      onChange={(event) =>
                        updateNode(selected.id, {
                          layout: { maxWidth: event.target.value },
                        })
                      }
                    />
                  </label>
                </>
              ) : null}
              <div className="vc-inspector-actions">
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() => duplicateNode(selected.id)}
                >
                  复制节点
                </button>
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() => {
                    useVisualDocumentStore
                      .getState()
                      .toggleLock(selected.id)
                  }}
                >
                  {selected.locked ? '解锁' : '锁定'}
                </button>
                <button
                  type="button"
                  className="danger-button"
                  onClick={() => deleteNode(selected.id)}
                >
                  删除节点
                </button>
              </div>
              <div className="vc-inspector-meta">
                <code>{selected.id}</code>
                <span>
                  {selected.provenance?.createdBy || 'user'} · revision{' '}
                  {selected.provenance?.revision ?? 1}
                </span>
              </div>
            </div>
          ) : (
            <p className="vc-inspector-empty">在画布中选择一个节点查看属性。</p>
          )}
        </aside>
      </div>
    </div>
  )
}
