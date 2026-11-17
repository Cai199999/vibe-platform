import { describe, expect, it } from 'vitest'
import {
  PROJECT_DOC_KINDS,
  buildProjectDocs,
  docsToMarkdown,
} from '../projectDocs.js'
import {
  createEmptyVisualDocument,
  addVisualPage,
} from '../../visual/visualDocument.js'

function buildContract() {
  let visual = createEmptyVisualDocument({ projectId: 'proj_docs', title: '订单台' })
  visual = addVisualPage(visual, { title: '订单列表', path: '/orders' }).document
  return {
    contractId: 'contract_docs',
    appShape: 'full_stack_system',
    status: 'CONFIRMED',
    databaseEngine: 'mysql',
    databaseDdl: 'CREATE TABLE orders (id BIGINT PRIMARY KEY, title TEXT);',
    relations: [
      {
        parentTable: 'users',
        childTable: 'orders',
        foreignKey: 'user_id',
        type: 'one_to_many',
      },
    ],
    apiRoutes: [
      { method: 'GET', path: '/api/v1/orders', description: '订单列表' },
    ],
    components: [{ name: 'OrderList', role: 'list', description: '订单列表' }],
    componentInteractions: [
      { component: 'OrderList', event: 'mount', apiRoute: 'GET /api/v1/orders' },
    ],
    implementationPath: [{ phase: 'backend', title: '后端', tasks: ['接口'] }],
    assumptions: ['订单状态由后端维护'],
    contractMarkdown: '# 订单台实施契约\n\n后端与数据库。',
    sourceHash: 'sha256:source',
    visualDocument: visual,
  }
}

describe('项目方案文档投影', () => {
  it('固定生成 01–05 五份模拟文档', () => {
    const docs = buildProjectDocs({
      contract: buildContract(),
      projectName: '订单台',
    })
    expect(docs.map((doc) => doc.title)).toEqual(PROJECT_DOC_KINDS)
    expect(docs.every((doc) => doc.simulated)).toBe(true)
    expect(docs.every((doc) => doc.status === 'CONFIRMED')).toBe(true)
    expect(docs[0].sections.length).toBeGreaterThan(0)
  })

  it('数据库文档与开发规范包含禁止物理外键和分层要求', () => {
    const docs = buildProjectDocs({
      contract: buildContract(),
      projectName: '订单台',
    })
    const db = docs[2]
    const final = docs[4]
    expect(db.sections.map((item) => item.body).join('\n')).toContain(
      '禁止物理外键',
    )
    const finalText = final.sections.map((item) => item.body).join('\n')
    expect(finalText).toContain('route / service / repository / dto')
    expect(finalText).toContain('feature / shared / hooks / api')
  })

  it('可以导出统一的 Markdown', () => {
    const docs = buildProjectDocs({
      contract: buildContract(),
      projectName: '订单台',
    })
    const markdown = docsToMarkdown(docs)
    expect(markdown).toContain('# 01-需求与功能模块清单')
    expect(markdown).toContain('# 05-最终设计方案与开发规范')
  })
})
