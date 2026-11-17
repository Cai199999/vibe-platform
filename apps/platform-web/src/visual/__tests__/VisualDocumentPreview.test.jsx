import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createRoot } from 'react-dom/client'
import VisualDocumentPreview from '../VisualDocumentPreview.jsx'
import {
  addVisualPage,
  createEmptyVisualDocument,
  createVisualNode,
  insertVisualNodeIntoDocument,
} from '../visualDocument.js'

function buildDocument() {
  let doc = createEmptyVisualDocument({ projectId: 'proj_preview', title: '预览' })
  const button = createVisualNode('button', { props: { text: '去用户页' } })
  doc = insertVisualNodeIntoDocument(doc, {
    parentId: doc.root.id,
    type: 'button',
    node: button,
  })
  const added = addVisualPage(doc, { title: '用户管理', path: '/users' })
  doc = added.document
  doc = insertVisualNodeIntoDocument(doc, {
    parentId: added.page.root.id,
    type: 'text',
    node: createVisualNode('text', { props: { text: '用户列表页面' } }),
  })
  doc.interactions = [
    {
      id: 'int_nav',
      nodeId: button.id,
      event: 'click',
      action: 'navigate',
      targetPageId: added.page.pageId,
      status: 'CONFIRMED',
    },
  ]
  // 切回首页，模拟用户在首页点击导航。
  doc.activePageId = doc.pages[0].pageId
  doc.root = doc.pages[0].root
  return { doc, buttonId: button.id, targetPageId: added.page.pageId }
}

describe('VisualDocumentPreview', () => {
  let container
  let root
  const onSwitchPage = vi.fn()

  beforeEach(() => {
    globalThis.IS_REACT_ACT_ENVIRONMENT = true
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    const { doc } = buildDocument()
    act(() => {
      root.render(
        <VisualDocumentPreview
          document={doc}
          onClose={() => {}}
          onSwitchPage={onSwitchPage}
        />,
      )
    })
  })

  afterEach(() => {
    act(() => root.unmount())
    container.remove()
    globalThis.IS_REACT_ACT_ENVIRONMENT = false
    onSwitchPage.mockClear()
  })

  it('渲染页面 Tab 与真实 DOM 组件', () => {
    expect(container.querySelector('[data-visual-preview]')).toBeTruthy()
    expect(container.querySelectorAll('.visual-preview-pages button')).toHaveLength(2)
    expect(container.textContent).toContain('去用户页')
    expect(container.querySelector('[data-preview-node-id]')).toBeTruthy()
  })

  it('点击页面 Tab 与 navigate 按钮都调用切换回调', () => {
    const tabs = container.querySelectorAll('.visual-preview-pages button')
    act(() => {
      tabs[1].dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    expect(onSwitchPage).toHaveBeenCalled()

    onSwitchPage.mockClear()
    const navButton = [...container.querySelectorAll('button')].find(
      (button) => button.textContent.includes('去用户页'),
    )
    expect(navButton).toBeTruthy()
    act(() => {
      navButton.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    expect(onSwitchPage).toHaveBeenCalled()
  })
})
