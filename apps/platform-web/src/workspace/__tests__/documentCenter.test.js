import { beforeEach, describe, expect, it } from 'vitest'
import {
  MAX_ACTIVE_DOCUMENTS,
  activeKnowledgeDocuments,
  addProjectDocument,
  loadProjectDocuments,
  mergeKnowledgeDocuments,
  removeProjectDocument,
  toggleProjectDocument,
} from '../documentCenter.js'

const projectId = 'proj_doc_center'

const doc = (name, content = '# 需求\n内容') => ({
  name,
  content,
  size: content.length,
  decision: 'STORE',
  reuse: 'FREQUENT',
})

describe('documentCenter', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('保存并返回启用的文档', () => {
    const result = addProjectDocument(projectId, doc('gis.md'))
    expect(result.ok).toBe(true)
    expect(loadProjectDocuments(projectId)).toHaveLength(1)
    expect(activeKnowledgeDocuments(projectId)).toEqual([
      { name: 'gis.md', content: '# 需求\n内容' },
    ])
  })

  it('合并时按内容去重并最多 3 份', () => {
    const merged = mergeKnowledgeDocuments(
      [doc('a.md'), doc('b.md')],
      [doc('a.md'), doc('c.md'), doc('d.md')],
    )
    expect(merged.map((item) => item.name)).toEqual(['a.md', 'b.md', 'c.md'])
  })

  it('最多允许 3 份启用', () => {
    for (let index = 0; index < MAX_ACTIVE_DOCUMENTS; index += 1) {
      addProjectDocument(projectId, doc(`doc-${index}.md`))
    }
    const extra = addProjectDocument(projectId, doc('doc-extra.md'))
    expect(extra.ok).toBe(true)
    const docs = loadProjectDocuments(projectId)
    expect(docs.filter((item) => item.active)).toHaveLength(
      MAX_ACTIVE_DOCUMENTS,
    )
    const target = docs.find((item) => item.name === 'doc-extra.md')
    const toggled = toggleProjectDocument(projectId, target.id, true)
    expect(toggled.ok).toBe(false)
  })

  it('删除文档后不再进入 RAG', () => {
    const result = addProjectDocument(projectId, doc('remove-me.md'))
    const target = result.docs[0]
    removeProjectDocument(projectId, target.id)
    expect(loadProjectDocuments(projectId)).toHaveLength(0)
    expect(activeKnowledgeDocuments(projectId)).toEqual([])
  })
})
