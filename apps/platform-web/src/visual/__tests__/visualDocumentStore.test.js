import { beforeEach, describe, expect, it } from 'vitest'
import { createEmptyVisualDocument, findVisualNode } from '../visualDocument.js'
import { useVisualDocumentStore } from '../visualDocumentStore.js'

function resetStore() {
  const doc = createEmptyVisualDocument({ projectId: 'proj_store' })
  useVisualDocumentStore.setState({
    doc,
    selectedId: doc.root.id,
    history: [],
    future: [],
    lastError: '',
  })
}

describe('VisualDocument Zustand store', () => {
  beforeEach(resetStore)

  it('添加 / 选中 / 删除节点只改本地文档', () => {
    const store = useVisualDocumentStore.getState()
    const rootId = store.doc.root.id

    expect(store.addNode('section', rootId)).toBe(true)
    const afterSection = useVisualDocumentStore.getState()
    const section = afterSection.doc.root.children[0]
    expect(section.type).toBe('section')
    expect(afterSection.selectedId).toBe(section.id)

    expect(afterSection.addNode('text', section.id)).toBe(true)
    const afterText = useVisualDocumentStore.getState()
    expect(afterText.doc.root.children[0].children[0].type).toBe('text')
    expect(afterText.doc.revision).toBe(2)
    expect(afterText.history).toHaveLength(2)
  })

  it('撤销 / 重做恢复完整文档快照', () => {
    const store = useVisualDocumentStore.getState()
    store.addNode('section', store.doc.root.id)
    const id = useVisualDocumentStore.getState().doc.root.children[0].id

    expect(useVisualDocumentStore.getState().undo()).toBe(true)
    expect(
      findVisualNode(useVisualDocumentStore.getState().doc.root, id),
    ).toBeNull()
    expect(useVisualDocumentStore.getState().redo()).toBe(true)
    expect(
      findVisualNode(useVisualDocumentStore.getState().doc.root, id),
    ).not.toBeNull()
  })

  it('拖拽换父容器通过一次命令提交', () => {
    const store = useVisualDocumentStore.getState()
    store.addNode('section', store.doc.root.id)
    const sectionId = useVisualDocumentStore.getState().doc.root.children[0].id
    useVisualDocumentStore.getState().addNode('text', store.doc.root.id)
    const textId = useVisualDocumentStore
      .getState()
      .doc.root.children.find((node) => node.type === 'text').id

    useVisualDocumentStore.getState().moveNode(textId, sectionId, 0)
    const moved = useVisualDocumentStore.getState()
    expect(findVisualNode(moved.doc.root, textId).parent.id).toBe(sectionId)
    expect(moved.doc.root.children[0].children[0].id).toBe(textId)
  })

  it('删除根节点后是合法空白画布，撤销可恢复', () => {
    const store = useVisualDocumentStore.getState()
    const rootId = store.doc.root.id
    store.addNode('section', rootId)
    useVisualDocumentStore.getState().deleteNode(rootId)
    expect(useVisualDocumentStore.getState().doc.root).toBeNull()
    expect(useVisualDocumentStore.getState().undo()).toBe(true)
    expect(useVisualDocumentStore.getState().doc.root).not.toBeNull()
  })
})


describe('Agent insert 与冻结', () => {
  beforeEach(resetStore)

  it('insertNodes 一次提交并选中首个新节点', () => {
    const store = useVisualDocumentStore.getState()
    store.addNode('container', store.doc.root.id)
    const containerId = useVisualDocumentStore.getState().doc.root.children[0].id
    const before = useVisualDocumentStore.getState().doc.revision
    const ok = useVisualDocumentStore.getState().insertNodes(containerId, [
      {
        type: 'form',
        name: '登录表单',
        children: [
          { type: 'input', props: { placeholder: '用户名' } },
          { type: 'button', props: { text: '登录' } },
        ],
      },
    ])
    expect(ok).toBe(true)
    const after = useVisualDocumentStore.getState()
    expect(after.doc.revision).toBe(before + 1)
    expect(after.selectedId).toBeTruthy()
    expect(after.doc.root.children[0].children[0].type).toBe('form')
  })

  it('冻结后禁止一切编辑并写 contentHash', async () => {
    const store = useVisualDocumentStore.getState()
    store.addNode('section', store.doc.root.id)
    const frozen = await useVisualDocumentStore.getState().freezeDocument()
    expect(frozen.status).toBe('FROZEN')
    expect(frozen.metadata.contentHash).toMatch(/^sha256:[0-9a-f]{64}$/)
    expect(useVisualDocumentStore.getState().doc.status).toBe('FROZEN')
    expect(useVisualDocumentStore.getState().addNode('text')).toBe(false)
    expect(useVisualDocumentStore.getState().undo()).toBe(false)
    expect(useVisualDocumentStore.getState().updateText('nd_x', 'x')).toBe(false)
  })
})
