import { useCallback, useLayoutEffect, useState } from 'react'

/**
 * 画布与沙盒预览共用的“填充视口”计算。
 *
 * 设计宽度固定为 1024px：内容始终按桌面版式排版，再由 transform 缩放。
 *   scale = 容器宽度 / 1024
 *   逻辑高度 = 容器高度 / scale
 *
 * 缩放后宽高都严格等于容器，因此不会出现 contain 方案的黑边，也不会像
 * cover 那样裁掉内容。CanvasStage 与 ScaledPreview 使用同一套参数，
 * 保证画布与预览中的页面在任意布局下都是同样的尺寸与位置。
 */
export const VIEWPORT_BASE_WIDTH = 1024

export default function useFillViewport(containerRef) {
  const [{ scale, logicalHeight }, setViewport] = useState({
    scale: 0,
    logicalHeight: 0,
  })

  const recalc = useCallback(() => {
    const node = containerRef.current
    if (!node) return
    const width = node.clientWidth
    const height = node.clientHeight
    if (width <= 0 || height <= 0) {
      // display:none 的休眠 Tab 保持上一次比例，重新可见后 ResizeObserver 会重算。
      return
    }
    const nextScale = width / VIEWPORT_BASE_WIDTH
    setViewport({ scale: nextScale, logicalHeight: height / nextScale })
  }, [containerRef])

  useLayoutEffect(() => {
    const node = containerRef.current
    if (!node) return undefined
    const observer = new ResizeObserver(recalc)
    observer.observe(node)
    recalc()
    return () => observer.disconnect()
  }, [containerRef, recalc])

  return { scale, logicalHeight, recalc }
}
