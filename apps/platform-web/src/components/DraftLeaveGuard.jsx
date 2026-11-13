/**
 * 未保存画布草稿的离开守卫。
 *
 * 关闭项目 Tab / 退出登录 / 刷新页面时复用同一套文案与交互：
 *  - 取消：什么都不做，草稿、画布与视口状态原样保留；
 *  - 确认：由调用方负责清理 IndexedDB 草稿，再执行关闭 / 退出。
 *
 * 刷新页面无法自定义浏览器原生提示，因此 WorkspacePage 会额外监听
 * beforeunload + pagehide，并在同步 localStorage 中写墓碑。
 */
export default function DraftLeaveGuard({
  open,
  actionText = '关闭',
  confirmText,
  busy = false,
  onCancel,
  onConfirm,
}) {
  if (!open) return null

  return (
    <div
      className="snapshot-modal-backdrop draft-guard-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onCancel?.()
      }}
    >
      <div
        className="snapshot-modal draft-guard-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="draft-guard-title"
        data-draft-guard
      >
        <header className="snapshot-modal-head">
          <h2 id="draft-guard-title">⚠️ 有尚未保存的画布修改</h2>
        </header>
        <p className="muted draft-guard-copy">
          建议先点击上方【保存快照】。是否确认放弃并{actionText}？
        </p>
        <p className="draft-guard-detail">
          放弃后本地草稿与未提交的位置调整将无法恢复。
        </p>
        <div className="snapshot-modal-actions">
          <button
            type="button"
            className="ghost-button"
            disabled={busy}
            onClick={onCancel}
          >
            取消
          </button>
          <button
            type="button"
            className="danger-button"
            disabled={busy}
            onClick={onConfirm}
          >
            {busy ? '清理中…' : (confirmText ?? `放弃并${actionText}`)}
          </button>
        </div>
      </div>
    </div>
  )
}
