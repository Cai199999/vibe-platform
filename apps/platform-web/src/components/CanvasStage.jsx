import { useEffect, useMemo, useRef, useState } from 'react'
import useFillViewport, {
  VIEWPORT_BASE_HEIGHT,
  VIEWPORT_BASE_WIDTH,
} from '../workspace/useFillViewport.js'
import usePanZoom from '../workspace/usePanZoom.js'

const clamp01 = (value) => Math.min(1, Math.max(0, value))

const POPOVER_WIDTH = 272
const POPOVER_HEIGHT = 164
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

function popoverStyleFor(rect, stageSize, index) {
  if (!rect || stageSize.width === 0) return null
  const { width, height } = stageSize
  const leftPx = rect.x * width
  const rightPx = (rect.x + rect.width) * width
  const centerY = (rect.y + rect.height / 2) * height
  // 同时存在多个浮层时按顺序轻微错开，避免完全重叠。
  const stagger = index * 12
  const clampTop = (value) =>
    Math.min(
      Math.max(12, value + stagger),
      Math.max(12, height - POPOVER_HEIGHT - 12),
    )

  if (width - rightPx >= POPOVER_WIDTH + POPOVER_GAP) {
    return {
      left: rightPx + POPOVER_GAP,
      top: clampTop(centerY - POPOVER_HEIGHT / 2),
      zIndex: 30 + index,
    }
  }
  if (leftPx >= POPOVER_WIDTH + POPOVER_GAP) {
    return {
      right: width - leftPx + POPOVER_GAP,
      top: clampTop(centerY - POPOVER_HEIGHT / 2),
      zIndex: 30 + index,
    }
  }

  // 窄屏兜底：横跨选区下方 / 上方，宽度由 left/right 自适应。
  const below = (rect.y + rect.height) * height + 12
  const above = rect.y * height - POPOVER_HEIGHT - 12
  const top = below + POPOVER_HEIGHT <= height - 12 ? below : Math.max(12, above)
  return {
    left: 12,
    right: 12,
    width: 'auto',
    top: clampTop(top),
    zIndex: 30 + index,
  }
}

/**
 * 单个标注旁的浮动输入框。
 *
 * 每个标注拥有独立输入内容；用户可在画布上连续圈出最多 3 个区域，最后在
 * 顶部工具栏点击「提交标注」一次性提交 AnnotationEnvelope[]。
 */
function AnnotationPopover({
  annotation,
  index,
  total,
  stageSize,
  submitting,
  inputRef,
  onChange,
  onRemove,
}) {
  const style = useMemo(
    () => popoverStyleFor(annotation.rect, stageSize, index),
    [annotation.rect, stageSize, index],
  )
  if (!style) return null

  const targetLabel = annotation.target?.label || '组件'
  return (
    <form
      className={`canvas-popover tone-${index % 3}`}
      style={style}
      role="dialog"
      aria-label={`标注 ${index + 1} 修改意图`}
      data-annotation-id={annotation.id}
      onPointerDown={(event) => event.stopPropagation()}
      onPointerUp={(event) => event.stopPropagation()}
      onSubmit={(event) => event.preventDefault()}
    >
      <div className="popover-head">
        <span className="popover-dot" />
        <span>
          标注 {index + 1} / {total}
        </span>
        <span className="popover-target">{targetLabel}</span>
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
      <div className="popover-row">
        <input
          ref={inputRef}
          type="text"
          value={annotation.intent}
          maxLength={120}
          placeholder="例如：改成蓝色 / 文字改为提交 / 放大一倍"
          disabled={submitting}
          onChange={(event) => onChange?.(annotation.id, event.target.value)}
        />
      </div>
      <p className="popover-hint">
        {annotation.intent.trim()
          ? '已填写 · 在顶部点击「提交标注」'
          : '每个标注写一条指令，最多 3 个标注'}
      </p>
    </form>
  )
}

/**
 * 画布 Stage：
 *  - 展示一个与沙盒初始源码一致的 Mock 页面；
 *  - 鼠标 / 触控拖拽圈选组件，坐标归一化到 0~1；
 *  - 最多保留 3 个标注，每个标注都有自己的浮动意图输入框；
 *  - 圈选完成后由顶部「提交标注」统一发送 AnnotationEnvelope[]。
 */
export default function CanvasStage({
  annotations = [],
  onAnnotationAdd,
  onAnnotationIntentChange,
  onAnnotationRemove,
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
  const [stageSize, setStageSize] = useState({ width: 0, height: 0 })
  const [limitNotice, setLimitNotice] = useState(false)
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

  // 监听画布尺寸，浮层按像素计算左右/上下空间，避免超出可视区域。
  useEffect(() => {
    const node = stageRef.current
    if (!node) return undefined
    const update = () => setStageSize({ width: node.clientWidth, height: node.clientHeight })
    update()
    const observer = new ResizeObserver(update)
    observer.observe(node)
    return () => observer.disconnect()
  }, [])

  // 新增标注后自动展开浮层并聚焦它的输入框；编辑已有标注不会抢焦点。
  useEffect(() => {
    const last = annotations[annotations.length - 1]
    if (!last || last.id === focusedAnnotationRef.current) return undefined
    focusedAnnotationRef.current = last.id
    setPopoversHidden(false)
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

  const toPoint = (event) => {
    const box = stageRef.current.getBoundingClientRect()
    return {
      x: clamp01((event.clientX - box.left) / box.width),
      y: clamp01((event.clientY - box.top) / box.height),
    }
  }

  const targetAtPoint = (clientX, clientY) => {
    if (!Number.isFinite(clientX) || !Number.isFinite(clientY)) return null
    return readTarget(document.elementFromPoint(clientX, clientY))
  }

  const handlePointerDown = (event) => {
    // 中键 / 空格 + 左键优先进入平移，避免误触发画布圈选。
    if (viewport.startPan(event)) {
      return
    }
    if (disabled || event.button !== 0) {
      return
    }
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
      // 以选区中心命中的元素为准；起点落在卡片留白时也能识别到按钮 / 标题。
      const box = stageRef.current.getBoundingClientRect()
      const centerX = box.left + (rect.x + rect.width / 2) * box.width
      const centerY = box.top + (rect.y + rect.height / 2) * box.height
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
        <span className="chrome-url">vibe://canvas/preview</span>
        <span className="chrome-badge">src/App.jsx</span>
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
        {/* 页面层与 iframe 使用同一套基准变换，再叠加用户平移 / 缩放。 */}
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
        </div>

        {annotations.map((annotation, index) => (
          <div
            key={annotation.id}
            className={`annotation-ellipse tone-${index % 3}`}
            data-annotation-index={index}
            style={{
              left: `${annotation.rect.x * 100}%`,
              top: `${annotation.rect.y * 100}%`,
              width: `${annotation.rect.width * 100}%`,
              height: `${annotation.rect.height * 100}%`,
            }}
          >
            <span className="annotation-tag">标注 {index + 1}</span>
          </div>
        ))}

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
              total={annotations.length}
              stageSize={stageSize}
              submitting={submitting}
              inputRef={(node) => {
                if (node) inputRefs.current.set(annotation.id, node)
                else inputRefs.current.delete(annotation.id)
              }}
              onChange={onAnnotationIntentChange}
              onRemove={onAnnotationRemove}
            />
          ))}

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
            : `左键圈选（最多 ${MAX_ANNOTATIONS} 个） · 空格/中键平移 · 滚轮缩放`}
        </div>
      </div>
    </div>
  )
}
