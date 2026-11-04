import { useEffect, useMemo, useState } from 'react'
import CanvasStage from './components/CanvasStage.jsx'
import RunPanel from './components/RunPanel.jsx'
import { INITIAL_APP } from './mockProject.js'

const TARGET_FILE = 'src/App.jsx'
const DEFAULT_INSTRUCTION = '改颜色'

/**
 * 同步执行的进度提示只负责“体验”，不代表真实节点进度：
 * 真实状态要等 M2 事件流；M1 用时间轴模拟，避免界面看起来卡死。
 */
const STAGES = [
  { at: 0, title: '正在解析画布标注', detail: '归一化坐标 → 组件选区 → AnnotationEnvelope' },
  { at: 3, title: 'Planner 正在规划修改方案', detail: '理解“改颜色”的目标组件与验收条件' },
  { at: 6, title: 'Coder 正在生成代码', detail: '输出 Unified Diff / 全量文件' },
  { at: 9, title: 'Applier 正在写入沙盒', detail: 'git apply + Vite HMR 热更新' },
  { at: 12, title: 'Verifier 正在运行构建', detail: 'npm run build 收集错误日志' },
  { at: 16, title: '遇到报错，正在尝试自我修复（第 1 次）', detail: '错误日志回灌 Coder，重新生成补丁' },
  { at: 22, title: '再次验证中', detail: '等待沙盒返回最终结果' },
]

const newId = (prefix) => {
  const random =
    typeof crypto !== 'undefined' && crypto.randomUUID
      ? crypto.randomUUID().replace(/-/g, '').slice(0, 12)
      : Math.random().toString(36).slice(2, 14)
  return `${prefix}_${random}`
}

const stageIndexFor = (elapsed) => {
  let index = 0
  STAGES.forEach((stage, i) => {
    if (elapsed >= stage.at) index = i
  })
  return index
}

/**
 * 将画布上的归一化选区组装为 AnnotationEnvelope。
 * worldTarget 保留归一化坐标，后端可换算到任意分辨率。
 */
function buildAnnotation(rect, instruction) {
  const now = new Date().toISOString()
  return {
    version: 1,
    annotationId: newId('ann'),
    type: 'drag',
    worldTarget: {
      x: Number(rect.x.toFixed(6)),
      y: Number(rect.y.toFixed(6)),
      width: Number(rect.width.toFixed(6)),
      height: Number(rect.height.toFixed(6)),
      coordinateSpace: 'normalized',
    },
    target: {
      mode: 'source',
      source: {
        file: TARGET_FILE,
        line: 11,
        column: 7,
        component: 'App',
        sourceKind: 'jsx',
      },
      selector: "[data-vibe-target='cta-button']",
      snippet: {
        startLine: 1,
        endLine: INITIAL_APP.split('\n').length,
        content: INITIAL_APP,
        hash: 'sha256:canvas-demo',
      },
    },
    intent: {
      kind: 'UI_ONLY',
      ui: {
        summary: `将按钮改为红色（用户标注：${instruction}）`,
        acceptance: ['按钮背景色变为红色', '其余布局与文案保持不变'],
        elementRole: 'button',
      },
    },
    payload: {
      text: instruction,
      from: null,
      to: null,
      strokes: [],
    },
    createdAt: now,
  }
}

async function extractError(response) {
  let body = null
  try {
    body = await response.json()
  } catch {
    // 非 JSON 错误体，走兜底。
  }
  const detail = body?.detail
  if (typeof detail === 'string') return detail
  if (detail?.message) return `${detail.code || response.status} · ${detail.message}`
  if (body?.message) return `${body.code || response.status} · ${body.message}`
  return `HTTP ${response.status} ${response.statusText || ''}`.trim()
}

export default function App() {
  const [annotation, setAnnotation] = useState(null)
  const [instruction, setInstruction] = useState(DEFAULT_INSTRUCTION)
  const [submitting, setSubmitting] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!submitting) return undefined
    const startedAt = Date.now()
    const timer = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - startedAt) / 1000))
    }, 400)
    return () => window.clearInterval(timer)
  }, [submitting])

  const stageIndex = useMemo(() => stageIndexFor(elapsed), [elapsed])
  const canSubmit = Boolean(annotation) && instruction.trim().length > 0 && !submitting

  const submit = async () => {
    if (!canSubmit) return
    const text = instruction.trim()

    setSubmitting(true)
    setElapsed(0)
    setResult(null)
    setError(null)

    const payload = {
      runId: newId('run'),
      tenantId: 'tenant_local',
      projectId: 'proj_canvas_demo',
      annotation: buildAnnotation(annotation, text),
      files: { [TARGET_FILE]: INITIAL_APP },
      options: {
        llmProvider: 'mock',
        mockScenario: 'fail_first',
        maxRetries: 3,
        keepSandbox: true,
      },
    }

    try {
      const response = await fetch('/api/v1/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!response.ok) {
        setError(await extractError(response))
        return
      }
      setResult(await response.json())
    } catch (cause) {
      setError(`网络请求失败：${cause?.message || cause}`)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">V</span>
          <div>
            <h1>Vibe Studio</h1>
            <p>画布标注 · 单 Agent 闭环</p>
          </div>
        </div>
        <div className="topbar-meta">
          <span className="chip">M1</span>
          <span className="chip chip-muted">React / Vite Sandbox</span>
          <span className={`live-dot ${submitting ? 'is-busy' : ''}`}>
            <i />
            {submitting ? 'Agent Running' : 'Ready'}
          </span>
        </div>
      </header>

      <main className="layout">
        <section className="studio">
          <div className="studio-head">
            <div>
              <span className="eyebrow">Canvas</span>
              <h2>拖拽圈选，描述你想改的地方</h2>
            </div>
            <button
              className="ghost-button"
              type="button"
              disabled={!annotation || submitting}
              onClick={() => setAnnotation(null)}
            >
              清除标注
            </button>
          </div>

          <CanvasStage
            annotation={annotation}
            onAnnotationChange={setAnnotation}
            disabled={submitting}
          />

          <div className="composer">
            <div className="composer-field">
              <label htmlFor="instruction">修改意图</label>
              <input
                id="instruction"
                type="text"
                value={instruction}
                maxLength={120}
                placeholder="例如：改颜色"
                disabled={submitting}
                onChange={(event) => setInstruction(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') submit()
                }}
              />
            </div>

            <div className="composer-meta">
              <span className={`selection-state ${annotation ? 'is-ready' : ''}`}>
                {annotation
                  ? `已圈选 ${(annotation.width * 100).toFixed(0)}% × ${(annotation.height * 100).toFixed(0)}%`
                  : '尚未圈选组件'}
              </span>
              <span className="chip chip-muted">Mock LLM · fail_first</span>
            </div>

            <button className="primary-button" type="button" disabled={!canSubmit} onClick={submit}>
              {submitting ? 'Agent 执行中…' : '提交标注'}
              <span aria-hidden="true">→</span>
            </button>
          </div>

          <p className="footnote">
            同步链路：platform-web → <code>/api/v1/runs</code> → Java BFF → python-ai → Docker 沙盒。
            首次执行会创建 Vite 沙盒，通常需要 20–60 秒。
          </p>
        </section>

        <RunPanel
          submitting={submitting}
          stages={STAGES}
          stageIndex={stageIndex}
          elapsed={elapsed}
          result={result}
          error={error}
          hasAnnotation={Boolean(annotation)}
        />
      </main>
    </div>
  )
}
