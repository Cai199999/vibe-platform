import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext.jsx'
import OfflineVisualCanvas from '../visual/OfflineVisualCanvas.jsx'
import ReadOnlySourceCanvas from '../visual/ReadOnlySourceCanvas.jsx'
import {
  buildSourceProjection,
  SourceProjectionError,
} from '../visual/sourceProjection.js'
import { analyzeExistingReact } from '../visual/sourceAnalysisClient.js'
import {
  buildSemanticReviewPacket,
  storeSemanticReviewPacket,
} from '../visual/visualReview.js'

const MAX_SOURCE_BYTES = 500 * 1024

/**
 * 离线虚拟画布入口。
 *
 * 默认是轨道一：本地 VisualDocument 编辑。
 * 通过顶部“导入源码分析”切换到轨道二 MVP：把已有 React 文件解析为
 * 只读投影，仅用于查看结构，不写回源码、不写入 visualDocumentStore。
 */
export default function VisualCanvasPage() {
  const [params] = useSearchParams()
  const { session } = useAuth()
  const navigate = useNavigate()
  const [handoffError, setHandoffError] = useState('')
  const [analysisError, setAnalysisError] = useState('')
  const [analysisBusy, setAnalysisBusy] = useState(false)
  const [sourceProjection, setSourceProjection] = useState(null)
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

  const handleAnalyzeSource = async (file) => {
    if (!file || analysisBusy) return
    setAnalysisError('')
    if (file.size > MAX_SOURCE_BYTES) {
      setAnalysisError('文件超过 500KB，请拆分后再导入')
      return
    }
    setAnalysisBusy(true)
    try {
      const source = await file.text()
      const report = await analyzeExistingReact(
        { fileName: file.name, source },
        session?.token,
      )
      const projection = buildSourceProjection(report)
      setSourceProjection(projection)
    } catch (cause) {
      const message =
        cause instanceof SourceProjectionError
          ? cause.message
          : String(cause?.message || cause)
      setAnalysisError(`源码分析失败：${message}`)
    } finally {
      setAnalysisBusy(false)
    }
  }

  if (sourceProjection) {
    return (
      <div className="visual-canvas-page">
        <ReadOnlySourceCanvas
          projection={sourceProjection}
          onBack={() => {
            setSourceProjection(null)
            setAnalysisError('')
          }}
        />
      </div>
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
      {analysisError && (
        <div className="vc-validation-banner is-warning" role="alert">
          <span>{analysisError}</span>
          <button type="button" onClick={() => setAnalysisError('')}>
            关闭
          </button>
        </div>
      )}
      <OfflineVisualCanvas
        projectId={projectId}
        title={title}
        token={session?.token}
        onFrozen={handleFrozen}
        onAnalyzeSource={handleAnalyzeSource}
        analyzeBusy={analysisBusy}
      />
    </div>
  )
}
