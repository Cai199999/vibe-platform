import { memo, useEffect, useRef, useState } from 'react'
import AgentCatalogMenu from './AgentCatalogMenu.jsx'
import ArchitectPanel from './ArchitectPanel.jsx'
import CanvasStage from './CanvasStage.jsx'
import ErrorBoundary from './ErrorBoundary.jsx'
import InspectDraftCard from './InspectDraftCard.jsx'
import RuntimePanel from './RuntimePanel.jsx'
import SupervisorTrace from './SupervisorTrace.jsx'
import {
  ViewportProvider,
  useViewportState,
} from '../workspace/ViewportContext.jsx'
import {
  CREATE_NEW,
  MODIFY_EXISTING,
  isCreateAction,
} from '../workspace/annotationActions.js'

/**
 * 单个项目窗口：支持「对比模式」与「展示模式」两种布局。
 *
 *  - 对比模式：单屏全屏显示画布或预览，顶部 Tab 切换；
 *  - 展示模式：画布与预览左右 50% 同显。
 *
 * 两个面板从挂载起始终存在，只通过 CSS display 控制显隐：
 *  - CanvasStage 的圈选、缩放、浮层与 ResizeObserver 状态不会丢失；
 *  - iframe 元素不重建，Vite HMR 的 WebSocket 连接与页面状态保持；
 *  - 切换视图时外层 .project-viewport 高度固定，不会引起页面跳动。
 */
function ProjectWorkspaceInner({
  project,
  run,
  active,
  modelProvider,
  onModelProviderChange,
  onAnnotationAdd,
  onAnnotationIntentChange,
  onAnnotationActionChange,
  onAnnotationRemove,
  onAnnotationUnmount,
  onAnnotationSend,
  onAnnotationMove,
  // 直接拖拽：SDK 只回传“目标 + 手势位移”，代码同步由 WorkspacePage
  // 静默提交；这里负责把拖拽中的视觉变化同步到右侧预览。
  onComponentDragStart,
  onComponentDragMove,
  onComponentDragEnd,
  onComponentDragCancel,
  dragRollback = null,
  dragCommit = null,
  dragSyncActive = false,
  // 批量拖拽草稿：数量 > 0 时显示悬浮操作条；保存中禁用全部按钮。
  layoutDraftCount = 0,
  // 视觉暂存的新增组件：只存在于前端，保存布局时才一次性写入代码。
  pendingAdditions = [],
  onRemovePendingAddition,
  layoutSaving = false,
  onSaveLayout,
  onUndoLayout,
  onClarificationAnswer,
  onAnnotationRollback,
  onPlacementConfirm,
  onPlacementDismiss,
  onClearAnnotations,
  // iframe 内部路由切换 / 重载：清空画布标注与当前 Inspect 草稿。
  onFrameRouteChange,
  // iframe 文档重建（首次加载 / HMR）：父级清空视觉暂存幽灵节点。
  onFrameReady,
  onApprovalDecision,
  onCancelRun,
  onSubmit,
  onRetry,
  // 大型重构超时后的“转后台运行”；不阻塞用户继续圈选。
  onRunInBackground,
  onExport,
  canExport = false,
  exporting = false,
  onSaveSnapshot,
  canSaveSnapshot = false,
  previewRefreshNonce = 0,
  // 项目 Tab 每次被选中时递增，用于强制同步 / 重载两侧 iframe。
  tabActivationNonce = 0,
  architectState,
  supervisorGate,
  agentCatalog,
  agentToken,
  onRefreshAgents,
  previewFocusNonce = 0,
  onArchitectOpen,
  onArchitectStart,
  onArchitectRestart,
  onStartCreate,
  onFrontendBootstrap,
  onArchitectClose,
  onArchitectAnswer,
  onArchitectDecide,
  onArchitectAskHelper,
  onArchitectCancel,
  onArchitectExport,
  onFullstackGenerate,
  onReleaseGate,
}) {
  const { linked, setLinked } = useViewportState()
  // 当前布局：compare = 单屏切换，dual = 双屏同显。
  const [viewMode, setViewMode] = useState('compare')
  // 仅对比模式生效：当前显示画布还是预览。
  const [mode, setMode] = useState('canvas')
  // 精准点击选择（Inspect）模式与命中组件后的标注草稿。
  const [inspectMode, setInspectMode] = useState(false)
  const [inspectDraft, setInspectDraft] = useState(null)
  // 顶部工具栏的「更多」折叠菜单：清空标注 / 保存快照 / 导出 / Agent 目录。
  const [moreOpen, setMoreOpen] = useState(false)
  const moreRef = useRef(null)
  // 双屏同步：画布 iframe 的滚动位置与点击反馈。
  // nonce 保证即使位置相同也会重新下发（iframe 重载后需要再次对齐）。
  const [scrollSync, setScrollSync] = useState(null)
  const [clickFeedback, setClickFeedback] = useState(null)
  const syncNonceRef = useRef(0)
  const clickNonceRef = useRef(0)
  // 画布最新滚动位置：即使当前是单屏模式也要记录，否则从单屏切换到
  // 双屏时预览只能从 0 开始，与用户已经滚到的位置脱节。
  const canvasScrollRef = useRef(null)
  // 拖拽视觉同步：按 selector 保存所有未保存草稿的预览位移。
  // 批量布局允许多个组件同时处于「已拖动、未保存」状态，只保留最新
  // 一个会让右侧预览在拖第二个组件时把第一个弹回原位。
  const [dragFeedback, setDragFeedback] = useState(null)
  const dragFeedbackRef = useRef(new Map())
  const dragNonceRef = useRef(0)

  const publishDragFeedback = (payload) => {
    if (!payload?.selector) return
    // drag-start 不携带 inlineX/Y；已有草稿再次被拖动时沿用上一帧位置，
    // 否则预览会先闪回原位。同一组件后续 move/end 会覆盖为最新值。
    const previous = dragFeedbackRef.current.get(payload.selector)
    const next = {
      ...previous,
      ...payload,
      inlineX: payload.inlineX ?? previous?.inlineX ?? 0,
      inlineY: payload.inlineY ?? previous?.inlineY ?? 0,
    }
    dragFeedbackRef.current.set(payload.selector, next)
    if (viewMode !== 'dual') return
    dragNonceRef.current += 1
    setDragFeedback({
      nonce: dragNonceRef.current,
      items: [...dragFeedbackRef.current.values()],
    })
  }

  const handleComponentDragStart = (payload) => {
    publishDragFeedback({ ...payload, phase: 'move' })
    onComponentDragStart?.(payload)
  }

  const handleComponentDragMove = (payload) => {
    publishDragFeedback({ ...payload, phase: 'move' })
    onComponentDragMove?.(payload)
  }

  const handleComponentDragEnd = (payload) => {
    publishDragFeedback({ ...payload, phase: 'end' })
    onComponentDragEnd?.(payload)
  }

  const handleComponentDragCancel = (payload) => {
    onComponentDragCancel?.(payload)
  }

  // 后端已完成（成功提交 / 单次失败回滚）后，只清理对应 selector：
  // 批量草稿里未被提交的其他组件必须继续保留预览位移。
  useEffect(() => {
    const commitNonce = dragCommit?.nonce
    const rollbackNonce = dragRollback?.nonce
    if (!commitNonce && !rollbackNonce) return
    const settled = new Set(
      [
        ...(dragCommit?.items ?? []),
        ...(dragRollback?.items ?? []),
      ]
        .map((item) => item?.selector)
        .filter(Boolean),
    )
    settled.forEach((selector) => dragFeedbackRef.current.delete(selector))
    const remaining = [...dragFeedbackRef.current.values()]
    if (remaining.length === 0) {
      setDragFeedback(null)
      return
    }
    dragNonceRef.current += 1
    setDragFeedback({ nonce: dragNonceRef.current, items: remaining })
    // nonce 是唯一触发条件；items 内容从 ref 读取。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dragCommit?.nonce, dragRollback?.nonce])

  const publishScrollSync = (payload) => {
    if (!payload) return
    syncNonceRef.current += 1
    setScrollSync({
      scrollX: Number(payload.scrollX) || 0,
      scrollY: Number(payload.scrollY) || 0,
      nonce: syncNonceRef.current,
    })
  }

  const handleFrameScroll = (payload) => {
    if (!payload) return
    canvasScrollRef.current = payload
    // 单屏模式不驱动右侧预览，但位置必须留到切换双屏时立即对齐。
    if (viewMode !== 'dual') return
    publishScrollSync(payload)
  }

  // 进入双屏：预览 iframe 可能刚挂载、还没有收到画布的 scroll 事件，
  // 这里用最近一次快照补一次对齐，避免右侧停留在页面顶部。
  useEffect(() => {
    if (viewMode !== 'dual') return
    publishScrollSync(canvasScrollRef.current)
    // 已有未保存草稿时切到双屏：预览需要立即补齐全部组件的临时位移。
    if (dragFeedbackRef.current.size > 0) {
      dragNonceRef.current += 1
      setDragFeedback({
        nonce: dragNonceRef.current,
        items: [...dragFeedbackRef.current.values()],
      })
    }
    // publishScrollSync 只依赖 ref 与 setState，不需要进入依赖。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewMode])

  const handleFrameInteraction = (payload) => {
    if (viewMode !== 'dual' || !payload?.rect) return
    clickNonceRef.current += 1
    // 保留点击点（iframe 视口坐标），预览据此画小涟漪；rect 仅作为
    // Inspect / 旧 SDK 没有 x/y 时的回退锚点。
    setClickFeedback({
      x: payload.x,
      y: payload.y,
      rect: payload.rect,
      // 点击发生时的滚动位置：预览先对齐再重放 click，确保命中同一元素。
      scrollX: payload.scrollX,
      scrollY: payload.scrollY,
      synthetic: Boolean(payload.synthetic),
      nonce: clickNonceRef.current,
    })
  }

  /*
   * 关键问题计数：只统计 required 问题；后端未标注 required 时退化为
   * clarification.questions 全量。用于顶部不可关闭 Banner 与生成拦截。
   */
  const pendingKeyQuestions = (() => {
    if (!architectState?.runId) return 0
    if (architectState.status !== 'AWAITING_CLARIFICATION') return 0
    const questions = architectState.clarification?.questions
    if (Array.isArray(questions) && questions.length > 0) {
      const required = questions.filter((question) => question.required !== false)
      return Math.max(1, required.length)
    }
    const missing = architectState.clarification?.missingSlots
    return Array.isArray(missing) && missing.length > 0 ? missing.length : 1
  })()

  const architectIncomplete =
    Boolean(architectState?.runId) &&
    !['CONFIRMED', 'CANCELLED'].includes(architectState.status)

  const architectBannerText = (() => {
    if (!architectIncomplete) return ''
    const status = architectState.status
    if (status === 'AWAITING_CLARIFICATION') {
      return `架构设计尚未完成，请确认剩余 ${pendingKeyQuestions} 个关键问题`
    }
    if (status === 'AWAITING_CONTRACT_CONFIRMATION') {
      return '架构设计尚未完成，请确认实施契约'
    }
    if (status === 'FAILED') {
      return '架构设计尚未完成：上一次分析失败，请重新发起架构对话'
    }
    return '架构设计尚未完成，Architect 正在整理方案…'
  })()

  useEffect(() => {
    if (!moreOpen) return undefined
    const handlePointer = (event) => {
      if (moreRef.current && !moreRef.current.contains(event.target)) {
        setMoreOpen(false)
      }
    }
    const handleKey = (event) => {
      if (event.key === 'Escape') setMoreOpen(false)
    }
    window.addEventListener('mousedown', handlePointer)
    window.addEventListener('keydown', handleKey)
    return () => {
      window.removeEventListener('mousedown', handlePointer)
      window.removeEventListener('keydown', handleKey)
    }
  }, [moreOpen])

  // Supervisor 派发 UI Run（或门控释放）后，自动露出右侧执行时间轴。
  // 使用递增 nonce 而不是布尔值，保证连续两次 handoff 都能重新聚焦。
  useEffect(() => {
    if (previewFocusNonce > 0) setMode('preview')
  }, [previewFocusNonce])
  // iframe 的复合 nonce：快照回滚 / Run 完成后的增量与 Tab 激活增量合并。
  // 使用字符串避免两个递增计数器数值相同时（如 1 + 1 与 2 + 0）key 冲突。
  const frameSyncNonce = `${previewRefreshNonce}:${tabActivationNonce}`
  const annotations = run.annotations
  const submitting = run.submitting
  const previewReady = Boolean(run.result?.previewUrl)
  // 新建项目的画布阶段：
  //   blank -> 只有【✨ 开始创建】；bootstrapping -> 等待骨架屏；
  //   failed -> 失败重试；live -> 直接渲染真实前端沙盒；
  //   旧演示 / 导入项目继续沿用 mock 画布。
  const canvasMode =
    project.bootstrap === 'pending'
      ? 'blank'
      : project.bootstrap === 'bootstrapping'
        ? 'bootstrapping'
        : project.bootstrap === 'failed'
          ? 'failed'
          : project.canvasMode === 'live'
            ? 'live'
            : 'mock'
  const canvasVisible = viewMode === 'dual' || mode === 'canvas'
  const previewVisible = viewMode === 'dual' || mode === 'preview'
  // 审批 / 澄清面板在预览侧。对比模式停留在画布时提供一个显式入口，
  // 避免输入被冻结却找不到处理位置；不强制切换视图，保留用户的画布上下文。
  const pauseCta = run.approval
    ? '查看审批差异'
    : run.clarification
      ? '回答澄清问题'
      : null
  // 只有每个标注都写了指令才允许提交，避免静默丢标注。
  const submitReady =
    annotations.length > 0 &&
    annotations.every((annotation) => annotation.intent.trim().length > 0)
  // 审批未决时不允许再发起新 Run，画布交互一并冻结。
  const approvalPending = Boolean(run.approval)
  // both 门控未释放时 UI Graph 不能执行，画布与提交入口同步冻结。
  const gateActive =
    supervisorGate?.status === 'AWAITING_CONTRACT' ||
    supervisorGate?.status === 'RELEASING'
  const gateStatusText =
    supervisorGate?.status === 'RELEASING'
      ? '正在放行界面修改…'
      : '正在进行全局架构设计，请先确认契约'
  const status = submitting
    ? 'running'
    : approvalPending
      ? 'pending'
      : run.error
        ? 'error'
        : run.result
          ? 'success'
          : 'idle'
  const inspectBlocked = submitting || approvalPending || gateActive

  /**
   * 精准选择在画布与预览两种模式下都可使用。
   * - live 画布：CanvasStage 通过 postMessage 把 hover / pick 透传给 iframe SDK；
   * - mock 画布没有真实 SDK：如果已有预览，则自动切到预览，避免用户以为按钮失效；
   * - 手动关闭时清空草稿与高亮。
   */
  const toggleInspect = () => {
    if (!previewReady || inspectBlocked) return
    const next = !inspectMode
    setInspectMode(next)
    setInspectDraft(null)
    if (next && mode === 'canvas' && canvasMode !== 'live') {
      setMode('preview')
    }
  }

  /** SDK 回传的命中结果转换为本地标注草稿。 */
  const handleInspectPick = (payload) => {
    if (!payload?.source?.file) return
    const rect = payload.rect ?? { x: 0, y: 0, width: 0, height: 0 }
    // SDK 的 rect 相对当前 iframe 逻辑视口；视口会随画布尺寸扩展，
    // 因此必须使用 payload 里的真实 innerWidth / innerHeight 归一化。
    // inspect SDK 上报的逻辑视口固定为 1440×900；缺失时按同一基准兜底。
    const viewportWidth = Number(payload.viewport?.width) || 1440
    const viewportHeight = Number(payload.viewport?.height) || 900
    const toWorld = (value, total) =>
      Math.min(1, Math.max(0, Number(value || 0) / total))
    const worldRect = {
      x: toWorld(rect.x, viewportWidth),
      y: toWorld(rect.y, viewportHeight),
      width: toWorld(rect.width, viewportWidth),
      height: toWorld(rect.height, viewportHeight),
    }
    const source = payload.source
    setInspectDraft({
      source: {
        file: source.file,
        line: Number(source.line) || 1,
        column: Number(source.column) || 1,
        component: source.component || 'Component',
        sourceKind: source.sourceKind || 'jsx',
        marker: source.marker || '',
        selector: source.selector || '',
        kind: source.kind || 'element',
        // 实例作用域：同一源码可能对应多个 DOM 实例，必须原样带给
        // AnnotationEnvelope，Coder 才能只改 callSite 而不是基础组件。
        instance: Number(source.instance) || undefined,
        instanceCount: Number(source.instanceCount) || undefined,
        instanceId: source.instanceId || undefined,
        callSite: source.callSite || null,
      },
      container: payload.container
        ? {
            source: payload.container.source,
            rect: payload.container.rect,
            relativeRect: payload.container.relativeRect,
          }
        : null,
      rect,
      worldRect,
      // 文档坐标 + 滚动快照：Inspect 草稿同样必须跟随 iframe 滚动。
      documentRect: {
        x: Number(rect.x) + Number(payload.viewport?.scrollX || 0),
        y: Number(rect.y) + Number(payload.viewport?.scrollY || 0),
        width: Number(rect.width) || 0,
        height: Number(rect.height) || 0,
      },
      frameScroll: {
        scrollX: Number(payload.viewport?.scrollX || 0),
        scrollY: Number(payload.viewport?.scrollY || 0),
        width: viewportWidth,
        height: viewportHeight,
      },
      tag: payload.tag || source.tag || 'element',
      role: payload.role || source.tag || 'element',
      label: payload.label || source.component || source.tag || '组件',
      intent: '',
      action:
        payload.source.kind === 'container' ? CREATE_NEW : MODIFY_EXISTING,
      createdAt: new Date().toISOString(),
    })
  }

  const handleInspectDraftChange = (patch) => {
    setInspectDraft((previous) => (previous ? { ...previous, ...patch } : previous))
  }

  const handleInspectDraftSubmit = () => {
    if (!inspectDraft || !inspectDraft.intent?.trim()) return
    const action = isCreateAction(inspectDraft.action)
      ? CREATE_NEW
      : MODIFY_EXISTING
    const clicked = inspectDraft.source
    const container = inspectDraft.container?.source
    // CREATE_NEW 的落点是父容器；MODIFY_EXISTING 的落点就是点击元素。
    // 源码出处必须同步切换，否则 AnnotationEnvelope 指向的仍是叶子节点，
    // 模型会插错位置。
    const effective =
      isCreateAction(action) && container ? container : clicked
    const targetView = (sourceItem, fallback = {}) => ({
      marker: sourceItem.marker || '',
      selector: sourceItem.selector || '',
      label: sourceItem.component || sourceItem.tag || fallback.label || '组件',
      role:
        fallback.role ||
        (sourceItem.kind === 'container' ? 'container' : 'element'),
      component: sourceItem.component || 'Component',
      tag:
        sourceItem.tag ||
        fallback.tag ||
        (isCreateAction(action) ? 'div' : 'button'),
      kind: sourceItem.kind || 'element',
      instance: Number(sourceItem.instance) || undefined,
      instanceCount: Number(sourceItem.instanceCount) || undefined,
      instanceId: sourceItem.instanceId || undefined,
      callSite: sourceItem.callSite || null,
    })
    onAnnotationAdd?.({
      rect: inspectDraft.worldRect,
      target: targetView(clicked, inspectDraft),
      addTarget: targetView(effective, inspectDraft),
      relativeRect:
        isCreateAction(action)
          ? (inspectDraft.container?.relativeRect ?? {
              x: 0,
              y: 0,
              width: 1,
              height: 1,
            })
          : null,
      action,
      documentRect: inspectDraft.documentRect ?? null,
      frameScroll: inspectDraft.frameScroll ?? null,
      measured: {
        mode: 'inspect',
        source: effective,
        clickedSource: clicked,
      },
      intent: inspectDraft.intent.trim(),
      source: {
        file: effective.file,
        line: Number(effective.line) || 1,
        column: Number(effective.column) || 1,
        component: effective.component || 'Component',
        sourceKind: effective.sourceKind || 'jsx',
        kind: effective.kind || 'element',
        instance: Number(effective.instance) || undefined,
        instanceCount: Number(effective.instanceCount) || undefined,
        instanceId: effective.instanceId || undefined,
        callSite: effective.callSite || null,
      },
    })
    setInspectDraft(null)
  }

  /**
   * iframe 路由切换 / 重载：当前页面已变。
   * 本组件先丢弃未提交的 Inspect 草稿，再把事件交给 WorkspacePage
   * 清空整组标注（只清前端状态，不回滚代码）。
   */
  const handleFrameRouteChange = (payload) => {
    setInspectDraft(null)
    onFrameRouteChange?.(payload)
  }

  /** HMR / 首次加载：真实 DOM 已重建，交给 WorkspacePage 清理幽灵草稿。 */
  const handleFrameReady = (payload) => {
    onFrameReady?.(payload)
  }

  return (
    <section
      className={`tab-window ${active ? 'is-active' : 'is-hidden'}`}
      aria-hidden={!active}
    >
      {/* 不可关闭的架构待办 Banner：只要主流程未确认就常驻页面最顶部，
          点击后直接打开 Architect 面板处理关键问题。 */}
      {architectIncomplete && (
        <div
          className={`architect-pending-banner ${architectState.status === 'AWAITING_CLARIFICATION' ? 'is-attention' : ''}`}
          role="alert"
          data-architect-pending-banner
        >
          <span className="architect-pending-mark" aria-hidden="true">
            ⚠️
          </span>
          <span className="architect-pending-text">{architectBannerText}</span>
          <button
            type="button"
            className="architect-pending-action"
            onClick={onArchitectOpen}
          >
            {pendingKeyQuestions > 0 ? '去确认' : '查看进度'}
          </button>
        </div>
      )}

      <div className="project-toolbar">
        <div className="project-toolbar-left">
          {annotations.length > 0 && (
            <span className="selection-state is-ready">
              {annotations.length} 个标注
            </span>
          )}
          <button
            className={`inspect-toggle ${inspectMode ? 'is-active' : ''}`}
            type="button"
            data-inspect-toggle
            aria-pressed={inspectMode}
            aria-label={inspectMode ? '退出选择' : '精准选择'}
            disabled={!previewReady || inspectBlocked}
            title={
              !previewReady
                ? '请先执行一次 Agent，等预览沙盒启动后可精准选择组件'
                : inspectMode
                  ? '退出精准选择模式'
                  : '精准选择：移动鼠标高亮组件，左键点击生成标注'
            }
            onClick={toggleInspect}
          >
            <svg
              className="inspect-toggle-icon"
              viewBox="0 0 24 24"
              width="15"
              height="15"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="M5 3l13.5 7.6-6.2 1.5L9.4 19 5 3z" />
              <path d="M13 13l5 5" />
            </svg>
            {inspectMode ? '退出' : '选择'}
          </button>

          <label className="model-picker" title="模型设置">
            <span className="model-gear" aria-hidden="true">
              ⚙
            </span>
            <select
              aria-label="模型"
              value={modelProvider}
              disabled={submitting}
              onChange={(event) => onModelProviderChange?.(event.target.value)}
            >
              <option value="auto">DeepSeek V3</option>
              <option value="mock">本地演示</option>
            </select>
          </label>
        </div>

        <div className="project-toolbar-right">
          {gateActive && (
            <span className="supervisor-gate-chip" role="status">
              <span className="mini-spinner" aria-hidden="true" />
              {gateStatusText}
            </span>
          )}
          <button
            className={`architect-launch ${
              architectState?.contract ? 'has-contract' : ''
            } ${architectState?.status === 'AWAITING_CLARIFICATION' ? 'needs-answer' : ''} ${
              run.dirty ? 'is-locked' : ''
            }`}
            type="button"
            aria-disabled={Boolean(run.dirty)}
            title={
              run.dirty
                ? '请先保存当前修改，再进入全栈方案'
                : '基于当前沙盒源码，澄清应用形态并生成可实施的全栈契约'
            }
            // 不做原生 disabled：需要保留点击事件来 Toast 解释为什么不能用。
            onClick={onArchitectOpen}
          >
            <span className="architect-launch-mark" aria-hidden="true">
              ◈
            </span>
            下一步：构建全栈方案
            {architectState?.status === 'AWAITING_CLARIFICATION' && (
              <span className="architect-launch-dot" aria-label="有待回答问题" />
            )}
          </button>

          <button
            className="submit-annotations"
            type="button"
            disabled={
              !submitReady ||
              submitting ||
              Boolean(run.clarification) ||
              approvalPending ||
              gateActive
            }
            title={
              gateActive
                ? gateStatusText
                : approvalPending
                  ? '请先处理预览面板中的 Diff 审批'
                  : run.clarification
                    ? '请先回答画布浮层中的澄清问题'
                    : submitReady
                      ? `提交全部 ${annotations.length} 个标注`
                      : annotations.length === 0
                        ? '请先在画布上圈选组件'
                        : '请为每个标注填写修改意图'
            }
            onClick={onSubmit}
          >
            {submitting && !run.pendingAnnotationId ? (
              <>
                <span className="mini-spinner" aria-label="执行中" />
                {run.pendingAction === 'rollback' ? '回滚中…' : '执行中…'}
              </>
            ) : (
              '提交'
            )}
          </button>

          {viewMode === 'compare' && (
            <div className="segmented pane-switch" role="group" aria-label="单屏视图切换">
              <button
                type="button"
                className={mode === 'canvas' ? 'is-active' : ''}
                aria-pressed={mode === 'canvas'}
                onClick={() => setMode('canvas')}
              >
                画布
              </button>
              <button
                type="button"
                className={mode === 'preview' ? 'is-active' : ''}
                aria-pressed={mode === 'preview'}
                onClick={() => setMode('preview')}
              >
                <span className={`seg-status seg-status-${status}`} aria-hidden="true" />
                预览
              </button>
            </div>
          )}

          <button
            type="button"
            className={`view-mode-toggle ${viewMode === 'dual' ? 'is-active' : ''}`}
            aria-label={viewMode === 'compare' ? '展示模式' : '对比模式'}
            aria-pressed={viewMode === 'dual'}
            title={
              viewMode === 'compare'
                ? '切换到双屏展示模式'
                : '切换回单屏对比模式'
            }
            onClick={() =>
              setViewMode((current) =>
                current === 'compare' ? 'dual' : 'compare',
              )
            }
          >
            {viewMode === 'compare' ? (
              <svg viewBox="0 0 20 20" width="15" height="15" aria-hidden="true">
                <rect x="3" y="4" width="14" height="12" rx="2" />
              </svg>
            ) : (
              <svg viewBox="0 0 20 20" width="15" height="15" aria-hidden="true">
                <rect x="2.5" y="4" width="7" height="12" rx="1.6" />
                <rect x="10.5" y="4" width="7" height="12" rx="1.6" />
              </svg>
            )}
          </button>

          {viewMode === 'dual' && (
            <button
              type="button"
              className={`link-toggle ${linked ? 'is-active' : ''}`}
              aria-pressed={linked}
              title="画布与预览同步缩放、平移"
              onClick={() => setLinked((value) => !value)}
            >
              <span className="link-dot" aria-hidden="true" />
              视口联动
            </button>
          )}

          <div className="toolbar-more" ref={moreRef}>
            <button
              type="button"
              className={`toolbar-more-trigger ${moreOpen ? 'is-open' : ''}`}
              data-toolbar-more
              aria-haspopup="menu"
              aria-expanded={moreOpen}
              aria-label="更多操作"
              title="更多操作"
              onClick={() => setMoreOpen((value) => !value)}
            >
              <span aria-hidden="true">⋯</span>
            </button>
            {moreOpen && (
              <div className="toolbar-more-menu" role="menu" aria-label="更多操作">
                <button
                  type="button"
                  role="menuitem"
                  className="ghost-button toolbar-menu-item"
                  disabled={
                    annotations.length === 0 || submitting || approvalPending
                  }
                  onClick={() => {
                    setMoreOpen(false)
                    onClearAnnotations?.()
                  }}
                >
                  清除标注
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className="snapshot-save-button toolbar-menu-item"
                  data-snapshot-save
                  disabled={!canSaveSnapshot || submitting}
                  title={
                    canSaveSnapshot
                      ? '把当前沙盒代码保存为一个可回滚的快照'
                      : '请先执行一次 Agent，生成可快照的沙盒项目'
                  }
                  onClick={() => {
                    setMoreOpen(false)
                    onSaveSnapshot?.()
                  }}
                >
                  <span aria-hidden="true">💾</span>
                  保存快照
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className={`export-button toolbar-menu-item ${exporting ? 'is-busy' : ''}`}
                  disabled={!canExport || exporting}
                  title={
                    canExport
                      ? '下载当前项目的 tar.gz（排除 node_modules / .git / dist）'
                      : '请先执行一次 Agent，生成可导出的沙盒项目'
                  }
                  onClick={() => {
                    setMoreOpen(false)
                    onExport?.()
                  }}
                >
                  <span aria-hidden="true">📥</span>
                  {exporting ? '导出中…' : '导出代码'}
                </button>
                <div className="toolbar-more-divider" role="separator" />
                <div className="toolbar-more-agents">
                  <AgentCatalogMenu
                    agents={agentCatalog?.agents ?? []}
                    loading={Boolean(agentCatalog?.loading)}
                    error={agentCatalog?.error ?? null}
                    onRefresh={onRefreshAgents}
                    token={agentToken}
                  />
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      <SupervisorTrace
        run={run}
        architectState={architectState}
        gate={supervisorGate}
      />

      <div className={`project-viewport view-${viewMode}`}>
        <div
          className={`viewport-pane canvas-pane ${canvasVisible ? 'is-visible' : 'is-hidden'}`}
        >
          <ErrorBoundary
            label="画布区域"
            compact
            resetKey={project.id}
          >
            <CanvasStage
              mode={canvasMode}
              liveUrl={run.result?.previewUrl ?? ''}
              refreshNonce={frameSyncNonce}
              // 非激活 Tab 销毁 iframe：隐藏 Tab 的 Vite/HMR 进程不再常驻。
              // 切回 Tab 时按同一 URL 重新挂载，沙盒状态在服务端不会丢。
              suspended={!active}
              onStartCreate={onStartCreate}
              onRetryBootstrap={onFrontendBootstrap}
              onOpenArchitect={onArchitectOpen}
              annotations={annotations}
              onAnnotationAdd={onAnnotationAdd}
              onAnnotationIntentChange={onAnnotationIntentChange}
              onAnnotationActionChange={onAnnotationActionChange}
              onAnnotationRemove={onAnnotationRemove}
              onAnnotationUnmount={onAnnotationUnmount}
              onAnnotationSend={onAnnotationSend}
              onAnnotationMove={onAnnotationMove}
              onClarificationAnswer={onClarificationAnswer}
              inspectMode={inspectMode && canvasVisible && !inspectBlocked}
              onInspectPick={handleInspectPick}
              onFrameScroll={handleFrameScroll}
              onFrameRouteChange={handleFrameRouteChange}
              onFrameReady={handleFrameReady}
              onFrameInteraction={handleFrameInteraction}
              onComponentDragStart={handleComponentDragStart}
              onComponentDragMove={handleComponentDragMove}
              onComponentDragEnd={handleComponentDragEnd}
              onComponentDragCancel={handleComponentDragCancel}
              dragRollback={dragRollback}
              dragCommit={dragCommit}
              dragSyncActive={dragSyncActive}
              pendingAdditions={pendingAdditions}
              onRemovePendingAddition={onRemovePendingAddition}
              clarification={run.clarification}
              onAnnotationRollback={onAnnotationRollback}
              pendingAnnotationId={run.pendingAnnotationId}
              pendingAction={run.pendingAction}
              submitting={submitting}
              disabled={submitting}
              blocked={approvalPending || gateActive}
            />
          </ErrorBoundary>

          {/* 画布模式下命中 Inspect 后，输入卡片直接浮在画布上；预览模式
              仍由 RuntimePanel 渲染，避免双屏时出现两份输入框。 */}
          {inspectDraft &&
            !submitting &&
            !run.approval &&
            viewMode === 'compare' &&
            mode === 'canvas' && (
              <InspectDraftCard
                draft={inspectDraft}
                onChange={handleInspectDraftChange}
                onCancel={() => setInspectDraft(null)}
                onSubmit={handleInspectDraftSubmit}
              />
            )}

          {pauseCta && viewMode === 'compare' && mode === 'canvas' && (
            <button
              className="view-result-cta is-pending"
              type="button"
              onClick={() => setMode('preview')}
            >
              {pauseCta}
              <span aria-hidden="true">→</span>
            </button>
          )}

          {!pauseCta &&
            previewReady &&
            viewMode === 'compare' &&
            mode === 'canvas' && (
              <button
                className="view-result-cta"
                type="button"
                onClick={() => setMode('preview')}
              >
                查看效果
                <span aria-hidden="true">→</span>
              </button>
            )}
        </div>

        <div
          className={`viewport-pane preview-pane ${previewVisible ? 'is-visible' : 'is-hidden'}`}
        >
          <ErrorBoundary
            label="预览区域"
            compact
            resetKey={`${project.id}:${run.result?.runId ?? 'idle'}`}
          >
            <RuntimePanel
              run={run}
              projectName={project.name}
              modelProvider={modelProvider}
              expanded
              active={active}
              visible={active && previewVisible}
              onRetry={submitReady ? onRetry : undefined}
              onRunInBackground={onRunInBackground}
              onGoToCanvas={() => setMode('canvas')}
              onApprovalDecision={onApprovalDecision}
              onCancelRun={onCancelRun}
              inspectMode={inspectMode && previewVisible && !inspectBlocked}
              onInspectPick={handleInspectPick}
              onFrameRouteChange={handleFrameRouteChange}
              inspectDraft={inspectDraft}
              onInspectDraftChange={handleInspectDraftChange}
              onInspectDraftCancel={() => setInspectDraft(null)}
              onInspectDraftSubmit={handleInspectDraftSubmit}
              refreshNonce={frameSyncNonce}
              syncScroll={viewMode === 'dual' ? scrollSync : null}
              interactionFeedback={
                viewMode === 'dual' ? clickFeedback : null
              }
              dragFeedback={viewMode === 'dual' ? dragFeedback : null}
              dragRollback={dragRollback}
              dragCommit={dragCommit}
            />
          </ErrorBoundary>
        </div>

        {/* 画布草稿条：位移与新增组件都只暂存在前端，
            点击【保存布局】时才合并成一次 BATCH_LAYOUT 写入代码。 */}
        {(layoutDraftCount > 0 || pendingAdditions.length > 0) && (
          <div
            className={`layout-draft-bar has-additions ${
              layoutSaving ? 'is-saving' : ''
            }`}
            role="status"
            aria-live="polite"
            data-layout-draft-bar
          >
            <div className="layout-draft-copy">
              {layoutSaving ? (
                <span className="mini-spinner" aria-hidden="true" />
              ) : (
                <span className="layout-draft-dot" aria-hidden="true" />
              )}
              <span className="layout-draft-text">
                <strong>
                  {layoutSaving
                    ? '正在同步画布变更...'
                    : `📝 当前有 ${
                        layoutDraftCount + pendingAdditions.length
                      } 个未保存的组件变更`}
                </strong>
                <span>
                  {layoutSaving
                    ? 'Agent 正在生成合并补丁，请稍候'
                    : '保存后一次性写入代码，可继续在画布上调整'}
                </span>
              </span>
            </div>
            {pendingAdditions.length > 0 && (
              <ul className="layout-draft-additions">
                {pendingAdditions.map((item) => (
                  <li key={item.annotationId}>
                    <span className="layout-draft-addition-label">
                      {item.componentLabel || '新增组件'}
                    </span>
                    <span className="layout-draft-addition-intent">
                      {item.intent}
                    </span>
                    <button
                      type="button"
                      className="layout-draft-addition-remove"
                      title="移除这个暂存组件"
                      aria-label={`移除暂存组件：${item.intent}`}
                      onClick={() => onRemovePendingAddition?.(item.annotationId)}
                      disabled={layoutSaving}
                    >
                      ×
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <div className="layout-draft-actions">
              <button
                type="button"
                className="layout-draft-undo"
                onClick={onUndoLayout}
                disabled={layoutSaving}
              >
                撤销
              </button>
              <button
                type="button"
                className="layout-draft-save"
                onClick={onSaveLayout}
                disabled={layoutSaving}
              >
                {layoutSaving ? '同步中…' : '保存布局'}
              </button>
            </div>
          </div>
        )}

        {dragSyncActive &&
          layoutDraftCount === 0 &&
          pendingAdditions.length === 0 && (
            <div className="canvas-sync-hint" role="status" aria-live="polite">
              <span className="mini-spinner" aria-hidden="true" />
              代码同步中...
            </div>
          )}
      </div>

      {architectState?.open && (
        <ArchitectPanel
          projectName={project.name}
          state={architectState}
          onClose={onArchitectClose}
          onStart={onArchitectStart}
          onRestart={onArchitectRestart}
          onAnswer={onArchitectAnswer}
          onDecide={onArchitectDecide}
          onAskHelper={onArchitectAskHelper}
          onCancel={onArchitectCancel}
          onFullstackGenerate={onFullstackGenerate}
          onFrontendBootstrap={onFrontendBootstrap}
          onExport={onArchitectExport}
          exporting={exporting}
          autoBootstrap={project.bootstrap !== 'ready'}
          gate={supervisorGate}
          onReleaseGate={onReleaseGate}
        />
      )}
    </section>
  )
}

/**
 * 每个项目窗口拥有独立的视口状态容器：
 *  - 画布与预览各自保存 zoom / panX / panY；
 *  - 切换对比模式面板只改 CSS 可见性，状态不会重置；
 *  - 展示模式下可开启视口联动，两边共享同一组变换。
 */
function ProjectWorkspace(props) {
  return (
    <ViewportProvider>
      <ProjectWorkspaceInner {...props} />
    </ViewportProvider>
  )
}

// 页面级 runStates 更新时，只有受影响项目的 run 引用会变化；
// 其余 Tab 通过 memo 直接跳过整棵子树（画布、预览、标注浮层）的重渲染。
export default memo(ProjectWorkspace)
