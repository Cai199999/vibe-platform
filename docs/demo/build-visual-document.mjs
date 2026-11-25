import fs from 'node:fs'
import {
  createVisualNode,
  createEmptyVisualDocument,
  validateVisualDocument,
} from '/home/admin1/Workspace/vibe-platform/apps/platform-web/src/visual/visualDocument.js'

const MAP = fs
  .readFileSync(
    '/home/admin1/Workspace/vibe-platform/docs/demo/assets/survey-map.data-uri.txt',
    'utf8',
  )
  .trim()

const n = (type, id, name, opts = {}) =>
  createVisualNode(type, { id, name, ...opts })

const text = (id, name, value, tw = []) =>
  n('text', id, name, { props: { text: value }, style: { tailwind: tw } })
const button = (id, name, value, tw = []) =>
  n('button', id, name, { props: { text: value }, style: { tailwind: tw } })
const input = (id, name, placeholder, tw = []) =>
  n('input', id, name, {
    props: { placeholder },
    style: { tailwind: tw },
  })
const section = (id, name, children, tw = []) =>
  n('section', id, name, { children, style: { tailwind: tw } })
const stack = (id, name, children, opts = {}) =>
  n('stack', id, name, {
    children,
    layout: {
      display: 'flex',
      direction: 'column',
      gap: opts.gap || '8px',
      align: 'stretch',
      justify: 'start',
    },
    style: { tailwind: opts.tw || [] },
  })
const row = (id, name, children, opts = {}) =>
  n('container', id, name, {
    children,
    layout: {
      display: 'flex',
      direction: 'row',
      gap: opts.gap || '12px',
      align: opts.align || 'center',
      justify: opts.justify || 'between',
      wrap: true,
    },
    style: { tailwind: opts.tw || [] },
  })
const grid = (id, name, children, columns = 4, gap = '16px') =>
  n('grid', id, name, {
    children,
    layout: {
      display: 'grid',
      direction: 'row',
      gap,
      columns,
      align: 'stretch',
      justify: 'start',
      wrap: true,
    },
  })
const card = (id, name, children, tw = []) =>
  section(id, name, children, [
    'rounded-xl',
    'border',
    'border-slate-200',
    'bg-white',
    'p-5',
    'shadow-sm',
    ...tw,
  ])
const list = (id, name, children, source, count, tw = []) =>
  n('list', id, name, {
    children,
    repeat: {
      source,
      itemAlias: 'item',
      keyField: 'id',
      sampleCount: count,
      emptyText: '暂无数据',
    },
    style: { tailwind: ['m-0', 'list-none', 'p-0', 'space-y-3', ...tw] },
  })
const image = (id, name, src, alt, tw = []) =>
  n('image', id, name, {
    props: { src, alt },
    a11y: { label: alt },
    style: { tailwind: ['w-full', 'rounded-lg', 'object-cover', ...tw] },
  })

const T = {
  page: ['min-h-screen', 'bg-slate-50'],
  title: ['text-2xl', 'font-semibold', 'text-slate-900'],
  h2: ['text-lg', 'font-semibold', 'text-slate-900'],
  body: ['text-sm', 'text-slate-600'],
  meta: ['text-xs', 'text-slate-500'],
  stat: ['text-3xl', 'font-semibold', 'text-slate-900'],
  primary: [
    'rounded-lg',
    'bg-blue-600',
    'px-4',
    'py-2',
    'text-sm',
    'font-medium',
    'text-white',
  ],
  secondary: [
    'rounded-lg',
    'border',
    'border-slate-300',
    'bg-white',
    'px-3',
    'py-2',
    'text-sm',
    'text-slate-700',
  ],
  input: [
    'w-full',
    'rounded-lg',
    'border',
    'border-slate-300',
    'bg-white',
    'px-3',
    'py-2',
    'text-sm',
  ],
  badgeAmber: [
    'inline-flex',
    'rounded-full',
    'bg-amber-50',
    'px-2.5',
    'py-1',
    'text-xs',
    'font-medium',
    'text-amber-700',
  ],
  badgeBlue: [
    'inline-flex',
    'rounded-full',
    'bg-blue-50',
    'px-2.5',
    'py-1',
    'text-xs',
    'font-medium',
    'text-blue-700',
  ],
  badgeViolet: [
    'inline-flex',
    'rounded-full',
    'bg-violet-50',
    'px-2.5',
    'py-1',
    'text-xs',
    'font-medium',
    'text-violet-700',
  ],
  badgeEmerald: [
    'inline-flex',
    'rounded-full',
    'bg-emerald-50',
    'px-2.5',
    'py-1',
    'text-xs',
    'font-medium',
    'text-emerald-700',
  ],
}

function statCard(id, label, value, delta, deltaTw = ['text-xs', 'text-emerald-600']) {
  return card(id, label, [
    text(`${id}_label`, '指标', label, T.meta),
    text(`${id}_value`, '数值', value, T.stat),
    text(`${id}_delta`, '变化', delta, deltaTw),
  ])
}

function taskCard(id, title, meta, badge, badgeTw, action) {
  return card(id, title, [
    text(`${id}_title`, '任务标题', title, ['text-sm', 'font-semibold', 'text-slate-900']),
    text(`${id}_meta`, '任务信息', meta, T.meta),
    row(`${id}_footer`, '任务操作', [
      text(`${id}_badge`, '状态', badge, badgeTw),
      button(`${id}_btn`, '操作', action, T.secondary),
    ]),
  ], ['p-4'])
}

// ---------------- 项目总览 ----------------
const overview = n('page', 'nd_ov_page', '项目总览', {
  layout: { display: 'block', padding: '24px', gap: '20px' },
  style: { tailwind: T.page },
  children: [
    row('nd_ov_header', '顶部栏', [
      stack('nd_ov_header_left', '标题区', [
        text('nd_ov_title', '平台标题', '测绘任务与成果管理平台', T.title),
        text('nd_ov_subtitle', '副标题', '华东测区 · 2026 年度 · 12 个项目', T.meta),
      ]),
      row('nd_ov_header_actions', '顶部操作', [
        button('nd_ov_export', '导出周报', '导出周报', T.secondary),
        button('nd_ov_new', '新建项目', '新建项目', T.primary),
      ], { justify: 'end', gap: '8px' }),
    ], { justify: 'between', align: 'center' }),
    grid('nd_ov_stats', '统计卡片', [
      statCard('nd_ov_stat1', '项目总数', '12', '+2 本月'),
      statCard('nd_ov_stat2', '进行中任务', '38', '6 个待派发'),
      statCard('nd_ov_stat3', '待质检成果', '9', '2 个返工', ['text-xs', 'text-rose-600']),
      statCard('nd_ov_stat4', '本月完成率', '86%', '+4.2%'),
    ], 4, '16px'),
    grid('nd_ov_main', '主要内容', [
      card('nd_ov_map_card', '任务点位', [
        row('nd_ov_map_head', '地图标题', [
          text('nd_ov_map_title', '地图标题', '任务点位', T.h2),
          text('nd_ov_map_region', '区域', '华东测区 · 1:2000', T.meta),
        ]),
        image('nd_ov_map', '任务点位底图', MAP, '测绘任务点位示意底图'),
        text(
          'nd_ov_map_legend',
          '图例说明',
          '待派发 6 · 进行中 12 · 待质检 5 · 已完成 21',
          T.meta,
        ),
      ], ['p-4']),
      card('nd_ov_activity_card', '近期动态', [
        text('nd_ov_activity_title', '动态标题', '近期动态', T.h2),
        stack('nd_ov_activity_list', '动态列表', [
          stack('nd_ov_activity_1', '动态项', [
            text('nd_ov_activity_text_1', '动态内容', '质检员 王工 通过了 T-07 的成果', T.body),
            text('nd_ov_activity_time_1', '动态时间', '10 分钟前', T.meta),
          ], { gap: '4px' }),
          stack('nd_ov_activity_2', '动态项', [
            text('nd_ov_activity_text_2', '动态内容', '作业员 张工 上传了 T-01 的 128 张像片', T.body),
            text('nd_ov_activity_time_2', '动态时间', '32 分钟前', T.meta),
          ], { gap: '4px' }),
          stack('nd_ov_activity_3', '动态项', [
            text('nd_ov_activity_text_3', '动态内容', '项目经理 李工 把 T-06 派发给了周工', T.body),
            text('nd_ov_activity_time_3', '动态时间', '1 小时前', T.meta),
          ], { gap: '4px' }),
          stack('nd_ov_activity_4', '动态项', [
            text('nd_ov_activity_text_4', '动态内容', '系统自动归档了 T-10 的成果包', T.body),
            text('nd_ov_activity_time_4', '动态时间', '2 小时前', T.meta),
          ], { gap: '4px' }),
        ], { gap: '14px' }),
      ], ['p-4']),
    ], 2, '20px'),
    card('nd_ov_projects_card', '重点项目', [
      text('nd_ov_projects_title', '项目标题', '重点项目', T.h2),
      stack('nd_ov_project_list', '项目列表', [
        row('nd_ov_project_row_1', '项目行', [
          stack('nd_ov_project_info_1', '项目信息', [
            text('nd_ov_project_name_1', '项目名称', '华东测区 1:2000 影像更新', T.body),
            text('nd_ov_project_meta_1', '项目信息', '负责人：李工 · 截止 11-30', T.meta),
          ], { gap: '2px' }),
          text('nd_ov_project_status_1', '项目状态', '进行中', T.badgeBlue),
        ], { tw: ['rounded-lg', 'border', 'border-slate-200', 'bg-white', 'px-4', 'py-3'] }),
        row('nd_ov_project_row_2', '项目行', [
          stack('nd_ov_project_info_2', '项目信息', [
            text('nd_ov_project_name_2', '项目名称', '华南测区 DOM 生产', T.body),
            text('nd_ov_project_meta_2', '项目信息', '负责人：陈工 · 截止 12-05', T.meta),
          ], { gap: '2px' }),
          text('nd_ov_project_status_2', '项目状态', '进行中', T.badgeBlue),
        ], { tw: ['rounded-lg', 'border', 'border-slate-200', 'bg-white', 'px-4', 'py-3'] }),
        row('nd_ov_project_row_3', '项目行', [
          stack('nd_ov_project_info_3', '项目信息', [
            text('nd_ov_project_name_3', '项目名称', '西北测区 DLG 修测', T.body),
            text('nd_ov_project_meta_3', '项目信息', '负责人：赵工 · 截止 12-12', T.meta),
          ], { gap: '2px' }),
          text('nd_ov_project_status_3', '项目状态', '待派发', T.badgeAmber),
        ], { tw: ['rounded-lg', 'border', 'border-slate-200', 'bg-white', 'px-4', 'py-3'] }),
        row('nd_ov_project_row_4', '项目行', [
          stack('nd_ov_project_info_4', '项目信息', [
            text('nd_ov_project_name_4', '项目名称', '华东测区 2025 年度成果入库', T.body),
            text('nd_ov_project_meta_4', '项目信息', '负责人：王工 · 已完成', T.meta),
          ], { gap: '2px' }),
          text('nd_ov_project_status_4', '项目状态', '已完成', T.badgeEmerald),
        ], { tw: ['rounded-lg', 'border', 'border-slate-200', 'bg-white', 'px-4', 'py-3'] }),
      ], { gap: '12px' }),
    ], ['p-4']),
  ],
})

// ---------------- 任务看板 ----------------
const boardColumn = (id, title, count, cards, badgeTw) =>
  card(id, title, [
    row(`${id}_head`, '列标题', [
      text(`${id}_title`, '列名', title, T.h2),
      text(`${id}_count`, '数量', String(count), T.meta),
    ]),
    ...cards,
  ], ['p-4', 'bg-slate-50'])

const tasksPage = n('page', 'nd_tk_page', '任务看板', {
  layout: { display: 'block', padding: '24px', gap: '20px' },
  style: { tailwind: T.page },
  children: [
    row('nd_tk_header', '看板顶部', [
      stack('nd_tk_header_left', '看板标题区', [
        text('nd_tk_title', '看板标题', '测绘任务看板', T.title),
        text('nd_tk_subtitle', '看板副标题', '按状态跟踪任务，支持派发与质检流转', T.meta),
      ]),
      button('nd_tk_dispatch', '派发任务', '派发任务', T.primary),
    ]),
    grid('nd_tk_board', '四列看板', [
      boardColumn('nd_tk_col1', '待派发', 6, [
        taskCard('nd_tk_t1', 'T-01 像控点采集', '作业员：张工 · 截止 11-25', '待派发', T.badgeAmber, '派发'),
        taskCard('nd_tk_t2', 'T-02 无人机航飞', '作业员：王工 · 截止 11-26', '待派发', T.badgeAmber, '派发'),
        taskCard('nd_tk_t3', 'T-03 外业调绘', '作业员：赵工 · 截止 11-27', '待派发', T.badgeAmber, '派发'),
      ]),
      boardColumn('nd_tk_col2', '进行中', 12, [
        taskCard('nd_tk_t4', 'T-04 空三加密', '作业员：李工 · 进度 60%', '进行中', T.badgeBlue, '查看'),
        taskCard('nd_tk_t5', 'T-05 DOM 生产', '作业员：陈工 · 进度 45%', '进行中', T.badgeBlue, '查看'),
        taskCard('nd_tk_t6', 'T-06 DLG 编辑', '作业员：周工 · 进度 80%', '进行中', T.badgeBlue, '查看'),
      ]),
      boardColumn('nd_tk_col3', '待质检', 5, [
        taskCard('nd_tk_t7', 'T-07 正射影像质检', '提交人：王工 · 11-20', '待质检', T.badgeViolet, '质检'),
        taskCard('nd_tk_t8', 'T-08 地形图质检', '提交人：赵工 · 11-20', '待质检', T.badgeViolet, '质检'),
        taskCard('nd_tk_t9', 'T-09 成果包检查', '提交人：李工 · 11-19', '待质检', T.badgeViolet, '质检'),
      ]),
      boardColumn('nd_tk_col4', '已完成', 21, [
        taskCard('nd_tk_t10', 'T-10 控制测量', '完成于 11-18 · 质检合格', '已完成', T.badgeEmerald, '归档'),
        taskCard('nd_tk_t11', 'T-11 像片整理', '完成于 11-17 · 质检合格', '已完成', T.badgeEmerald, '归档'),
        taskCard('nd_tk_t12', 'T-12 成果入库', '完成于 11-16 · 质检合格', '已完成', T.badgeEmerald, '归档'),
      ]),
    ], 4, '16px'),
    card('nd_tk_dispatch_card', '快速派发', [
      text('nd_tk_dispatch_title', '派发标题', '快速派发', T.h2),
      row('nd_tk_dispatch_form', '派发表单', [
        input('nd_tk_assignee', '作业员', '选择作业员'),
        input('nd_tk_deadline', '截止日期', '选择截止日期'),
        button('nd_tk_dispatch_submit', '确认派发', '确认派发', T.primary),
      ], { gap: '12px', justify: 'start' }),
    ], ['p-4']),
  ],
})

// ---------------- 成果质检 ----------------
function qcRow(id, title, meta, status, statusTw) {
  return row(id, title, [
    stack(`${id}_info`, '成果信息', [
      text(`${id}_name`, '成果名称', title, ['text-sm', 'font-semibold', 'text-slate-900']),
      text(`${id}_meta`, '成果信息', meta, T.meta),
    ], { gap: '2px' }),
    row(`${id}_actions`, '质检操作', [
      text(`${id}_status`, '质检状态', status, statusTw),
      button(`${id}_pass`, '合格', '合格', T.secondary),
      button(`${id}_rework`, '返工', '返工', T.secondary),
    ], { justify: 'end', gap: '8px' }),
  ], {
    tw: [
      'rounded-lg',
      'border',
      'border-slate-200',
      'bg-white',
      'px-4',
      'py-3',
    ],
  })
}

const qcPage = n('page', 'nd_qc_page', '成果质检', {
  layout: { display: 'block', padding: '24px', gap: '20px' },
  style: { tailwind: T.page },
  children: [
    row('nd_qc_header', '质检顶部', [
      stack('nd_qc_header_left', '质检标题区', [
        text('nd_qc_title', '质检标题', '成果质检', T.title),
        text('nd_qc_subtitle', '质检副标题', '影像成果、地形图与成果包的质检流转', T.meta),
      ]),
      row('nd_qc_header_actions', '质检操作', [
        input('nd_qc_search', '搜索成果', '搜索成果名称 / 任务编号'),
        button('nd_qc_batch', '批量通过', '批量通过', T.primary),
      ], { justify: 'end', gap: '8px' }),
    ]),
    grid('nd_qc_stats', '质检统计', [
      statCard('nd_qc_stat1', '待质检', '9', '其中 2 个返工', ['text-xs', 'text-amber-600']),
      statCard('nd_qc_stat2', '本月合格', '128', '+18 本月'),
      statCard('nd_qc_stat3', '返工率', '3.1%', '-0.8%'),
    ], 3, '16px'),
    card('nd_qc_list_card', '质检列表', [
      text('nd_qc_list_title', '列表标题', '待质检成果', T.h2),
      qcRow('nd_qc_r1', 'T-03 正射影像 DOM', '上传人：赵工 · 11-20 14:32', '待质检', T.badgeViolet),
      qcRow('nd_qc_r2', 'T-05 数字高程模型 DEM', '上传人：陈工 · 11-20 11:08', '待质检', T.badgeViolet),
      qcRow('nd_qc_r3', 'T-07 地形图 DLG', '上传人：王工 · 11-19 17:45', '返工', T.badgeAmber),
      qcRow('nd_qc_r4', 'T-08 成果包 ZIP', '上传人：李工 · 11-19 09:20', '待质检', T.badgeViolet),
    ], ['p-4']),
  ],
})

// ---------------- 影像上传 ----------------
function uploadRow(id, title, meta, status, statusTw) {
  return row(id, title, [
    stack(`${id}_info`, '上传信息', [
      text(`${id}_name`, '文件名称', title, ['text-sm', 'font-semibold', 'text-slate-900']),
      text(`${id}_meta`, '上传信息', meta, T.meta),
    ], { gap: '2px' }),
    text(`${id}_status`, '上传状态', status, statusTw),
  ], {
    tw: [
      'rounded-lg',
      'border',
      'border-slate-200',
      'bg-white',
      'px-4',
      'py-3',
    ],
  })
}

const uploadPage = n('page', 'nd_up_page', '影像上传', {
  layout: { display: 'block', padding: '24px', gap: '20px' },
  style: { tailwind: T.page },
  children: [
    row('nd_up_header', '上传顶部', [
      stack('nd_up_header_left', '上传标题区', [
        text('nd_up_title', '上传标题', '影像上传', T.title),
        text('nd_up_subtitle', '上传副标题', '支持拖拽上传 · 单文件 ≤ 2GB · 自动校验坐标系', T.meta),
      ]),
      button('nd_up_task', '选择任务', '选择任务', T.secondary),
    ]),
    card('nd_up_dropzone_card', '上传区', [
      stack('nd_up_dropzone', '拖拽区', [
        text('nd_up_drop_title', '拖拽标题', '把影像文件拖到这里', ['text-lg', 'font-semibold', 'text-slate-900']),
        text('nd_up_drop_hint', '拖拽提示', '支持 TIFF / JPEG / ZIP，上传后自动关联任务', T.meta),
        button('nd_up_pick', '选择文件', '选择文件', T.primary),
      ], {
        gap: '10px',
        tw: [
          'items-center',
          'rounded-xl',
          'border',
          'border-dashed',
          'border-slate-300',
          'bg-slate-50',
          'px-6',
          'py-10',
        ],
      }),
    ], ['p-4']),
    card('nd_up_form_card', '上传表单', [
      text('nd_up_form_title', '表单标题', '上传信息', T.h2),
      row('nd_up_form', '上传表单', [
        input('nd_up_task_no', '任务编号', '例如 T-03'),
        input('nd_up_file', '文件', '选择影像文件'),
        button('nd_up_submit', '开始上传', '开始上传', T.primary),
      ], { gap: '12px', justify: 'start' }),
    ], ['p-4']),
    card('nd_up_recent_card', '最近上传', [
      text('nd_up_recent_title', '最近上传标题', '最近上传', T.h2),
      uploadRow('nd_up_r1', 'T-03_DOM_20261120.tif', '赵工 · 11-20 14:32 · 1.8 GB', '已完成', T.badgeEmerald),
      uploadRow('nd_up_r2', 'T-05_DEM_20261120.tif', '陈工 · 11-20 11:08 · 2.4 GB', '校验中', T.badgeBlue),
      uploadRow('nd_up_r3', 'T-07_DLG_20261119.zip', '王工 · 11-19 17:45 · 860 MB', '已完成', T.badgeEmerald),
    ], ['p-4']),
  ],
})

const pages = [
  { pageId: 'pg_overview', path: '/', title: '项目总览', root: overview },
  { pageId: 'pg_tasks', path: '/tasks', title: '任务看板', root: tasksPage },
  { pageId: 'pg_qc01', path: '/qc', title: '成果质检', root: qcPage },
  { pageId: 'pg_upload', path: '/upload', title: '影像上传', root: uploadPage },
]

const doc = createEmptyVisualDocument({
  projectId: 'proj_gis_demo',
  title: '测绘任务与成果管理平台',
})
doc.docId = 'vdoc_gis_demo'
doc.revision = 1
doc.pages = pages
doc.activePageId = pages[0].pageId
doc.root = pages[0].root
doc.bindings = [
  { id: 'activities', nodeId: 'nd_ov_activity_list', entity: 'activities', status: 'CONFIRMED' },
  { id: 'projects', nodeId: 'nd_ov_project_list', entity: 'projects', status: 'CONFIRMED' },
]
doc.interactions = [
  {
    id: 'int_new_project',
    nodeId: 'nd_ov_new',
    action: 'open_modal',
    bindingId: 'projects',
    status: 'CONFIRMED',
  },
  {
    id: 'int_dispatch',
    nodeId: 'nd_tk_dispatch_submit',
    action: 'call_api',
    bindingId: 'projects',
    status: 'CONFIRMED',
  },
]
doc.metadata = {
  createdBy: 'user',
  createdAt: new Date().toISOString(),
  lastModifiedBy: 'user',
  contentHash: '',
}

const validation = validateVisualDocument(doc)
console.log('nodeCount', validation.nodeCount, 'errors', validation.errors.length, 'warnings', validation.warnings.length)
if (validation.errors.length) {
  console.log('ERRORS', validation.errors.slice(0, 20))
}
if (validation.warnings.length) {
  console.log('WARNINGS', validation.warnings.slice(0, 10))
}
fs.writeFileSync(
  '/home/admin1/Workspace/vibe-platform/docs/demo/gis-visual-document.json',
  JSON.stringify(doc, null, 2),
)
console.log('written docs/demo/gis-visual-document.json')
