/**
 * 网页预览模拟层（只读，不写回 VisualDocument）。
 *
 * 当 V0 只给出稀疏 JSON 时，预览仍需要像真实网页：
 *   - 根据页面 path/title 选择模板；
 *   - 补充示例统计、表格、卡片、表单与页脚；
 *   - 仅用于预览，绝不修改文档或参与 CodeGen。
 */

const TABLE_ROWS = [
  ['陈思远', '管理员', '平台架构', '在线', '2 分钟前'],
  ['林悦', '运营', '增长运营', '在线', '18 分钟前'],
  ['周启明', '开发', '订单服务', '待激活', '尚未登录'],
  ['赵宁', '访客', '外部合作', '已停用', '3 天前'],
  ['顾言', '成员', '数据平台', '在线', '1 小时前'],
]

const TEMPLATE_KEYWORDS = [
  ['form', ['login', '登录', 'register', '注册', '表单', '新建', 'create']],
  [
    'table',
    ['user', '用户', 'order', '订单', 'list', '列表', 'manage', '管理', '成员'],
  ],
  ['dashboard', ['dashboard', '总览', '首页', 'home', '工作台', '看板']],
]

const MEANINGFUL_TYPES = new Set([
  'text',
  'button',
  'input',
  'form',
  'list',
  'table',
  'image',
])

function countMeaningful(node, count = { value: 0 }) {
  if (!node) return count
  if (MEANINGFUL_TYPES.has(node.type)) {
    const text = String(node.props?.text || '').trim()
    const placeholder = /占位|待补充|等待|未命名|在这里输入/.test(text)
    if (!placeholder && (node.type !== 'text' || text)) {
      count.value += 1
    }
  }
  ;(node.children || []).forEach((child) => countMeaningful(child, count))
  return count
}

export function shouldSimulatePage(page, document) {
  const meaningful = countMeaningful(page?.root).value
  // 页面已有足够业务内容时不再叠加模板，避免与真实 JSON 重复。
  return meaningful < 7
}

export function previewTemplateForPage(page) {
  const text = `${page?.path || ''} ${page?.title || ''}`.toLowerCase()
  for (const [template, keywords] of TEMPLATE_KEYWORDS) {
    if (keywords.some((keyword) => text.includes(keyword))) return template
  }
  return 'cards'
}

function pageTitle(page, document) {
  return page?.title || document?.title || '业务工作台'
}

export function buildPreviewSimulation(page, document) {
  const template = previewTemplateForPage(page)
  const title = pageTitle(page, document)
  const sharedFooter = {
    copyright: `© 2026 ${document?.title || 'Vibe Console'} · 模拟预览`,
    links: ['服务条款', '隐私政策', '联系支持'],
  }
  if (template === 'form') {
    return {
      template,
      title: title || '新建记录',
      description: '填写并提交业务信息，预览仅展示示例数据。',
      fields: [
        { label: '邮箱', placeholder: 'name@company.com', type: 'email' },
        { label: '角色', placeholder: '普通成员', type: 'select' },
        { label: '备注', placeholder: '可选：说明用途或负责范围', type: 'textarea' },
      ],
      actions: ['取消', '发送邀请'],
      footer: sharedFooter,
    }
  }
  if (template === 'table') {
    return {
      template,
      title: title || '用户管理',
      description: '管理团队成员、角色与状态。以下内容为预览模拟数据。',
      stats: [
        { label: '总用户数', value: '12,480', delta: '+12.4%' },
        { label: '本月新增', value: '1,206', delta: '+8.1%' },
        { label: '活跃用户', value: '8,932', delta: '+3.7%' },
        { label: '待处理', value: '37', delta: '-2.5%' },
      ],
      toolbar: {
        search: '搜索姓名、邮箱或部门…',
        filters: ['全部', '在线', '待激活', '已停用'],
        action: '＋ 邀请成员',
      },
      columns: ['成员', '角色', '部门', '状态', '最近活跃'],
      rows: TABLE_ROWS,
      footer: sharedFooter,
    }
  }
  if (template === 'dashboard') {
    return {
      template,
      title: title || '工作台总览',
      description: '关键指标、业务趋势与待办事项。以下内容为预览模拟数据。',
      stats: [
        { label: '今日访问', value: '8,432', delta: '+6.8%' },
        { label: '待办任务', value: '24', delta: '-3.1%' },
        { label: '接口成功率', value: '99.97%', delta: '+0.02%' },
        { label: '平均响应', value: '182ms', delta: '-14ms' },
      ],
      cards: [
        { title: '用户增长', description: '近 7 天新增用户与留存趋势。', action: '查看详情' },
        { title: '订单履约', description: '待发货、运输中与已完成订单。', action: '进入履约' },
        { title: '系统健康', description: '服务可用性、错误率与告警。', action: '查看监控' },
      ],
      footer: sharedFooter,
    }
  }
  return {
    template: 'cards',
    title: title || '业务页面',
    description: '按业务模块组织内容。以下内容为预览模拟数据。',
    cards: [
      { title: '核心业务', description: '集中处理主要业务对象与关键动作。', action: '进入模块' },
      { title: '数据概览', description: '查看关键指标与近期变化趋势。', action: '查看数据' },
      { title: '配置中心', description: '管理角色、权限与系统参数。', action: '打开配置' },
    ],
    footer: sharedFooter,
  }
}
