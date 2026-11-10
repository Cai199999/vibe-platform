import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import useFillViewport from '../workspace/useFillViewport.js'
import usePanZoom from '../workspace/usePanZoom.js'

/**
 * 沙盒预览使用「固定 1440×900 逻辑视口 + 真实页面滚动 + 用户视口变换」：
 *
 *  1. iframe 固定为 1440×900 基准，与画布完全一致，保证双屏版式不漂移；
 *  2. 不再通过外层 scale 强行适配容器：容器本身 overflow:auto，
 *     滚轮优先滚动容器，页面内容 100% 可达；
 *  3. Ctrl / Meta + 滚轮缩放、中键 / 空格 + 左键平移叠加在同一组 transform 上；
 *  4. iframe 上方覆盖一层透明手势层：iframe 会吞掉指针与滚轮事件，只有
 *     通过手势层才能在预览内容任意位置进行缩放和平移；
 *  5. 视口状态存放在 ViewportContext，切换画布 / 预览不会重置。
 *  6. 展示模式下接收画布的 scroll / click postMessage，实时同步滚动并
 *     在点击位置绘制涟漪反馈。
 *
 * Inspect 模式（精准点击选择）：
 *  - iframe 是跨域的，父页面不能直接访问 DOM，因此通过 postMessage 把
 *    归一化后的 iframe 内坐标发给沙盒里的 inspect SDK；
 *  - SDK 调用 document.elementFromPoint(x, y)，向上找最近的 data-source，
 *    回传 rect + 源码文件 / 行号 / 组件名；
 *  - 父页面收到 rect 后按当前 scale / translate 映射回屏幕坐标，绘制蓝色
 *    高亮框（Chrome DevTools 审查元素风格）；
 *  - 左键点击不执行页面内控件，而是把命中的源码信息交给上层生成标注。
 *
 * CREATE_NEW 拖拽定位：
 *  - Agent 生成的新组件带 data-vibe-created="<annotationId>"，父页面
 *    通过 measure 消息拿到它在 iframe 内的真实 rect / 父容器 rect；
 *  - 在该位置绘制一个绝对定位的拖拽浮层，用户拖动后记录相对父容器
 *    左上角的 x/y 像素；
 *  - 点击“确认位置”后，把坐标交给上层触发一次 MODIFY_EXISTING Patch，
 *    由 Coder 写回 position:absolute; left/top。
 */
export default function ScaledPreview({
  src,
  title,
  visible = true,
  inspectMode = false,
  onInspectPick,
  placement = null,
  onPlacementConfirm,
  onPlacementDismiss,
  // 双屏同步：{ scrollX, scrollY, nonce }，由画布的 SDK scroll 事件驱动。
  syncScroll = null,
  // 画布中的点击反馈：{ rect, nonce }，在预览同位置画一个短暂涟漪。
  interactionFeedback = null,
}) {
  const screenRef = useRef(null)
  const iframeRef = useRef(null)
  const onPickRef = useRef(onInspectPick)
  const inspectModeRef = useRef(inspectMode)
  // SDK 是否已握手；未就绪时不拦截滚轮、不强制滚动。
  const readyRef = useRef(false)
  // 最近一次预览 iframe 滚动快照，用于滚轮边界链式滚动。
  const scrollStateRef = useRef(null)
  const syncScrollRef = useRef(syncScroll)
  syncScrollRef.current = syncScroll
  const [hover, setHover] = useState(null)
  // 新组件测量结果 + 用户拖拽产生的 iframe 坐标偏移。
  const [measurement, setMeasurement] = useState(null)
  const [dragOffset, setDragOffset] = useState({ x: 0, y: 0 })
  const [measureFailed, setMeasureFailed] = useState(false)
  const [measureNonce, setMeasureNonce] = useState(0)
  const dragRef = useRef(null)
  const placementKeyRef = useRef(null)
  const measurementRef = useRef(null)
  const {
    scale: baseScale,
    offsetX: baseOffsetX,
    offsetY: baseOffsetY,
    designWidth,
    designHeight,
    recalc,
  } = useFillViewport(screenRef)
  const viewport = usePanZoom({
    containerRef: screenRef,
    panel: 'preview',
    baseScale,
    baseOffsetX,
    baseOffsetY,
    contentWidth: designWidth,
    contentHeight: designHeight,
  })

  useEffect(() => {
    onPickRef.current = onInspectPick
  }, [onInspectPick])

  useEffect(() => {
    inspectModeRef.current = inspectMode
    if (!inspectMode) setHover(null)
  }, [inspectMode])

  // 拖拽层换了目标 / 消失时，重置测量与位移，避免把上一个组件的坐标
  // 误套到新组件上。
  useEffect(() => {
    const key = placement?.annotationId ?? null
    if (placementKeyRef.current === key) return
    placementKeyRef.current = key
    setMeasurement(null)
    measurementRef.current = null
    setDragOffset({ x: 0, y: 0 })
    setMeasureFailed(false)
    dragRef.current = null
  }, [placement?.annotationId])

  // visibility / display 切换后补两帧，确保拿到舞台的最终尺寸。
  // 预览可能刚从 display:none / content-visibility:hidden 的休眠 Tab
  // 恢复，单帧测量有时仍会读到旧 flex 高度。
  useLayoutEffect(() => {
    if (!visible) return undefined
    const first = window.requestAnimationFrame(() => {
      recalc()
      window.requestAnimationFrame(recalc)
    })
    const timer = window.setTimeout(recalc, 120)
    return () => {
      window.cancelAnimationFrame(first)
      window.clearTimeout(timer)
    }
  }, [visible, recalc])

  // 跨域 iframe <-> 父页面消息桥。
  useEffect(() => {
    const handler = (event) => {
      const frame = iframeRef.current
      if (!frame || event.source !== frame.contentWindow) return
      const data = event.data
      if (!data || data.channel !== 'vibe-inspect') return
      if (data.type === 'ready') {
        readyRef.current = true
        scrollStateRef.current = data.payload?.scroll ?? null
        sendInspect(message('enable', { enabled: inspectModeRef.current }))
        // iframe 重载（HMR / 首次加载）后重新测量拖拽目标。
        setMeasurement(null)
        measurementRef.current = null
        setMeasureFailed(false)
        // 预览加载前画布可能已经滚动过，这里补一次对齐。
        const pending = syncScrollRef.current
        if (pending) {
          sendInspect(
            message('scroll-to', {
              x: pending.scrollX ?? 0,
              y: pending.scrollY ?? 0,
              behavior: 'auto',
            }),
          )
        }
        return
      }
      if (data.type === 'measured') {
        const payload = data.payload
        if (!payload) return
        if (payload.ok) {
          measurementRef.current = payload
          setMeasurement(payload)
          setMeasureFailed(false)
        } else {
          setMeasureFailed(true)
        }
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
        return
      }
      if (data.type === 'scroll') {
        // 预览自身滚动快照：只用于滚轮边界判断，不反向驱动画布。
        scrollStateRef.current = data.payload ?? null
      }
    }
    window.addEventListener('message', handler)
    return () => window.removeEventListener('message', handler)
  }, [])

  // 模式切换或 iframe src 变化时重新下发 enable（iframe 重载会丢状态）。
  useEffect(() => {
    readyRef.current = false
    scrollStateRef.current = null
    sendInspect(message('enable', { enabled: inspectMode }))
    if (!inspectMode) setHover(null)
  }, [inspectMode, src])

  // 画布滚动 → 预览同步。nonce 保证相同的滚动位置也会再次下发
  // （例如预览 iframe 刚重载，需要重新对齐）。
  useEffect(() => {
    if (!syncScroll || !readyRef.current) return
    sendInspect(
      message('scroll-to', {
        x: syncScroll.scrollX ?? 0,
        y: syncScroll.scrollY ?? 0,
        behavior: 'auto',
      }),
    )
    // sendInspect 只做 postMessage，无需进入依赖。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [syncScroll?.nonce])

  /*
   * 预览和画布一样恢复真实浏览器的滚轮手感：
   * 普通滚轮滚动 iframe 内部页面，Ctrl / Cmd + 滚轮才缩放画布。
   * 透明手势层会拦截原生滚动，因此必须手动转发给 SDK。
   */
  useEffect(() => {
    const node = screenRef.current
    if (!node) return undefined

    const onWheel = (event) => {
      if (event.ctrlKey || event.metaKey) return
      if (!readyRef.current) return
      const unit =
        event.deltaMode === 1
          ? 16
          : event.deltaMode === 2
            ? node.clientHeight
            : 1
      const dx = event.deltaX * unit
      const dy = event.deltaY * unit
      // 页面滚到边界后，剩余滚轮交还外层容器。
      const snapshot = scrollStateRef.current
      if (!snapshot) return
      const scrollX = Number(snapshot.scrollX) || 0
      const scrollY = Number(snapshot.scrollY) || 0
      const maxX = Number(snapshot.maxX) || 0
      const maxY = Number(snapshot.maxY) || 0
      const canScrollX =
        dx < 0 ? scrollX > 0.5 : dx > 0 ? scrollX < maxX - 0.5 : false
      const canScrollY =
        dy < 0 ? scrollY > 0.5 : dy > 0 ? scrollY < maxY - 0.5 : false
      if (!canScrollX && !canScrollY) return

      event.preventDefault()
      sendInspect(
        message('scroll-by', {
          dx: canScrollX ? dx : 0,
          dy: canScrollY ? dy : 0,
        }),
      )
    }

    node.addEventListener('wheel', onWheel, { passive: false })
    return () => node.removeEventListener('wheel', onWheel)
    // sendInspect 只做 postMessage；readyRef 自行维护。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  /** 把浏览器指针坐标映射到 iframe 内部当前逻辑视口坐标。 */
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
    // clientWidth / clientHeight 是未经 transform 缩放前的逻辑尺寸。
    const scaleX = rect.width / (frame.clientWidth || designWidth)
    const scaleY = rect.height / (frame.clientHeight || designHeight)
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

  // 新组件刚生成时 HMR 可能还没完成，短轮询 measure 直到 marker 出现。
  // 失败 16 次（约 8 秒）后提示用户检查生成结果，而不是无限静默。
  useEffect(() => {
    if (!placement?.selector) return undefined
    let cancelled = false
    let attempts = 0
    let timer = null
    const tick = () => {
      if (cancelled || measurementRef.current) return
      sendInspect(
        message('measure', {
          selector: placement.selector,
          parentSelector: placement.parentSelector || '',
          requestId: placement.annotationId,
        }),
      )
      attempts += 1
      if (attempts < 16) timer = window.setTimeout(tick, 500)
    }
    tick()
    return () => {
      cancelled = true
      if (timer) window.clearTimeout(timer)
    }
    // sendInspect 只做 postMessage，不参与依赖；按目标 / src 重新测量即可。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    placement?.annotationId,
    placement?.selector,
    placement?.parentSelector,
    src,
    measureNonce,
  ])

  /** 拖拽开始：记录起点与当前位移，使用 pointer capture 跟随鼠标。 */
  const beginPlacementDrag = (event) => {
    if (event.button !== 0) return
    event.preventDefault()
    event.stopPropagation()
    event.currentTarget.setPointerCapture?.(event.pointerId)
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      baseX: dragOffset.x,
      baseY: dragOffset.y,
    }
  }

  const movePlacementDrag = (event) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    event.preventDefault()
    event.stopPropagation()
    const scale = viewport.scale > 0 ? viewport.scale : 1
    setDragOffset({
      x: drag.baseX + (event.clientX - drag.startX) / scale,
      y: drag.baseY + (event.clientY - drag.startY) / scale,
    })
  }

  const endPlacementDrag = (event) => {
    if (dragRef.current?.pointerId !== event.pointerId) return
    event.preventDefault()
    event.stopPropagation()
    dragRef.current = null
  }

  /** 确认位置：把“父容器相对坐标”回传给上层触发 Coder Patch。 */
  const confirmPlacement = () => {
    if (!placement || !measurement?.relativeRect) return
    const relative = measurement.relativeRect
    onPlacementConfirm?.(placement, {
      x: relative.relativeX + dragOffset.x,
      y: relative.relativeY + dragOffset.y,
      width: relative.widthPx ?? measurement.rect?.width ?? null,
      height: relative.heightPx ?? measurement.rect?.height ?? null,
    })
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

  /*
   * 点击反馈锚点。
   *  - SDK 的 click 事件带 clientX / clientY（iframe 视口坐标）；
   *  - Inspect 的 picked 事件只有元素 rect，回退到元素中心。
   * 只绘制一个固定世界尺寸的小涟漪，而不是整个元素矩形——点击 <main>
   * 这类大容器时，按 rect 绘制会得到覆盖整页的巨型光斑且可能滚出视口。
   */
  const feedbackRect = interactionFeedback?.rect ?? null
  const feedbackHasPoint =
    Number.isFinite(Number(interactionFeedback?.x)) &&
    Number.isFinite(Number(interactionFeedback?.y))
  const feedbackX = feedbackHasPoint
    ? Number(interactionFeedback.x)
    : (feedbackRect?.x ?? 0) + (feedbackRect?.width ?? 0) / 2
  const feedbackY = feedbackHasPoint
    ? Number(interactionFeedback.y)
    : (feedbackRect?.y ?? 0) + (feedbackRect?.height ?? 0) / 2
  const feedbackSize = Math.max(10, 44 * viewport.scale)

  return (
    <div ref={screenRef} className="runtime-preview-screen">
      <iframe
        ref={iframeRef}
        className="runtime-preview-frame"
        src={src}
        title={title}
        sandbox="allow-scripts allow-same-origin allow-forms allow-modals"
        onLoad={() => {
          setHover(null)
          sendInspect(message('enable', { enabled: inspectModeRef.current }))
        }}
        style={{
          width: `${designWidth}px`,
          height: `${designHeight}px`,
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

      {/* 画布点击 → 预览视觉反馈：在点击点绘制一次短促的涟漪。
          坐标使用 iframe 内视口 CSS 像素，和 iframe 共享同一 transform，
          因此缩放 / 平移 / 双屏滚动后仍然严格对齐。 */}
      {interactionFeedback && (feedbackHasPoint || feedbackRect) && (
        <div
          key={`click-${interactionFeedback.nonce}`}
          className="preview-click-feedback"
          data-preview-click-feedback
          aria-hidden="true"
          style={{
            left: `${
              viewport.translateX + feedbackX * viewport.scale - feedbackSize / 2
            }px`,
            top: `${
              viewport.translateY + feedbackY * viewport.scale - feedbackSize / 2
            }px`,
            width: `${feedbackSize}px`,
            height: `${feedbackSize}px`,
            borderRadius: '50%',
          }}
        />
      )}

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

      {/* CREATE_NEW：新组件上覆盖可拖拽定位框。松手位置以 iframe 内部
          父容器左上角为原点，确认后交给 Coder 回写绝对定位。 */}
      {placement && measurement?.rect && (
        <div
          className={`placement-frame ${dragRef.current ? 'is-dragging' : ''}`}
          data-placement-frame
          style={{
            left: `${
              viewport.translateX +
              (measurement.rect.x + dragOffset.x) * viewport.scale
            }px`,
            top: `${
              viewport.translateY +
              (measurement.rect.y + dragOffset.y) * viewport.scale
            }px`,
            width: `${Math.max(24, measurement.rect.width * viewport.scale)}px`,
            height: `${Math.max(24, measurement.rect.height * viewport.scale)}px`,
          }}
          onPointerDown={beginPlacementDrag}
          onPointerMove={movePlacementDrag}
          onPointerUp={endPlacementDrag}
          onPointerCancel={endPlacementDrag}
        >
          <span className="placement-handle" aria-hidden="true">
            ✥
          </span>
          <span className="placement-label">拖动到目标位置</span>
          <div
            className="placement-actions"
            onPointerDown={(event) => event.stopPropagation()}
          >
            <button
              type="button"
              className="placement-confirm"
              data-placement-confirm
              onClick={confirmPlacement}
            >
              确认位置
            </button>
            <button
              type="button"
              className="placement-cancel"
              onClick={() => onPlacementDismiss?.(placement)}
            >
              取消
            </button>
          </div>
        </div>
      )}

      {placement && !measurement && measureFailed && (
        <div className="placement-warning" role="status">
          <span>未找到刚生成的新组件，HMR 可能仍在刷新。</span>
          <button
            type="button"
            onClick={() => {
              measurementRef.current = null
              setMeasurement(null)
              setMeasureFailed(false)
              setMeasureNonce((value) => value + 1)
            }}
          >
            重试
          </button>
          <button type="button" onClick={() => onPlacementDismiss?.(placement)}>
            关闭
          </button>
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
