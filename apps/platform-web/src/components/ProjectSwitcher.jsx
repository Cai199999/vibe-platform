import { useEffect, useRef, useState } from 'react'

/**
 * 项目下拉栏：切换项目、新建项目、导入项目。
 *
 * 导入支持 zip（React / HTML 项目包）、平台导出的全栈 tar.gz 与 png / jpg 截图；真正的解压、
 * npm install 与沙盒启动全部由后端完成，前端只负责选择文件与展示结果。
 */
export default function ProjectSwitcher({
  projects,
  activeProject,
  openTabs,
  limitReached,
  canExport = false,
  exporting = false,
  importing = false,
  onOpenProject,
  onNewProject,
  onImportFile,
  onExport,
}) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef(null)
  const fileInputRef = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    const handlePointer = (event) => {
      if (rootRef.current && !rootRef.current.contains(event.target)) {
        setOpen(false)
      }
    }
    const handleKey = (event) => {
      if (event.key === 'Escape') setOpen(false)
    }
    window.addEventListener('mousedown', handlePointer)
    window.addEventListener('keydown', handleKey)
    return () => {
      window.removeEventListener('mousedown', handlePointer)
      window.removeEventListener('keydown', handleKey)
    }
  }, [open])

  const openSet = new Set(openTabs)

  return (
    <div className="project-switcher" ref={rootRef}>
      <button
        className="project-switcher-button"
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="project-glyph" aria-hidden="true">
          ▦
        </span>
        <span className="project-switcher-text">
          <strong>{activeProject?.name ?? '选择项目'}</strong>
        </span>
        <span className="account-caret" aria-hidden="true">
          ▾
        </span>
      </button>

      {open && (
        <div className="project-menu" role="menu">
          <div className="project-menu-list">
            {projects.map((project) => {
              const isActive = activeProject?.id === project.id
              const isOpen = openSet.has(project.id)
              return (
                <button
                  key={project.id}
                  type="button"
                  role="menuitem"
                  className={`project-menu-item ${isActive ? 'is-active' : ''}`}
                  onClick={() => {
                    onOpenProject(project.id)
                    setOpen(false)
                  }}
                >
                  <span className="project-menu-dot" />
                  <span className="project-menu-name">{project.name}</span>
                  {isOpen && <span className="project-menu-state">已打开</span>}
                  {isActive && <span className="project-menu-check">✓</span>}
                </button>
              )
            })}
          </div>

          <div className="project-menu-actions">
            <button
              type="button"
              role="menuitem"
              disabled={limitReached}
              title={limitReached ? '最多只能同时打开 5 个项目，请关闭其他项目' : undefined}
              onClick={() => {
                onNewProject()
                setOpen(false)
              }}
            >
              <span aria-hidden="true">＋</span>
              新建项目
              {limitReached && <span className="project-menu-badge">5 / 5</span>}
            </button>
            <button
              type="button"
              role="menuitem"
              className="project-menu-import"
              disabled={limitReached || importing}
              title={
                limitReached
                  ? '最多只能同时打开 5 个项目，请关闭其他项目'
                  : '上传 zip / 全栈 tar.gz 或 png / jpg 截图导入项目'
              }
              onClick={() => fileInputRef.current?.click()}
            >
              <span aria-hidden="true">{importing ? '⏳' : '📦'}</span>
              {importing ? '正在导入…' : '导入项目'}
              {limitReached && <span className="project-menu-badge">5 / 5</span>}
            </button>
            <input
              ref={fileInputRef}
              className="project-import-input"
              type="file"
              accept=".zip,.tar.gz,.tgz,.png,.jpg,.jpeg,application/zip,application/gzip,application/x-tar,image/png,image/jpeg"
              hidden
              onChange={(event) => {
                const file = event.target.files?.[0]
                // 允许连续选择同一个文件：先清空 value。
                event.target.value = ''
                if (!file) return
                setOpen(false)
                onImportFile?.(file)
              }}
            />
            <button
              type="button"
              role="menuitem"
              className="project-menu-export"
              disabled={!activeProject || !canExport || exporting}
              title={
                canExport
                  ? '下载当前项目的 tar.gz 源码包'
                  : '当前项目还没有可导出的沙盒，请先执行一次 Agent'
              }
              onClick={() => {
                if (!activeProject) return
                onExport?.(activeProject.id)
                setOpen(false)
              }}
            >
              <span aria-hidden="true">📥</span>
              {exporting ? '正在导出…' : '导出当前项目'}
            </button>
          </div>

          {limitReached && (
            <p className="project-menu-hint">
              最多只能同时打开 5 个项目，请关闭其他项目后再新建。
            </p>
          )}
        </div>
      )}
    </div>
  )
}
