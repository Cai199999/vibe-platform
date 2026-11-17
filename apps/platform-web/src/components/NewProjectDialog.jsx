/**
 * 新建项目强制分流。
 *
 * 用户必须先明确选择：
 *   - 从零开始生成：进入 NEW_PROJECT / 虚拟树 / CodeGen；
 *   - 导入源码分析：进入 EXISTING_SOURCE / 只读投影 / 源码直改。
 *
 * 不在创建后让 Architect 猜测项目类型。
 */

const SOURCE_ACCEPT = '.jsx,.tsx,.js,.ts'

export default function NewProjectDialog({
  open,
  onClose,
  onCreateBlank,
  onImportSource,
  busy = false,
  error = '',
}) {
  if (!open) return null

  return (
    <div className="snapshot-modal-backdrop" role="dialog" aria-modal="true">
      <div className="snapshot-modal new-project-dialog">
        <div className="snapshot-modal-head">
          <h2>新建项目</h2>
          <button
            type="button"
            className="ghost-button"
            onClick={onClose}
            disabled={busy}
          >
            关闭
          </button>
        </div>
        <p className="muted">
          请先选择项目来源。两种模式的 Architect 上下文与代码生成策略完全不同。
        </p>

        <div className="new-project-options">
          <section className="new-project-option" data-project-mode="new">
            <span className="new-project-option-mark" aria-hidden="true">
              ✦
            </span>
            <div>
              <h3>从零开始生成</h3>
              <p>
                通过 Architect 澄清生成 VisualDocument 虚拟原型；
                冻结后由 CodeGen 编译为 React 源码。
              </p>
              <button
                type="button"
                className="primary-button"
                disabled={busy}
                onClick={onCreateBlank}
              >
                从零开始
              </button>
            </div>
          </section>

          <section
            className="new-project-option"
            data-project-mode="existing"
          >
            <span className="new-project-option-mark" aria-hidden="true">
              ◇
            </span>
            <div>
              <h3>导入源码分析</h3>
              <p>
                导入已有 React 文件，生成只读投影。源码始终是唯一事实源，
                不转虚拟树、不走 CodeGen；后续编辑走 AST 直改。
              </p>
              <label className="primary-button new-project-import">
                {busy ? '正在分析…' : '选择 .jsx / .tsx 文件'}
                <input
                  type="file"
                  accept={SOURCE_ACCEPT}
                  hidden
                  disabled={busy}
                  onChange={(event) => {
                    const file = event.target.files?.[0]
                    event.target.value = ''
                    if (file) onImportSource?.(file)
                  }}
                />
              </label>
            </div>
          </section>
        </div>

        {error && (
          <p className="new-project-error" role="alert">
            {error}
          </p>
        )}
      </div>
    </div>
  )
}
