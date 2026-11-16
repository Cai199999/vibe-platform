import { describe, expect, it } from 'vitest'
import {
  createEmptyVisualDocument,
  createVisualNode,
  deleteVisualNodeFromDocument,
  duplicateVisualNodeInDocument,
  findVisualNode,
  insertVisualNodeIntoDocument,
  moveVisualNodeInDocument,
  updateVisualNodeInDocument,
  validateVisualDocument,
} from '../visualDocument.js'

function emptyDoc() {
  return createEmptyVisualDocument({ projectId: 'proj_test' })
}

describe('VisualDocument 纯函数', () => {
  it('空白文档合法，首次添加组件会创建根页面', () => {
    const doc = createEmptyVisualDocument({ withRoot: false })
    expect(validateVisualDocument(doc).ok).toBe(true)

    const child = createVisualNode('section')
    const next = insertVisualNodeIntoDocument(doc, {
      parentId: null,
      type: 'section',
      node: child,
    })

    expect(next.root.type).toBe('page')
    expect(next.root.children[0].id).toBe(child.id)
    expect(next.revision).toBe(1)
    expect(validateVisualDocument(next).ok).toBe(true)
  })

  it('支持增删改与任意根节点删除', () => {
    let doc = emptyDoc()
    const section = createVisualNode('section')
    doc = insertVisualNodeIntoDocument(doc, {
      parentId: doc.root.id,
      type: 'section',
      node: section,
    })
    const text = createVisualNode('text', { props: { text: '标题' } })
    doc = insertVisualNodeIntoDocument(doc, {
      parentId: section.id,
      type: 'text',
      node: text,
    })
    doc = updateVisualNodeInDocument(doc, text.id, {
      props: { text: '新标题' },
    })
    expect(findVisualNode(doc.root, text.id).node.props.text).toBe('新标题')

    doc = deleteVisualNodeFromDocument(doc, doc.root.id)
    expect(doc.root).toBeNull()
    expect(validateVisualDocument(doc).ok).toBe(true)
  })

  it('支持换父容器和同父容器排序', () => {
    let doc = emptyDoc()
    const a = createVisualNode('section', { name: 'A' })
    const b = createVisualNode('section', { name: 'B' })
    doc = insertVisualNodeIntoDocument(doc, {
      parentId: doc.root.id,
      type: 'section',
      node: a,
    })
    doc = insertVisualNodeIntoDocument(doc, {
      parentId: doc.root.id,
      type: 'section',
      node: b,
    })

    doc = moveVisualNodeInDocument(doc, {
      nodeId: b.id,
      parentId: a.id,
      index: 0,
    })
    expect(findVisualNode(doc.root, b.id).parent.id).toBe(a.id)
    expect(findVisualNode(doc.root, a.id).node.children[0].id).toBe(b.id)
  })

  it('禁止把节点拖进自己的子树', () => {
    let doc = emptyDoc()
    const parent = createVisualNode('section')
    const child = createVisualNode('container')
    doc = insertVisualNodeIntoDocument(doc, {
      parentId: doc.root.id,
      type: 'section',
      node: parent,
    })
    doc = insertVisualNodeIntoDocument(doc, {
      parentId: parent.id,
      type: 'container',
      node: child,
    })
    const before = doc
    const next = moveVisualNodeInDocument(doc, {
      nodeId: parent.id,
      parentId: child.id,
      index: 0,
    })
    expect(next).toBe(before)
  })

  it('复制子树会生成全新 nodeId', () => {
    let doc = emptyDoc()
    const section = createVisualNode('section')
    doc = insertVisualNodeIntoDocument(doc, {
      parentId: doc.root.id,
      type: 'section',
      node: section,
    })
    const text = createVisualNode('text')
    doc = insertVisualNodeIntoDocument(doc, {
      parentId: section.id,
      type: 'text',
      node: text,
    })
    doc = duplicateVisualNodeInDocument(doc, section.id)
    const copied = doc.root.children[1]
    expect(copied.id).not.toBe(section.id)
    expect(copied.children[0].id).not.toBe(text.id)
    expect(validateVisualDocument(doc).ok).toBe(true)
  })

  it('校验器拒绝重复 nodeId 与像素坐标', () => {
    const doc = emptyDoc()
    doc.root.children.push({
      ...createVisualNode('text'),
      id: 'nd_dup',
      x: 20,
    })
    doc.root.children.push({
      ...createVisualNode('text'),
      id: 'nd_dup',
    })
    const report = validateVisualDocument(doc)
    expect(report.ok).toBe(false)
    expect(report.errors.join('；')).toContain('重复 nodeId')
    expect(report.errors.join('；')).toContain('像素坐标')
  })
})
