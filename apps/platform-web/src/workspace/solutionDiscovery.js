/**
 * 方案侦察的前端纯函数：把服务端选项转成 architect.start 的硬约束。
 *
 * 保持无副作用，便于 Vitest 直接覆盖；真正的检索与模型分析都在后端。
 */

export const SOLUTION_KIND_LABELS = {
  REUSE: '直接复用',
  ADAPT: '改造复用',
  BUILD: '自研',
}

export const INTEGRATION_COST_LABELS = {
  LOW: '低',
  MEDIUM: '中',
  HIGH: '高',
}

export const SOLUTION_SOURCE_LABELS = {
  github: 'GitHub 联网检索',
  local: '本地复用目录',
  build: '自研',
}

export function solutionSourceLabel(option) {
  return SOLUTION_SOURCE_LABELS[option?.source] ?? ''
}

function buildIntegrationPlan(option) {
  if (option.kind === 'BUILD') {
    return '按契约从零实现；复用成熟框架与基础库，不复制整站模板。'
  }
  const pros = (option.pros ?? []).slice(0, 2).join('；')
  const cons = (option.cons ?? []).slice(0, 2).join('；')
  const role = option.kind === 'REUSE' ? '依赖' : '改造基线'
  return `以 ${option.title} 为${role}；优势：${pros || '无'}；注意：${cons || '无'}。`
}

/**
 * 把服务端选项映射成 solutionChoice；只保留后端 schema 认识的字段。
 */
export function buildSolutionChoice(option) {
  if (!option || !option.optionId) return null
  return {
    optionId: option.optionId,
    kind: option.kind || 'BUILD',
    title: option.title || '自研方案',
    sourceUrl: option.sourceUrl || undefined,
    license: option.license || undefined,
    summary: option.summary || '',
    integrationPlan: option.integrationPlan || buildIntegrationPlan(option),
    notes: Array.isArray(option.notes) ? option.notes.slice(0, 10) : [],
  }
}

/** 用户选择“跳过侦察、直接自研”时的固定选项。 */
export function defaultSolutionChoice() {
  return buildSolutionChoice({
    optionId: 'opt_build',
    kind: 'BUILD',
    title: '自研方案',
    summary: '跳过方案检索，直接进入架构澄清。',
  })
}

/** 卡片上的一行元信息：许可证 / stars / 语言 / 集成成本。 */
export function solutionMeta(option) {
  if (!option) return ''
  const parts = []
  if (option.license) parts.push(option.license)
  if (Number.isFinite(option.stars)) parts.push(`${option.stars} stars`)
  if (option.language) parts.push(option.language)
  if (option.integrationCost) {
    parts.push(
      `集成成本：${
        INTEGRATION_COST_LABELS[option.integrationCost] ?? option.integrationCost
      }`,
    )
  }
  return parts.join(' · ')
}
