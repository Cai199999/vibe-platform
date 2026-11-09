/**
 * Inspect 模式命中组件后的标注输入卡片。
 *
 * 与画布圈选浮层不同，这里直接展示 SDK 回传的源码文件 / 行号 / 组件名，
 * 用户只需要补充修改意图。确认后由 ProjectWorkspace 生成一份带
 * inspectSource 的标注草稿，最终进入标准 AnnotationEnvelope。
 */
export default function InspectDraftCard({
  draft,
  onChange,
  onCancel,
  onSubmit,
}) {
  if (!draft) return null
  const source = draft.source ?? {}
  const container = draft.container?.source
  const ready = Boolean(draft.intent?.trim())
  const fileLabel = source.file || '未知文件'
  const location = source.file
    ? `${source.file}:${source.line || 1}${
        source.column ? `:${source.column}` : ''
      }`
    : ''
  // ADD 时插入父级可能与点击元素不同，卡片必须显式展示真正的落点。
  const addTargetFile =
    draft.action === 'ADD' && container?.file
      ? `${container.file}:${container.line || 1}`
      : location

  return (
    <div className="inspect-draft" data-inspect-draft role="dialog" aria-label="精准选择标注">
      <div className="inspect-draft-head">
        <div className="inspect-draft-title">
          <span className="inspect-draft-badge">INSPECT</span>
          <strong>精准选择</strong>
        </div>
        <button
          className="inspect-draft-close"
          type="button"
          aria-label="关闭精准选择标注"
          onClick={onCancel}
        >
          ×
        </button>
      </div>

      <div className="inspect-draft-source" data-inspect-source>
        <code>{location || fileLabel}</code>
        <span className="inspect-draft-tag">
          &lt;{draft.tag || 'element'}&gt;
          {source.component ? ` · ${source.component}` : ''}
        </span>
      </div>

      <div className="inspect-draft-actions" role="group" aria-label="操作类型">
        <button
          type="button"
          className={draft.action === 'MODIFY' ? 'is-active' : ''}
          aria-pressed={draft.action === 'MODIFY'}
          onClick={() => onChange?.({ action: 'MODIFY' })}
        >
          修改此组件
        </button>
        <button
          type="button"
          className={draft.action === 'ADD' ? 'is-active' : ''}
          aria-pressed={draft.action === 'ADD'}
          onClick={() => onChange?.({ action: 'ADD' })}
        >
          在容器内新增
        </button>
      </div>

      {draft.action === 'ADD' && (
        <p className="inspect-draft-hint">
          插入父级：<code>{addTargetFile}</code>
        </p>
      )}

      <textarea
        className="inspect-draft-intent"
        data-inspect-intent
        rows={3}
        maxLength={500}
        value={draft.intent || ''}
        placeholder={
          draft.action === 'ADD'
            ? '描述要在这个容器里新增什么，例如：加一个蓝色“立即购买”按钮'
            : '描述要改什么，例如：把按钮背景改成绿色，文字改为立即体验'
        }
        onChange={(event) => onChange?.({ intent: event.target.value })}
      />

      <div className="inspect-draft-footer">
        <span className="inspect-draft-meta">
          {source.sourceKind?.toUpperCase() || 'SOURCE'}
          {draft.role ? ` · ${draft.role}` : ''}
        </span>
        <div className="inspect-draft-buttons">
          <button className="ghost-button" type="button" onClick={onCancel}>
            取消
          </button>
          <button
            className="primary-button"
            type="button"
            disabled={!ready}
            title={ready ? '加入待提交标注' : '请先填写修改意图'}
            onClick={onSubmit}
          >
            添加标注
          </button>
        </div>
      </div>
    </div>
  )
}
