/**
 * Direct Ops HITL 决策面板。
 *
 * 与 RuntimePanel 的 DecisionPanel 共用同一套 decision-option 样式与
 * “只回传 optionId”的约定；区别是这里的选项来自 ScopePolicy，不需要
 * 轮次 / 契约等 Agent 元数据。
 */
export default function DirectOpsDecisionDialog({
  decision,
  busy = false,
  onChoose,
  onClose,
}) {
  if (!decision) return null
  const options = decision.options || []
  const cancelOption = options.find((option) => option.action === 'CANCEL')

  const close = () => {
    if (busy) return
    if (cancelOption) {
      onChoose?.(cancelOption.optionId)
    } else {
      onClose?.()
    }
  }

  return (
    <div className="dialog-backdrop" role="presentation">
      <div
        className="account-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="direct-decision-title"
      >
        <div className="dialog-head">
          <div>
            <span className="eyebrow">Direct Ops</span>
            <h2 id="direct-decision-title">需要你选择安全路径</h2>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭"
            disabled={busy}
            onClick={close}
          >
            ×
          </button>
        </div>

        <p className="muted">
          {(decision.reasons || []).join('；') ||
            '该节点不能直接删除，请选择处理方式。'}
        </p>

        {decision.anchor?.file && (
          <p className="decision-target">
            <code>{decision.anchor.file}</code>
            {decision.source?.line ? (
              <span className="muted"> · 第 {decision.source.line} 行</span>
            ) : null}
            {decision.anchor?.component ? (
              <span className="muted"> · {decision.anchor.component}</span>
            ) : null}
          </p>
        )}

        <div className="decision-options">
          {options.map((option) => (
            <button
              key={option.optionId}
              type="button"
              className={`decision-option ${
                option.action === 'CANCEL' ? 'is-cancel' : ''
              }`}
              disabled={busy}
              onClick={() => onChoose?.(option.optionId)}
            >
              <span className="decision-option-label">{option.label}</span>
              {option.description && (
                <span className="decision-option-desc">
                  {option.description}
                </span>
              )}
            </button>
          ))}
        </div>

        <p className="muted">
          “仅隐藏视觉”不会修改代码或提交 Git，刷新页面后会恢复。
        </p>
      </div>
    </div>
  )
}
