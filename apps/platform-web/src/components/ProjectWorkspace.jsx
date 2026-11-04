import CanvasStage from './CanvasStage.jsx'
import RuntimePanel from './RuntimePanel.jsx'

/**
 * 单个项目窗口：左侧标注画布，右侧该项目的沙盒实时预览。
 * 所有 Tab 常驻挂载（非激活时仅隐藏），以便保留画布状态与 iframe 预览，切换不闪烁。
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
  const annotation = run.annotation
  const submitting = run.submitting

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

        <div className="tab-window-actions">
          <span className={`selection-state ${annotation ? 'is-ready' : ''}`}>
            {annotation
              ? `已圈选 ${(annotation.width * 100).toFixed(0)}% × ${(annotation.height * 100).toFixed(0)}%`
              : '尚未圈选组件'}
          </span>
          <button
            className="ghost-button"
            type="button"
            disabled={!annotation || submitting}
            onClick={onClearAnnotation}
          >
            清除标注
          </button>
        </div>
      </div>

      <div className="tab-grid">
        <div className="tab-canvas">
          <CanvasStage
            annotation={annotation}
            intent={run.intent}
            onIntentChange={onIntentChange}
            onAnnotationChange={onAnnotationChange}
            onSubmit={onSubmit}
            submitting={submitting}
            disabled={submitting}
          />

          <div className="studio-status">
            <span className="chip chip-muted">Mock LLM · fail_first</span>
            <span className="chip chip-muted">src/App.jsx</span>
            <span className="studio-tip">圈选后输入意图，Enter 发送</span>
          </div>
        </div>

        <RuntimePanel run={run} projectName={project.name} />
      </div>

      <p className="footnote">
        同步链路：platform-web → <code>/api/v1/runs</code> → Java BFF → python-ai → Docker 沙盒。
        首次执行会创建 Vite 沙盒，通常需要 20–60 秒。
      </p>
    </section>
  )
}
