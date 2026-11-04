import { useCallback, useEffect, useRef, useState } from 'react'
import {
  VIEWPORT_BASE_HEIGHT,
  VIEWPORT_BASE_WIDTH,
} from './useFillViewport.js'
import {
  MAX_ZOOM,
  MIN_ZOOM,
  useViewportState,
} from './ViewportContext.jsx'

// 滚轮灵敏度：deltaY 每 100 约缩放 1.16 倍，触控板小增量也能平滑过渡。
const WHEEL_ZOOM_SPEED = 0.0015
// 平移边界：无论怎么拖，画面至少保留这么多像素在容器内，防止“弄丢”。
const MIN_VISIBLE_PX = 96

const clamp = (value, min, max) => Math.min(max, Math.max(min, value))

/**
 * 单轴平移边界：只做「至少保留 MIN_VISIBLE_PX 可见」的硬保护，绝不居中。
 *
 * Cover 基准在 100% 时会让其中一个方向恰好等于容器尺寸，若该方向强制居中，
 * 就会表现为「对比模式只能上下拖、展示模式只能左右拖」。视口应当是无限画布：
 * X / Y 两个方向始终独立、自由平移，只限制极端拖出可视区。
 */
const clampAxis = (translate, contentSize, containerSize) => {
  const min = MIN_VISIBLE_PX - contentSize
  const max = containerSize - MIN_VISIBLE_PX
  // 容器比最小可见区还小时无解，退回居中，避免产生翻转的 clamp 区间。
  if (min > max) return (containerSize - contentSize) / 2
  return clamp(translate, min, max)
}

/**
 * 画布 / 预览共用的平移缩放交互。
 *
 *  - 滚轮：以鼠标指针为中心缩放（先求指针下方的世界坐标，再反算新的平移）；
 *  - 中键拖拽 / 空格 + 左键拖拽：平移；
 *  - 缩放边界 0.25x ~ 3x（相对初始铺满比例）；
 *  - 平移边界保证画面至少保留 96px 可见；
 *  - 状态交由 ViewportContext 保存，切换面板 / 布局不会丢失。
 */
export default function usePanZoom({
  containerRef,
  panel,
  baseScale,
  baseOffsetX,
  baseOffsetY,
}) {
  const { views, updateViewport, resetViewport } = useViewportState()
  const view = views[panel] ?? { zoom: 1, panX: 0, panY: 0 }
  const [spaceHeld, setSpaceHeld] = useState(false)
  const [panning, setPanning] = useState(false)

  const spaceRef = useRef(false)
  const panRef = useRef(null)
  const baseRef = useRef({ baseScale, baseOffsetX, baseOffsetY })
  baseRef.current = { baseScale, baseOffsetX, baseOffsetY }

  // 空格是“临时抓手”修饰键；只有输入类控件不拦截，避免无法输入空格。
  // 按钮 / 链接不排除：刚点过工具栏按钮时焦点仍在 button 上，若排除会导致
  // 空格 + 左键平移失效（Figma / PS 也允许在任何位置按住空格抓手）。
  useEffect(() => {
    const isTypingTarget = (target) =>
      target instanceof HTMLElement &&
      (target.isContentEditable ||
        ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))

    const onKeyDown = (event) => {
      if (event.code !== 'Space' || event.repeat || isTypingTarget(event.target)) {
        return
      }
      event.preventDefault()
      spaceRef.current = true
      setSpaceHeld(true)
    }
    const onKeyUp = (event) => {
      if (event.code !== 'Space') return
      spaceRef.current = false
      setSpaceHeld(false)
    }
    const onBlur = () => {
      spaceRef.current = false
      setSpaceHeld(false)
    }

    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('keyup', onKeyUp)
    window.addEventListener('blur', onBlur)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('keyup', onKeyUp)
      window.removeEventListener('blur', onBlur)
    }
  }, [])

  // 平移期间指针使用 setPointerCapture，可能移出面板；给 body 挂全局抓手光标，
  // 避免拖拽过程中光标闪回默认箭头（Figma / PS 手感）。
  useEffect(() => {
    if (!panning) return undefined
    document.body.classList.add('is-viewport-panning')
    return () => document.body.classList.remove('is-viewport-panning')
  }, [panning])

  const clampView = useCallback(
    (next) => {
      const node = containerRef.current
      const { baseScale: scaleBase, baseOffsetX: ox, baseOffsetY: oy } =
        baseRef.current
      if (!node || scaleBase <= 0) return next

      const width = node.clientWidth
      const height = node.clientHeight
      const contentWidth = VIEWPORT_BASE_WIDTH * scaleBase * next.zoom
      const contentHeight = VIEWPORT_BASE_HEIGHT * scaleBase * next.zoom

      // X / Y 各自独立计算，任何布局模式（对比 / 展示）都不锁轴。
      const translateX = clampAxis(ox + next.panX, contentWidth, width)
      const translateY = clampAxis(oy + next.panY, contentHeight, height)

      return { ...next, panX: translateX - ox, panY: translateY - oy }
    },
    [containerRef],
  )

  // 原生 wheel 监听必须非 passive，才能 preventDefault 阻止页面滚动。
  useEffect(() => {
    const node = containerRef.current
    if (!node) return undefined

    const onWheel = (event) => {
      event.preventDefault()
      const rect = node.getBoundingClientRect()
      const pointerX = event.clientX - rect.left
      const pointerY = event.clientY - rect.top
      const {
        baseScale: scaleBase,
        baseOffsetX: ox,
        baseOffsetY: oy,
      } = baseRef.current
      const factor = Math.exp(-event.deltaY * WHEEL_ZOOM_SPEED)

      updateViewport(panel, (current) => {
        const currentScale = scaleBase * current.zoom
        if (currentScale <= 0) return current

        // 指针下方的世界坐标（1280×720 设计视口坐标系）。
        const worldX = (pointerX - (ox + current.panX)) / currentScale
        const worldY = (pointerY - (oy + current.panY)) / currentScale
        const nextZoom = clamp(current.zoom * factor, MIN_ZOOM, MAX_ZOOM)
        const nextScale = scaleBase * nextZoom

        return clampView({
          zoom: nextZoom,
          panX: pointerX - worldX * nextScale - ox,
          panY: pointerY - worldY * nextScale - oy,
        })
      })
    }

    node.addEventListener('wheel', onWheel, { passive: false })
    return () => node.removeEventListener('wheel', onWheel)
  }, [containerRef, panel, updateViewport, clampView])

  const startPan = useCallback((event) => {
    const middleButton = event.button === 1
    const spaceLeft = event.button === 0 && spaceRef.current
    if (!middleButton && !spaceLeft) return false

    event.preventDefault()
    event.stopPropagation()
    try {
      event.currentTarget.setPointerCapture(event.pointerId)
    } catch {
      // 某些浏览器 / 合成事件不支持捕获，忽略即可。
    }
    panRef.current = {
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
    }
    setPanning(true)
    return true
  }, [])

  const movePan = useCallback(
    (event) => {
      const pan = panRef.current
      if (!pan || pan.pointerId !== event.pointerId) return false

      event.preventDefault()
      const dx = event.clientX - pan.x
      const dy = event.clientY - pan.y
      pan.x = event.clientX
      pan.y = event.clientY

      updateViewport(panel, (previous) =>
        clampView({
          ...previous,
          panX: previous.panX + dx,
          panY: previous.panY + dy,
        }),
      )
      return true
    },
    [panel, updateViewport, clampView],
  )

  const endPan = useCallback((event) => {
    const pan = panRef.current
    if (!pan) return false
    if (event && pan.pointerId !== event.pointerId) return false
    panRef.current = null
    setPanning(false)
    return true
  }, [])

  const reset = useCallback(() => {
    resetViewport(panel)
  }, [panel, resetViewport])

  return {
    view,
    scale: baseScale * view.zoom,
    translateX: baseOffsetX + view.panX,
    translateY: baseOffsetY + view.panY,
    zoomPercent: Math.round(view.zoom * 100),
    spaceHeld,
    panning,
    reset,
    startPan,
    movePan,
    endPan,
  }
}
