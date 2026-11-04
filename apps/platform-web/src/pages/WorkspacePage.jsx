import { useEffect, useMemo, useState } from 'react'
import AppHeader from '../components/AppHeader.jsx'
import AccountDialog from '../components/AccountDialog.jsx'
import CanvasStage from '../components/CanvasStage.jsx'
import RunPanel from '../components/RunPanel.jsx'
import { useAuth } from '../auth/AuthContext.jsx'
import { INITIAL_APP } from '../mockProject.js'

const TARGET_FILE = 'src/App.jsx'
const PROJECT_ID = 'proj_canvas_demo'

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

export default function WorkspacePage() {
  const { user, session } = useAuth()
  const [annotation, setAnnotation] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [accountOpen, setAccountOpen] = useState(false)

  useEffect(() => {
    if (!submitting) return undefined
    const startedAt = Date.now()
    const timer = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - startedAt) / 1000))
    }, 400)
    return () => window.clearInterval(timer)
  }, [submitting])

  const stageIndex = useMemo(() => stageIndexFor(elapsed), [elapsed])

  /**
   * 由画布内浮层触发：annotation 已在 state 中，instruction 来自浮层输入框。
   */
  const submit = async (instruction) => {
    const text = instruction.trim()
    if (!annotation || !text || submitting) return

    setSubmitting(true)
    setElapsed(0)
    setResult(null)
    setError(null)

    const payload = {
      runId: newId('run'),
      tenantId: user?.tenantId || 'tenant_local',
      projectId: PROJECT_ID,
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
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${session?.token ?? ''}`,
        },
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
      <AppHeader busy={submitting} onOpenAccount={() => setAccountOpen(true)} />

      <main className="layout">
        <section className="studio">
          <div className="studio-head">
            <div>
              <span className="eyebrow">Canvas · Demo Project</span>
              <h2>拖拽圈选，画布内直接下达指令</h2>
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
            onSubmit={submit}
            submitting={submitting}
            disabled={submitting}
          />

          <div className="studio-status">
            <span className={`selection-state ${annotation ? 'is-ready' : ''}`}>
              {annotation
                ? `已圈选 ${(annotation.width * 100).toFixed(0)}% × ${(annotation.height * 100).toFixed(0)}%`
                : '尚未圈选组件'}
            </span>
            <span className="chip chip-muted">Mock LLM · fail_first</span>
            <span className="studio-tip">圈选后输入意图，Enter 发送</span>
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

      <AccountDialog open={accountOpen} onClose={() => setAccountOpen(false)} />
    </div>
  )
}
