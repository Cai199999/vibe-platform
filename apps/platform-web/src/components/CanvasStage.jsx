import { useEffect, useMemo, useRef, useState } from 'react'

const clamp01 = (value) => Math.min(1, Math.max(0, value))

const POPOVER_WIDTH = 272
const POPOVER_HEIGHT = 150
const POPOVER_GAP = 14

function normalizeRect(originX, originY, x, y) {
  return {
    x: Math.min(originX, x),
    y: Math.min(originY, y),
    width: Math.abs(x - originX),
    height: Math.abs(y - originY),
  }
}

/**
 * 画布 Stage：
 *  - 展示一个与沙盒初始源码一致的 Mock 页面；
 *  - 鼠标 / 触控拖拽圈选组件，坐标归一化到 0~1；
 *  - 圈选完成后，在选区旁弹出浮动意图输入框，直接发送给 Agent。
 */
export default function CanvasStage({
  annotation,
  intent,
  onIntentChange,
  onAnnotationChange,
  onSubmit,
  submitting,
  disabled,
}) {
  const stageRef = useRef(null)
  const inputRef = useRef(null)
  const [draft, setDraft] = useState(null)
  const [drawing, setDrawing] = useState(false)
  const [stageSize, setStageSize] = useState({ width: 0, height: 0 })

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

  // 产生新圈选后聚焦输入框。意图草稿由父级按项目持有，切换 Tab 后仍会保留；
  // 新选区 / 清除选区时的草稿重置在 WorkspacePage 的 annotation 回调里完成。
  useEffect(() => {
    if (!annotation) return undefined
    const frame = window.requestAnimationFrame(() => inputRef.current?.focus())
    return () => window.cancelAnimationFrame(frame)
  }, [annotation])

  const toPoint = (event) => {
    const box = stageRef.current.getBoundingClientRect()
    return {
      x: clamp01((event.clientX - box.left) / box.width),
      y: clamp01((event.clientY - box.top) / box.height),
    }
  }

  const handlePointerDown = (event) => {
    if (disabled || event.button !== 0) {
      return
    }
    event.preventDefault()
    const point = toPoint(event)
    event.currentTarget.setPointerCapture(event.pointerId)
    setDrawing(true)
    setDraft({ originX: point.x, originY: point.y, x: point.x, y: point.y })
    // 开始新圈选时清空旧标注，避免视觉与提交内容不一致。
    onAnnotationChange(null)
  }

  const handlePointerMove = (event) => {
    if (!drawing || !draft) {
      return
    }
    const point = toPoint(event)
    setDraft((current) => (current ? { ...current, x: point.x, y: point.y } : current))
  }

  const finishDrawing = (event) => {
    if (!drawing || !draft) {
      return
    }
    const point = toPoint(event)
    const rect = normalizeRect(draft.originX, draft.originY, point.x, point.y)
    setDrawing(false)
    setDraft(null)
    if (rect.width > 0.02 && rect.height > 0.02) {
      onAnnotationChange(rect)
    }
  }

  const activeRect = draft ? normalizeRect(draft.originX, draft.originY, draft.x, draft.y) : annotation

  /**
   * 浮层定位：优先放选区右侧，其次左侧；两侧空间都不足时放到选区下方。
   * 所有位置都做边界收拢，避免被 canvas-stage 裁切。
   */
  const popoverStyle = useMemo(() => {
    if (!annotation || drawing || stageSize.width === 0) return null
    const { width, height } = stageSize
    const leftPx = annotation.x * width
    const rightPx = (annotation.x + annotation.width) * width
    const centerY = (annotation.y + annotation.height / 2) * height
    const clampTop = (value) =>
      Math.min(Math.max(12, value), Math.max(12, height - POPOVER_HEIGHT - 12))

    if (width - rightPx >= POPOVER_WIDTH + POPOVER_GAP) {
      return { left: rightPx + POPOVER_GAP, top: clampTop(centerY - POPOVER_HEIGHT / 2) }
    }
    if (leftPx >= POPOVER_WIDTH + POPOVER_GAP) {
      return { right: width - leftPx + POPOVER_GAP, top: clampTop(centerY - POPOVER_HEIGHT / 2) }
    }

    // 窄屏兜底：横跨选区下方，宽度由 left/right 自适应。
    const below = (annotation.y + annotation.height) * height + 12
    const above = annotation.y * height - POPOVER_HEIGHT - 12
    const top = below + POPOVER_HEIGHT <= height - 12 ? below : Math.max(12, above)
    return { left: 12, right: 12, width: 'auto', top: clampTop(top) }
  }, [annotation, drawing, stageSize])

  const handleSend = (event) => {
    event.preventDefault()
    const text = intent.trim()
    if (!text || !annotation || submitting) return
    onSubmit?.(text)
  }

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
        className={`canvas-stage ${disabled ? 'is-disabled' : ''} ${drawing ? 'is-drawing' : ''}`}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={finishDrawing}
        onPointerCancel={finishDrawing}
      >
        <div className="mock-page">
          <section className="mock-card">
            <p className="mock-kicker">Vibe Studio</p>
            <h1 className="mock-title">
              把想法画出来。
              <br />
              剩下的交给 Agent。
            </h1>
            <p className="mock-copy">
              在画布圈选任意组件，浮层内写下修改意图。Agent 会在隔离沙盒中生成代码、运行构建，并自动修复错误。
            </p>
            <button className="mock-cta" data-vibe-target="cta-button" type="button">
              开始体验
            </button>
            <span className="cta-hint">圈选我，然后输入“改颜色”</span>
          </section>
        </div>

        {activeRect && (
          <div
            className="annotation-ellipse"
            style={{
              left: `${activeRect.x * 100}%`,
              top: `${activeRect.y * 100}%`,
              width: `${activeRect.width * 100}%`,
              height: `${activeRect.height * 100}%`,
            }}
          >
            <span className="annotation-tag">标注</span>
          </div>
        )}

        {annotation && !drawing && popoverStyle && (
          <form
            className="canvas-popover"
            style={popoverStyle}
            role="dialog"
            aria-label="输入修改意图"
            onPointerDown={(event) => event.stopPropagation()}
            onPointerUp={(event) => event.stopPropagation()}
            onSubmit={handleSend}
          >
            <div className="popover-head">
              <span className="popover-dot" />
              <span>对选区下达指令</span>
              <span className="popover-target">button</span>
            </div>
            <div className="popover-row">
              <input
                ref={inputRef}
                type="text"
                value={intent}
                maxLength={120}
                placeholder="例如：把按钮改成红色"
                disabled={submitting}
                onChange={(event) => onIntentChange?.(event.target.value)}
              />
              <button
                className="popover-send"
                type="submit"
                disabled={!intent.trim() || submitting}
              >
                {submitting ? <span className="mini-spinner" aria-label="执行中" /> : '发送'}
              </button>
            </div>
            <p className="popover-hint">
              {submitting ? 'Agent 正在执行，请稍候…' : 'Enter 发送 · 重新圈选可更换目标'}
            </p>
          </form>
        )}

        <div className="canvas-hint">
          {drawing ? '松开鼠标完成圈选' : '按住鼠标拖拽，圈选要修改的组件'}
        </div>
      </div>
    </div>
  )
}
