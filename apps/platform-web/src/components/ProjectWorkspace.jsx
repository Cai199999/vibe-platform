import { useState } from 'react'
import CanvasStage from './CanvasStage.jsx'
import RuntimePanel from './RuntimePanel.jsx'
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
  onAnnotationRemove,
  onAnnotationSend,
  onAnnotationRollback,
  onClearAnnotations,
  onSubmit,
  onRetry,
  onExport,
  canExport = false,
  exporting = false,
}) {
  const { linked, setLinked } = useViewportState()
  // 当前布局：compare = 单屏切换，dual = 双屏同显。
  const [viewMode, setViewMode] = useState('compare')
  // 仅对比模式生效：当前显示画布还是预览。
  const [mode, setMode] = useState('canvas')
  const annotations = run.annotations
  const submitting = run.submitting
  const previewReady = Boolean(run.result?.previewUrl)
  const canvasVisible = viewMode === 'dual' || mode === 'canvas'
  const previewVisible = viewMode === 'dual' || mode === 'preview'
  // 只有每个标注都写了指令才允许提交，避免静默丢标注。
  const submitReady =
    annotations.length > 0 &&
    annotations.every((annotation) => annotation.intent.trim().length > 0)
  const status = submitting
    ? 'running'
    : run.error
      ? 'error'
      : run.result
        ? 'success'
        : 'idle'
  const targetLabels = annotations
    .map((annotation) => annotation.target?.label)
    .filter(Boolean)
    .join('、')

  return (
    <section
      className={`tab-window ${active ? 'is-active' : 'is-hidden'}`}
      aria-hidden={!active}
    >
      <div className="tab-window-head">
        <div className="tab-window-title">
          <span className="eyebrow">Workspace · {project.template}</span>
          <h2>{project.name}</h2>
          <p>沿画布圈选组件，浮层内下达修改意图，Agent 在隔离沙盒中执行。</p>
        </div>
      </div>

      <div className="project-toolbar">
        <div className="project-toolbar-left">
          <code className="source-path">src/App.jsx</code>
          <span className={`selection-state ${annotations.length ? 'is-ready' : ''}`}>
            {annotations.length
              ? `已圈选 ${annotations.length}/3 个标注${targetLabels ? ` · ${targetLabels}` : ''}`
              : '尚未圈选组件'}
          </span>
          <label className="model-picker" title="auto：配置了 DeepSeek Key 时自动使用真实模型，否则使用 Mock">
            <span>模型</span>
            <select
              value={modelProvider}
              disabled={submitting}
              onChange={(event) => onModelProviderChange?.(event.target.value)}
            >
              <option value="auto">自动（有 Key 用 DeepSeek）</option>
              <option value="deepseek">DeepSeek</option>
              <option value="mock">Mock（离线演示）</option>
            </select>
          </label>
        </div>

        <div className="project-toolbar-right">
          <button
            className="ghost-button"
            type="button"
            disabled={annotations.length === 0 || submitting}
            onClick={onClearAnnotations}
          >
            清除标注
          </button>

          <button
            className="submit-annotations"
            type="button"
            disabled={!submitReady || submitting}
            title={
              submitReady
                ? `一次性提交 ${annotations.length} 个标注，Agent 将合并处理`
                : annotations.length === 0
                  ? '请先在画布上圈选组件'
                  : '请为每个标注填写修改意图；也可直接在单个浮层内发送'
            }
            onClick={onSubmit}
          >
            {submitting && !run.pendingAnnotationId ? (
              <>
                <span className="mini-spinner" aria-label="执行中" />
                {run.pendingAction === 'rollback' ? '回滚中…' : '执行中…'}
              </>
            ) : (
              `批量提交 (${annotations.length}/3)`
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

      <div className={`project-viewport view-${viewMode}`}>
        <div
          className={`viewport-pane canvas-pane ${canvasVisible ? 'is-visible' : 'is-hidden'}`}
        >
          <CanvasStage
            annotations={annotations}
            onAnnotationAdd={onAnnotationAdd}
            onAnnotationIntentChange={onAnnotationIntentChange}
            onAnnotationRemove={onAnnotationRemove}
            onAnnotationSend={onAnnotationSend}
            onAnnotationRollback={onAnnotationRollback}
            pendingAnnotationId={run.pendingAnnotationId}
            pendingAction={run.pendingAction}
            submitting={submitting}
            disabled={submitting}
          />

          {previewReady && viewMode === 'compare' && mode === 'canvas' && (
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
          />
        </div>
      </div>

      <p className="footnote">
        同步链路：platform-web → <code>/api/v1/runs</code> → Java BFF → python-ai → Docker 沙盒。
        一次最多提交 3 个标注，Planner / Coder 会合并成一次代码修改；首次执行创建
        Vite 沙盒通常需要 20–60 秒，关闭 Tab 会销毁对应沙盒。
      </p>
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
