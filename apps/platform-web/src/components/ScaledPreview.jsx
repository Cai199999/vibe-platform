import { useCallback, useLayoutEffect, useRef, useState } from 'react'

/**
 * 沙盒预览使用固定的“设计分辨率”渲染，再整体等比缩放到容器中。
 *
 * 为什么不用 100% 宽高：
 *  - iframe 内部页面会随容器宽度反复重排，双屏/单屏切换时视觉不一致；
 *  - 固定 1024×640 后，Vite HMR 连接、页面滚动位置与组件状态都保持稳定；
 *  - 外层只用 transform 缩放，不重建 iframe，因此不会触发重新加载。
 */
const DESIGN_WIDTH = 1024
const DESIGN_HEIGHT = 640

export default function ScaledPreview({ src, title, visible = true }) {
  const screenRef = useRef(null)
  const [scale, setScale] = useState(0)

  const recalc = useCallback(() => {
    const node = screenRef.current
    if (!node) return
    const { width, height } = node.getBoundingClientRect()
    if (width <= 0 || height <= 0) {
      // 面板被 display:none 时尺寸为 0；保持上一次比例，切回后 ResizeObserver 会重算。
      return
    }
    // contain：完整展示页面，短边留白由外层背景填充，绝不裁切内容。
    setScale(Math.min(width / DESIGN_WIDTH, height / DESIGN_HEIGHT))
  }, [])

  useLayoutEffect(() => {
    const node = screenRef.current
    if (!node) return undefined
    const observer = new ResizeObserver(recalc)
    observer.observe(node)
    recalc()
    return () => observer.disconnect()
  }, [recalc])

  // display:none → block 时 ResizeObserver 理论上会触发；再补一帧确保拿到最终尺寸。
  useLayoutEffect(() => {
    if (!visible) return undefined
    const frame = window.requestAnimationFrame(recalc)
    return () => window.cancelAnimationFrame(frame)
  }, [visible, recalc])

  const scalerStyle = {
    width: `${DESIGN_WIDTH}px`,
    height: `${DESIGN_HEIGHT}px`,
    transform: `translate(-50%, -50%) scale(${scale})`,
    opacity: scale > 0 ? 1 : 0,
  }

  return (
    <div ref={screenRef} className="runtime-preview-screen">
      <div className="runtime-preview-scaler" style={scalerStyle}>
        <iframe
          className="runtime-preview-frame"
          src={src}
          title={title}
          sandbox="allow-scripts allow-same-origin allow-forms allow-modals"
          scrolling="no"
          style={{ width: `${DESIGN_WIDTH}px`, height: `${DESIGN_HEIGHT}px` }}
        />
      </div>
    </div>
  )
}
