import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
} from '@dnd-kit/core'
import {
  SortableContext,
  horizontalListSortingStrategy,
  sortableKeyboardCoordinates,
  useSortable,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'

const STATUS_LABEL = {
  idle: '未运行',
  running: '执行中',
  success: '已完成',
  error: '失败',
}

function SortableTab({ project, active, status, onSelect, onClose }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: project.id,
  })

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
  }

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`workspace-tab ${active ? 'is-active' : ''} ${isDragging ? 'is-dragging' : ''}`}
      {...attributes}
      {...listeners}
      role="tab"
      aria-label={project.name}
      aria-selected={active}
      tabIndex={active ? 0 : -1}
      onClick={() => onSelect(project.id)}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onSelect(project.id)
        }
      }}
    >
      <span className={`tab-status tab-status-${status}`} title={STATUS_LABEL[status]} />
      <span className="tab-name">{project.name}</span>
      <button
        className="tab-close"
        type="button"
        aria-label={`关闭 ${project.name}`}
        onPointerDown={(event) => event.stopPropagation()}
        onClick={(event) => {
          event.stopPropagation()
          onClose(project.id)
        }}
      >
        ×
      </button>
    </div>
  )
}

/**
 * 可拖拽排序的多 Tab 栏。
 * 拖拽使用 @dnd-kit/core + @dnd-kit/sortable，支持指针与键盘操作。
 */
export default function WorkspaceTabs({
  projects,
  openTabs,
  activeTabId,
  statuses,
  onSelect,
  onClose,
  onReorder,
  onNewProject,
  limitReached,
}) {
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  )

  const projectMap = new Map(projects.map((project) => [project.id, project]))

  const handleDragEnd = ({ active, over }) => {
    if (!over || active.id === over.id) return
    const fromIndex = openTabs.indexOf(active.id)
    const toIndex = openTabs.indexOf(over.id)
    if (fromIndex >= 0 && toIndex >= 0) {
      onReorder(fromIndex, toIndex)
    }
  }

  return (
    <div className="workspace-tabs">
      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
        <SortableContext items={openTabs} strategy={horizontalListSortingStrategy}>
          <div className="workspace-tab-list" role="tablist" aria-label="项目窗口">
            {openTabs.map((projectId) => {
              const project = projectMap.get(projectId)
              if (!project) return null
              return (
                <SortableTab
                  key={projectId}
                  project={project}
                  active={activeTabId === projectId}
                  status={statuses[projectId] ?? 'idle'}
                  onSelect={onSelect}
                  onClose={onClose}
                />
              )
            })}
          </div>
        </SortableContext>
      </DndContext>

      <button
        className="tab-new"
        type="button"
        disabled={limitReached}
        title={limitReached ? '最多只能同时打开 5 个项目，请关闭其他项目' : '新建项目'}
        onClick={onNewProject}
      >
        ＋
      </button>

      {limitReached && <span className="tabs-hint">已达 5 个项目上限</span>}
    </div>
  )
}
