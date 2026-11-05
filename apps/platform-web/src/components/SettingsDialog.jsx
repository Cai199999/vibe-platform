import { useEffect } from 'react'
import { useSettings } from '../settings/SettingsContext.jsx'

/**
 * 用户设置弹窗。
 *
 * 当前只包含「代码生成后需人工审批」开关；开关值由 SettingsContext
 * 统一管理并持久化到 localStorage，Run 请求组装时直接读取。
 */
export default function SettingsDialog({ open, onClose }) {
  const { requireManualApproval, setRequireManualApproval } = useSettings()

  useEffect(() => {
    if (!open) return undefined
    const handleKey = (event) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [open, onClose])

  if (!open) return null

  const toggle = () => setRequireManualApproval(!requireManualApproval)

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <div
        className="account-dialog settings-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-dialog-title"
        data-settings-dialog
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="dialog-head">
          <div>
            <span className="eyebrow">Settings</span>
            <h2 id="settings-dialog-title">设置</h2>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭">
            ×
          </button>
        </div>

        <div className="settings-list">
          <div className="settings-row">
            <div className="settings-copy">
              <strong>代码生成后需人工审批</strong>
              <p>
                开启后，构建验证通过会暂停并展示逐文件 Diff，批准后才提交 Git
                checkpoint。关闭时验证通过即自动完成。
              </p>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={requireManualApproval}
              aria-label="代码生成后需人工审批"
              data-settings-toggle="requireManualApproval"
              className={`settings-switch ${requireManualApproval ? 'is-on' : ''}`}
              onClick={toggle}
            >
              <span className="settings-switch-thumb" aria-hidden="true" />
            </button>
          </div>
        </div>

        <p className="settings-note">
          当前状态：{requireManualApproval ? '已开启人工审批' : '已关闭，验证通过后直接完成'}
          。设置保存在本浏览器 localStorage，对后续新发起的 Run 生效。
        </p>

        <div className="dialog-actions">
          <button className="primary-button" type="button" onClick={onClose}>
            完成
          </button>
        </div>
      </div>
    </div>
  )
}
