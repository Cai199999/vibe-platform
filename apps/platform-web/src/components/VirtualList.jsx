import { useLayoutEffect, useMemo, useRef, useState } from 'react'

/**
 * 轻量虚拟滚动列表。
 *
 * 适用场景：快照历史、Agent 执行轨迹等可能达到数百条的长列表。
 * 只渲染可视区 + overscan 行，避免一次创建大量 DOM 导致滚动卡顿。
 *
 * 行高支持：
 *  - number：固定行高，性能最好；
 *  - (item, index) => number：估算行高（展开确认框等场景）；
 *  - 实际渲染后会通过 useLayoutEffect 测量真实高度并修正偏移。
 *
 * @param {object} props
 * @param {Array} props.items
 * @param {number|Function} props.itemHeight
 * @param {number} props.height 可视区高度（px）
 * @param {number} [props.overscan=4]
 * @param {(item: any, index: number) => React.ReactNode} props.renderItem
 * @param {(item: any, index: number) => string|number} [props.getKey]
 * @param {string} [props.className]
 */
export default function VirtualList({
  items,
  itemHeight,
  height,
  overscan = 4,
  renderItem,
  getKey,
  className = '',
}) {
  const [scrollTop, setScrollTop] = useState(0)
  const [measured, setMeasured] = useState({})
  const rowNodes = useRef(new Map())

  const estimate = useMemo(
    () =>
      items.map((item, index) =>
        typeof itemHeight === 'function' ? itemHeight(item, index) : itemHeight,
      ),
    [items, itemHeight],
  )

  const heights = useMemo(
    () => estimate.map((value, index) => measured[index] ?? value),
    [estimate, measured],
  )
  const offsets = useMemo(() => {
    const result = new Array(items.length + 1)
    result[0] = 0
    for (let index = 0; index < items.length; index += 1) {
      result[index + 1] = result[index] + heights[index]
    }
    return result
  }, [heights, items.length])
  const totalHeight = offsets[items.length] ?? 0

  // 真实 DOM 高度可能与估算不同（文案换行 / 展开确认框），渲染后修正。
  useLayoutEffect(() => {
    let changed = false
    const next = { ...measured }
    rowNodes.current.forEach((element, index) => {
      if (!element) return
      const actual = element.offsetHeight
      if (actual > 0 && Math.abs(actual - (next[index] ?? estimate[index])) > 1) {
        next[index] = actual
        changed = true
      }
    })
    if (changed) setMeasured(next)
  })

  // 二分查找可视区起点。
  let start = 0
  let low = 0
  let high = items.length
  while (low < high) {
    const mid = Math.floor((low + high) / 2)
    if (offsets[mid] <= scrollTop) {
      start = mid
      low = mid + 1
    } else {
      high = mid
    }
  }
  start = Math.max(0, Math.min(items.length - 1, start - overscan))

  let end = start
  const visibleBottom = scrollTop + height
  while (end < items.length && offsets[end] < visibleBottom) end += 1
  end = Math.min(items.length, end + overscan)

  return (
    <div
      className={`virtual-list ${className}`.trim()}
      style={{ height, overflowY: 'auto', position: 'relative' }}
      onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
      data-virtual-list
    >
      <div style={{ height: totalHeight, position: 'relative' }}>
        {items.slice(start, end).map((item, offset) => {
          const index = start + offset
          return (
            <div
              key={getKey ? getKey(item, index) : index}
              ref={(element) => {
                if (element) rowNodes.current.set(index, element)
                else rowNodes.current.delete(index)
              }}
              style={{
                position: 'absolute',
                top: offsets[index],
                left: 0,
                right: 0,
                minHeight: heights[index],
              }}
            >
              {renderItem(item, index)}
            </div>
          )
        })}
      </div>
    </div>
  )
}
