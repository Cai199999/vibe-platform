import { beforeEach, describe, expect, it } from 'vitest'
import {
  buildVisualDelta,
  clearVisualV0Baseline,
  loadVisualV0Baseline,
  storeVisualV0Baseline,
} from '../visualDelta.js'
import {
  createEmptyVisualDocument,
  createVisualNode,
  deleteVisualNodeFromDocument,
  insertVisualNodeIntoDocument,
  moveVisualNodeInDocument,
  updateVisualNodeInDocument,
} from '../visualDocument.js'

function baselineDoc() {
  let doc = createEmptyVisualDocument({ projectId: 'proj_delta' })
  const section = createVisualNode('section', { name: 'Header' })
  doc = insertVisualNodeIntoDocument(doc, {
    parentId: doc.root.id,
    type: 'section',
    node: section,
  })
  const button = createVisualNode('button', { props: { text: '保存' } })
  doc = insertVisualNodeIntoDocument(doc, {
    parentId: section.id,
    type: 'button',
    node: button,
  })
  return { doc, sectionId: section.id, buttonId: button.id }
}

describe('buildVisualDelta', () => {
  it('识别新增、修改、移动与删除', () => {
    const { doc, sectionId, buttonId } = baselineDoc()
    let current = updateVisualNodeInDocument(doc, buttonId, {
      props: { text: '提交' },
    })
    const text = createVisualNode('text', { props: { text: '说明' } })
    current = insertVisualNodeIntoDocument(current, {
      parentId: sectionId,
      type: 'text',
      node: text,
    })
    const nested = createVisualNode('container', { name: 'Card' })
    current = insertVisualNodeIntoDocument(current, {
      parentId: sectionId,
      type: 'container',
      node: nested,
    })
    current = moveVisualNodeInDocument(current, {
      nodeId: buttonId,
      parentId: nested.id,
      index: 0,
    })
    current = {
      ...current,
      interactions: [
        {
          id: 'int_new',
          nodeId: buttonId,
          event: 'click',
          action: 'submit',
          status: 'UNRESOLVED',
        },
      ],
    }

    const delta = buildVisualDelta(doc, current)
    expect(delta.added.map((item) => item.id)).toContain(text.id)
    expect(delta.added.map((item) => item.id)).toContain(nested.id)
    expect(delta.updated.map((item) => item.id)).toContain(buttonId)
    expect(delta.moved.map((item) => item.id)).toContain(buttonId)
    expect(delta.interactions.added.map((item) => item.id)).toContain('int_new')
    expect(delta.summary).toContain('新增 2 个节点')
  })

  it('识别删除节点', () => {
    const { doc, buttonId } = baselineDoc()
    const current = deleteVisualNodeFromDocument(doc, buttonId)
    const delta = buildVisualDelta(doc, current)
    expect(delta.removed.map((item) => item.id)).toContain(buttonId)
  })
})

describe('v0 基线存储', () => {
  beforeEach(() => window.localStorage.clear())

  it('按项目读写并清理基线', () => {
    const { doc } = baselineDoc()
    expect(storeVisualV0Baseline('proj_delta', doc)).toBe(true)
    expect(loadVisualV0Baseline('proj_delta')?.docId).toBe(doc.docId)
    clearVisualV0Baseline('proj_delta')
    expect(loadVisualV0Baseline('proj_delta')).toBeNull()
  })
})
