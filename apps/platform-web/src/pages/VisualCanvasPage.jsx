import { useEffect, useMemo, useRef, useState } from 'react'
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
import {
  PROJECT_MODE,
  projectEntrySource,
  readImportedSource,
  readStoredProject,
  resolveProjectMode,
} from '../workspace/projectMode.js'

const MAX_SOURCE_BYTES = 500 * 1024

/**
 * 离线虚拟画布入口。
 *
 * 两种完全隔离的模式：
 *   - NEW_PROJECT：VisualDocument 本地编辑 + 冻结 + CodeGen；
 *   - EXISTING_SOURCE：导入源码 → 只读投影；不写 store、不转虚拟树、
 *     不触发 CodeGen。后续编辑走独立 AST 命令层（Phase 2）。
 */
export default function VisualCanvasPage() {
  const [params] = useSearchParams()
  const { session } = useAuth()
  const navigate = useNavigate()
  const [handoffError, setHandoffError] = useState('')
  const [analysisError, setAnalysisError] = useState('')
  const [analysisBusy, setAnalysisBusy] = useState(false)
  const [analysisRetry, setAnalysisRetry] = useState(0)
  const [sourceProjection, setSourceProjection] = useState(null)
  const analysisRequestRef = useRef(0)
  const projectId = params.get('project') || ''
  const title = params.get('title') || '未命名页面'

  const storedProject = useMemo(
    () => readStoredProject(projectId),
    [projectId],
  )
  const existingMode = useMemo(() => {
    if (params.get('mode') === 'existing') return true
    if (!storedProject) return false
    return resolveProjectMode(storedProject) === PROJECT_MODE.EXISTING_SOURCE
  }, [params, storedProject])

  // 存量项目：进入页面即从项目源码生成只读投影，不渲染可编辑虚拟画布。
  useEffect(() => {
    if (!existingMode || sourceProjection) return undefined
    const imported = readImportedSource(projectId)
    const entry = imported || projectEntrySource(storedProject)
    if (!entry?.source) {
      setAnalysisError('未找到可分析的源码；请返回工作台重新导入 .jsx / .tsx')
      return undefined
    }
    // StrictMode 会执行两次 effect：用递增 requestId 只采纳最后一次结果，
    // 不依赖 cleanup 取消（cleanup 会把唯一一次请求标记为已取消）。
    const requestId = analysisRequestRef.current + 1
    analysisRequestRef.current = requestId
    setAnalysisBusy(true)
    setAnalysisError('')
    void analyzeExistingReact(
      { fileName: entry.fileName, source: entry.source },
      session?.token,
    )
      .then((report) => {
        if (analysisRequestRef.current !== requestId) return
        setSourceProjection(buildSourceProjection(report))
      })
      .catch((cause) => {
        if (analysisRequestRef.current !== requestId) return
        const message =
          cause instanceof SourceProjectionError
            ? cause.message
            : String(cause?.message || cause)
        setAnalysisError(`源码分析失败：${message}`)
      })
      .finally(() => {
        if (analysisRequestRef.current === requestId) {
          setAnalysisBusy(false)
        }
      })
    return undefined
  }, [
    analysisRetry,
    existingMode,
    projectId,
    session?.token,
    sourceProjection,
    storedProject,
  ])

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

  if (existingMode) {
    return (
      <div className="visual-canvas-page">
        {analysisError && (
          <div className="vc-validation-banner is-warning" role="alert">
            <span>{analysisError}</span>
            <button
              type="button"
              onClick={() => {
                setAnalysisError('')
                setAnalysisRetry((value) => value + 1)
              }}
            >
              重新分析
            </button>
            <button
              type="button"
              onClick={() => navigate('/workspace')}
            >
              返回工作台
            </button>
          </div>
        )}
        {analysisBusy && !sourceProjection && (
          <div className="vc-validation-banner is-warning" role="status">
            <span>正在解析存量 React 源码…</span>
          </div>
        )}
        {sourceProjection ? (
          <ReadOnlySourceCanvas
            projection={sourceProjection}
            mode="EXISTING_SOURCE"
            backLabel="返回工作台"
            onBack={() => navigate('/workspace')}
          />
        ) : (
          !analysisBusy && (
            <ReadOnlySourceCanvas
              projection={null}
              mode="EXISTING_SOURCE"
              backLabel="返回工作台"
              onBack={() => navigate('/workspace')}
            />
          )
        )}
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
      <OfflineVisualCanvas
        projectId={projectId}
        title={title}
        token={session?.token}
        onFrozen={handleFrozen}
      />
    </div>
  )
}
