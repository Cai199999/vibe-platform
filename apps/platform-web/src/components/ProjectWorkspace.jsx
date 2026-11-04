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
  onAnnotationChange,
  onIntentChange,
  onClearAnnotation,
  onSubmit,
  onRetry,
}) {
  const { linked, setLinked } = useViewportState()
  // 当前布局：compare = 单屏切换，dual = 双屏同显。
  const [viewMode, setViewMode] = useState('compare')
  // 仅对比模式生效：当前显示画布还是预览。
  const [mode, setMode] = useState('canvas')
  const annotation = run.annotation
  const submitting = run.submitting
  const previewReady = Boolean(run.result?.previewUrl)
  const canvasVisible = viewMode === 'dual' || mode === 'canvas'
  const previewVisible = viewMode === 'dual' || mode === 'preview'
  const status = submitting
    ? 'running'
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
          <span className={`selection-state ${annotation ? 'is-ready' : ''}`}>
            {annotation
              ? `已圈选 ${(annotation.width * 100).toFixed(0)}% × ${(annotation.height * 100).toFixed(0)}%`
              : '尚未圈选组件'}
          </span>
          <span className="chip chip-muted">Mock LLM · fail_first</span>
        </div>

        <div className="project-toolbar-right">
          <button
            className="ghost-button"
            type="button"
            disabled={!annotation || submitting}
            onClick={onClearAnnotation}
          >
            清除标注
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
        </div>
      </div>

      <div className={`project-viewport view-${viewMode}`}>
        <div
          className={`viewport-pane canvas-pane ${canvasVisible ? 'is-visible' : 'is-hidden'}`}
        >
          <CanvasStage
            annotation={annotation}
            intent={run.intent}
            onIntentChange={onIntentChange}
            onAnnotationChange={onAnnotationChange}
            onSubmit={onSubmit}
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
            onRetry={run.lastInstruction ? onRetry : undefined}
          />
        </div>
      </div>

      <p className="footnote">
        同步链路：platform-web → <code>/api/v1/runs</code> → Java BFF → python-ai → Docker 沙盒。
        首次执行会创建 Vite 沙盒，通常需要 20–60 秒；关闭 Tab 会销毁对应沙盒。
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
