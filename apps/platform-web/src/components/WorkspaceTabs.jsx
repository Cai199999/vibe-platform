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
  pending: '待审批',
  success: '已完成',
  error: '失败',
}

function SnapshotTab({ tab, active, onSelect, onClose }) {
  return (
    <div
      className={`workspace-tab snapshot-tab ${active ? 'is-active' : ''}`}
      role="tab"
      aria-label={`快照 ${tab.message}`}
      aria-selected={active}
      tabIndex={active ? 0 : -1}
      data-snapshot-tab-id={tab.id}
      title={`${tab.message} · ${tab.shortHash}`}
      onClick={() => onSelect(tab.id)}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onSelect(tab.id)
        }
      }}
    >
      <span className="tab-status tab-status-snapshot" aria-hidden="true">
        🕘
      </span>
      <span className="tab-name">[快照] {tab.message}</span>
      <button
        className="tab-close"
        type="button"
        aria-label={`关闭快照 ${tab.message}`}
        onPointerDown={(event) => event.stopPropagation()}
        onClick={(event) => {
          event.stopPropagation()
          onClose(tab.id)
        }}
      >
        ×
      </button>
    </div>
  )
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
  snapshotTabs = [],
  activeSnapshotId = null,
  onSelect,
  onSelectSnapshot,
  onCloseSnapshot,
  onClose,
  onReorder,
  onNewProject,
  onOpenHistory,
  historyAvailable = false,
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
      <div className="workspace-tab-list" role="tablist" aria-label="项目窗口">
        <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
          <SortableContext items={openTabs} strategy={horizontalListSortingStrategy}>
            {openTabs.map((projectId) => {
              const project = projectMap.get(projectId)
              if (!project) return null
              return (
                <SortableTab
                  key={projectId}
                  project={project}
                  active={activeSnapshotId == null && activeTabId === projectId}
                  status={statuses[projectId] ?? 'idle'}
                  onSelect={onSelect}
                  onClose={onClose}
                />
              )
            })}
          </SortableContext>
        </DndContext>

        {/* 快照是虚拟只读标签：不参与项目 Tab 的拖拽排序与数量上限。 */}
        {snapshotTabs.map((tab) => (
          <SnapshotTab
            key={tab.id}
            tab={tab}
            active={activeSnapshotId === tab.id}
            onSelect={onSelectSnapshot}
            onClose={onCloseSnapshot}
          />
        ))}
      </div>

      <button
        className="tab-history"
        type="button"
        data-snapshot-history-toggle
        disabled={!historyAvailable}
        title={
          historyAvailable
            ? '查看当前项目的版本历史'
            : '请先执行一次 Agent，生成可快照的沙盒'
        }
        onClick={onOpenHistory}
      >
        <span aria-hidden="true">🕘</span>
        版本历史
      </button>

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
