import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
} from 'react'
import {
  isPreviewLive,
  resolveEffectiveLinked,
} from './previewState.js'

/**
 * 画布 / 预览各自的视口状态（缩放 + 平移）。
 *
 * 这里刻意不把状态放在 CanvasStage / ScaledPreview 内部：
 *  - 对比模式下切换「画布 / 预览」只改变 CSS 可见性，组件本身不卸载；
 *  - 即使用户切到展示模式、再切回来，状态仍保存在 ProjectWorkspace 级别的
 *    Provider 中，scale / x / y 零丢失；
 *  - 每个项目窗口拥有独立的 Provider，多 Tab 之间互不串状态。
 *
 * zoom 是相对「铺满容器的基准比例」的倍率：
 *   1 = 初始 cover 比例，0.25 ~ 3 = 可交互缩放范围。
 * panX / panY 是相对基准居中位置的屏幕像素偏移。
 */
export const MIN_ZOOM = 0.25
export const MAX_ZOOM = 3
export const DEFAULT_VIEW = Object.freeze({ zoom: 1, panX: 0, panY: 0 })

const ViewportContext = createContext(null)

const clamp = (value, min, max) => Math.min(max, Math.max(min, value))

/**
 * 视口状态中心。
 *
 * linked = true 时（展示模式的「视口联动」），任意一侧的更新都会同时写入
 * 画布和预览。两个面板在展示模式下宽高完全一致、基准变换也一致，因此同一
 * 组 zoom/pan 会产生完全相同的视觉变换。
 */
export function ViewportProvider({ children, previewState = 'LIVE' }) {
  const [views, setViews] = useState({
    canvas: DEFAULT_VIEW,
    preview: DEFAULT_VIEW,
  })
  // linkedPreference 是用户的显式选择，任何预览状态变化都不得覆盖它。
  const [linkedPreference, setLinkedPreference] = useState(false)
  // 真正参与视口计算的是“偏好 + 预览实时可用”的交集。
  const linked = resolveEffectiveLinked(linkedPreference, previewState)

  const updateViewport = useCallback(
    (panel, updater) => {
      setViews((previous) => {
        const current = previous[panel] ?? DEFAULT_VIEW
        const candidate =
          typeof updater === 'function' ? updater(current) : updater
        const next = {
          zoom: clamp(candidate.zoom ?? current.zoom, MIN_ZOOM, MAX_ZOOM),
          panX: candidate.panX ?? current.panX,
          panY: candidate.panY ?? current.panY,
        }
        if (linked) {
          return { canvas: next, preview: next }
        }
        return { ...previous, [panel]: next }
      })
    },
    [linked],
  )

  const resetViewport = useCallback(
    (panel) => {
      setViews((previous) =>
        linked
          ? { canvas: DEFAULT_VIEW, preview: DEFAULT_VIEW }
          : { ...previous, [panel]: DEFAULT_VIEW },
      )
    },
    [linked],
  )

  const value = useMemo(
    () => ({
      views,
      linked,
      linkedPreference,
      setLinked: setLinkedPreference,
      setLinkedPreference,
      previewState,
      linkDisabled: !isPreviewLive(previewState),
      updateViewport,
      resetViewport,
    }),
    [
      views,
      linked,
      linkedPreference,
      previewState,
      updateViewport,
      resetViewport,
    ],
  )

  return (
    <ViewportContext.Provider value={value}>{children}</ViewportContext.Provider>
  )
}

export function useViewportState() {
  const value = useContext(ViewportContext)
  if (!value) {
    throw new Error('useViewportState 必须在 ViewportProvider 内使用')
  }
  return value
}
