import DiffView from './DiffView.jsx'

/**
 * 虚拟“快照标签页”。
 *
 * 快照不是可编辑的工作区：这里只展示该版本的元信息与代码 Diff，
 * 画布 / 预览 / Agent 输入全部不挂载，从结构上保证只读。
 */
export default function SnapshotWorkspace({ snapshot, onClose, onRestore }) {
  const committedAt = formatTime(snapshot.committedAt)
  return (
    <section
      className="tab-window snapshot-window is-active"
      data-snapshot-tab
      data-commit-hash={snapshot.commitHash}
    >
      <div className="project-toolbar">
        <div className="project-toolbar-left">
          <span className="snapshot-badge">只读快照</span>
          <strong className="snapshot-title" title={snapshot.message}>
            {snapshot.message}
          </strong>
          <span className="snapshot-meta">
            {snapshot.shortHash} · {committedAt}
            {snapshot.projectName ? ` · ${snapshot.projectName}` : ''}
          </span>
        </div>
        <div className="project-toolbar-right">
          {onRestore && (
            <button
              className="ghost-button is-danger"
              type="button"
              disabled={snapshot.loading || snapshot.restoring}
              onClick={() => onRestore(snapshot)}
            >
              {snapshot.restoring ? '恢复中…' : '恢复到此版本'}
            </button>
          )}
          <button className="ghost-button" type="button" onClick={onClose}>
            关闭快照
          </button>
        </div>
      </div>

      <div className="snapshot-body">
        {snapshot.loading && <p className="muted">正在读取快照 Diff…</p>}
        {snapshot.error && (
          <p className="snapshot-error" role="alert">
            快照读取失败：{snapshot.error}
          </p>
        )}
        {!snapshot.loading && !snapshot.error && (
          <DiffView
            patch={snapshot.diff}
            title={`快照 ${snapshot.shortHash} · ${snapshot.message}`}
          />
        )}
        {snapshot.truncated && (
          <p className="muted">Diff 体积过大，已截断展示。</p>
        )}
      </div>
    </section>
  )
}

function formatTime(value) {
  if (!value) return '未知时间'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}
