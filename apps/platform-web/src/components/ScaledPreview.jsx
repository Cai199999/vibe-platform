import { useLayoutEffect, useRef } from 'react'
import useFillViewport, {
  VIEWPORT_BASE_HEIGHT,
  VIEWPORT_BASE_WIDTH,
} from '../workspace/useFillViewport.js'

/**
 * 沙盒预览采用「固定 1280×720 设计视口 + cover 缩放」策略：
 *
 *  1. iframe 始终以 1280×720 作为内部视口，页面按桌面版式排版；
 *  2. scale = max(containerWidth / 1280, containerHeight / 720)；
 *  3. 用 translate 把缩放后的画面居中，多余部分由外层 overflow:hidden 裁切；
 *  4. 不使用 flex 居中、不添加 padding / margin，画面四边严格贴住容器。
 *
 * 全程只改 iframe 的尺寸与 transform，不修改 src、不重建节点：
 *  - Vite HMR 的 WebSocket 连接、页面滚动位置和组件状态都保持不变；
 *  - 缩放参数与 CanvasStage 完全一致，画布与预览的内容比例严格相同。
 */
export default function ScaledPreview({ src, title, visible = true }) {
  const screenRef = useRef(null)
  const { scale, offsetX, offsetY, recalc } = useFillViewport(screenRef)

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
          transform: `translate(${offsetX}px, ${offsetY}px) scale(${scale})`,
          transformOrigin: 'top left',
          opacity: scale > 0 ? 1 : 0,
        }}
      />
    </div>
  )
}
