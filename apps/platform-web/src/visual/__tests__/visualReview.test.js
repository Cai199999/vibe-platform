import { describe, expect, it } from 'vitest'
import {
  createEmptyVisualDocument,
  createVisualNode,
  insertVisualNodeIntoDocument,
} from '../visualDocument.js'
import {
  buildSemanticReviewContext,
  buildSemanticReviewPacket,
} from '../visualReview.js'

function reviewDocument() {
  let doc = createEmptyVisualDocument({ projectId: 'proj_review' })
  const list = createVisualNode('list', {
    name: '职位列表',
    repeat: {
      source: 'job_descriptions',
      itemAlias: 'job',
      keyField: 'id',
      sampleCount: 3,
      emptyText: '',
    },
  })
  doc = insertVisualNodeIntoDocument(doc, {
    parentId: doc.root.id,
    type: 'list',
    node: list,
  })
  doc = insertVisualNodeIntoDocument(doc, {
    parentId: doc.root.id,
    type: 'button',
    node: createVisualNode('button', { props: { text: '新建职位' } }),
  })
  return doc
}

describe('语义审查包', () => {
  it('包含全部交互组件与未绑定 repeat 数据源', () => {
    const packet = buildSemanticReviewPacket(reviewDocument())
    expect(packet.interactiveComponents.map((item) => item.type)).toEqual(
      expect.arrayContaining(['list', 'button']),
    )
    expect(packet.repeatsWithoutBinding.map((item) => item.name)).toContain(
      '职位列表',
    )
    expect(packet.summary).toContain('未绑定 1 个')
    expect(packet.summary).toContain('交互组件 2 个')
  })

  it('审查上下文明确要求全量审查，不只增量', () => {
    const packet = buildSemanticReviewPacket(reviewDocument())
    const context = buildSemanticReviewContext(packet)
    expect(context).toContain('不能只审查增量')
    expect(context).toContain('interactiveComponents')
  })
})
