import { useCallback, useEffect, useRef, useState } from 'react'
import AdminHeader from '../components/AdminHeader.jsx'
import { useAuth } from '../auth/AuthContext.jsx'
import { fetchAdminMetrics } from '../admin/adminClient.js'

const REFRESH_MS = 30_000

const NUMBER = new Intl.NumberFormat('zh-CN')
const TIME = new Intl.DateTimeFormat('zh-CN', {
  hour12: false,
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
})

/**
 * Agent 颜色按“实体”固定分配，不随排名变化。
 * 取值来自 dataviz 参考色板中针对深色背景校验通过的前四个分类槽位。
 */
const AGENT_COLORS = {
  ui_graph: '#3987e5',
  architect_graph: '#d95926',
  backend_coder: '#199e70',
  script_agent: '#c98500',
}
const FALLBACK_COLORS = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#9085e9']

const STATUS_META = {
  SUCCEEDED: { label: '成功', tone: 'success' },
  FAILED: { label: '失败', tone: 'failed' },
  RUNNING: { label: '运行中', tone: 'running' },
  AWAITING_APPROVAL: { label: '待审批', tone: 'warn' },
  AWAITING_CLARIFICATION: { label: '待澄清', tone: 'warn' },
  AWAITING_CONTRACT: { label: '待确认', tone: 'warn' },
  CANCELLED: { label: '已取消', tone: 'muted' },
  REJECTED: { label: '已拒绝', tone: 'muted' },
}

function colorForAgent(agentId = '') {
  if (AGENT_COLORS[agentId]) return AGENT_COLORS[agentId]
  let hash = 0
  for (let index = 0; index < agentId.length; index += 1) {
    hash = (hash * 31 + agentId.charCodeAt(index)) >>> 0
  }
  return FALLBACK_COLORS[hash % FALLBACK_COLORS.length]
}

function formatPercent(rate, digits = 1) {
  const value = Number(rate)
  if (!Number.isFinite(value)) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

function formatDuration(milliseconds) {
  const value = Math.max(0, Number(milliseconds) || 0)
  if (value < 1000) return `${Math.round(value)} ms`
  const seconds = value / 1000
  if (seconds < 60) return `${seconds.toFixed(1)} s`
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds % 60)
  return `${minutes}m ${String(rest).padStart(2, '0')}s`
}

function formatTime(iso) {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return TIME.format(date)
}

function freshnessLabel(seconds) {
  if (seconds == null) return '等待数据'
  if (seconds < 5) return '刚刚更新'
  if (seconds < 60) return `${seconds} 秒前更新`
  return `${Math.floor(seconds / 60)} 分钟前更新`
}

function KpiIcon({ name }) {
  const paths = {
    sandbox: (
      <>
        <path d="M12 3 4 7.2v9.6L12 21l8-4.2V7.2L12 3Z" />
        <path d="m4 7.2 8 4.2 8-4.2M12 11.4V21" />
      </>
    ),
    runs: (
      <>
        <circle cx="12" cy="12" r="8.5" />
        <path d="m10 8.5 5 3.5-5 3.5v-7Z" />
      </>
    ),
    success: (
      <>
        <circle cx="12" cy="12" r="8.5" />
        <path d="m8 12.2 2.6 2.6L16 9.6" />
      </>
    ),
    duration: (
      <>
        <circle cx="12" cy="12" r="8.5" />
        <path d="M12 7.5V12l3 1.8" />
      </>
    ),
  }
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      {paths[name]}
    </svg>
  )
}

function KpiCard({ icon, label, value, hint }) {
  return (
    <article className="admin-kpi">
      <div className="admin-kpi-top">
        <span className="admin-kpi-label">{label}</span>
        <span className="admin-kpi-icon">
          <KpiIcon name={icon} />
        </span>
      </div>
      <strong className="admin-kpi-value">{value}</strong>
      <span className="admin-kpi-hint">{hint}</span>
    </article>
  )
}

function AgentShareChart({ items }) {
  if (!items?.length) {
    return (
      <div className="admin-empty">
        <strong>暂无 Agent 调用数据</strong>
        <span>执行一次画布修改或全栈设计后，这里会显示调用占比。</span>
      </div>
    )
  }

  return (
    <div className="admin-agent-chart" role="list" aria-label="各 Agent 调用次数占比">
      {items.map((item) => {
        const color = colorForAgent(item.agentId)
        const percent = Math.max(0, Math.min(100, Number(item.percentage) * 100))
        return (
          <div className="admin-agent-row" role="listitem" key={item.agentId}>
            <div className="admin-agent-legend">
              <span className="admin-agent-swatch" style={{ background: color }} aria-hidden="true" />
              <span className="admin-agent-names">
                <strong>{item.displayName}</strong>
                <code>{item.agentId}</code>
              </span>
            </div>
            <div
              className="admin-agent-track"
              tabIndex={0}
              aria-label={`${item.displayName} 占比 ${formatPercent(item.percentage)}，共 ${item.runs} 次`}
            >
              <div
                className="admin-agent-bar"
                style={{
                  width: `${Math.max(percent, 1.5)}%`,
                  background: color,
                }}
              />
              <span className="admin-agent-tooltip" role="tooltip">
                {NUMBER.format(item.runs)} 次 · {formatPercent(item.percentage)}
              </span>
            </div>
            <div className="admin-agent-stat">
              <strong>{formatPercent(item.percentage)}</strong>
              <span>{NUMBER.format(item.runs)} 次</span>
            </div>
          </div>
        )
      })}
    </div>
  )
}

function RecentRunsTable({ runs }) {
  if (!runs?.length) {
    return (
      <div className="admin-empty">
        <strong>暂无执行记录</strong>
        <span>发起一次 Run 后，这里会展示 RunID、Agent、状态与耗时。</span>
      </div>
    )
  }

  return (
    <div className="admin-table-wrap">
      <table className="admin-table">
        <thead>
          <tr>
            <th scope="col">Run ID</th>
            <th scope="col">Agent</th>
            <th scope="col">状态</th>
            <th scope="col" className="is-numeric">
              耗时
            </th>
            <th scope="col">开始时间</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => {
            const meta = STATUS_META[run.status] ?? { label: run.status, tone: 'muted' }
            return (
              <tr key={run.runId}>
                <td>
                  <code className="admin-run-id" title={run.runId}>
                    {run.runId}
                  </code>
                </td>
                <td>
                  <span className="admin-table-agent">
                    <i
                      className="admin-agent-swatch"
                      style={{ background: colorForAgent(run.agentId) }}
                      aria-hidden="true"
                    />
                    <span>
                      <strong>{run.agentName}</strong>
                      <code>{run.agentId}</code>
                    </span>
                  </span>
                </td>
                <td>
                  <span className={`admin-status is-${meta.tone}`}>
                    <i aria-hidden="true" />
                    {meta.label}
                  </span>
                </td>
                <td className="is-numeric">
                  <span className="admin-duration">{formatDuration(run.durationMs)}</span>
                </td>
                <td>
                  <span className="admin-time">{formatTime(run.startedAt)}</span>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function DashboardSkeleton() {
  return (
    <>
      <section className="admin-kpis" aria-hidden="true">
        {[0, 1, 2, 3].map((item) => (
          <div className="admin-kpi is-skeleton" key={item}>
            <span className="admin-skeleton-line is-short" />
            <span className="admin-skeleton-line is-value" />
            <span className="admin-skeleton-line" />
          </div>
        ))}
      </section>
      <section className="admin-panel is-skeleton" aria-hidden="true">
        <span className="admin-skeleton-line is-short" />
        <span className="admin-skeleton-line is-block" />
      </section>
      <section className="admin-panel is-skeleton" aria-hidden="true">
        <span className="admin-skeleton-line is-short" />
        <span className="admin-skeleton-line is-block" />
      </section>
    </>
  )
}

/**
 * M3.1 管理中台看板。
 *
 * 访问控制由 /admin 路由守卫与 BFF @IsAdmin 双层保证；本页只负责
 * 拉取 /api/v1/admin/metrics 并渲染。
 */
export default function AdminPage() {
  const { session, user } = useAuth()
  const abortRef = useRef(null)
  const [state, setState] = useState({
    status: 'loading',
    data: null,
    error: null,
    refreshing: false,
    fetchedAt: null,
  })

  const load = useCallback(
    async ({ silent = false } = {}) => {
      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller

      setState((previous) => {
        if (silent && previous.data) {
          return { ...previous, refreshing: true, error: null }
        }
        return { status: 'loading', data: null, error: null, refreshing: false, fetchedAt: null }
      })

      try {
        const payload = await fetchAdminMetrics({
          token: session?.token,
          role: user?.role,
          signal: controller.signal,
        })
        if (controller.signal.aborted) return
        setState({
          status: 'ready',
          data: payload?.data ?? null,
          error: null,
          refreshing: false,
          fetchedAt: Date.now(),
        })
      } catch (cause) {
        if (cause?.name === 'AbortError' || controller.signal.aborted) return
        setState((previous) => ({
          status: previous.data ? 'ready' : 'error',
          data: previous.data,
          error: cause,
          refreshing: false,
          fetchedAt: previous.fetchedAt,
        }))
      }
    },
    [session?.token, user?.role],
  )

  useEffect(() => {
    load()
    const timer = window.setInterval(() => load({ silent: true }), REFRESH_MS)
    return () => {
      window.clearInterval(timer)
      abortRef.current?.abort()
    }
  }, [load])

  const { data, error, status, refreshing, fetchedAt } = state
  const overview = data?.overview
  const agentRuns = data?.agentDistribution?.reduce((sum, item) => sum + item.runs, 0) ?? 0

  return (
    <div className="app-shell admin-shell">
      <AdminHeader />

      <main className="admin-main" aria-labelledby="admin-title">
        <div className="admin-page-head">
          <div>
            <span className="eyebrow">Admin Console</span>
            <h1 id="admin-title">系统概览</h1>
            <p>多 Agent 调度、沙盒与 Run 执行质量的实时聚合视图。</p>
          </div>
          <div className="admin-page-actions">
            {data?.demo && <span className="admin-badge is-demo">演示数据</span>}
            {data?.timezone && <span className="admin-badge">{data.timezone}</span>}
            <span className="admin-freshness" title={fetchedAt ? new Date(fetchedAt).toLocaleString('zh-CN') : undefined}>
              {data ? freshnessLabel(data.freshnessSeconds) : '加载中'}
            </span>
            <button
              className="ghost-button admin-refresh"
              type="button"
              onClick={() => load({ silent: Boolean(data) })}
              disabled={refreshing || status === 'loading'}
            >
              <span className={refreshing ? 'admin-refresh-glyph is-spinning' : 'admin-refresh-glyph'} aria-hidden="true">
                ↻
              </span>
              {refreshing ? '刷新中' : '刷新'}
            </button>
          </div>
        </div>

        {error && (
          <div className="admin-inline-error" role="alert">
            <strong>{error.status === 403 ? '管理员权限已失效' : '最近一次刷新失败'}</strong>
            <span>{error.message}</span>
            {error.requestId && <code>requestId: {error.requestId}</code>}
          </div>
        )}

        {status === 'loading' && <DashboardSkeleton />}

        {status === 'error' && (
          <section className="admin-panel admin-error-panel" role="alert">
            <span className="admin-error-code">{error?.code || error?.status || 'METRICS_ERROR'}</span>
            <h2>{error?.status === 403 ? '没有管理中台权限' : '指标加载失败'}</h2>
            <p>{error?.message || 'BFF 未返回可用的指标数据。'}</p>
            {error?.requestId && (
              <p className="admin-error-meta">
                requestId: <code>{error.requestId}</code>
              </p>
            )}
            <button className="primary-button" type="button" onClick={() => load()}>
              重新加载
            </button>
          </section>
        )}

        {status === 'ready' && data && (
          <>
            <section className="admin-kpis" aria-label="核心指标">
              <KpiCard
                icon="sandbox"
                label="活跃沙盒"
                value={NUMBER.format(overview?.activeSandboxes ?? 0)}
                hint="当前存活预览容器"
              />
              <KpiCard
                icon="runs"
                label="今日 Run"
                value={NUMBER.format(overview?.totalRunsToday ?? 0)}
                hint={`累计记录 ${NUMBER.format(overview?.totalRuns ?? 0)} 次`}
              />
              <KpiCard
                icon="success"
                label="成功率"
                value={formatPercent(overview?.successRate)}
                hint={
                  overview?.successRateSampleSize
                    ? `样本 ${NUMBER.format(overview.successRateSampleSize)} 次终态 Run`
                    : '暂无成功/失败样本'
                }
              />
              <KpiCard
                icon="duration"
                label="平均耗时"
                value={formatDuration(overview?.averageDurationMs)}
                hint="成功与失败 Run 墙钟均值"
              />
            </section>

            <section className="admin-panel admin-chart-panel" aria-labelledby="agent-distribution-title">
              <div className="admin-panel-head">
                <div>
                  <h2 id="agent-distribution-title">Agent 调用分布</h2>
                  <p>按已记录 Run 的次数占比，颜色与 Agent 固定绑定。</p>
                </div>
                <span className="admin-panel-meta">{NUMBER.format(agentRuns)} 次调用</span>
              </div>
              <AgentShareChart items={data.agentDistribution} />
            </section>

            <section className="admin-panel admin-table-panel" aria-labelledby="recent-runs-title">
              <div className="admin-panel-head">
                <div>
                  <h2 id="recent-runs-title">最近执行</h2>
                  <p>最近 10 次 Run，包含正在进行中的任务。</p>
                </div>
                <span className="admin-panel-meta">自动刷新 30s</span>
              </div>
              <RecentRunsTable runs={data.recentRuns} />
            </section>

            <footer className="admin-footnote">
              <span>指标来自 BFF 进程内聚合，服务重启后归零；M3.2 将切换为 run 聚合表。</span>
              {data.generatedAt && <span>生成于 {formatTime(data.generatedAt)}</span>}
            </footer>
          </>
        )}
      </main>
    </div>
  )
}
