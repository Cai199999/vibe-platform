import { useState } from 'react'
import {
  CREATE_NEW,
  MODIFY_EXISTING,
  isCreateAction,
} from '../workspace/annotationActions.js'

/**
 * Inspect 模式命中组件后的标注输入卡片。
 *
 * 与画布圈选浮层不同，这里直接展示 SDK 回传的源码文件 / 行号 / 组件名，
 * 用户只需要补充修改意图。确认后由 ProjectWorkspace 生成一份带
 * inspectSource 的标注草稿，最终进入标准 AnnotationEnvelope。
 *
 * 顶部悬浮工具栏（➕ ❌ ✏️）走 Direct Ops：不调用 LLM，由后端 AST
 * 确定性写入。Phase 1 只有 ✏️ 真正落盘，➕ / ❌ 先发 dryRun 验证契约。
 */
export default function InspectDraftCard({
  draft,
  onChange,
  onCancel,
  onSubmit,
  onDirectAction,
}) {
  const [textOpen, setTextOpen] = useState(false)
  const [directText, setDirectText] = useState('')
  const [directBusy, setDirectBusy] = useState(false)
  const [directHint, setDirectHint] = useState('')
  if (!draft) return null
  const source = draft.source ?? {}

  const runDirect = async (
    action,
    payload,
    dryRun = false,
    targetOverride = source,
  ) => {
    if (!onDirectAction || directBusy) return
    setDirectBusy(true)
    setDirectHint('')
    try {
      const result = await onDirectAction({
        action,
        dryRun,
        target: targetOverride,
        payload,
      })
      if (!result) return
      if (result.status === 'DRY_RUN') {
        setDirectHint(
          `dryRun 通过：${result.risk} · ${
            result.resolvedTarget?.wouldChange === false
              ? '内容无变化'
              : '可以执行'
          }`,
        )
      } else if (result.status === 'SUCCEEDED') {
        setDirectHint(`已保存 · ${result.latencyMs}ms`)
        setTextOpen(false)
      }
    } catch (cause) {
      setDirectHint(`操作失败：${cause?.message || cause}`)
    } finally {
      setDirectBusy(false)
    }
  }
  const container = draft.container?.source
  const ready = Boolean(draft.intent?.trim())
  const fileLabel = source.file || '未知文件'
  const location = source.file
    ? `${source.file}:${source.line || 1}${
        source.column ? `:${source.column}` : ''
      }`
    : ''
  // CREATE_NEW 时插入父级可能与点击元素不同，卡片必须显式展示真正的落点。
  const creating = isCreateAction(draft.action)
  const addTargetFile =
    creating && container?.file
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

      <div
        className="inspect-direct-toolbar"
        role="toolbar"
        aria-label="直接操作"
        data-direct-toolbar
      >
        <button
          type="button"
          title="新增（Phase 0 dryRun）"
          disabled={directBusy}
          onClick={() =>
            runDirect(
              'ADD_COMPONENT',
              { componentType: 'Button' },
              true,
            )
          }
        >
          ➕
        </button>
        <button
          type="button"
          title="删除（Phase 0 dryRun）"
          disabled={directBusy}
          onClick={() => runDirect('DELETE_NODE', {}, true)}
        >
          ❌
        </button>
        <button
          type="button"
          className={textOpen ? 'is-active' : ''}
          title="直接修改文字"
          disabled={directBusy}
          onClick={() => {
            setTextOpen((previous) => !previous)
            setDirectHint('')
          }}
        >
          ✏️
        </button>
        <button
          type="button"
          title="移动到当前容器末尾"
          disabled={directBusy || !draft.container?.source}
          onClick={() => {
            const container = draft.container?.source
            if (!container) return
            runDirect(
              'MOVE_NODE',
              {},
              false,
              {
                ...source,
                placement: {
                  parentFile: container.file,
                  parentSelector: container.selector,
                  parentLine: container.line,
                  parentColumn: container.column,
                  insertPosition: 'inside_end',
                  siblingIndex: null,
                },
              },
            )
          }}
        >
          ↕
        </button>
        {directHint && (
          <span className="inspect-direct-hint" data-direct-hint>
            {directHint}
          </span>
        )}
      </div>

      {textOpen && (
        <div className="inspect-direct-text" data-direct-text-editor>
          <input
            type="text"
            maxLength={2000}
            value={directText}
            placeholder="输入新的文案，例如：立即体验"
            onChange={(event) => setDirectText(event.target.value)}
          />
          <button
            type="button"
            className="primary-button"
            disabled={directBusy || !directText.trim()}
            onClick={() =>
              runDirect('UPDATE_TEXT', { text: directText.trim() }, false)
            }
          >
            直接保存
          </button>
        </div>
      )}

      <div className="inspect-draft-actions" role="group" aria-label="操作类型">
        <button
          type="button"
          className={!creating ? 'is-active' : ''}
          aria-pressed={!creating}
          onClick={() => onChange?.({ action: MODIFY_EXISTING })}
        >
          修改此组件
        </button>
        <button
          type="button"
          className={creating ? 'is-active' : ''}
          aria-pressed={creating}
          onClick={() => onChange?.({ action: CREATE_NEW })}
        >
          在容器内新增
        </button>
      </div>

      {creating && (
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
          creating
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
