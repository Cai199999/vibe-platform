import { useState } from 'react'
import CanvasStage from './CanvasStage.jsx'
import RuntimePanel from './RuntimePanel.jsx'

/**
 * 单个项目窗口：单视口 + 顶部「画布编辑 / 实时预览」切换。
 *
 * 两种视图同时挂载，仅通过 display:none 切换：
 *  - CanvasStage 的圈选、浮层和 ResizeObserver 状态不会因切换而丢失；
 *  - iframe 一旦加载便保持存活，切回预览时不需要重新冷启动。
 * 外层 .project-viewport 使用固定高度，切换视图不会引起页面跳动。
 */
export default function ProjectWorkspace({
  project,
  run,
  active,
  onAnnotationChange,
  onIntentChange,
  onClearAnnotation,
  onSubmit,
}) {
  const [mode, setMode] = useState('canvas')
  const annotation = run.annotation
  const submitting = run.submitting
  const previewReady = Boolean(run.result?.previewUrl)
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

          <div className="segmented" role="group" aria-label="项目视图切换">
            <button
              type="button"
              className={mode === 'canvas' ? 'is-active' : ''}
              aria-pressed={mode === 'canvas'}
              onClick={() => setMode('canvas')}
            >
              画布编辑
            </button>
            <button
              type="button"
              className={mode === 'preview' ? 'is-active' : ''}
              aria-pressed={mode === 'preview'}
              onClick={() => setMode('preview')}
            >
              <span className={`seg-status seg-status-${status}`} aria-hidden="true" />
              实时预览
            </button>
          </div>
        </div>
      </div>

      <div className={`project-viewport mode-${mode}`}>
        <div className={`viewport-pane canvas-pane ${mode === 'canvas' ? 'is-visible' : 'is-hidden'}`}>
          <CanvasStage
            annotation={annotation}
            intent={run.intent}
            onIntentChange={onIntentChange}
            onAnnotationChange={onAnnotationChange}
            onSubmit={onSubmit}
            submitting={submitting}
            disabled={submitting}
          />

          {previewReady && mode === 'canvas' && (
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

        <div className={`viewport-pane preview-pane ${mode === 'preview' ? 'is-visible' : 'is-hidden'}`}>
          <RuntimePanel run={run} projectName={project.name} expanded />
        </div>
      </div>

      <p className="footnote">
        同步链路：platform-web → <code>/api/v1/runs</code> → Java BFF → python-ai → Docker 沙盒。
        首次执行会创建 Vite 沙盒，通常需要 20–60 秒；关闭 Tab 会销毁对应沙盒。
      </p>
    </section>
  )
}
