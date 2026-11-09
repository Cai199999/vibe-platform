import { useEffect, useLayoutEffect, useRef, useState } from 'react'
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
 *
 * Inspect 模式（精准点击选择）：
 *  - iframe 是跨域的，父页面不能直接访问 DOM，因此通过 postMessage 把
 *    归一化后的 iframe 内坐标发给沙盒里的 inspect SDK；
 *  - SDK 调用 document.elementFromPoint(x, y)，向上找最近的 data-source，
 *    回传 rect + 源码文件 / 行号 / 组件名；
 *  - 父页面收到 rect 后按当前 scale / translate 映射回屏幕坐标，绘制蓝色
 *    高亮框（Chrome DevTools 审查元素风格）；
 *  - 左键点击不执行页面内控件，而是把命中的源码信息交给上层生成标注。
 */
export default function ScaledPreview({
  src,
  title,
  visible = true,
  inspectMode = false,
  onInspectPick,
}) {
  const screenRef = useRef(null)
  const iframeRef = useRef(null)
  const onPickRef = useRef(onInspectPick)
  const inspectModeRef = useRef(inspectMode)
  const [hover, setHover] = useState(null)
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

  useEffect(() => {
    onPickRef.current = onInspectPick
  }, [onInspectPick])

  useEffect(() => {
    inspectModeRef.current = inspectMode
    if (!inspectMode) setHover(null)
  }, [inspectMode])

  // visibility / display 切换后补一帧，确保拿到舞台的最终尺寸。
  useLayoutEffect(() => {
    if (!visible) return undefined
    const frame = window.requestAnimationFrame(recalc)
    return () => window.cancelAnimationFrame(frame)
  }, [visible, recalc])

  // 跨域 iframe <-> 父页面消息桥。
  useEffect(() => {
    const handler = (event) => {
      const frame = iframeRef.current
      if (!frame || event.source !== frame.contentWindow) return
      const data = event.data
      if (!data || data.channel !== 'vibe-inspect') return
      if (data.type === 'ready') {
        sendInspect(message('enable', { enabled: inspectModeRef.current }))
        return
      }
      if (data.type === 'hover') {
        const payload = data.payload
        setHover(payload && payload.ok ? payload : null)
        return
      }
      if (data.type === 'picked') {
        setHover(null)
        if (data.payload && data.payload.ok) {
          onPickRef.current?.(data.payload)
        }
      }
    }
    window.addEventListener('message', handler)
    return () => window.removeEventListener('message', handler)
  }, [])

  // 模式切换或 iframe src 变化时重新下发 enable（iframe 重载会丢状态）。
  useEffect(() => {
    sendInspect(message('enable', { enabled: inspectMode }))
    if (!inspectMode) setHover(null)
  }, [inspectMode, src])

  /** 把浏览器指针坐标映射到 iframe 内部 1280×720 设计视口坐标。 */
  const toFramePoint = (event) => {
    const frame = iframeRef.current
    if (!frame) return null
    const rect = frame.getBoundingClientRect()
    if (rect.width <= 0 || rect.height <= 0) return null
    if (
      event.clientX < rect.left ||
      event.clientX > rect.right ||
      event.clientY < rect.top ||
      event.clientY > rect.bottom
    ) {
      return null
    }
    const scaleX = rect.width / VIEWPORT_BASE_WIDTH
    const scaleY = rect.height / VIEWPORT_BASE_HEIGHT
    return {
      x: (event.clientX - rect.left) / scaleX,
      y: (event.clientY - rect.top) / scaleY,
    }
  }

  const sendInspect = (payload) => {
    const frame = iframeRef.current
    if (!frame || !frame.contentWindow) return
    frame.contentWindow.postMessage(
      { channel: 'vibe-inspect', ...payload },
      '*',
    )
  }

  const handleInspectPointerMove = (event) => {
    const point = toFramePoint(event)
    if (!point) {
      sendInspect(message('clear'))
      setHover(null)
      return
    }
    sendInspect(message('move', point))
  }

  const handleInspectPick = (event) => {
    if (event.button !== 0) return
    event.preventDefault()
    const point = toFramePoint(event)
    if (!point) return
    sendInspect(message('pick', point))
  }

  return (
    <div ref={screenRef} className="runtime-preview-screen">
      <iframe
        ref={iframeRef}
        className="runtime-preview-frame"
        src={src}
        title={title}
        sandbox="allow-scripts allow-same-origin allow-forms allow-modals"
        scrolling="no"
        onLoad={() => {
          setHover(null)
          sendInspect(message('enable', { enabled: inspectModeRef.current }))
        }}
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
        className={`preview-gesture-layer ${viewport.spaceHeld ? 'is-pan-ready' : ''} ${viewport.panning ? 'is-panning' : ''} ${inspectMode ? 'is-inspecting' : ''}`}
        data-preview-gesture
        onPointerDown={(event) => {
          if (inspectMode) {
            handleInspectPick(event)
            return
          }
          if (!viewport.startPan(event)) {
            event.preventDefault()
          }
        }}
        onPointerMove={(event) => {
          if (inspectMode) {
            handleInspectPointerMove(event)
            return
          }
          viewport.movePan(event)
        }}
        onPointerLeave={() => {
          if (!inspectMode) return
          setHover(null)
          sendInspect(message('clear'))
        }}
        onPointerUp={viewport.endPan}
        onPointerCancel={viewport.endPan}
        onDoubleClick={(event) => {
          if (!inspectMode) viewport.reset(event)
        }}
        onAuxClick={(event) => {
          if (!inspectMode) event.preventDefault()
        }}
        title={
          inspectMode
            ? '精准选择：移动鼠标高亮组件，左键点击生成标注'
            : '滚轮缩放 · 中键或空格+左键平移 · 双击重置'
        }
      />

      {/* 蓝色高亮框按 transform 映射回屏幕坐标，与 iframe 内容严格对齐。 */}
      {inspectMode && hover?.rect && (
        <div
          className="inspect-highlight"
          data-inspect-highlight
          style={{
            left: `${viewport.translateX + hover.rect.x * viewport.scale}px`,
            top: `${viewport.translateY + hover.rect.y * viewport.scale}px`,
            width: `${hover.rect.width * viewport.scale}px`,
            height: `${hover.rect.height * viewport.scale}px`,
          }}
        >
          <span className="inspect-highlight-label">
            {hover.source?.component || hover.tag}
            <em>
              {hover.source?.file}
              {hover.source?.line ? `:${hover.source.line}` : ''}
            </em>
          </span>
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
      </div>
    </div>
  )
}

const message = (type, extra = {}) => ({ type, ...extra })

const scaleReady = (scale) => scale > 0
