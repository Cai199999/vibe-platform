import { useEffect, useRef, useState } from 'react'

/**
 * 项目下拉栏：切换项目、新建项目。
 * 导入项目留到 Phase 3，先以禁用项展示，避免入口缺失。
 */
export default function ProjectSwitcher({
  projects,
  activeProject,
  openTabs,
  onOpenProject,
  onNewProject,
}) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef(null)

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
          <small>当前项目</small>
          <strong>{activeProject?.name ?? '选择项目'}</strong>
        </span>
        <span className="account-caret" aria-hidden="true">
          ▾
        </span>
      </button>

      {open && (
        <div className="project-menu" role="menu">
          <div className="project-menu-head">
            <span>工作区项目</span>
            <small>{projects.length} 个</small>
          </div>

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
              onClick={() => {
                onNewProject()
                setOpen(false)
              }}
            >
              <span aria-hidden="true">＋</span>
              新建项目
            </button>
            <button type="button" role="menuitem" disabled title="Phase 3 支持导入 HTML / URL">
              <span aria-hidden="true">↥</span>
              导入项目
              <span className="project-menu-badge">Phase 3</span>
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
