import { useRef, useState } from 'react'

const clamp01 = (value) => Math.min(1, Math.max(0, value))

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
 *  - 鼠标 / 触控拖拽即可圈选组件，坐标归一化到 0~1，窗口缩放不丢失；
 *  - 圈选结果通过 onAnnotationChange 回传给外层。
 */
export default function CanvasStage({ annotation, onAnnotationChange, disabled }) {
  const stageRef = useRef(null)
  const [draft, setDraft] = useState(null)
  const [drawing, setDrawing] = useState(false)

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
              在左侧画布圈选任意组件，写下修改意图。Agent 会在隔离沙盒中生成代码、运行构建，并自动修复错误。
            </p>
            <button className="mock-cta" data-vibe-target="cta-button" type="button">
              开始体验
            </button>
            <span className="cta-hint">圈选我，然后写“改颜色”</span>
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

        <div className="canvas-hint">
          {drawing ? '松开鼠标完成圈选' : '按住鼠标拖拽，圈选要修改的组件'}
        </div>
      </div>
    </div>
  )
}
