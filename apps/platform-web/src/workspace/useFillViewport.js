import { useCallback, useLayoutEffect, useRef, useState } from 'react'

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

// 首帧先用 1:1 视口渲染，避免测量完成前 scale=0 导致画布整块不可见。
const INITIAL_VIEWPORT = {
  scale: 1,
  offsetX: 0,
  offsetY: 0,
  designWidth: VIEWPORT_BASE_WIDTH,
  designHeight: VIEWPORT_BASE_HEIGHT,
}

/** 读取容器内容盒尺寸；隐藏 / 未挂载时返回 0，由调用方保留上一次结果。 */
function measureContainer(node) {
  if (!node) return { width: 0, height: 0 }
  const width = node.clientWidth
  const height = node.clientHeight
  if (width > 0 && height > 0) return { width, height }
  // display:none / content-visibility:hidden 的瞬间，clientWidth 可能为 0；
  // 此时 getBoundingClientRect 也不能给出有效尺寸，统一交给下一次回调。
  return { width: 0, height: 0 }
}

export default function useFillViewport(containerRef) {
  const [viewport, setViewport] = useState(INITIAL_VIEWPORT)
  // 记录上一次实际尺寸，避免 ResizeObserver 在同一帧重复触发 setState，
  // 也避免“尺寸没变但状态对象变了”引起画布下游无意义重渲染。
  const lastSizeRef = useRef({ width: 0, height: 0 })

  const recalc = useCallback(() => {
    const node = containerRef.current
    const { width, height } = measureContainer(node)
    if (width <= 0 || height <= 0) {
      // display:none 的休眠 Tab 保持上一次比例；重新可见后 RO / 激活钩子重算。
      return
    }
    const previous = lastSizeRef.current
    if (previous.width === width && previous.height === height) return
    lastSizeRef.current = { width, height }

    // 用较小的比例确保逻辑视口两个方向都不小于基准尺寸；
    // 多出来的宽 / 高直接交给页面自身（width/height 100%）铺满。
    const scale = Math.min(
      width / VIEWPORT_BASE_WIDTH,
      height / VIEWPORT_BASE_HEIGHT,
    )
    if (!Number.isFinite(scale) || scale <= 0) return
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

    // 1) 容器尺寸变化（窗口缩放、Tab 切换、画布 / 预览布局变化）。
    const observer = new ResizeObserver(recalc)
    observer.observe(node, { box: 'content-box' })

    // 2) 浏览器缩放 / 移动端横竖屏切换：ResizeObserver 不一定能捕获
    //    visualViewport 的变化，这里显式补一发，并在下一帧再兜底一次，
    //    避免 flex 链在同一个 tick 内还没稳定时读到旧尺寸。
    let raf = 0
    const schedule = () => {
      if (raf) window.cancelAnimationFrame(raf)
      raf = window.requestAnimationFrame(() => {
        raf = 0
        recalc()
        // 第二帧：等 tab-window 的 flex 分配 / 动画结束后的最终尺寸。
        raf = window.requestAnimationFrame(recalc)
      })
    }

    window.addEventListener('resize', schedule)
    window.addEventListener('orientationchange', schedule)
    window.visualViewport?.addEventListener('resize', schedule)

    recalc()
    schedule()

    return () => {
      observer.disconnect()
      window.removeEventListener('resize', schedule)
      window.removeEventListener('orientationchange', schedule)
      window.visualViewport?.removeEventListener('resize', schedule)
      if (raf) window.cancelAnimationFrame(raf)
    }
  }, [containerRef, recalc])

  return { ...viewport, recalc }
}
