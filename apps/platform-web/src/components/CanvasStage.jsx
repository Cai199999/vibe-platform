import { useEffect, useMemo, useRef, useState } from 'react'
import useFillViewport, {
  VIEWPORT_BASE_HEIGHT,
  VIEWPORT_BASE_WIDTH,
} from '../workspace/useFillViewport.js'
import usePanZoom from '../workspace/usePanZoom.js'

const clamp01 = (value) => Math.min(1, Math.max(0, value))

const POPOVER_WIDTH = 320
const POPOVER_MAX_HEIGHT = 360
const POPOVER_GAP = 14
const MAX_ANNOTATIONS = 3

// 画布 Mock 页面与沙盒初始源码共享的 data-vibe-target 标记。
// 圈选时优先按选区中心命中的元素确定目标，帮助 Agent 把自然语言指令绑定到
// 正确的组件，而不是把三个标注都当成同一个按钮。
const TARGET_META = {
  'cta-button': { label: '按钮', role: 'button', component: 'App' },
  title: { label: '标题', role: 'heading', component: 'App' },
  copy: { label: '说明文字', role: 'paragraph', component: 'App' },
  kicker: { label: '小标题', role: 'text', component: 'App' },
  card: { label: '卡片', role: 'card', component: 'App' },
}

function normalizeRect(originX, originY, x, y) {
  return {
    x: Math.min(originX, x),
    y: Math.min(originY, y),
    width: Math.abs(x - originX),
    height: Math.abs(y - originY),
  }
}

function readTarget(element) {
  const node = element?.closest?.('[data-vibe-target]')
  if (!node) return null
  const marker = node.dataset?.vibeTarget
  if (!marker) return null
  const meta = TARGET_META[marker] ?? {
    label: marker,
    role: node.tagName?.toLowerCase() || 'element',
    component: 'App',
  }
  return {
    marker,
    selector: `[data-vibe-target='${marker}']`,
    label: meta.label,
    role: meta.role,
    component: meta.component,
    tag: node.tagName?.toLowerCase() || '',
  }
}

/**
 * 浮层与圈使用同一套“世界坐标”（1280×720 设计视口）。
 *
 * 浮层是 annotation-layer 的普通子元素，和页面共享父级 translate + scale，
 * 不做任何 1/scale 反向补偿：缩放画布时圆角、字号、输入框与浮层一起等比
 * 缩放，视觉上等同于画在画布上的一张图片。因此这里所有尺寸 / 间距都直接
 * 使用世界单位，不再除以 scale。
 */
function popoverStyleFor(rect, index, active) {
  if (!rect) return null
  const width = VIEWPORT_BASE_WIDTH
  const height = VIEWPORT_BASE_HEIGHT
  const leftPx = rect.x * width
  const rightPx = (rect.x + rect.width) * width
  const centerY = (rect.y + rect.height / 2) * height
  // 同时存在多个浮层时按顺序轻微错开，避免完全重叠。
  const stagger = index * 12
  const clampTop = (value) =>
    Math.min(
      Math.max(10, value + stagger),
      Math.max(10, height - POPOVER_MAX_HEIGHT - 10),
    )
  // 浮层永远高于圈线层（圈最高 100），激活的浮层再抬高一层。
  const zIndex = active ? 320 : 300 + index

  if (width - rightPx >= POPOVER_WIDTH + POPOVER_GAP) {
    return {
      left: rightPx + POPOVER_GAP,
      top: clampTop(centerY - POPOVER_MAX_HEIGHT / 2),
      zIndex,
    }
  }
  if (leftPx >= POPOVER_WIDTH + POPOVER_GAP) {
    return {
      right: width - leftPx + POPOVER_GAP,
      top: clampTop(centerY - POPOVER_MAX_HEIGHT / 2),
      zIndex,
    }
  }

  // 窄屏兜底：横跨选区下方 / 上方，固定宽度水平居中。
  const below = (rect.y + rect.height) * height + POPOVER_GAP
  const above = rect.y * height - POPOVER_MAX_HEIGHT - POPOVER_GAP
  const top =
    below + POPOVER_MAX_HEIGHT <= height - 10 ? below : Math.max(10, above)
  return {
    left: Math.max(10, (width - POPOVER_WIDTH) / 2),
    width: POPOVER_WIDTH,
    top: clampTop(top),
    zIndex,
  }
}

/**
 * 单个标注旁的迷你 Chatbox。
 *
 * 每个标注拥有独立 threadId / chatHistory；用户发送后立即触发该标注的
 * 增量 Run，后端复用沙盒，并把同一圈的历史与上一次修改记录送入模型。
 */
function AnnotationPopover({
  annotation,
  index,
  submitting,
  pending,
  pendingAction,
  active,
  panReady,
  inputRef,
  onChange,
  onRemove,
  onSend,
  onRollback,
  onActivate,
  onDeactivate,
}) {
  const historyRef = useRef(null)
  const style = useMemo(
    () => popoverStyleFor(annotation.rect, index, active),
    [annotation.rect, index, active],
  )
  const history = annotation.chatHistory ?? []

  useEffect(() => {
    const node = historyRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [history.length, pending])

  if (!style) return null

  const sending = Boolean(submitting && pending)
  const rollingBack = sending && pendingAction === 'rollback'
  return (
    <form
      className={`canvas-popover tone-${index % 3} ${active ? 'is-active' : ''}`}
      style={style}
      role="dialog"
      aria-label={`标注 ${index + 1} 多轮对话`}
      data-annotation-id={annotation.id}
      onPointerDown={(event) => {
        // 中键 / 空格+左键是全局平移手势：即使从浮层上按下也必须继续
        // 冒泡给画布，否则浮层覆盖画布中心时会出现“中间区域无法拖动”的死角。
        if (event.button === 1 || (event.button === 0 && panReady)) return
        event.stopPropagation()
        onActivate?.(annotation.id)
      }}
      onPointerUp={(event) => {
        if (event.button === 1 || (event.button === 0 && panReady)) return
        event.stopPropagation()
      }}
      onFocusCapture={() => onActivate?.(annotation.id)}
      onBlurCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) {
          onDeactivate?.(annotation.id)
        }
      }}
      onSubmit={(event) => {
        event.preventDefault()
        onSend?.(annotation.id)
      }}
    >
      <div className="popover-head">
        <span className="popover-dot" aria-label={`标注 ${index + 1}`} />
        <button
          className="popover-remove"
          type="button"
          title="删除这个标注"
          aria-label={`删除标注 ${index + 1}`}
          disabled={submitting}
          onClick={() => onRemove?.(annotation.id)}
        >
          ×
        </button>
      </div>

      {history.length > 0 && (
        <div
          className="popover-chat"
          ref={historyRef}
          aria-label={`标注 ${index + 1} 对话历史`}
        >
          {history.map((message, messageIndex) => (
            <div
              key={message.id || `${message.role}-${messageIndex}`}
              className={`chat-bubble is-${message.role}`}
              data-turn={message.turnCount ?? undefined}
            >
              <span className="chat-role">
                {message.role === 'user' ? '你' : 'Agent'}
              </span>
              <p>{message.content}</p>
              {message.role === 'assistant' && message.commitHash && (
                <div className="chat-checkpoint">
                  <span
                    className="chat-commit"
                    title={`Git ${message.commitHash}`}
                  >
                    turn {message.turnCount} · {message.commitHash.slice(0, 7)}
                  </span>
                  <button
                    className="chat-rollback"
                    type="button"
                    disabled={submitting}
                    title="恢复这一轮的代码与对话，丢弃之后的所有修改"
                    onClick={() => onRollback?.(annotation.id, message)}
                  >
                    ↩️ 回滚到此步
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      <div className="popover-row">
        <input
          ref={inputRef}
          type="text"
          value={annotation.intent}
          maxLength={120}
          placeholder="描述修改意图..."
          disabled={submitting}
          onChange={(event) => onChange?.(annotation.id, event.target.value)}
        />
        <button
          className="popover-send"
          type="submit"
          disabled={submitting || !annotation.intent.trim()}
          title="只对当前圈执行一次增量修改"
        >
          {sending ? <span className="mini-spinner" aria-label="执行中" /> : '发送'}
        </button>
      </div>
      {sending && (
        <p className="popover-hint">
          {rollingBack ? '正在恢复…' : '正在修改…'}
        </p>
      )}
    </form>
  )
}

/**
 * 画布 Stage：
 *  - 展示一个与沙盒初始源码一致的 Mock 页面；
 *  - 鼠标 / 触控拖拽圈选组件，坐标转换为 1280×720 世界坐标；
 *  - 页面与标注层使用同一 transform，平移 / 缩放后圈不会漂移；
 *  - 每个圈拥有独立多轮 Chatbox，可单独发送增量修改。
 */
export default function CanvasStage({
  annotations = [],
  onAnnotationAdd,
  onAnnotationIntentChange,
  onAnnotationRemove,
  onAnnotationSend,
  onAnnotationRollback,
  pendingAnnotationId,
  pendingAction,
  submitting,
  disabled,
}) {
  const stageRef = useRef(null)
  const startTargetRef = useRef(null)
  const inputRefs = useRef(new Map())
  const focusedAnnotationRef = useRef(null)
  const limitTimerRef = useRef(null)
  const [draft, setDraft] = useState(null)
  const [drawing, setDrawing] = useState(false)
  const [limitNotice, setLimitNotice] = useState(false)
  // 当前激活的标注：浮层 / 圈提升到最高 z-index，点击画布空白处恢复。
  const [activeAnnotationId, setActiveAnnotationId] = useState(null)
  // 用户圈选多个区域时，可临时隐藏浮层，避免输入框挡住下一笔圈选。
  const [popoversHidden, setPopoversHidden] = useState(false)

  // 与沙盒预览共用 1280×720 设计视口：两边缩放参数与页面尺寸完全一致。
  const {
    scale: baseScale,
    offsetX: baseOffsetX,
    offsetY: baseOffsetY,
  } = useFillViewport(stageRef)
  // 画布自己的平移 / 缩放状态，保存在 ProjectWorkspace 级别的 ViewportContext。
  const viewport = usePanZoom({
    containerRef: stageRef,
    panel: 'canvas',
    baseScale,
    baseOffsetX,
    baseOffsetY,
  })

  // 新增标注后自动展开浮层并聚焦它的输入框；编辑已有标注不会抢焦点。
  useEffect(() => {
    const last = annotations[annotations.length - 1]
    if (!last || last.id === focusedAnnotationRef.current) return undefined
    focusedAnnotationRef.current = last.id
    setPopoversHidden(false)
    setActiveAnnotationId(last.id)
    const frame = window.requestAnimationFrame(() =>
      inputRefs.current.get(last.id)?.focus(),
    )
    return () => window.cancelAnimationFrame(frame)
  }, [annotations])

  useEffect(
    () => () => {
      if (limitTimerRef.current) window.clearTimeout(limitTimerRef.current)
    },
    [],
  )

  const atLimit = annotations.length >= MAX_ANNOTATIONS

  const flashLimitNotice = () => {
    setLimitNotice(true)
    if (limitTimerRef.current) window.clearTimeout(limitTimerRef.current)
    limitTimerRef.current = window.setTimeout(() => setLimitNotice(false), 2600)
  }

  const activateAnnotation = (annotationId) => {
    setActiveAnnotationId(annotationId)
  }

  const deactivateAnnotation = (annotationId) => {
    setActiveAnnotationId((current) =>
      current === annotationId ? null : current,
    )
  }

  /**
   * Screen -> World：先减去画布在屏幕上的左上角与当前 translate，再除以
   * 当前 scale，最后归一化到 1280×720 设计视口。这样无论怎么平移 / 缩放，
   * 圈记录的都是页面内容坐标，而不是屏幕坐标。
   */
  const toPoint = (event) => {
    const node = stageRef.current
    if (!node || viewport.scale <= 0) return { x: 0, y: 0 }
    const box = node.getBoundingClientRect()
    const worldX =
      (event.clientX - box.left - viewport.translateX) / viewport.scale
    const worldY =
      (event.clientY - box.top - viewport.translateY) / viewport.scale
    return {
      x: clamp01(worldX / VIEWPORT_BASE_WIDTH),
      y: clamp01(worldY / VIEWPORT_BASE_HEIGHT),
    }
  }

  /** 忽略标注浮层，找到页面内容上的 data-vibe-target。 */
  const targetAtPoint = (clientX, clientY) => {
    if (!Number.isFinite(clientX) || !Number.isFinite(clientY)) return null
    const elements = document.elementsFromPoint(clientX, clientY)
    for (const element of elements) {
      if (
        element.closest?.('.annotation-layer') ||
        element.closest?.('.viewport-controls') ||
        element.closest?.('.annotation-limit') ||
        element.closest?.('.canvas-hint')
      ) {
        continue
      }
      const target = readTarget(element)
      if (target) return target
    }
    return null
  }

  const handlePointerDown = (event) => {
    // 中键 / 空格 + 左键优先进入平移，避免误触发画布圈选。
    if (viewport.startPan(event)) {
      return
    }
    if (disabled || event.button !== 0) {
      return
    }
    // 点击标注标签 = 选中该圈；不开始新的圈选。
    const annotationNode = event.target.closest?.('[data-annotation-id]')
    if (annotationNode) {
      activateAnnotation(annotationNode.getAttribute('data-annotation-id'))
      return
    }
    setActiveAnnotationId(null)
    if (atLimit) {
      flashLimitNotice()
      return
    }
    event.preventDefault()
    const point = toPoint(event)
    startTargetRef.current = readTarget(event.target)
    event.currentTarget.setPointerCapture(event.pointerId)
    setDrawing(true)
    setDraft({ originX: point.x, originY: point.y, x: point.x, y: point.y })
  }

  const handlePointerMove = (event) => {
    if (viewport.movePan(event)) {
      return
    }
    if (!drawing || !draft) {
      return
    }
    const point = toPoint(event)
    setDraft((current) => (current ? { ...current, x: point.x, y: point.y } : current))
  }

  const finishDrawing = (event) => {
    if (viewport.endPan(event)) {
      return
    }
    if (!drawing || !draft) {
      return
    }
    const point = toPoint(event)
    const rect = normalizeRect(draft.originX, draft.originY, point.x, point.y)
    setDrawing(false)
    setDraft(null)

    if (rect.width > 0.02 && rect.height > 0.02) {
      // 以选区中心的页面元素为准；起点落在卡片留白时也能识别到按钮 / 标题。
      const box = stageRef.current.getBoundingClientRect()
      const centerWorldX = (rect.x + rect.width / 2) * VIEWPORT_BASE_WIDTH
      const centerWorldY = (rect.y + rect.height / 2) * VIEWPORT_BASE_HEIGHT
      const centerX =
        box.left + viewport.translateX + centerWorldX * viewport.scale
      const centerY =
        box.top + viewport.translateY + centerWorldY * viewport.scale
      const target = targetAtPoint(centerX, centerY) ?? startTargetRef.current
      onAnnotationAdd?.({ rect, target })
    }
    startTargetRef.current = null
  }

  const draftRect = draft
    ? normalizeRect(draft.originX, draft.originY, draft.x, draft.y)
    : null

  const skipPopovers = drawing || popoversHidden

  return (
    <div className="canvas-frame">
      <div className="canvas-chrome">
        <span className="chrome-dots">
          <i className="dot-red" />
          <i className="dot-amber" />
          <i className="dot-green" />
        </span>
      </div>

      <div
        ref={stageRef}
        className={`canvas-stage ${disabled ? 'is-disabled' : ''} ${drawing ? 'is-drawing' : ''} ${viewport.spaceHeld ? 'is-pan-ready' : ''} ${viewport.panning ? 'is-panning' : ''}`}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={finishDrawing}
        onPointerCancel={finishDrawing}
        onAuxClick={(event) => event.preventDefault()}
      >
        {/* 页面层与标注层共享同一个 transform：两者“长在一起”。 */}
        <div
          className="canvas-viewport"
          style={{
            width: `${VIEWPORT_BASE_WIDTH}px`,
            height: `${VIEWPORT_BASE_HEIGHT}px`,
            transform: `translate(${viewport.translateX}px, ${viewport.translateY}px) scale(${viewport.scale})`,
            transformOrigin: 'top left',
          }}
        >
          <div className="mock-page">
            <section className="mock-card" data-vibe-target="card">
              <p className="mock-kicker" data-vibe-target="kicker">
                Vibe Studio
              </p>
              <h1 className="mock-title" data-vibe-target="title">
                把想法画出来。
                <br />
                剩下的交给 Agent。
              </h1>
              <p className="mock-copy" data-vibe-target="copy">
                在画布上圈选任意组件，写下修改意图。Agent 会生成代码、运行构建，并自动修复错误。
              </p>
              <button className="mock-cta" data-vibe-target="cta-button" type="button">
                开始体验
              </button>
              <span className="cta-hint">圈选任意组件，写下你的修改意图</span>
            </section>
          </div>

          <div className="annotation-layer">
            {annotations.map((annotation, index) => {
              const active = annotation.id === activeAnnotationId
              return (
                <div
                  key={annotation.id}
                  className={`annotation-ellipse tone-${index % 3} ${active ? 'is-active' : ''}`}
                  data-annotation-id={annotation.id}
                  data-annotation-index={index}
                  style={{
                    left: `${annotation.rect.x * 100}%`,
                    top: `${annotation.rect.y * 100}%`,
                    width: `${annotation.rect.width * 100}%`,
                    height: `${annotation.rect.height * 100}%`,
                    zIndex: active ? 100 : 20 + index,
                  }}
                >
                  <span className="annotation-tag">标注 {index + 1}</span>
                </div>
              )
            })}

            {draftRect && (
              <div
                className="annotation-ellipse is-draft"
                style={{
                  left: `${draftRect.x * 100}%`,
                  top: `${draftRect.y * 100}%`,
                  width: `${draftRect.width * 100}%`,
                  height: `${draftRect.height * 100}%`,
                }}
              >
                <span className="annotation-tag">新标注</span>
              </div>
            )}

            {!skipPopovers &&
              annotations.map((annotation, index) => (
                <AnnotationPopover
                  key={annotation.id}
                  annotation={annotation}
                  index={index}
                  submitting={submitting}
                  pending={pendingAnnotationId === annotation.id}
                  pendingAction={pendingAnnotationId === annotation.id ? pendingAction : null}
                  active={annotation.id === activeAnnotationId}
                  panReady={viewport.spaceHeld}
                  inputRef={(node) => {
                    if (node) inputRefs.current.set(annotation.id, node)
                    else inputRefs.current.delete(annotation.id)
                  }}
                  onChange={onAnnotationIntentChange}
                  onRemove={onAnnotationRemove}
                  onSend={onAnnotationSend}
                  onRollback={onAnnotationRollback}
                  onActivate={activateAnnotation}
                  onDeactivate={deactivateAnnotation}
                />
              ))}
          </div>
        </div>

        {(atLimit || limitNotice) && (
          <div
            className={`annotation-limit ${limitNotice ? 'is-flashing' : ''}`}
            role="status"
            data-annotation-limit
          >
            最多支持 3 个标注 · {annotations.length}/3
            {limitNotice
              ? ' · 请先删除或清除一个标注，再继续圈选'
              : ' · 已达上限'}
          </div>
        )}

        <div className="viewport-controls" onPointerDown={(event) => event.stopPropagation()}>
          <span className="viewport-zoom" data-viewport-zoom>
            {viewport.zoomPercent}%
          </span>
          <button
            className="viewport-reset"
            type="button"
            onClick={viewport.reset}
            title="恢复 100% 并回到初始位置"
          >
            重置视图
          </button>
          {annotations.length > 0 && (
            <button
              className="viewport-reset"
              type="button"
              aria-pressed={popoversHidden}
              title="临时隐藏标注输入框，方便继续圈选下一个区域"
              onClick={() => setPopoversHidden((value) => !value)}
            >
              {popoversHidden ? '显示标注输入' : '隐藏标注输入'}
            </button>
          )}
        </div>

        <div className="canvas-hint">
          {drawing
            ? '松开鼠标完成圈选'
            : '圈选组件 · 空格/中键平移 · 滚轮缩放'}
        </div>
      </div>
    </div>
  )
}
