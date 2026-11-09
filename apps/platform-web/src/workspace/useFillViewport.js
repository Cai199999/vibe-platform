import { useCallback, useLayoutEffect, useState } from 'react'

/**
 * 画布与沙盒预览共用的“最小设计视口 + 动态铺满”计算。
 *
 * 设计视口至少为 1280×720（沙盒默认桌面视口），但不再用 cover 裁切：
 *   scale = min(容器宽 / 1280, 容器高 / 720)
 *   designWidth  = 容器宽 / scale  （≥ 1280）
 *   designHeight = 容器高 / scale  （≥ 720）
 *
 * 然后把 designWidth × designHeight 的逻辑视口整体缩放回容器尺寸。
 * 这样：
 *   - 画面严格铺满容器，四周没有黑边；
 *   - 逻辑视口只会变宽 / 变高，不会把页面上下裁掉；
 *   - 页面仍以至少 1280×720 的桌面尺寸排版，不会因为容器变矮而挤压内容。
 *
 * 画布与预览使用完全相同的容器尺寸与算法，因此缩放、偏移和排版始终一致。
 */
export const VIEWPORT_BASE_WIDTH = 1280
export const VIEWPORT_BASE_HEIGHT = 720

const EMPTY_VIEWPORT = {
  scale: 0,
  offsetX: 0,
  offsetY: 0,
  designWidth: VIEWPORT_BASE_WIDTH,
  designHeight: VIEWPORT_BASE_HEIGHT,
}

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
    // 用较小的比例确保逻辑视口两个方向都不小于基准尺寸；
    // 多出来的宽 / 高直接交给页面自身（width/height 100%）铺满。
    const scale = Math.min(
      width / VIEWPORT_BASE_WIDTH,
      height / VIEWPORT_BASE_HEIGHT,
    )
    setViewport({
      scale,
      offsetX: 0,
      offsetY: 0,
      designWidth: width / scale,
      designHeight: height / scale,
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
