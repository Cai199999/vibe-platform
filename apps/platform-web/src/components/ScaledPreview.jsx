import { useLayoutEffect, useRef } from 'react'
import useFillViewport, { VIEWPORT_BASE_WIDTH } from '../workspace/useFillViewport.js'

/**
 * 沙盒预览采用「固定逻辑宽度 + 视觉撑满」策略：
 *
 *  1. iframe 始终以 1024px 作为内部视口宽度，页面按桌面版式排版；
 *  2. 以容器宽度计算缩放比 scale = containerWidth / 1024；
 *  3. iframe 的逻辑高度取 containerHeight / scale，缩放后宽高都恰好等于容器；
 *  4. 使用 transform-origin: top left + scale，不做 flex 居中，也不留任何黑边。
 *
 * 全程只改 iframe 的尺寸与 transform，不修改 src、不重建节点：
 *  - Vite HMR 的 WebSocket 连接、页面滚动位置和组件状态都保持不变；
 *  - 视口宽高比始终与容器一致，不会出现上下/左右留白。
 */
export default function ScaledPreview({ src, title, visible = true }) {
  const screenRef = useRef(null)
  const { scale, logicalHeight, recalc } = useFillViewport(screenRef)

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
          height: logicalHeight > 0 ? `${logicalHeight}px` : '100%',
          transform: `scale(${scale})`,
          transformOrigin: 'top left',
          opacity: scale > 0 ? 1 : 0,
        }}
      />
    </div>
  )
}
