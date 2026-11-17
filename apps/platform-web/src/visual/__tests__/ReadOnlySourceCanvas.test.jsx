import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createRoot } from 'react-dom/client'
import ReadOnlySourceCanvas from '../ReadOnlySourceCanvas.jsx'
import {
  SOURCE_PROJECTION_TIP,
  buildSourceProjection,
} from '../sourceProjection.js'
import { sampleSourceAnalysis } from './sourceAnalysisFixture.js'

describe('ReadOnlySourceCanvas', () => {
  let container
  let root

  beforeEach(() => {
    globalThis.IS_REACT_ACT_ENVIRONMENT = true
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    const projection = buildSourceProjection(sampleSourceAnalysis())
    act(() => {
      root.render(
        <ReadOnlySourceCanvas projection={projection} onBack={() => {}} />,
      )
    })
  })

  afterEach(() => {
    act(() => root.unmount())
    container.remove()
    globalThis.IS_REACT_ACT_ENVIRONMENT = false
  })

  it('渲染只读投影与固定提示', () => {
    expect(container.querySelector('[data-source-projection]')).toBeTruthy()
    expect(container.textContent).toContain(SOURCE_PROJECTION_TIP)
    expect(container.textContent).toContain('Card.jsx')
    expect(container.querySelectorAll('[data-source-projection-node]').length).toBeGreaterThan(
      2,
    )
  })

  it('hover 节点展示层级、className 与只读提示', () => {
    const header = [...container.querySelectorAll('[data-projection-tag]')].find(
      (element) => element.getAttribute('data-projection-tag') === 'header',
    )
    expect(header).toBeTruthy()

    act(() => {
      header.dispatchEvent(
        new MouseEvent('mouseover', {
          bubbles: true,
          clientX: 120,
          clientY: 80,
        }),
      )
    })

    const tooltip = container.querySelector('[data-projection-tip]')
    expect(tooltip).toBeTruthy()
    expect(tooltip.textContent).toContain('Card › div › Header › header')
    expect(tooltip.textContent).toContain('items-center')
    expect(tooltip.textContent).toContain(SOURCE_PROJECTION_TIP)
  })
})
