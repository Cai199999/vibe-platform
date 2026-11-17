/**
 * 项目方案文档投影（模拟文档）。
 *
 * 五份文档不是独立 LLM 产物，而是当前契约 / VisualDocument 的确定性投影：
 *   - 只读、可查看、可导出；
 *   - simulated=true，明确标注“由当前契约自动生成”；
 *   - 不写回契约、不作为第二事实源；
 *   - 后续若接入正式文档生成器，保持同一 docId / sections 接口即可。
 */

export const PROJECT_DOC_KINDS = [
  '01-需求与功能模块清单',
  '02-技术选型与架构设计',
  '03-数据库设计文档',
  '04-API接口设计清单',
  '05-最终设计方案与开发规范',
]

function codeBlock(value, language = 'text') {
  const text = String(value || '').trim()
  if (!text) return '（暂无）'
  return `\`\`\`${language}\n${text}\n\`\`\``
}

function table(rows, headers) {
  if (!rows.length) return '（暂无）'
  const head = `| ${headers.join(' | ')} |`
  const sep = `| ${headers.map(() => '---').join(' | ')} |`
  const body = rows.map(
    (row) => `| ${row.map((cell) => String(cell ?? '')).join(' | ')} |`,
  )
  return [head, sep, ...body].join('\n')
}

function visualPages(visualDocument) {
  if (!visualDocument) return []
  if (Array.isArray(visualDocument.pages) && visualDocument.pages.length) {
    return visualDocument.pages
  }
  if (visualDocument.root) {
    return [
      {
        pageId: 'legacy',
        path: '/',
        title: visualDocument.title || '页面',
        root: visualDocument.root,
      },
    ]
  }
  return []
}

function flattenNodes(root, output = []) {
  if (!root) return output
  output.push(root)
  ;(root.children || []).forEach((child) => flattenNodes(child, output))
  return output
}

function safeJson(value) {
  if (!value || (typeof value === 'object' && Object.keys(value).length === 0)) {
    return '（暂无）'
  }
  return codeBlock(JSON.stringify(value, null, 2), 'json')
}

export function buildProjectDocs({
  contract,
  visualDocument,
  projectName = '未命名项目',
} = {}) {
  const document =
    visualDocument ||
    contract?.visualDocument ||
    null
  const pages = visualPages(document)
  const pageRows = pages.map((page) => [
    page.title || page.path,
    page.path,
    page.pageId,
    flattenNodes(page.root).length,
  ])
  const componentRows = (contract?.components || []).map((item) => [
    item.name || '',
    item.role || '',
    item.section || '',
    item.description || '',
  ])
  const interactionRows = (contract?.componentInteractions || []).map(
    (item) => [
      item.component || '',
      item.event || '',
      item.apiRoute || item.api_route || '',
      item.logic || '',
    ],
  )
  const apiRows = (contract?.apiRoutes || []).map((item) => [
    item.method || '',
    item.path || '',
    item.description || '',
  ])
  const relationRows = (contract?.relations || []).map((item) => [
    item.parentTable || item.parent_table || '',
    item.childTable || item.child_table || '',
    item.foreignKey || item.foreign_key || '',
    item.type || '',
  ])
  const implementation = (contract?.implementationPath || []).map((phase) => [
    phase.phase || '',
    phase.title || '',
    (phase.tasks || []).join('；'),
  ])
  const status = contract?.status === 'CONFIRMED' ? 'CONFIRMED' : 'DRAFT'
  const revision = document?.revision ?? 0
  const contentHash =
    document?.metadata?.contentHash || contract?.sourceHash || ''
  const baseMeta = {
    simulated: true,
    revision,
    contentHash,
    status,
    generatedAt: new Date().toISOString(),
  }

  const docs = []

  docs.push({
    ...baseMeta,
    docId: `doc_${contract?.contractId || 'project'}_01`,
    kind: PROJECT_DOC_KINDS[0],
    title: PROJECT_DOC_KINDS[0],
    sections: [
      {
        title: '项目目标',
        body:
          contract?.contractMarkdown?.slice(0, 1200) ||
          `${projectName} 的需求与功能模块由 Architect 澄清结果投影生成。`,
      },
      {
        title: '页面与模块',
        body: table(pageRows, ['页面', '路径', 'pageId', '节点数']),
      },
      {
        title: '业务组件',
        body: table(componentRows, ['组件', '角色', '区域', '说明']),
      },
      {
        title: '关键交互',
        body: table(interactionRows, ['组件', '事件', 'API', '逻辑']),
      },
    ],
  })

  docs.push({
    ...baseMeta,
    docId: `doc_${contract?.contractId || 'project'}_02`,
    kind: PROJECT_DOC_KINDS[1],
    title: PROJECT_DOC_KINDS[1],
    sections: [
      {
        title: '应用形态',
        body: contract?.appShape || '（尚未确认）',
      },
      {
        title: '技术栈',
        body: safeJson(contract?.techStack),
      },
      {
        title: '实施阶段',
        body: table(implementation, ['阶段', '标题', '任务']),
      },
      {
        title: '前端结构',
        body: table(pageRows, ['页面', '路径', 'pageId', '节点数']),
      },
    ],
  })

  docs.push({
    ...baseMeta,
    docId: `doc_${contract?.contractId || 'project'}_03`,
    kind: PROJECT_DOC_KINDS[2],
    title: PROJECT_DOC_KINDS[2],
    sections: [
      {
        title: '数据库引擎',
        body: contract?.databaseEngine || '（尚未确认）',
      },
      {
        title: 'DDL',
        body: codeBlock(contract?.databaseDdl, 'sql'),
      },
      {
        title: '表关系',
        body: table(relationRows, ['父表', '子表', '外键字段', '类型']),
      },
      {
        title: '约束',
        body:
          '- 禁止物理外键（FOREIGN KEY），关联由 `<parent>_id` 与应用层事务保证；\n'
          + '- 表名使用 snake_case；列表查询必须带租户条件与分页。',
      },
    ],
  })

  docs.push({
    ...baseMeta,
    docId: `doc_${contract?.contractId || 'project'}_04`,
    kind: PROJECT_DOC_KINDS[3],
    title: PROJECT_DOC_KINDS[3],
    sections: [
      {
        title: 'REST 接口',
        body: table(apiRows, ['方法', '路径', '说明']),
      },
      {
        title: '组件交互',
        body: table(interactionRows, ['组件', '事件', 'API', '逻辑']),
      },
      {
        title: '未解决交互',
        body:
          (document?.interactions || [])
            .filter((item) => item.status !== 'CONFIRMED')
            .map((item) => `- ${item.nodeId}: ${item.intent || item.action}`)
            .join('\n') || '（全部已确认）',
      },
    ],
  })

  docs.push({
    ...baseMeta,
    docId: `doc_${contract?.contractId || 'project'}_05`,
    kind: PROJECT_DOC_KINDS[4],
    title: PROJECT_DOC_KINDS[4],
    sections: [
      {
        title: '最终契约',
        body: contract?.contractMarkdown || '（尚未生成契约）',
      },
      {
        title: '假设与边界',
        body:
          (contract?.assumptions || []).map((item) => `- ${item}`).join('\n') ||
          '（暂无）',
      },
      {
        title: '开发规范',
        body:
          '- 数据库禁止物理外键；关联通过业务字段与应用层事务保证；\n'
          + '- 后端按 route / service / repository / dto 分层，禁止业务逻辑写入路由；\n'
          + '- 前端按 feature / shared / hooks / api 分层，禁止单文件巨石组件；\n'
          + '- 依赖单向，禁止循环依赖；所有生成代码必须通过 AST / 构建 / 契约门禁。',
      },
    ],
  })

  return docs
}

export function docsToMarkdown(docs) {
  return docs
    .map((doc) => {
      const sections = (doc.sections || [])
        .map((section) => `## ${section.title}\n\n${section.body}`)
        .join('\n\n')
      return `# ${doc.title}\n\n${sections}`
    })
    .join('\n\n---\n\n')
}
