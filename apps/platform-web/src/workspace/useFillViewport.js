import { useCallback } from 'react'

/**
 * 画布 / 预览的固定设计基准。
 *
 * 旧实现用「容器实测尺寸反推 scale」把 1280×720 的逻辑视口强行缩放到
 * 容器大小。这会带来两个问题：
 *   1. 页面并没有按真实桌面宽度排版，文字与大屏版式被整体压扁；
 *   2. iframe 内部永远没有滚动条，页面底部内容无法查看。
 *
 * 现在统一改为真实浏览器模型：
 *   - 逻辑视口固定为 1440×900（桌面端基准）；
 *   - 外层画布容器 `overflow: auto`，内容超出时出现真实滚动条；
 *   - 画布焦点态下滚轮直接缩放；网页焦点态下滚轮滚动 iframe 内容。
 *
 * 1440×900 同时作为标注归一化、Inspect 命中与双屏滚动同步的坐标系，
 * 画布与预览使用完全相同的基准，因此两边的页面位置一一对应。
 */
export const VIEWPORT_BASE_WIDTH = 1440
export const VIEWPORT_BASE_HEIGHT = 900

const FIXED_VIEWPORT = {
  scale: 1,
  offsetX: 0,
  offsetY: 0,
  designWidth: VIEWPORT_BASE_WIDTH,
  designHeight: VIEWPORT_BASE_HEIGHT,
}

/**
 * 兼容旧调用签名。
 *
 * 仍保留 `recalc` 供 Tab 激活等场景调用，但固定基准下不需要重新测量，
 * 因此是稳定的空操作，不会触发任何无意义的重渲染。
 */
export default function useFillViewport(_containerRef) {
  const recalc = useCallback(() => {}, [])
  return { ...FIXED_VIEWPORT, recalc }
}
