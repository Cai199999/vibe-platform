import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext.jsx'
import OfflineVisualCanvas from '../visual/OfflineVisualCanvas.jsx'
import {
  buildSemanticReviewPacket,
  storeSemanticReviewPacket,
} from '../visual/visualReview.js'

/**
 * 离线虚拟画布入口。
 *
 * 该页面不需要沙盒、不需要真实源码，也不需要后端运行；文档自动保存
 * 在浏览器 localStorage 中。冻结后把语义审查包交给 Architect。
 */
export default function VisualCanvasPage() {
  const [params] = useSearchParams()
  const { session } = useAuth()
  const navigate = useNavigate()
  const [handoffError, setHandoffError] = useState('')
  const projectId = params.get('project') || ''
  const title = params.get('title') || '未命名页面'

  const handleFrozen = (document) => {
    setHandoffError('')
    const packet = buildSemanticReviewPacket(document)
    if (!storeSemanticReviewPacket(packet)) {
      setHandoffError('无法保存语义审查包，请检查浏览器存储权限后重试')
      return
    }
    const targetProject = document.projectId || projectId
    navigate(
      `/workspace?visualReview=1&project=${encodeURIComponent(targetProject)}`,
    )
  }

  return (
    <div className="visual-canvas-page">
      {handoffError && (
        <div className="vc-validation-banner is-warning" role="alert">
          <span>{handoffError}</span>
          <button type="button" onClick={() => setHandoffError('')}>
            关闭
          </button>
        </div>
      )}
      <OfflineVisualCanvas
        projectId={projectId}
        title={title}
        token={session?.token}
        onFrozen={handleFrozen}
      />
    </div>
  )
}
