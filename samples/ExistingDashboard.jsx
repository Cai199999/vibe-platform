import { useMemo, useState } from 'react'

/**
 * 存量项目示例：SaaS 用户管理后台。
 *
 * 这份文件刻意覆盖真实老项目里最常见的 JSX 结构：
 *  - 页面壳 / 侧边栏 / 顶栏 / 主内容区 / 页脚
 *  - 统计卡片、搜索筛选、表格、空状态、弹窗、表单
 *  - 同文件组件拆分、props、map 列表、条件渲染、Tailwind class
 *
 * 它只用于“只读源码投影”演示：不会写回源码，也不会生成虚拟树。
 */

const NAV_ITEMS = [
  { id: 'overview', label: '总览', icon: '▦' },
  { id: 'users', label: '用户管理', icon: '◉', active: true },
  { id: 'roles', label: '角色权限', icon: '◇' },
  { id: 'billing', label: '账单与订阅', icon: '▤' },
  { id: 'settings', label: '系统设置', icon: '⚙' },
]

const STATS = [
  { label: '总用户数', value: '12,480', delta: '+12.4%', tone: 'blue' },
  { label: '本月新增', value: '1,206', delta: '+8.1%', tone: 'emerald' },
  { label: '活跃用户', value: '8,932', delta: '+3.7%', tone: 'violet' },
  { label: '待处理工单', value: '37', delta: '-2.5%', tone: 'amber' },
]

const USERS = [
  {
    id: 'u_001',
    name: '陈思远',
    email: 'siyuan.chen@example.com',
    role: '管理员',
    department: '平台架构',
    status: 'active',
    lastSeen: '2 分钟前',
  },
  {
    id: 'u_002',
    name: '林悦',
    email: 'yue.lin@example.com',
    role: '运营',
    department: '增长运营',
    status: 'active',
    lastSeen: '18 分钟前',
  },
  {
    id: 'u_003',
    name: '周启明',
    email: 'qiming.zhou@example.com',
    role: '开发',
    department: '订单服务',
    status: 'invited',
    lastSeen: '尚未登录',
  },
  {
    id: 'u_004',
    name: '赵宁',
    email: 'ning.zhao@example.com',
    role: '访客',
    department: '外部合作',
    status: 'disabled',
    lastSeen: '3 天前',
  },
]

const FILTERS = [
  { id: 'active', label: '在线用户' },
  { id: 'invited', label: '待激活' },
  { id: 'disabled', label: '已停用' },
]

const STATUS_STYLES = {
  active: 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200',
  invited: 'bg-amber-50 text-amber-700 ring-1 ring-amber-200',
  disabled: 'bg-slate-100 text-slate-500 ring-1 ring-slate-200',
}

const STATUS_LABELS = {
  active: '在线',
  invited: '待激活',
  disabled: '已停用',
}

function Avatar({ name, tone = 'blue' }) {
  const tones = {
    blue: 'bg-blue-100 text-blue-700',
    emerald: 'bg-emerald-100 text-emerald-700',
    violet: 'bg-violet-100 text-violet-700',
    amber: 'bg-amber-100 text-amber-700',
  }
  return (
    <span
      className={`inline-flex h-9 w-9 items-center justify-center rounded-full text-sm font-semibold ${tones[tone]}`}
    >
      {name.slice(0, 1)}
    </span>
  )
}

function StatusBadge({ status }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${STATUS_STYLES[status]}`}
    >
      {STATUS_LABELS[status] || '未知'}
    </span>
  )
}

function SidebarNav() {
  return (
    <aside className="hidden w-64 shrink-0 flex-col border-r border-slate-200 bg-white px-4 py-6 lg:flex">
      <div className="flex items-center gap-3 px-2">
        <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-blue-600 text-lg font-bold text-white">
          V
        </span>
        <div>
          <p className="text-sm font-semibold text-slate-900">Vibe Console</p>
          <p className="text-xs text-slate-500">企业工作台</p>
        </div>
      </div>

      <nav className="mt-8 space-y-1" aria-label="主导航">
        {NAV_ITEMS.map((item) => (
          <a
            key={item.id}
            href={`#${item.id}`}
            className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition ${
              item.active
                ? 'bg-blue-50 font-medium text-blue-700'
                : 'text-slate-600 hover:bg-slate-50'
            }`}
          >
            <span className="w-4 text-center text-xs">{item.icon}</span>
            <span>{item.label}</span>
          </a>
        ))}
      </nav>

      <div className="mt-auto rounded-xl border border-slate-200 bg-slate-50 p-4">
        <p className="text-sm font-semibold text-slate-800">升级到企业版</p>
        <p className="mt-1 text-xs leading-5 text-slate-500">
          解锁审计日志、单点登录与专属客户成功经理。
        </p>
        <button className="mt-3 w-full rounded-lg bg-slate-900 px-3 py-2 text-xs font-medium text-white">
          查看方案
        </button>
      </div>
    </aside>
  )
}

function TopBar() {
  const [query, setQuery] = useState('')

  return (
    <header className="flex items-center justify-between border-b border-slate-200 bg-white px-6 py-3">
      <div className="flex items-center gap-3">
        <span className="text-sm text-slate-500">工作台</span>
        <span className="text-slate-300">/</span>
        <span className="text-sm font-medium text-slate-900">用户管理</span>
      </div>

      <div className="flex items-center gap-4">
        <label className="relative hidden md:block">
          <span className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-slate-400">
            ⌕
          </span>
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索用户、邮箱或部门…"
            className="w-64 rounded-lg border border-slate-200 bg-slate-50 py-2 pl-9 pr-3 text-sm outline-none focus:border-blue-400 focus:bg-white"
          />
        </label>
        <button className="relative rounded-lg border border-slate-200 p-2 text-slate-500 hover:bg-slate-50">
          <span aria-hidden="true">🔔</span>
          <span className="absolute -right-1 -top-1 h-2.5 w-2.5 rounded-full bg-rose-500 ring-2 ring-white" />
        </button>
        <div className="flex items-center gap-2">
          <Avatar name="陈思远" tone="emerald" />
          <div className="hidden text-left sm:block">
            <p className="text-sm font-medium text-slate-900">陈思远</p>
            <p className="text-xs text-slate-500">平台管理员</p>
          </div>
        </div>
      </div>
    </header>
  )
}

function PageHeader({ onCreate }) {
  return (
    <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <p className="text-xs font-semibold uppercase tracking-widest text-blue-600">
          Team Management
        </p>
        <h1 className="mt-2 text-2xl font-semibold text-slate-900">用户与权限</h1>
        <p className="mt-1 text-sm text-slate-500">
          管理团队成员、角色权限与账号状态，变更会立即生效。
        </p>
      </div>
      <div className="flex items-center gap-2">
        <button className="rounded-lg border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50">
          导出 CSV
        </button>
        <button
          onClick={onCreate}
          className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-blue-700"
        >
          ＋ 邀请成员
        </button>
      </div>
    </div>
  )
}

function StatCard({ label, value, delta, tone }) {
  const toneStyles = {
    blue: 'from-blue-50 to-white text-blue-700',
    emerald: 'from-emerald-50 to-white text-emerald-700',
    violet: 'from-violet-50 to-white text-violet-700',
    amber: 'from-amber-50 to-white text-amber-700',
  }
  return (
    <article
      className={`rounded-2xl border border-slate-200 bg-gradient-to-br p-5 shadow-sm ${toneStyles[tone]}`}
    >
      <p className="text-xs font-medium uppercase tracking-wide opacity-80">{label}</p>
      <p className="mt-2 text-3xl font-semibold text-slate-900">{value}</p>
      <p className="mt-1 text-xs text-slate-500">
        <span className="font-medium text-emerald-600">{delta}</span> 较上月
      </p>
    </article>
  )
}

function SearchBar({ query, onQueryChange, onCreate }) {
  return (
    <div className="flex flex-col gap-3 border-b border-slate-200 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex flex-1 items-center gap-2">
        <div className="relative flex-1">
          <input
            type="search"
            value={query}
            onChange={(event) => onQueryChange(event.target.value)}
            placeholder="按姓名或邮箱筛选…"
            className="w-full rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-sm outline-none focus:border-blue-400 focus:bg-white"
          />
        </div>
        <select className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-600">
          <option value="all">全部角色</option>
          <option value="admin">管理员</option>
          <option value="member">普通成员</option>
          <option value="guest">访客</option>
        </select>
      </div>
      <button
        onClick={onCreate}
        className="rounded-lg border border-blue-200 bg-blue-50 px-3 py-2 text-sm font-medium text-blue-700 hover:bg-blue-100"
      >
        批量邀请
      </button>
    </div>
  )
}

function FilterPanel() {
  return (
    <aside className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <h2 className="text-sm font-semibold text-slate-900">筛选</h2>
      <div className="mt-4 space-y-3">
        {FILTERS.map((filter) => (
          <label key={filter.id} className="flex items-center gap-2 text-sm text-slate-600">
            <input type="checkbox" className="h-4 w-4 rounded border-slate-300" />
            <span>{filter.label}</span>
          </label>
        ))}
      </div>

      <hr className="my-5 border-slate-200" />

      <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
        部门
      </h3>
      <div className="mt-3 space-y-2 text-sm text-slate-600">
        <p>平台架构 · 24</p>
        <p>增长运营 · 18</p>
        <p>订单服务 · 31</p>
        <p>外部合作 · 7</p>
      </div>

      <button className="mt-5 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-600 hover:bg-slate-50">
        重置筛选
      </button>
    </aside>
  )
}

function UserTable({ users, onEdit }) {
  return (
    <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
      <SearchBar
        query=""
        onQueryChange={() => {}}
        onCreate={() => {}}
      />
      <div className="overflow-x-auto">
        <table className="min-w-full divide-y divide-slate-200 text-left text-sm">
          <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-5 py-3 font-medium">成员</th>
              <th className="px-5 py-3 font-medium">角色</th>
              <th className="px-5 py-3 font-medium">部门</th>
              <th className="px-5 py-3 font-medium">状态</th>
              <th className="px-5 py-3 font-medium">最近活跃</th>
              <th className="px-5 py-3 text-right font-medium">操作</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {users.map((user, index) => (
              <tr key={user.id} className="hover:bg-slate-50">
                <td className="px-5 py-4">
                  <div className="flex items-center gap-3">
                    <Avatar name={user.name} tone={index % 2 === 0 ? 'blue' : 'violet'} />
                    <div>
                      <p className="font-medium text-slate-900">{user.name}</p>
                      <p className="text-xs text-slate-500">{user.email}</p>
                    </div>
                  </div>
                </td>
                <td className="px-5 py-4 text-slate-600">{user.role}</td>
                <td className="px-5 py-4 text-slate-600">{user.department}</td>
                <td className="px-5 py-4">
                  <StatusBadge status={user.status} />
                </td>
                <td className="px-5 py-4 text-slate-500">{user.lastSeen}</td>
                <td className="px-5 py-4 text-right">
                  <button
                    onClick={() => onEdit(user)}
                    className="rounded-md px-2 py-1 text-xs font-medium text-blue-600 hover:bg-blue-50"
                  >
                    编辑
                  </button>
                  <button className="ml-1 rounded-md px-2 py-1 text-xs font-medium text-slate-500 hover:bg-slate-100">
                    更多
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="flex items-center justify-between border-t border-slate-200 px-5 py-3 text-xs text-slate-500">
        <span>共 {users.length} 位成员</span>
        <div className="flex items-center gap-1">
          <button className="rounded-md border border-slate-200 px-2 py-1">上一页</button>
          <button className="rounded-md border border-slate-200 bg-slate-50 px-2 py-1">1</button>
          <button className="rounded-md border border-slate-200 px-2 py-1">下一页</button>
        </div>
      </div>
    </div>
  )
}

function EmptyState({ onCreate }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-slate-300 bg-white px-6 py-16 text-center">
      <span className="flex h-14 w-14 items-center justify-center rounded-full bg-slate-100 text-2xl">
        ◌
      </span>
      <h2 className="mt-4 text-base font-semibold text-slate-900">还没有成员</h2>
      <p className="mt-1 max-w-sm text-sm text-slate-500">
        邀请第一位成员加入工作台，开始协作管理用户与权限。
      </p>
      <button
        onClick={onCreate}
        className="mt-5 rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
      >
        邀请成员
      </button>
    </div>
  )
}

function CreateUserDialog({ open, onClose }) {
  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
      <div className="w-full max-w-md rounded-2xl bg-white p-6 shadow-2xl">
        <div className="flex items-start justify-between">
          <div>
            <h2 className="text-lg font-semibold text-slate-900">邀请新成员</h2>
            <p className="mt-1 text-sm text-slate-500">对方会收到一封加入工作台的邮件。</p>
          </div>
          <button
            onClick={onClose}
            className="rounded-md p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
          >
            ×
          </button>
        </div>

        <form className="mt-5 space-y-4">
          <label className="block text-sm font-medium text-slate-700">
            邮箱
            <input
              type="email"
              placeholder="name@company.com"
              className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm outline-none focus:border-blue-400"
            />
          </label>
          <label className="block text-sm font-medium text-slate-700">
            角色
            <select className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-600">
              <option value="member">普通成员</option>
              <option value="admin">管理员</option>
              <option value="guest">访客</option>
            </select>
          </label>
          <label className="block text-sm font-medium text-slate-700">
            备注
            <textarea
              rows={3}
              placeholder="可选：说明加入原因或负责范围"
              className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm outline-none focus:border-blue-400"
            />
          </label>
          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg border border-slate-200 px-4 py-2 text-sm text-slate-600 hover:bg-slate-50"
            >
              取消
            </button>
            <button
              type="submit"
              className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
            >
              发送邀请
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

function Footer() {
  return (
    <footer className="flex flex-col gap-2 border-t border-slate-200 pt-6 text-xs text-slate-500 sm:flex-row sm:items-center sm:justify-between">
      <span>© 2026 Vibe Console · 企业版</span>
      <div className="flex items-center gap-4">
        <a href="#terms" className="hover:text-slate-700">
          服务条款
        </a>
        <a href="#privacy" className="hover:text-slate-700">
          隐私政策
        </a>
        <a href="#support" className="hover:text-slate-700">
          联系支持
        </a>
      </div>
    </footer>
  )
}

export default function DashboardPage() {
  const [query, setQuery] = useState('')
  const [dialogOpen, setDialogOpen] = useState(false)
  const [selectedUser, setSelectedUser] = useState(null)

  const filteredUsers = useMemo(() => {
    const keyword = query.trim().toLowerCase()
    if (!keyword) return USERS
    return USERS.filter((user) =>
      `${user.name} ${user.email} ${user.department}`.toLowerCase().includes(keyword),
    )
  }, [query])

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <div className="mx-auto flex min-h-screen max-w-[1440px]">
        <SidebarNav />

        <div className="flex min-w-0 flex-1 flex-col">
          <TopBar />

          <main className="flex-1 space-y-6 px-4 py-6 sm:px-6 lg:px-8">
            <PageHeader onCreate={() => setDialogOpen(true)} />

            <section className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
              {STATS.map((stat) => (
                <StatCard
                  key={stat.label}
                  label={stat.label}
                  value={stat.value}
                  delta={stat.delta}
                  tone={stat.tone}
                />
              ))}
            </section>

            <section className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1fr)_280px]">
              <div className="min-w-0 space-y-4">
                <SearchBar
                  query={query}
                  onQueryChange={setQuery}
                  onCreate={() => setDialogOpen(true)}
                />
                {filteredUsers.length === 0 ? (
                  <EmptyState onCreate={() => setDialogOpen(true)} />
                ) : (
                  <UserTable users={filteredUsers} onEdit={setSelectedUser} />
                )}
              </div>
              <FilterPanel />
            </section>

            {selectedUser && (
              <div className="rounded-xl border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-800">
                已选择 <strong>{selectedUser.name}</strong>（{selectedUser.email}）
                <button
                  onClick={() => setSelectedUser(null)}
                  className="ml-3 text-xs font-medium underline"
                >
                  取消选择
                </button>
              </div>
            )}

            <Footer />
          </main>
        </div>
      </div>

      <CreateUserDialog open={dialogOpen} onClose={() => setDialogOpen(false)} />
    </div>
  )
}
