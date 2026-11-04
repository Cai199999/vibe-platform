import { useLayoutEffect, useRef } from 'react'
import useFillViewport, {
  VIEWPORT_BASE_HEIGHT,
  VIEWPORT_BASE_WIDTH,
} from '../workspace/useFillViewport.js'
import usePanZoom from '../workspace/usePanZoom.js'

/**
 * 沙盒预览使用「固定 1280×720 设计视口 + cover 基准 + 用户视口变换」：
 *
 *  1. iframe 始终以 1280×720 作为内部视口，页面按桌面版式排版；
 *  2. 基准 scale / offset 由 useFillViewport 以 cover 方式计算，保证铺满；
 *  3. 用户滚轮缩放、中键 / 空格 + 左键平移叠加在同一组 transform 上；
 *  4. iframe 上方覆盖一层透明手势层：iframe 会吞掉指针与滚轮事件，只有
 *     通过手势层才能在预览内容任意位置进行缩放和平移；
 *  5. 视口状态存放在 ViewportContext，切换画布 / 预览不会重置。
 */
export default function ScaledPreview({ src, title, visible = true }) {
  const screenRef = useRef(null)
  const {
    scale: baseScale,
    offsetX: baseOffsetX,
    offsetY: baseOffsetY,
    recalc,
  } = useFillViewport(screenRef)
  const viewport = usePanZoom({
    containerRef: screenRef,
    panel: 'preview',
    baseScale,
    baseOffsetX,
    baseOffsetY,
  })

  // visibility / display 切换后补一帧，确保拿到舞台的最终尺寸。
  useLayoutEffect(() => {
    if (!visible) return undefined
    const frame = window.requestAnimationFrame(recalc)
    return () => window.cancelAnimationFrame(frame)
  }, [visible, recalc])

  return (
    <div ref={screenRef} className="runtime-preview-screen">
      <iframe
        className="runtime-preview-frame"
        src={src}
        title={title}
        sandbox="allow-scripts allow-same-origin allow-forms allow-modals"
        scrolling="no"
        style={{
          width: `${VIEWPORT_BASE_WIDTH}px`,
          height: `${VIEWPORT_BASE_HEIGHT}px`,
          transform: `translate(${viewport.translateX}px, ${viewport.translateY}px) scale(${viewport.scale})`,
          transformOrigin: 'top left',
          opacity: scaleReady(baseScale) ? 1 : 0,
        }}
      />

      {/* 透明手势层：拦截 iframe 吞掉的 wheel / pointer 事件。 */}
      <div
        className={`preview-gesture-layer ${viewport.spaceHeld ? 'is-pan-ready' : ''} ${viewport.panning ? 'is-panning' : ''}`}
        data-preview-gesture
        onPointerDown={(event) => {
          if (!viewport.startPan(event)) {
            event.preventDefault()
          }
        }}
        onPointerMove={viewport.movePan}
        onPointerUp={viewport.endPan}
        onPointerCancel={viewport.endPan}
        onDoubleClick={viewport.reset}
        onAuxClick={(event) => event.preventDefault()}
        title="滚轮缩放 · 中键或空格+左键平移 · 双击重置"
      />

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
      </div>
    </div>
  )
}

const scaleReady = (scale) => scale > 0
