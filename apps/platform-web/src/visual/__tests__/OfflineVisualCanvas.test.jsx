import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createRoot } from 'react-dom/client'
import OfflineVisualCanvas from '../OfflineVisualCanvas.jsx'
import { createEmptyVisualDocument } from '../visualDocument.js'
import { useVisualDocumentStore } from '../visualDocumentStore.js'

function clickByText(container, selector, text) {
  const element = [...container.querySelectorAll(selector)].find((item) =>
    item.textContent.includes(text),
  )
  expect(element).toBeTruthy()
  act(() => {
    element.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  })
}

describe('OfflineVisualCanvas', () => {
  let container
  let root

  beforeEach(() => {
    globalThis.IS_REACT_ACT_ENVIRONMENT = true
    window.localStorage.clear()
    const doc = createEmptyVisualDocument({ projectId: 'proj_ui' })
    useVisualDocumentStore.setState({
      doc,
      selectedId: doc.root.id,
      history: [],
      future: [],
      lastError: '',
    })
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    act(() => {
      root.render(<OfflineVisualCanvas projectId="proj_ui" title="UI 测试" />)
    })
  })

  afterEach(() => {
    act(() => root.unmount())
    container.remove()
    globalThis.IS_REACT_ACT_ENVIRONMENT = false
  })

  it('从组件库添加区块与文本，并渲染离线树', () => {
    expect(container.querySelector('.vc-empty-page')).toBeTruthy()

    clickByText(container, '.vc-palette-item', '区块')
    expect(container.querySelector('.vc-node-section')).toBeTruthy()

    clickByText(container, '.vc-palette-item', '文本')
    expect(container.querySelector('.vc-node-text')).toBeTruthy()
    expect(container.textContent).toContain('在这里输入文本')
  })

  it('删除选中节点后可从空画布恢复根页面', () => {
    clickByText(container, '.vc-palette-item', '区块')
    const canvasStore = useVisualDocumentStore.getState()
    const rootId = canvasStore.doc.root.id
    act(() => {
      canvasStore.deleteNode(rootId)
    })
    expect(container.querySelector('.vc-empty-canvas')).toBeTruthy()

    clickByText(container, '.primary-button', '添加区块')
    expect(container.querySelector('.vc-node-section')).toBeTruthy()
  })
})
