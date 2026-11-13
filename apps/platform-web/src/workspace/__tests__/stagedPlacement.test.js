import { describe, expect, it } from 'vitest'
import {
  buildPlacementPatch,
  clampGhostRect,
  clientToFramePoint,
  formatPlacementAcceptance,
  ghostRectToDocumentRect,
  parentAnchorId,
  stageTargetLabel,
} from '../stagedPlacement.js'

describe('暂存组件拖拽 / 落点 placement', () => {
  it('ghost 永远不会被拖出 iframe 视口', () => {
    const clamped = clampGhostRect({
      x: 0.94,
      y: -0.2,
      width: 0.3,
      height: 0.2,
    })
    console.log('[边界 clamp]', clamped)
    expect(clamped.x).toBeCloseTo(0.7)
    expect(clamped.y).toBe(0)
    expect(clamped.x + clamped.width).toBeLessThanOrEqual(1)
    expect(clamped.y + clamped.height).toBeLessThanOrEqual(1)
  })

  it('屏幕坐标按画布 scale 换算成 iframe CSS 像素', () => {
    const point = clientToFramePoint({
      clientX: 300,
      clientY: 160,
      box: { left: 100, top: 60 },
      scale: 0.5,
      width: 1440,
      height: 900,
    })
    console.log('[坐标换算]', point)
    expect(point.x).toBe(400)
    expect(point.y).toBe(200)
    expect(point.inside).toBe(true)
  })

  it('Drop 生成父容器锚点与相对坐标，且不写入绝对定位', () => {
    const ghostRect = { x: 0.25, y: 0.2, width: 0.12, height: 0.05 }
    const scroll = { scrollX: 0, scrollY: 120, width: 1440, height: 900 }
    const hit = {
      parent: {
        marker: 'src/App.jsx:12:5',
        selector: '[data-source="src/App.jsx:12:5"]',
        label: 'Header',
        component: 'Header',
        kind: 'container',
        file: 'src/App.jsx',
        line: 12,
        column: 5,
        instance: 2,
        instanceCount: 2,
        instanceId: 'src/App.jsx:12:5#2',
        callSite: { file: 'src/pages/Home.jsx', line: 8, column: 3 },
      },
      rect: { x: 120, y: 80, width: 640, height: 220 },
      documentRect: { x: 120, y: 200, width: 640, height: 220 },
      insertAnchor: {
        marker: 'src/App.jsx:20:7',
        instanceId: 'src/App.jsx:20:7#1',
      },
      insertPosition: 'before',
      siblingIndex: 1,
      siblingCount: 4,
      confidence: 'exact',
    }

    const patch = buildPlacementPatch({ ghostRect, hit, scroll, now: 123 })
    console.log('[placement]', JSON.stringify(patch.placement, null, 2))

    expect(patch.documentRect).toEqual({
      x: 360,
      y: 300,
      width: 172.8,
      height: 45,
    })
    expect(patch.placement.targetParentId).toBe('src/App.jsx:12:5#2')
    expect(patch.placement.insertAnchorId).toBe('src/App.jsx:20:7#1')
    expect(patch.placement.insertPosition).toBe('before')
    expect(patch.placement.siblingIndex).toBe(1)
    expect(patch.placement.relativeX).toBe(240)
    expect(patch.placement.relativeY).toBe(100)
    expect(JSON.stringify(patch.placement)).not.toMatch(/left|top|position:absolute/)
  })

  it('父容器身份优先 instanceId，退化时才用 marker#instance', () => {
    expect(parentAnchorId({ instanceId: 'a#2', marker: 'a' })).toBe('a#2')
    expect(parentAnchorId({ marker: 'a', instance: 3 })).toBe('a#3')
    expect(parentAnchorId(null)).toBe('page')
  })

  it('Tooltip 明确提示容器类型与复用风险', () => {
    const exact = stageTargetLabel({
      parent: { label: 'Header', kind: 'container', selector: '[data-x]' },
      confidence: 'exact',
    })
    const risky = stageTargetLabel({
      parent: {
        label: 'Card',
        kind: 'container',
        selector: '[data-x]',
        instanceCount: 2,
      },
      confidence: 'container_only',
    })
    console.log('[Tooltip]', exact, '/', risky)
    expect(exact).toBe('即将放入：Header 容器')
    expect(risky).toContain('低置信')
    expect(risky).toContain('多处复用')
  })

  it('归一化矩形按滚动快照恢复成文档坐标', () => {
    const rect = ghostRectToDocumentRect(
      { x: 0.5, y: 0.5, width: 0.1, height: 0.1 },
      { scrollX: 0, scrollY: 200, width: 1440, height: 900 },
    )
    expect(rect).toEqual({ x: 720, y: 650, width: 144, height: 90 })
  })

  it('acceptance 文案禁止绝对定位', () => {
    const text = formatPlacementAcceptance(
      { insertPosition: 'inside_end', insertAnchorId: '', siblingIndex: 2 },
      'Header',
    )
    console.log('[acceptance]', text)
    expect(text).toContain('Header')
    expect(text).toContain('inside_end')
    expect(text).toContain('禁止写死 left / top')
  })
})
