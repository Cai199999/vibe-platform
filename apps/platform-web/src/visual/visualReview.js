/**
 * 语义审查包：从冻结的 VisualDocument 提取业务绑定与交互缺口。
 *
 * 只传层级、语义、interactions、bindings；不传像素坐标、DOM 或源码。
 */

import { buildVisualDelta } from './visualDelta.js'
import { ensureVisualPages } from './visualDocument.js'

export const VISUAL_REVIEW_STORAGE_PREFIX = 'vibe.visual.review.'

export function buildSemanticReviewPacket(document, { baseline = null } = {}) {
  const nodes = []
  const walk = (node, parentId, path, depth, page) => {
    if (!node) return
    const currentPath = `${path}/${node.name || node.type}`
    nodes.push({
      nodeId: node.id,
      parentId: parentId || null,
      pageId: page?.pageId || '',
      pagePath: page?.path || '/',
      pageTitle: page?.title || '',
      path: currentPath,
      type: node.type,
      name: node.name || '',
      text: node.props?.text || '',
      purpose: node.semantics?.purpose || '',
      role: node.semantics?.role || '',
      businessEntity: node.semantics?.businessEntity || '',
      repeat: node.repeat || null,
      depth,
    })
    ;(node.children || []).forEach((child) =>
      walk(child, node.id, currentPath, depth + 1, page),
    )
  }
  const pages = ensureVisualPages(document)
  pages.forEach((page) => walk(page.root, null, '', 1, page))

  const interactions = (document?.interactions || []).map((item) => ({
    ...item,
    resolved: item.status === 'CONFIRMED' || Boolean(item.bindingId),
  }))
  const bindings = (document?.bindings || []).map((item) => ({
    ...item,
    resolved: item.status === 'CONFIRMED',
  }))
  const unresolvedInteractions = interactions.filter((item) => !item.resolved)
  const unresolvedBindings = bindings.filter((item) => !item.resolved)
  const repeatedNodes = nodes.filter((item) => item.repeat)
  const visualDelta = baseline ? buildVisualDelta(baseline, document) : null

  return {
    schemaVersion: '1.0',
    packetType: 'SEMANTIC_REVIEW_PACKET',
    projectId: document?.projectId || '',
    documentId: document?.docId || '',
    documentRevision: document?.revision ?? 0,
    contentHash: document?.metadata?.contentHash || '',
    title: document?.title || '',
    generatedAt: new Date().toISOString(),
    nodeCount: nodes.length,
    nodes,
    interactions,
    bindings,
    unresolvedInteractions,
    unresolvedBindings,
    repeatedNodes,
    visualDelta,
    summary: [
      `虚拟界面「${document?.title || '未命名页面'}」已冻结`,
      `页面 ${pages.length} 个，节点 ${nodes.length} 个`,
      `交互 ${interactions.length} 个，未解决 ${unresolvedInteractions.length} 个`,
      `数据绑定 ${bindings.length} 个，未解决 ${unresolvedBindings.length} 个`,
      `列表/表格数据源 ${repeatedNodes.length} 个`,
      ...(visualDelta ? [`相对 v0：${visualDelta.summary}`] : []),
    ].join('；'),
  }
}

export function storeSemanticReviewPacket(packet) {
  if (!packet?.projectId) return false
  try {
    window.sessionStorage.setItem(
      `${VISUAL_REVIEW_STORAGE_PREFIX}${packet.projectId}`,
      JSON.stringify(packet),
    )
    return true
  } catch {
    return false
  }
}

export function peekSemanticReviewPacket(projectId) {
  if (!projectId) return null
  try {
    const key = `${VISUAL_REVIEW_STORAGE_PREFIX}${projectId}`
    const raw = window.sessionStorage.getItem(key)
    if (!raw) return null
    return JSON.parse(raw)
  } catch {
    return null
  }
}

export function takeSemanticReviewPacket(projectId) {
  const packet = peekSemanticReviewPacket(projectId)
  if (!packet) return null
  try {
    window.sessionStorage.removeItem(
      `${VISUAL_REVIEW_STORAGE_PREFIX}${projectId}`,
    )
  } catch {
    // 读取成功即可继续；删除失败最多导致下一次重复启动。
  }
  return packet
}

/** Architect 的 system 消息；限制体积，避免污染主对话上下文。 */
export function buildSemanticReviewContext(packet) {
  return [
    '【SEMANTIC_REVIEW_PACKET】',
    '这是用户在离线画布中确认并冻结的虚拟界面。',
    '请不要重新询问页面布局、视觉风格或要求上传源码。',
    '请只针对 interactions / bindings 中尚未解决、且直接影响后端接口、',
    '数据库、鉴权或业务动作的节点发起最多 2 个澄清问题。',
    '问题必须引用具体组件名/动作，并给出至少 A/B/C 选项。',
    '',
    JSON.stringify(packet).slice(0, 12000),
  ].join('\n')
}
