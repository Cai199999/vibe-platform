import { useCallback, useLayoutEffect, useState } from 'react'

/**
 * 画布与沙盒预览共用的“固定设计视口 + 等比铺满”计算。
 *
 * 设计视口固定为 1280×720（沙盒默认桌面视口），内容始终按该尺寸排版，
 * 再由 transform 统一缩放：
 *   scale = max(容器宽 / 1280, 容器高 / 720)   // cover，保证宽高都铺满
 *   offsetX / offsetY = 剩余空间 / 2             // 多出的方向居中裁切
 *
 * 使用 cover 而不是 contain：contain 会在宽高比不一致时留下黑边；cover 则
 * 保证预览与画布都严格铺满容器，不出现任何留白。两者使用完全相同的
 * scale 与 offset，因此卡片尺寸、位置和垂直中心始终像素级一致。
 */
export const VIEWPORT_BASE_WIDTH = 1280
export const VIEWPORT_BASE_HEIGHT = 720

const EMPTY_VIEWPORT = { scale: 0, offsetX: 0, offsetY: 0 }

export default function useFillViewport(containerRef) {
  const [viewport, setViewport] = useState(EMPTY_VIEWPORT)

  const recalc = useCallback(() => {
    const node = containerRef.current
    if (!node) return
    const width = node.clientWidth
    const height = node.clientHeight
    if (width <= 0 || height <= 0) {
      // display:none 的休眠 Tab 保持上一次比例，重新可见后 ResizeObserver 会重算。
      return
    }
    const scale = Math.max(
      width / VIEWPORT_BASE_WIDTH,
      height / VIEWPORT_BASE_HEIGHT,
    )
    setViewport({
      scale,
      offsetX: (width - VIEWPORT_BASE_WIDTH * scale) / 2,
      offsetY: (height - VIEWPORT_BASE_HEIGHT * scale) / 2,
    })
  }, [containerRef])

  useLayoutEffect(() => {
    const node = containerRef.current
    if (!node) return undefined
    const observer = new ResizeObserver(recalc)
    observer.observe(node)
    recalc()
    return () => observer.disconnect()
  }, [containerRef, recalc])

  return { ...viewport, recalc }
}
