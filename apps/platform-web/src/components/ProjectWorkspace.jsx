import { useEffect, useState } from 'react'
import AgentCatalogMenu from './AgentCatalogMenu.jsx'
import ArchitectPanel from './ArchitectPanel.jsx'
import CanvasStage from './CanvasStage.jsx'
import RuntimePanel from './RuntimePanel.jsx'
import SupervisorTrace from './SupervisorTrace.jsx'
import {
  ViewportProvider,
  useViewportState,
} from '../workspace/ViewportContext.jsx'

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
  onAnnotationSend,
  onClarificationAnswer,
  onAnnotationRollback,
  onClearAnnotations,
  onApprovalDecision,
  onCancelRun,
  onSubmit,
  onRetry,
  onExport,
  canExport = false,
  exporting = false,
  architectState,
  supervisorGate,
  agentCatalog,
  onRefreshAgents,
  previewFocusNonce = 0,
  onArchitectOpen,
  onArchitectStart,
  onArchitectClose,
  onArchitectAnswer,
  onArchitectDecide,
  onArchitectCancel,
  onReleaseGate,
}) {
  const { linked, setLinked } = useViewportState()
  // 当前布局：compare = 单屏切换，dual = 双屏同显。
  const [viewMode, setViewMode] = useState('compare')
  // 仅对比模式生效：当前显示画布还是预览。
  const [mode, setMode] = useState('canvas')

  // Supervisor 派发 UI Run（或门控释放）后，自动露出右侧执行时间轴。
  // 使用递增 nonce 而不是布尔值，保证连续两次 handoff 都能重新聚焦。
  useEffect(() => {
    if (previewFocusNonce > 0) setMode('preview')
  }, [previewFocusNonce])
  const annotations = run.annotations
  const submitting = run.submitting
  const previewReady = Boolean(run.result?.previewUrl)
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

  return (
    <section
      className={`tab-window ${active ? 'is-active' : 'is-hidden'}`}
      aria-hidden={!active}
    >
      <div className="project-toolbar">
        <div className="project-toolbar-left">
          {annotations.length > 0 && (
            <span className="selection-state is-ready">
              {annotations.length} 个标注
            </span>
          )}
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
              <option value="mock">本地预览</option>
            </select>
          </label>
        </div>

        <div className="project-toolbar-right">
          <AgentCatalogMenu
            agents={agentCatalog?.agents ?? []}
            loading={Boolean(agentCatalog?.loading)}
            error={agentCatalog?.error ?? null}
            onRefresh={onRefreshAgents}
          />
          {gateActive && (
            <span className="supervisor-gate-chip" role="status">
              <span className="mini-spinner" aria-hidden="true" />
              {gateStatusText}
            </span>
          )}
          <button
            className={`architect-launch ${
              architectState?.contract ? 'has-contract' : ''
            } ${architectState?.status === 'AWAITING_CLARIFICATION' ? 'needs-answer' : ''}`}
            type="button"
            title="基于当前沙盒源码，澄清应用形态并生成可实施的全栈契约"
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
            className="ghost-button"
            type="button"
            disabled={annotations.length === 0 || submitting || approvalPending}
            onClick={onClearAnnotations}
          >
            清除标注
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

          <div className="segmented view-mode-switch" role="group" aria-label="视图模式">
            <button
              type="button"
              className={viewMode === 'compare' ? 'is-active' : ''}
              aria-pressed={viewMode === 'compare'}
              onClick={() => setViewMode('compare')}
            >
              对比模式
            </button>
            <button
              type="button"
              className={viewMode === 'dual' ? 'is-active' : ''}
              aria-pressed={viewMode === 'dual'}
              onClick={() => setViewMode('dual')}
            >
              展示模式
            </button>
          </div>

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
            className={`export-button ${exporting ? 'is-busy' : ''}`}
            disabled={!canExport || exporting}
            title={
              canExport
                ? '下载当前项目的 tar.gz（排除 node_modules / .git / dist）'
                : '请先执行一次 Agent，生成可导出的沙盒项目'
            }
            onClick={onExport}
          >
            <span aria-hidden="true">📥</span>
            {exporting ? '导出中…' : '导出代码'}
          </button>
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
          <CanvasStage
            annotations={annotations}
            onAnnotationAdd={onAnnotationAdd}
            onAnnotationIntentChange={onAnnotationIntentChange}
            onAnnotationActionChange={onAnnotationActionChange}
            onAnnotationRemove={onAnnotationRemove}
            onAnnotationSend={onAnnotationSend}
            onClarificationAnswer={onClarificationAnswer}
            clarification={run.clarification}
            onAnnotationRollback={onAnnotationRollback}
            pendingAnnotationId={run.pendingAnnotationId}
            pendingAction={run.pendingAction}
            submitting={submitting}
            disabled={submitting}
            blocked={approvalPending || gateActive}
          />

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
          <RuntimePanel
            run={run}
            projectName={project.name}
            expanded
            visible={active && previewVisible}
            onRetry={submitReady ? onRetry : undefined}
            onGoToCanvas={() => setMode('canvas')}
            onApprovalDecision={onApprovalDecision}
            onCancelRun={onCancelRun}
          />
        </div>
      </div>

      {architectState?.open && (
        <ArchitectPanel
          projectName={project.name}
          state={architectState}
          onClose={onArchitectClose}
          onStart={onArchitectStart}
          onRestart={onArchitectStart}
          onAnswer={onArchitectAnswer}
          onDecide={onArchitectDecide}
          onCancel={onArchitectCancel}
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
export default function ProjectWorkspace(props) {
  return (
    <ViewportProvider>
      <ProjectWorkspaceInner {...props} />
    </ViewportProvider>
  )
}
