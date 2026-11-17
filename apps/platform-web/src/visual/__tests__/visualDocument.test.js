import { describe, expect, it } from 'vitest'
import {
  addVisualPage,
  createEmptyVisualDocument,
  createVisualNode,
  deleteVisualNodeFromDocument,
  duplicateVisualNodeInDocument,
  findVisualNode,
  insertVisualNodeIntoDocument,
  insertVisualNodesIntoDocument,
  moveVisualNodeInDocument,
  switchVisualPage,
  updateVisualNodeInDocument,
  validateVisualDocument,
  visualDocumentFromContract,
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

  it('DRAFT 列表未绑定 repeat.source 只给 warning', () => {
    const doc = emptyDoc()
    doc.root.children.push(
      createVisualNode('list', { repeat: { source: 'orders', sampleCount: 3 } }),
    )
    const report = validateVisualDocument(doc)
    expect(report.ok).toBe(true)
    expect(report.warnings.join('；')).toContain('业务审查')
  })

  it('校验器拒绝 props 中的 JSX/HTML 标记', () => {
    const doc = emptyDoc()
    doc.root.children.push(
      createVisualNode('text', { props: { text: '<Button>提交</Button>' } }),
    )
    const report = validateVisualDocument(doc)
    expect(report.ok).toBe(false)
    expect(report.errors.join('；')).toContain('JSX/HTML')
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


describe('Architect 契约桥接', () => {
  it('取出 visualDocument 并补齐 projectId/title', () => {
    const source = createEmptyVisualDocument({ title: '订单工作台' })
    const document = visualDocumentFromContract(
      { visualDocument: source },
      { projectId: 'proj_arch', title: '回退标题' },
    )
    expect(document.projectId).toBe('proj_arch')
    expect(document.title).toBe('订单工作台')
    expect(validateVisualDocument(document).ok).toBe(true)
  })

  it('旧契约没有 visualDocument 时返回 null', () => {
    expect(visualDocumentFromContract({ components: [] })).toBeNull()
  })
})


describe('Agent 节点插入', () => {
  it('一次插入整组节点、重写冲突 id、只增加一次 revision', () => {
    let doc = emptyDoc()
    const container = createVisualNode('container')
    doc = insertVisualNodeIntoDocument(doc, {
      parentId: doc.root.id,
      type: 'container',
      node: container,
    })
    const revision = doc.revision
    const result = insertVisualNodesIntoDocument(doc, {
      parentId: container.id,
      nodes: [
        {
          id: container.id,
          type: 'form',
          name: '登录表单',
          children: [
            { id: 'dup', type: 'input', props: { placeholder: '用户名' } },
            { id: 'dup', type: 'button', props: { text: '登录' } },
          ],
        },
      ],
    })
    expect(result.document.revision).toBe(revision + 1)
    expect(result.insertedIds).toHaveLength(1)
    const form = findVisualNode(result.document.root, result.insertedIds[0]).node
    expect(form.type).toBe('form')
    expect(form.id).not.toBe(container.id)
    const childIds = form.children.map((child) => child.id)
    expect(new Set(childIds).size).toBe(2)
    expect(validateVisualDocument(result.document).ok).toBe(true)
  })
})

describe('VisualDocument 多页面', () => {
  it('新增 / 切换页面并保持 root 镜像与校验通过', () => {
    let doc = createEmptyVisualDocument({ projectId: 'proj_pages' })
    const added = addVisualPage(doc, { title: '用户管理', path: '/users' })
    doc = added.document
    expect(doc.pages).toHaveLength(2)
    expect(doc.activePageId).toBe(added.page.pageId)
    expect(doc.root.id).toBe(added.page.root.id)
    expect(validateVisualDocument(doc).ok).toBe(true)

    const home = doc.pages.find((page) => page.path === '/')
    const switched = switchVisualPage(doc, home.pageId)
    expect(switched.activePageId).toBe(home.pageId)
    expect(switched.root.id).toBe(home.root.id)
    expect(validateVisualDocument(switched).ok).toBe(true)
  })

  it('页面 root 必须存在，空页面列表表示空白画布', () => {
    const doc = { ...emptyDoc(), pages: [], activePageId: '', root: null }
    expect(validateVisualDocument(doc).ok).toBe(true)
  })
})

describe('业务审查前的 repeat 校验', () => {
  it('FROZEN 且 repeat 未绑定只给 warning，不阻止进入业务审查', () => {
    let doc = createEmptyVisualDocument({ projectId: 'proj_review' })
    doc = insertVisualNodeIntoDocument(doc, {
      parentId: doc.root.id,
      type: 'list',
      node: createVisualNode('list', {
        repeat: {
          source: 'job_descriptions',
          itemAlias: 'item',
          keyField: 'id',
          sampleCount: 2,
          emptyText: '',
        },
      }),
    })
    const frozen = { ...doc, status: 'FROZEN', metadata: { ...doc.metadata, contentHash: 'sha256:test' } }
    const report = validateVisualDocument(frozen)
    expect(report.ok).toBe(true)
    expect(report.warnings.join('\n')).toContain('job_descriptions')
  })
})
