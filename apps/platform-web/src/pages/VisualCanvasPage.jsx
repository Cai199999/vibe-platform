import { useSearchParams } from 'react-router-dom'
import OfflineVisualCanvas from '../visual/OfflineVisualCanvas.jsx'

/**
 * 离线虚拟画布入口。
 *
 * 该页面不需要沙盒、不需要真实源码，也不需要后端运行；文档自动保存
 * 在浏览器 localStorage 中。后续 Architect 会直接写入同一份文档。
 */
export default function VisualCanvasPage() {
  const [params] = useSearchParams()
  const projectId = params.get('project') || ''
  const title = params.get('title') || '未命名页面'
  return (
    <div className="visual-canvas-page">
      <OfflineVisualCanvas projectId={projectId} title={title} />
    </div>
  )
}
