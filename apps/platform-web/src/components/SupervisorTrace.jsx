/**
 * Supervisor 决策可视化。
 *
 * 目标：让“AI 思考”可被用户看见，而不是只显示一个 Loading：
 *  1. 请求发出后立刻显示“正在分析意图”；
 *  2. route 返回后展示最终 Handoff 判定与理由；
 *  3. handoff_both 显示两阶段时间轴：
 *     架构澄清（进行中） → 前端修改（等待门控释放 / 进行中 / 已完成）。
 *
 * 组件只消费 WorkspacePage / useArchitect / gate 的只读状态，
 * 不发起任何网络请求，也不改变调度语义。
 */

const HANDOFF_META = {
  handoff_ui: {
    icon: '🛠',
    label: '局部 UI 修改',
    detail: '交给 UI 工程师在沙盒中增量修改并构建验证',
  },
  handoff_architect: {
    icon: '🏛',
    label: '全局架构构建',
    detail: '交给全局架构师澄清应用形态并生成全栈契约',
  },
  handoff_both: {
    icon: '🧩',
    label: '全栈构建（架构 + 前端）',
    detail: '先确认契约，再放行前端修改，严格串行执行',
  },
  handoff_agent: {
    icon: '🧠',
    label: '专属专家执行',
    detail: '已派发到后端 / 数据库专家子图，正在生成并构建代码',
  },
  handoff_custom: {
    icon: '✨',
    label: '我的专属 Agent',
    detail: '命中企业 Manifest 声明的能力，进入专属路由',
  },
  rejected: {
    icon: '⚠',
    label: '未识别明确意图',
    detail: '需要补充画布标注或更明确的指令',
  },
}

const SUCCESS_STATUSES = new Set(['SUCCEEDED', 'AWAITING_APPROVAL'])

function stageState(stage) {
  return stage === 'done' || stage === 'active' || stage === 'waiting' || stage === 'failed'
    ? stage
    : 'waiting'
}

function bothStages({ architectState, gate, run }) {
  const architectStatus = architectState?.status
  const gateStatus = gate?.status

  let architectStage = 'active'
  if (
    architectStatus === 'CONFIRMED' ||
    gateStatus === 'RELEASED' ||
    gateStatus === 'RELEASING'
  ) {
    architectStage = 'done'
  }
  if (
    architectStatus === 'FAILED' ||
    architectStatus === 'CANCELLED' ||
    gateStatus === 'FAILED'
  ) {
    architectStage = 'failed'
  }

  let uiStage = 'waiting'
  if (gateStatus === 'RELEASED') {
    if (run.error) {
      uiStage = 'failed'
    } else if (run.submitting) {
      // 新 Run 启动后旧的 result 仍可能留在状态中，必须先看 submitting。
      uiStage = 'active'
    } else if (
      run.result &&
      SUCCESS_STATUSES.has(run.result.status) &&
      // 只有本轮事件流真正到达过终态，才把上一轮遗留的 result 当成完成。
      run.lastSeq > 0
    ) {
      uiStage = run.approval ? 'active' : 'done'
    } else {
      uiStage = 'active'
    }
  } else if (gateStatus === 'FAILED' || run.error) {
    uiStage = 'failed'
  }

  return [
    {
      id: 'architect',
      order: 1,
      title: '阶段 1 · 架构澄清',
      state: stageState(architectStage),
      detail:
        architectStage === 'done'
          ? '契约已确认，门控可释放'
          : architectStage === 'failed'
            ? '架构澄清已中止'
            : '进行中 · 先确认全栈实施契约',
    },
    {
      id: 'ui',
      order: 2,
      title: '阶段 2 · 前端修改',
      state: stageState(uiStage),
      detail:
        uiStage === 'waiting'
          ? '等待门控释放'
          : uiStage === 'active'
            ? run.approval
              ? '修改已完成，等待人工审批'
              : '进行中 · 正在执行界面修改'
            : uiStage === 'done'
              ? '已完成 · 可查看预览'
              : '前端修改已中止',
    },
  ]
}

function StageIcon({ state, order }) {
  if (state === 'done') return <span aria-hidden="true">✓</span>
  if (state === 'failed') return <span aria-hidden="true">!</span>
  if (state === 'active') return <span className="mini-spinner" aria-hidden="true" />
  return <span aria-hidden="true">{order}</span>
}

export default function SupervisorTrace({ run, architectState, gate }) {
  const decision = run.supervisorDecision
  const phase = run.supervisorPhase

  // 未发起过 Supervisor 请求时不占位。
  if (!phase && !decision) return null

  const meta = HANDOFF_META[decision?.handoff] ?? HANDOFF_META.rejected
  const isBoth = decision?.handoff === 'handoff_both'
  const analyzing = phase === 'analyzing'
  const rejected = decision?.handoff === 'rejected' || phase === 'rejected'
  const custom = decision?.handoff === 'handoff_custom'
  const expert = decision?.handoff === 'handoff_agent'
  const agentName =
    decision?.agentDisplayName ??
    (custom ? '企业专属 Agent' : meta.label)

  return (
    <section
      className={`supervisor-trace ${isBoth ? 'is-both' : ''} ${
        rejected ? 'is-rejected' : ''
      } ${custom ? 'is-custom' : ''} ${expert ? 'is-expert' : ''}`}
      data-supervisor-trace
      data-supervisor-phase={phase ?? 'idle'}
      data-supervisor-handoff={decision?.handoff ?? ''}
      aria-live="polite"
    >
      <div className="supervisor-trace-head">
        <span className={`supervisor-trace-orb ${analyzing ? 'is-thinking' : ''}`}>
          {analyzing ? '🤖' : meta.icon}
        </span>
        <div className="supervisor-trace-copy">
          <strong>
            {analyzing
              ? '🤖 正在分析意图…'
              : `🎯 判定为${meta.label}`}
          </strong>
          <span>
            {analyzing
              ? 'Supervisor 正在读取自然语言、画布标注与 Agent Manifest 能力'
              : decision?.reason ?? meta.detail}
          </span>
        </div>
        {!analyzing && decision && (
          <span className="supervisor-trace-badge">
            {decision.strategy === 'explicit_agent' ||
            decision.strategy === 'explicit_scope'
              ? '显式指令'
              : decision.strategy === 'custom_keyword'
                ? 'Manifest 命中'
                : '规则路由'}
          </span>
        )}
      </div>

      {isBoth && !analyzing && (
        <ol className="supervisor-timeline" data-both-timeline>
          {bothStages({ architectState, gate, run }).map((stage, index) => (
            <li
              key={stage.id}
              className={`supervisor-stage is-${stage.state}`}
              data-both-stage={stage.id}
              data-stage-state={stage.state}
            >
              <span className="supervisor-stage-node">
                <StageIcon state={stage.state} order={stage.order} />
              </span>
              <div className="supervisor-stage-copy">
                <strong>{stage.title}</strong>
                <span>{stage.detail}</span>
              </div>
              {index === 0 && <span className="supervisor-stage-link" aria-hidden="true" />}
            </li>
          ))}
        </ol>
      )}

      {expert && !analyzing && (
        <div className="supervisor-custom-card" data-expert-route-card>
          <div>
            <span className="supervisor-custom-eyebrow">BUILTIN · EXPERT AGENT</span>
            <strong>{agentName}</strong>
            <span>
              任务类型 <code>{decision.taskType}</code>
              {decision.signals?.length
                ? ` · 命中 ${decision.signals[0].split(':').pop()}`
                : ''}
            </span>
          </div>
          <p>
            已启动专属子图并接入实时事件流。生成 / 构建 / 沙盒重启的进度会同步到
            全栈面板，完成后可直接查看预览。
          </p>
        </div>
      )}

      {custom && !analyzing && (
        <div className="supervisor-custom-card" data-custom-route-card>
          <div>
            <span className="supervisor-custom-eyebrow">MANIFEST · CUSTOM AGENT</span>
            <strong>{agentName}</strong>
            <span>
              任务类型 <code>{decision.taskType}</code>
              {decision.signals?.length
                ? ` · 命中 ${decision.signals[0].split(':').pop()}`
                : ''}
            </span>
          </div>
          <p>
            已成功完成专属 Agent 路由。运行时将在 M3 接入独立受限沙盒；
            本次演示验证了「上传 Manifest → 自动注册 → 关键词路由」的秒级接入链路。
          </p>
        </div>
      )}
    </section>
  )
}
