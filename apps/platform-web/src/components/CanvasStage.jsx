import { memo, useEffect, useMemo, useRef, useState } from 'react'
import useFillViewport from '../workspace/useFillViewport.js'
import usePanZoom from '../workspace/usePanZoom.js'
import {
  CREATE_NEW,
  MODIFY_EXISTING,
  normalizeAction,
} from '../workspace/annotationActions.js'

const clamp01 = (value) => Math.min(1, Math.max(0, value))

const POPOVER_WIDTH = 320
const POPOVER_MAX_HEIGHT = 360
const POPOVER_GAP = 14
const MAX_ANNOTATIONS = 3

// 画布 Mock 页面与沙盒初始源码共享的 data-source 标记。
// data-vibe-target 是 M1 的旧标记，保留读取兼容；新源码统一写 data-source。
// data-source-kind="container" 用于 ADD：插入必须发生在容器内部，绝不能
// 把新增 JSX 塞进 button / p / h1 等叶子元素。
const TARGET_META = {
  page: { label: '页面', role: 'container', component: 'App' },
  'cta-button': { label: '按钮', role: 'button', component: 'App' },
  title: { label: '标题', role: 'heading', component: 'App' },
  copy: { label: '说明文字', role: 'paragraph', component: 'App' },
  kicker: { label: '小标题', role: 'text', component: 'App' },
  card: { label: '卡片', role: 'card', component: 'App' },
}

// 圈选判定阈值（全部基于真实 DOM 面积，而不是只看中心点）：
//  - 命中节点被圈覆盖 ≥ 35% 才算“改这个节点”；
//  - 圈的展示面积超过节点 4 倍时，视为圈了一片留白，走 ADD；
//  - 节点在圈内面积占比 < 12% 时，说明圈里大部分是空白，也走 ADD。
const MODIFY_MIN_COVERAGE = 0.35
const ADD_AREA_RATIO = 4
const MODIFY_MIN_SELECTION_SHARE = 0.12

function normalizeRect(originX, originY, x, y) {
  return {
    x: Math.min(originX, x),
    y: Math.min(originY, y),
    width: Math.abs(x - originX),
    height: Math.abs(y - originY),
  }
}

/** 把一个带标记的 DOM 节点转换为可序列化的目标描述。 */
function targetForNode(node) {
  if (!node) return null
  const sourceMarker = node.getAttribute?.('data-source')
  const legacyMarker = node.getAttribute?.('data-vibe-target')
  const marker = sourceMarker || legacyMarker
  if (!marker) return null
  const attr = sourceMarker ? 'data-source' : 'data-vibe-target'
  const meta = TARGET_META[marker] ?? {
    label: marker,
    role: node.tagName?.toLowerCase() || 'element',
    component: 'App',
  }
  return {
    marker,
    selector: `[${attr}="${marker}"]`,
    label: meta.label,
    role: meta.role,
    component: meta.component,
    tag: node.tagName?.toLowerCase() || '',
    kind: node.getAttribute?.('data-source-kind') || 'element',
  }
}

function readTarget(element) {
  const node = element?.closest?.('[data-source], [data-vibe-target]')
  return targetForNode(node)
}

/** 从任意节点向上找到最近的“容器型”data-source 祖先。 */
function nearestSourceContainer(element) {
  return element?.closest?.(
    '[data-source-kind="container"], [data-vibe-target="card"]',
  )
}

function intersectionArea(a, b) {
  const left = Math.max(a.left, b.left)
  const right = Math.min(a.right, b.right)
  const top = Math.max(a.top, b.top)
  const bottom = Math.min(a.bottom, b.bottom)
  if (right <= left || bottom <= top) return 0
  return (right - left) * (bottom - top)
}

/** 读取圈选区域内所有 data-source 叶子的真实覆盖面积。 */
function measureCoverage(pageRoot, selectionRect) {
  const selectionArea =
    Math.max(0, selectionRect.width) * Math.max(0, selectionRect.height)
  if (!pageRoot || selectionArea <= 0) {
    return { selectionArea, nodes: [], best: null }
  }
  const seen = new Set()
  const nodes = []
  pageRoot
    .querySelectorAll('[data-source], [data-vibe-target]')
    .forEach((node) => {
      if (seen.has(node)) return
      seen.add(node)
      const rect = node.getBoundingClientRect()
      const area = rect.width * rect.height
      if (area <= 0) return
      const overlap = intersectionArea(rect, selectionRect)
      if (overlap <= 0) return
      const candidate = {
        node,
        target: targetForNode(node),
        rect,
        area,
        overlap,
        coverage: overlap / area,
        share: overlap / selectionArea,
        // 容器只作为 ADD 的插入父级，不参与 MODIFY 命中。
        isContainer:
          node.getAttribute('data-source-kind') === 'container' ||
          node.getAttribute('data-vibe-target') === 'card',
      }
      nodes.push(candidate)
    })
  return { selectionArea, nodes, best: null }
}

/**
 * 圈选几何判定：
 *  - 命中叶子节点且覆盖充分 → MODIFY，目标是该节点；
 *  - 没命中叶子 / 圈远大于节点 → ADD，目标是最近的容器型 data-source 父级，
 *    并给出圈相对该父级的归一化坐标，供模型决定插入位置。
 */
function analyzeSelection(pageRoot, selectionRect, centerPoint, scale = 1) {
  const { selectionArea, nodes } = measureCoverage(pageRoot, selectionRect)
  const leaves = nodes.filter((item) => !item.isContainer && item.target)
  const best = leaves.sort((a, b) => {
    if (b.overlap !== a.overlap) return b.overlap - a.overlap
    // 面积相同时选更具体的节点（面积更小）。
    return a.area - b.area
  })[0]

  const coversEnough =
    best &&
    best.coverage >= MODIFY_MIN_COVERAGE &&
    selectionArea <= best.area * ADD_AREA_RATIO &&
    best.share >= MODIFY_MIN_SELECTION_SHARE
  const action = coversEnough ? MODIFY_EXISTING : CREATE_NEW

  // ADD 的父容器：优先从圈中心向上找容器；中心落在页边距时再退回到
  // “与圈重叠面积最大的容器”，最后兜底页面根容器。
  let parentNode = null
  if (centerPoint) {
    const elements = document.elementsFromPoint(centerPoint.x, centerPoint.y)
    for (const element of elements) {
      if (
        element.closest?.('.annotation-layer') ||
        element.closest?.('.viewport-controls') ||
        element.closest?.('.annotation-limit') ||
        element.closest?.('.canvas-hint')
      ) {
        continue
      }
      const container = nearestSourceContainer(element)
      if (container) {
        parentNode = container
        break
      }
    }
  }
  if (!parentNode) {
    const centerX = selectionRect.left + selectionRect.width / 2
    const centerY = selectionRect.top + selectionRect.height / 2
    const containers = nodes
      .filter((item) => item.isContainer && item.target)
      .sort((a, b) => a.area - b.area)
    const containing = containers.find(
      (item) =>
        centerX >= item.rect.left &&
        centerX <= item.rect.right &&
        centerY >= item.rect.top &&
        centerY <= item.rect.bottom,
    )
    parentNode =
      containing?.node ??
      containers.sort((a, b) => b.overlap - a.overlap)[0]?.node ??
      pageRoot
  }

  const parentTarget = targetForNode(parentNode)
  let relativeRect = null
  if (parentTarget && parentNode) {
    const parentRect = parentNode.getBoundingClientRect()
    if (parentRect.width > 0 && parentRect.height > 0) {
      // 画布可能被缩放（viewport.scale），getBoundingClientRect 返回的是
      // 屏幕像素。归一化比例不受缩放影响，但 relativeX/Y 等像素字段必须
      // 除以 scale，才能得到 iframe / 预览视口里的真实 CSS 像素。
      const safeScale = scale > 0 ? scale : 1
      relativeRect = {
        // 兼容字段：相对父容器的归一化坐标（0~1）。
        x: Number(
          ((selectionRect.left - parentRect.left) / parentRect.width).toFixed(6),
        ),
        y: Number(
          ((selectionRect.top - parentRect.top) / parentRect.height).toFixed(6),
        ),
        width: Number((selectionRect.width / parentRect.width).toFixed(6)),
        height: Number((selectionRect.height / parentRect.height).toFixed(6)),
        // 开发者级字段：相对父容器左上角的 CSS 像素偏移 + 父容器真实尺寸。
        // 模型据此使用 flex/grid/absolute 精确落位，而不是猜测插入顺序。
        relativeX: Number(
          ((selectionRect.left - parentRect.left) / safeScale).toFixed(2),
        ),
        relativeY: Number(
          ((selectionRect.top - parentRect.top) / safeScale).toFixed(2),
        ),
        widthPx: Number((selectionRect.width / safeScale).toFixed(2)),
        heightPx: Number((selectionRect.height / safeScale).toFixed(2)),
        parentWidth: Number((parentRect.width / safeScale).toFixed(2)),
        parentHeight: Number((parentRect.height / safeScale).toFixed(2)),
      }
    }
  }

  return {
    action,
    target: best?.target ?? null,
    addTarget: action === CREATE_NEW ? parentTarget : parentTarget ?? null,
    relativeRect,
    measured: {
      selectionArea: Math.round(selectionArea),
      hitArea: best ? Math.round(best.overlap) : 0,
      hitCoverage: best ? Number(best.coverage.toFixed(3)) : 0,
      candidateCount: leaves.length,
    },
  }
}

/**
 * 浮层与圈使用同一套“世界坐标”（固定 1440×900 逻辑视口）。
 *
 * 浮层是 annotation-layer 的普通子元素，和页面共享父级 translate + scale，
 * 不做任何 1/scale 反向补偿：缩放画布时圆角、字号、输入框与浮层一起等比
 * 缩放，视觉上等同于画在画布上的一张图片。因此这里所有尺寸 / 间距都直接
 * 使用世界单位，不再除以 scale。
 */
function popoverStyleFor(rect, index, active, width, height) {
  if (!rect) return null
  const leftPx = rect.x * width
  const rightPx = (rect.x + rect.width) * width
  const centerY = (rect.y + rect.height / 2) * height
  // 同时存在多个浮层时按顺序轻微错开，避免完全重叠。
  const stagger = index * 12
  const clampTop = (value) =>
    Math.min(
      Math.max(10, value + stagger),
      Math.max(10, height - POPOVER_MAX_HEIGHT - 10),
    )
  // 浮层永远高于圈线层（圈最高 100），激活的浮层再抬高一层。
  const zIndex = active ? 320 : 300 + index

  if (width - rightPx >= POPOVER_WIDTH + POPOVER_GAP) {
    return {
      left: rightPx + POPOVER_GAP,
      top: clampTop(centerY - POPOVER_MAX_HEIGHT / 2),
      zIndex,
    }
  }
  if (leftPx >= POPOVER_WIDTH + POPOVER_GAP) {
    return {
      right: width - leftPx + POPOVER_GAP,
      top: clampTop(centerY - POPOVER_MAX_HEIGHT / 2),
      zIndex,
    }
  }

  // 窄屏兜底：横跨选区下方 / 上方，固定宽度水平居中。
  const below = (rect.y + rect.height) * height + POPOVER_GAP
  const above = rect.y * height - POPOVER_MAX_HEIGHT - POPOVER_GAP
  const top =
    below + POPOVER_MAX_HEIGHT <= height - 10 ? below : Math.max(10, above)
  return {
    left: Math.max(10, (width - POPOVER_WIDTH) / 2),
    width: POPOVER_WIDTH,
    top: clampTop(top),
    zIndex,
  }
}

// HITL：不同触发原因对应不同的引导文案，方便用户理解为什么被中断。
const CLARIFICATION_REASON_TEXT = {
  ambiguous_intent: '这个需求还不够明确，确认几个细节后我再开始改：',
  ambiguous_visual_intent: '“好看”没有唯一答案，先确认你更在意哪一项：',
  missing_component_type: '新增组件还缺少关键信息，确认后我再开始改：',
  low_confidence: '我还不确定你的具体意图，先确认一下：',
  fullstack_scope: '这看起来是一个业务需求，先确认改动范围：',
  core_business_decision: '这属于核心业务决策，确认实现范围后我再继续：',
}

/**
 * 单个标注旁的迷你 Chatbox。
 *
 * 每个标注拥有独立 threadId / chatHistory；用户发送后立即触发该标注的
 * 增量 Run，后端复用沙盒，并把同一圈的历史与上一次修改记录送入模型。
 */
function AnnotationPopover({
  annotation,
  index,
  submitting,
  // 审批未决时禁止修改标注 / 发起新 Run，但聊天记录仍可查看。
  blocked = false,
  pending,
  pendingAction,
  active,
  hidden = false,
  panReady,
  inputRef,
  onChange,
  onActionChange,
  onRemove,
  onUnmount,
  onSend,
  onClarificationAnswer,
  clarification,
  clarificationPending,
  onRollback,
  onActivate,
  onDeactivate,
  viewportWidth,
  viewportHeight,
}) {
  const historyRef = useRef(null)
  const style = useMemo(
    () =>
      popoverStyleFor(
        annotation.rect,
        index,
        active,
        viewportWidth,
        viewportHeight,
      ),
    [annotation.rect, index, active, viewportWidth, viewportHeight],
  )
  const history = annotation.chatHistory ?? []
  const [answers, setAnswers] = useState({})
  // 预设选项之外的“自定义”折叠栏：按 questionId 记录展开状态与草稿。
  // 主输入栏始终保留，用户不必在“点选项”和“写补充说明”之间二选一。
  const [customOpen, setCustomOpen] = useState({})
  const [customText, setCustomText] = useState({})
  const [confirmingClose, setConfirmingClose] = useState(false)
  const sending = Boolean(submitting && pending)
  // 关闭确认按项目级 submitting 判定：单圈 / 批量提交都要保护。
  const executing = Boolean(submitting)
  const onUnmountRef = useRef(onUnmount)

  useEffect(() => {
    onUnmountRef.current = onUnmount
  }, [onUnmount])

  // 卸载钩子：浮层被删除 / 项目切换 / 错误边界降级时，通知工作区中止
  // 该标注的 route / dispatch / SSE，避免迟到结果回写已卸载的对话。
  // 依赖只保留 annotation.id：父级 callback 每次渲染都会变化，不能让它
  // 触发 cleanup，否则普通状态更新也会误中止请求。
  useEffect(
    () => () => {
      onUnmountRef.current?.(annotation.id)
    },
    [annotation.id],
  )

  useEffect(() => {
    const node = historyRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [history.length, pending, clarification])

  useEffect(() => {
    if (!executing) setConfirmingClose(false)
  }, [executing])

  // 新一轮澄清使用独立草稿；默认值（如“颜色”）预选，用户可直接提交。
  useEffect(() => {
    if (!clarification) {
      setAnswers({})
      setCustomOpen({})
      setCustomText({})
      return
    }
    const initial = {}
    const initialOpen = {}
    clarification.questions?.forEach((question) => {
      if (question.defaultValue !== undefined && question.defaultValue !== null) {
        initial[question.questionId] = question.defaultValue
      }
      // 没有预设选项 / 自由文本题：直接展开文本域，不再藏一层输入框。
      if (
        question.type === 'free_text' ||
        (question.options?.length ?? 0) === 0
      ) {
        initialOpen[question.questionId] = true
      }
    })
    setAnswers(initial)
    setCustomOpen(initialOpen)
    setCustomText({})
  }, [clarification?.clarificationId])

  if (!style) return null

  const rollingBack = sending && pendingAction === 'rollback'
  // 兼容旧项目状态：没有显式 action 时按是否命中元素推断。
  const action = annotation.action
    ? normalizeAction(annotation.action)
    : annotation.target
      ? MODIFY_EXISTING
      : CREATE_NEW

  const setAnswer = (questionId, value) => {
    setAnswers((previous) => ({ ...previous, [questionId]: value }))
  }

  const toggleMultiAnswer = (questionId, value) => {
    setAnswers((previous) => {
      const current = Array.isArray(previous[questionId])
        ? previous[questionId]
        : []
      return {
        ...previous,
        [questionId]: current.includes(value)
          ? current.filter((item) => item !== value)
          : [...current, value],
      }
    })
  }

  // 合并“当前草稿 + 本次一键选择的覆盖值”，用于判断能否直接提交。
  const effectiveAnswer = (question, overrides = {}) =>
    Object.prototype.hasOwnProperty.call(overrides, question.questionId)
      ? overrides[question.questionId]
      : answers[question.questionId]

  const answersAreComplete = (overrides = {}) =>
    Boolean(
      clarification?.questions?.every((question) => {
        if (!question.required) return true
        const value = effectiveAnswer(question, overrides)
        const extra = String(customText[question.questionId] ?? '').trim()
        if (Array.isArray(value)) return value.length > 0 || extra !== ''
        return (
          (value !== undefined &&
            value !== null &&
            String(value).trim() !== '') ||
          extra !== ''
        )
      }),
    )

  const clarificationComplete = answersAreComplete()

  // 全部为可选问题时，“完整”不代表有内容；至少要有一条答案或补充说明，
  // 否则后端会以“至少回答一个澄清问题”拒绝。
  const clarificationHasInput = Boolean(
    annotation.intent.trim() ||
      clarification?.questions?.some((question) => {
        const value = answers[question.questionId]
        const extra = String(customText[question.questionId] ?? '').trim()
        if (Array.isArray(value)) return value.length > 0 || extra !== ''
        return (
          (value !== undefined &&
            value !== null &&
            String(value).trim() !== '') ||
          extra !== ''
        )
      }),
  )

  const toggleCustom = (questionId) => {
    const next = !customOpen[questionId]
    setCustomOpen((previous) => ({ ...previous, [questionId]: next }))
    // 进入自定义模式时清空预设答案，避免单选框仍显示旧选项。
    setAnswers((previous) => ({ ...previous, [questionId]: '' }))
    if (!next) {
      setCustomText((previous) => ({ ...previous, [questionId]: '' }))
    }
  }

  const updateCustomText = (question, text) => {
    setCustomText((previous) => ({ ...previous, [question.questionId]: text }))
    if (question.type !== 'multi_choice') {
      setAnswer(question.questionId, text)
    }
  }

  /**
   * 一键决策：把结构化 quick option 映射到对应问题。
   *
   * 若其余必填问题已有答案，立即自动提交，用户无需再点“发送并继续”；
   * 若仍有缺失，则只填入该答案，保留逐项补充的交互。
   */
  const applyQuickOption = (option) => {
    if (!clarification || submitting) return
    const questionId =
      option.questionId || clarification.questions?.[0]?.questionId
    if (!questionId) return
    const value =
      option.value !== undefined && option.value !== null
        ? option.value
        : option.label
    const overrides = { [questionId]: value }
    setAnswers((previous) => ({ ...previous, ...overrides }))
    if (answersAreComplete(overrides)) {
      submitClarification('', overrides)
    }
  }

  const submitClarification = (comment = '', overrides = {}) => {
    const hasInput =
      Object.keys(overrides).length > 0 || clarificationHasInput
    if (
      !clarification ||
      !answersAreComplete(overrides) ||
      !hasInput
    ) {
      return
    }
    const payload = clarification.questions
      .map((question) => {
        const extra = String(customText[question.questionId] ?? '').trim()
        let value = effectiveAnswer(question, overrides)
        if (question.type === 'multi_choice') {
          const selected = Array.isArray(value) ? value : []
          const merged = extra && !selected.includes(extra)
            ? [...selected, extra]
            : selected
          value = merged
        } else if (extra && (!value || String(value).trim() === '')) {
          value = extra
        }
        if (Array.isArray(value)) {
          return value.length > 0
            ? { questionId: question.questionId, value }
            : null
        }
        if (value === undefined || value === null || String(value).trim() === '') {
          return null
        }
        return { questionId: question.questionId, value }
      })
      .filter(Boolean)
    // 主输入栏的补充说明作为 comment 一起提交，不强制用户把长文本
    // 拆成一个个问题答案。
    onClarificationAnswer?.(
      annotation.id,
      clarification,
      payload,
      String(comment || '').trim(),
    )
  }

  return (
    <form
      className={`canvas-popover tone-${index % 3} ${active ? 'is-active' : ''} ${hidden ? 'is-hidden' : ''}`}
      style={style}
      role="dialog"
      aria-hidden={hidden}
      aria-label={`标注 ${index + 1} 多轮对话`}
      data-annotation-id={annotation.id}
      data-action={action}
      onPointerDown={(event) => {
        // 中键 / 空格+左键是全局平移手势：即使从浮层上按下也必须继续
        // 冒泡给画布，否则浮层覆盖画布中心时会出现“中间区域无法拖动”的死角。
        if (event.button === 1 || (event.button === 0 && panReady)) return
        event.stopPropagation()
        onActivate?.(annotation.id)
      }}
      onPointerUp={(event) => {
        if (event.button === 1 || (event.button === 0 && panReady)) return
        event.stopPropagation()
      }}
      onFocusCapture={() => onActivate?.(annotation.id)}
      onBlurCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) {
          onDeactivate?.(annotation.id)
        }
      }}
      onSubmit={(event) => {
        event.preventDefault()
        // 澄清挂起期间，主输入栏不再关闭：发送时把当前问答 + 输入栏
        // 补充说明一起提交，而不是启动一个新 Run（否则会撞 RUN_BUSY）。
        if (clarificationPending) {
          submitClarification(annotation.intent)
          return
        }
        onSend?.(annotation.id)
      }}
    >
      <div className="popover-head">
        <span className="popover-dot" aria-label={`标注 ${index + 1}`} />
        {/* 操作类型切换：修改已有元素 / 在选区新增组件。
            默认根据圈选是否命中元素与文案自动推断，用户可手动覆盖。 */}
        <div className="popover-action" role="group" aria-label="操作类型">
          {[
            {
              value: MODIFY_EXISTING,
              label: '修改',
              title: '修改圈选到的已有元素',
            },
            {
              value: CREATE_NEW,
              label: '新增',
              title: '在圈选区域新增一个组件（位置稍后拖拽确认）',
            },
          ].map((option) => (
            <button
              key={option.value}
              type="button"
              className={action === option.value ? 'is-active' : ''}
              aria-pressed={action === option.value}
              title={option.title}
              disabled={blocked || submitting || Boolean(clarification)}
              onClick={() => onActionChange?.(annotation.id, option.value)}
            >
              {option.label}
            </button>
          ))}
        </div>
        <button
          className={`popover-remove ${executing ? 'is-running' : ''}`}
          type="button"
          title={
            executing
              ? '任务执行中，关闭前需要确认'
              : '删除这个标注'
          }
          aria-label={`删除标注 ${index + 1}`}
          disabled={blocked || Boolean(clarification)}
          onClick={() => {
            if (executing) {
              setConfirmingClose(true)
              return
            }
            onRemove?.(annotation.id)
          }}
        >
          ×
        </button>
      </div>

      {confirmingClose && (
        <div
          className="popover-close-confirm"
          role="alertdialog"
          aria-label="确认关闭标注"
        >
          <p>任务正在执行中，强行关闭可能丢失结果，是否继续？</p>
          <div className="popover-close-actions">
            <button
              type="button"
              className="popover-confirm-cancel"
              onClick={() => setConfirmingClose(false)}
            >
              继续等待
            </button>
            <button
              type="button"
              className="popover-confirm-close"
              onClick={() => {
                setConfirmingClose(false)
                onRemove?.(annotation.id)
              }}
            >
              仍然关闭
            </button>
          </div>
        </div>
      )}

      {history.length > 0 && (
        <div
          className="popover-chat"
          ref={historyRef}
          aria-label={`标注 ${index + 1} 对话历史`}
        >
          {history.map((message, messageIndex) => (
            <div
              key={message.id || `${message.role}-${messageIndex}`}
              className={`chat-bubble is-${message.role}`}
              data-turn={message.turnCount ?? undefined}
            >
              <span className="chat-role">
                {message.role === 'user' ? '你' : 'Agent'}
              </span>
              <p>{message.content}</p>
              {message.role === 'assistant' && message.commitHash && (
                <div className="chat-checkpoint">
                  <span
                    className="chat-commit"
                    title={`Git ${message.commitHash}`}
                  >
                    turn {message.turnCount} · {message.commitHash.slice(0, 7)}
                  </span>
                  <button
                    className="chat-rollback"
                    type="button"
                    disabled={blocked || submitting}
                    title="恢复这一轮的代码与对话，丢弃之后的所有修改"
                    onClick={() => onRollback?.(annotation.id, message)}
                  >
                    ↩️ 回滚到此步
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* HITL 澄清气泡：暂停状态机后，用户在这里补全意图再恢复执行。 */}
      {clarification && (
        <div
          className={`popover-clarification ${clarification.status === 'EXPIRED' ? 'is-expired' : ''}`}
          role="group"
          aria-label="Agent 需要确认"
        >
          <div className="clarification-head">
            <span className="clarification-badge">Agent 需要确认</span>
            {clarification.round > 0 && (
              <span className="clarification-round">
                第 {clarification.round}/{clarification.maxRounds ?? 3} 轮
              </span>
            )}
          </div>
          <p className="clarification-lead">
            {clarification.status === 'EXPIRED'
              ? '这轮确认已过期，请重新发送修改意图。'
              : CLARIFICATION_REASON_TEXT[clarification.reason] ??
                '请补充以下信息，确认后我继续执行：'}
          </p>
          {clarification.decisionSummary && (
            <p className="clarification-impact">
              <span className="clarification-impact-tag">核心决策</span>
              {clarification.decisionSummary}
            </p>
          )}

          {/* 结构化一键决策：只用于 CORE_BUSINESS，实现细节不会走到这里。 */}
          {clarification.status !== 'EXPIRED' &&
            clarification.clarificationOptions?.length > 0 && (
              <div className="clarification-quick" data-clarification-quick>
                <span className="clarification-quick-title">
                  一键选择，立即继续
                </span>
                <div className="clarification-quick-options">
                  {clarification.clarificationOptions.map((option) => (
                    <button
                      key={option.optionId}
                      type="button"
                      className={`clarification-quick-option ${option.recommended ? 'is-recommended' : ''}`}
                      data-clarification-quick-option={option.optionId}
                      data-recommended={option.recommended ? 'true' : 'false'}
                      disabled={submitting}
                      onClick={() => applyQuickOption(option)}
                    >
                      <span className="clarification-quick-label">
                        {option.label}
                        {option.recommended && (
                          <em className="clarification-recommended">推荐</em>
                        )}
                      </span>
                      {option.description && (
                        <span className="clarification-quick-desc">
                          {option.description}
                        </span>
                      )}
                    </button>
                  ))}
                </div>
              </div>
            )}

          {clarification.status !== 'EXPIRED' &&
            clarification.questions?.map((question) => {
              const value = answers[question.questionId]
              const hasOptions = (question.options?.length ?? 0) > 0
              const multi = question.type === 'multi_choice'
              // entity_fields / schema_fields 暂未提供结构化表单，
              // 降级为自由文本，保证 M2 不会因为未知题型卡死。
              const freeText = question.type === 'free_text' || !hasOptions
              const custom = Boolean(customOpen[question.questionId])
              const inputValue =
                typeof value === 'string' || typeof value === 'number'
                  ? String(value)
                  : ''
              const customValue = freeText
                ? inputValue
                : String(customText[question.questionId] ?? '')
              return (
                <div className="clarification-question" key={question.questionId}>
                  <p className="clarification-label">
                    {question.prompt}
                    {question.required && (
                      <em aria-label="必填" title="必填">
                        *
                      </em>
                    )}
                  </p>

                  {hasOptions && (
                    <div className="clarification-options">
                      {question.options.map((option) => {
                        const selected = multi
                          ? Array.isArray(value) && value.includes(option.value)
                          : value === option.value && !custom
                        return (
                          <button
                            key={option.value}
                            type="button"
                            className={`clarification-option ${selected ? 'is-selected' : ''}`}
                            aria-pressed={selected}
                            disabled={submitting}
                            onClick={() => {
                              // 点击预设选项只更新答案，不关闭主输入栏；
                              // 用户仍可继续写补充说明。
                              if (custom) {
                                setCustomOpen((previous) => ({
                                  ...previous,
                                  [question.questionId]: false,
                                }))
                                setCustomText((previous) => ({
                                  ...previous,
                                  [question.questionId]: '',
                                }))
                              }
                              if (multi) {
                                toggleMultiAnswer(question.questionId, option.value)
                              } else {
                                setAnswer(question.questionId, option.value)
                              }
                            }}
                          >
                            {option.label}
                          </button>
                        )
                      })}
                      <button
                        type="button"
                        className={`clarification-option is-custom ${custom ? 'is-selected' : ''}`}
                        aria-pressed={custom}
                        disabled={submitting}
                        onClick={() => toggleCustom(question.questionId)}
                      >
                        自定义
                      </button>
                    </div>
                  )}

                  {/* 自由文本题默认展开；选项题在点击“自定义”后平滑展开。
                      使用 textarea 允许开发者输入多行、较长的补充说明。 */}
                  <div
                    className={`clarification-expand ${freeText || custom ? 'is-open' : ''}`}
                    aria-hidden={!(freeText || custom)}
                  >
                    <textarea
                      className="clarification-input"
                      rows={2}
                      value={customValue}
                      maxLength={800}
                      tabIndex={freeText || custom ? 0 : -1}
                      placeholder={
                        freeText
                          ? '输入你的回答，支持多行…'
                          : '补充自定义说明，支持多行…'
                      }
                      disabled={submitting}
                      onChange={(event) => {
                        if (freeText) {
                          setAnswer(question.questionId, event.target.value)
                        } else {
                          updateCustomText(question, event.target.value)
                        }
                      }}
                    />
                  </div>
                </div>
              )
            })}

          {clarification.status !== 'EXPIRED' && (
            <button
              className="clarification-submit"
              type="button"
              disabled={
                submitting || !clarificationComplete || !clarificationHasInput
              }
              onClick={() => submitClarification(annotation.intent)}
            >
              {submitting ? (
                <span className="mini-spinner" aria-label="执行中" />
              ) : (
                '发送并继续'
              )}
            </button>
          )}
        </div>
      )}

      <div className="popover-row">
        <input
          ref={inputRef}
          type="text"
          value={annotation.intent}
          maxLength={120}
          placeholder={
            blocked
              ? '请先在上方处理待审批的修改…'
              : clarificationPending
                ? clarification
                  ? '补充说明（可选），随确认一起提交…'
                  : '其他标注有待确认的问题…'
                : '描述修改意图...'
          }
          disabled={blocked || submitting}
          onChange={(event) => onChange?.(annotation.id, event.target.value)}
        />
        <button
          className="popover-send"
          type="submit"
          disabled={
            blocked ||
            submitting ||
            (clarificationPending
              ? !clarificationComplete || !clarificationHasInput
              : !annotation.intent.trim())
          }
          title={
            clarificationPending
              ? '把当前回答与输入栏说明一起提交'
              : '只对当前圈执行一次增量修改'
          }
        >
          {sending ? <span className="mini-spinner" aria-label="执行中" /> : '发送'}
        </button>
      </div>
      {sending && (
        <p className="popover-hint">
          {rollingBack ? '正在恢复…' : '正在修改…'}
        </p>
      )}
    </form>
  )
}

/**
 * 画布 Stage：
 *  - 展示一个与沙盒初始源码一致的 Mock 页面；
 *  - 鼠标 / 触控拖拽圈选组件，坐标转换为固定 1440×900 世界坐标；
 *  - 页面与标注层使用同一 transform，平移 / 缩放后圈不会漂移；
 *  - 每个圈拥有独立多轮 Chatbox，可单独发送增量修改。
 */
const LIVE_PAGE_TARGET = {
  marker: 'page',
  selector: '',
  label: '页面',
  role: 'container',
  component: 'App',
  tag: 'div',
  kind: 'container',
}

/** 给沙盒预览 URL 加一个仅在画布内使用的 cache-busting 参数。 */
function withRefresh(url, nonce) {
  if (!url || !nonce) return url
  const separator = url.includes('?') ? '&' : '?'
  return `${url}${separator}vibe_canvas=${nonce}`
}

function CanvasStage({
  mode = 'mock',
  liveUrl = '',
  refreshNonce = 0,
  // 非激活 Tab 不挂载 iframe：避免最多 5 个隐藏 Tab 各自常驻一个
  // Vite 应用 + HMR WebSocket。切回时重新挂载，沙盒状态由服务端保存。
  suspended = false,
  onStartCreate,
  onRetryBootstrap,
  onOpenArchitect,
  annotations = [],
  onAnnotationAdd,
  onAnnotationIntentChange,
  onAnnotationActionChange,
  onAnnotationRemove,
  onAnnotationUnmount,
  onAnnotationSend,
  onAnnotationMove,
  onClarificationAnswer,
  clarification,
  onAnnotationRollback,
  pendingAnnotationId,
  pendingAction,
  submitting,
  disabled,
  blocked = false,
  // 精准选择（Inspect）：画布模式下也允许透传 hover / pick 给 iframe SDK。
  // 与预览区共用同一套子协议，但高亮框绘制在画布自己的 viewport 坐标里，
  // 因此缩放 / 平移后仍与真实 DOM 严格对齐。
  inspectMode = false,
  onInspectPick,
  // 画布内的页面滚动 / 点击由 SDK 回传，父级据此同步右侧预览。
  onFrameScroll,
  onFrameInteraction,
}) {
  const stageRef = useRef(null)
  // 固定 1440×900 的视口节点：所有屏幕 → 世界坐标换算都以它的
  // getBoundingClientRect 为准，自动包含滚动偏移与 zoom transform。
  const viewportRef = useRef(null)
  // iframe 内的 inspect SDK 是否已就绪；未就绪时不拦截滚轮，
  // 让浏览器先处理外层容器的原生滚动。
  const liveReadyRef = useRef(false)
  // 最近一次 iframe 滚动快照：滚轮到边界时把剩余滚动交还外层容器。
  const liveScrollRef = useRef(null)
  const onFrameScrollRef = useRef(onFrameScroll)
  const onFrameInteractionRef = useRef(onFrameInteraction)
  onFrameScrollRef.current = onFrameScroll
  onFrameInteractionRef.current = onFrameInteraction
  const startTargetRef = useRef(null)
  const inputRefs = useRef(new Map())
  const focusedAnnotationRef = useRef(null)
  const limitTimerRef = useRef(null)
  const [draft, setDraft] = useState(null)
  const [drawing, setDrawing] = useState(false)
  const [limitNotice, setLimitNotice] = useState(false)
  // 当前激活的标注：浮层 / 圈提升到最高 z-index，点击画布空白处恢复。
  const [activeAnnotationId, setActiveAnnotationId] = useState(null)
  // 用户圈选多个区域时，可临时隐藏浮层，避免输入框挡住下一笔圈选。
  const [popoversHidden, setPopoversHidden] = useState(false)
  // live 画布的双状态：
  //   annotate = 覆盖层接收指针，用于圈选 / 平移 / 缩放；
  //   interact = iframe 接管指针，用户可以直接点击真实页面里的按钮。
  const [interactionMode, setInteractionMode] = useState('annotate')
  const liveFrameRef = useRef(null)
  // pointerdown 时的命中目标：pointer capture 会把 pointerup 的 target
  // 重定向到 stage，因此“是否点在真实页面上”必须在下笔时记录。
  const livePressRef = useRef(false)
  const inspectModeRef = useRef(inspectMode)
  const onInspectPickRef = useRef(onInspectPick)
  // 精准选择命中的 iframe 逻辑坐标 rect（SDK 回传，单位 CSS px）。
  const [inspectHover, setInspectHover] = useState(null)
  // 移动交互：D 键 / 工具栏开启后，拖拽只做 transform 预览，
  // 松手时把位移翻译成自然语言交给 Agent，不直接改 DOM 样式。
  const [movingId, setMovingId] = useState(null)
  const [moveOffset, setMoveOffset] = useState({ x: 0, y: 0 })
  const moveDragRef = useRef(null)

  // 与沙盒预览共用固定 1440×900 逻辑视口：画布容器自身滚动，
  // 不再用外层 scale 强行适配屏幕，页面内容可以完整滚动查看。
  const {
    scale: baseScale,
    offsetX: baseOffsetX,
    offsetY: baseOffsetY,
    designWidth,
    designHeight,
    recalc: recalcViewport,
  } = useFillViewport(stageRef)
  // 画布自己的平移 / 缩放状态，保存在 ProjectWorkspace 级别的 ViewportContext。
  const viewport = usePanZoom({
    containerRef: stageRef,
    panel: 'canvas',
    baseScale,
    baseOffsetX,
    baseOffsetY,
    contentWidth: designWidth,
    contentHeight: designHeight,
  })

  // 非激活 Tab 用 display:none / content-visibility 休眠，期间 ResizeObserver
  // 读到的是 0×0。切回时必须主动补测，否则画布会沿用切走前的旧比例，
  // 表现为内容被截断或只占据左上角。
  useEffect(() => {
    if (suspended) return undefined
    const first = window.requestAnimationFrame(() => {
      recalcViewport()
      window.requestAnimationFrame(recalcViewport)
    })
    // 真实浏览器里 tab-window 的 flex 高度可能比 React 提交晚一帧，
    // 再加一个短延迟兜底，覆盖字体 / HMR / 沙盒 iframe 加载造成的回流。
    const timer = window.setTimeout(recalcViewport, 120)
    return () => {
      window.cancelAnimationFrame(first)
      window.clearTimeout(timer)
    }
  }, [suspended, mode, recalcViewport])

  // 新增标注后自动展开浮层并聚焦它的输入框；编辑已有标注不会抢焦点。
  useEffect(() => {
    const last = annotations[annotations.length - 1]
    if (!last || last.id === focusedAnnotationRef.current) return undefined
    focusedAnnotationRef.current = last.id
    setPopoversHidden(false)
    setActiveAnnotationId(last.id)
    const frame = window.requestAnimationFrame(() =>
      inputRefs.current.get(last.id)?.focus(),
    )
    return () => window.cancelAnimationFrame(frame)
  }, [annotations])

  useEffect(
    () => () => {
      if (limitTimerRef.current) window.clearTimeout(limitTimerRef.current)
    },
    [],
  )

  // 只有 mock / live 画布允许圈选；空白、骨架屏、失败态不可绘制。
  const interactive = mode === 'mock' || mode === 'live'
  // 交互态只对 live 画布生效：mock 页面没有可点击的真实业务逻辑。
  // Inspect 优先级最高：此时指针必须由覆盖层接管，不能穿透到 iframe。
  const canvasInspect = Boolean(inspectMode) && mode === 'live'
  const liveInteractive =
    mode === 'live' && interactionMode === 'interact' && !canvasInspect
  const atLimit = annotations.length >= MAX_ANNOTATIONS

  // 切换到 mock / 空白画布时，必须回到标注态，避免 iframe 消失后残留状态。
  useEffect(() => {
    if (mode !== 'live') setInteractionMode('annotate')
  }, [mode])

  // 选中目标后按 D 进入 / 退出移动预览；输入框内不拦截 D 键。
  useEffect(() => {
    const onKeyDown = (event) => {
      const tag = String(event.target?.tagName || '').toLowerCase()
      if (
        event.metaKey ||
        event.ctrlKey ||
        event.altKey ||
        tag === 'input' ||
        tag === 'textarea' ||
        event.target?.isContentEditable
      ) {
        return
      }
      if (String(event.key || '').toLowerCase() !== 'd') return
      if (!activeAnnotationId || disabled || blocked) return
      event.preventDefault()
      setMovingId((current) =>
        current === activeAnnotationId ? null : activeAnnotationId,
      )
      setMoveOffset({ x: 0, y: 0 })
      moveDragRef.current = null
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [activeAnnotationId, blocked, disabled])

  // 选中目标被删除 / 切换时，移动预览必须同步退出，避免浮层悬空。
  useEffect(() => {
    if (!activeAnnotationId || !annotations.some((item) => item.id === activeAnnotationId)) {
      setMovingId(null)
      setMoveOffset({ x: 0, y: 0 })
      moveDragRef.current = null
    }
  }, [activeAnnotationId, annotations])
  useEffect(() => {
    if (movingId && activeAnnotationId && movingId !== activeAnnotationId) {
      setMovingId(null)
      setMoveOffset({ x: 0, y: 0 })
      moveDragRef.current = null
    }
  }, [activeAnnotationId, movingId])

  // 开启 Inspect 时强制回到标注态；关闭时清空高亮，避免残留蓝框。
  useEffect(() => {
    inspectModeRef.current = inspectMode
    onInspectPickRef.current = onInspectPick
    if (canvasInspect) setInteractionMode('annotate')
    if (!canvasInspect) setInspectHover(null)
  }, [canvasInspect, inspectMode, onInspectPick])

  // Esc 是“退出浏览”的通用快捷键，和预设的「点灰色空白返回标注」互补。
  useEffect(() => {
    if (!liveInteractive) return undefined
    const onKeyDown = (event) => {
      if (event.key === 'Escape') setInteractionMode('annotate')
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [liveInteractive])

  /*
   * 交互态下 iframe 接管指针事件，父页面收不到中键 pointerdown。
   * 沙盒内的 inspect SDK 会把中键手势通过 postMessage 转发出来，
   * 这里换算成父页面屏幕像素后交给 usePanZoom，保证两种状态都能平移。
   */
  useEffect(() => {
    if (mode !== 'live') return undefined
    const onMessage = (event) => {
      const frame = liveFrameRef.current
      if (!frame || event.source !== frame.contentWindow) return
      const data = event.data
      if (!data || data.channel !== 'vibe-inspect') return
      if (data.type === 'ready') {
        // iframe 重载（HMR / 首次加载）后重新下发 Inspect 开关。
        liveReadyRef.current = true
        liveScrollRef.current = data.payload?.scroll ?? null
        inspectModeRef.current &&
          frame.contentWindow?.postMessage(
            { channel: 'vibe-inspect', type: 'enable', enabled: true },
            '*',
          )
        setInspectHover(null)
        return
      }
      if (data.type === 'scroll') {
        // 页面内部滚动位置：用于驱动右侧预览同步与滚轮边界判断。
        liveScrollRef.current = data.payload ?? null
        onFrameScrollRef.current?.(data.payload)
        return
      }
      if (data.type === 'click') {
        // 真实页面内的点击，父级在预览区绘制同位置的点击涟漪。
        onFrameInteractionRef.current?.(data.payload)
        return
      }
      if (data.type === 'hover') {
        const payload = data.payload
        setInspectHover(payload && payload.ok ? payload : null)
        return
      }
      if (data.type === 'picked') {
        setInspectHover(null)
        if (data.payload?.ok) {
          onInspectPickRef.current?.(data.payload)
          // Inspect 的点击同样换算成一次视觉反馈，让预览区知道用户点了哪。
          onFrameInteractionRef.current?.({
            ...data.payload,
            type: 'click',
            source: data.payload.source ?? null,
          })
        }
        return
      }
      if (data.type === 'pan-start') {
        viewport.beginExternalPan()
        return
      }
      if (data.type === 'pan-move') {
        const dx = Number(data.payload?.dx) || 0
        const dy = Number(data.payload?.dy) || 0
        // SDK 的位移单位是 iframe 内 CSS 像素；画布按 scale 缩放显示。
        viewport.moveExternalPan(dx * viewport.scale, dy * viewport.scale)
        return
      }
      if (data.type === 'pan-end' || data.type === 'pan-cancel') {
        viewport.endExternalPan()
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [
    mode,
    viewport.beginExternalPan,
    viewport.moveExternalPan,
    viewport.endExternalPan,
    viewport.scale,
  ])

  /** 把父页面指针坐标映射为 iframe 内部逻辑坐标（未受父层 transform 缩放）。 */
  const toLiveFramePoint = (event) => {
    const viewportNode = viewportRef.current
    const frame = liveFrameRef.current
    if (!viewportNode || !frame || viewport.scale <= 0) return null
    // viewport 自己的 rect 已包含 scroll 与 transform，无需再手动加减
    // stage.scrollTop / translate，避免两套偏移量重复计算。
    const box = viewportNode.getBoundingClientRect()
    const width = frame.clientWidth || designWidth
    const height = frame.clientHeight || designHeight
    const x = (event.clientX - box.left) / viewport.scale
    const y = (event.clientY - box.top) / viewport.scale
    if (x < 0 || y < 0 || x > width || y > height) return null
    return { x, y }
  }

  const sendLiveInspect = (payload) => {
    const frame = liveFrameRef.current
    if (!frame?.contentWindow) return
    frame.contentWindow.postMessage({ channel: 'vibe-inspect', ...payload }, '*')
  }

  // Inspect 开关或 iframe 地址变化时同步 SDK；iframe 自己发 ready 时也会补发。
  useEffect(() => {
    if (mode !== 'live') return
    sendLiveInspect(message('enable', { enabled: Boolean(inspectMode) }))
    if (!inspectMode) setInspectHover(null)
    // sendLiveInspect 只做 postMessage，不需要进入依赖。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inspectMode, liveUrl, refreshNonce, mode, suspended])

  // iframe 重载后 SDK 需要重新握手，先撤销 ready 标记。
  useEffect(() => {
    liveReadyRef.current = false
    liveScrollRef.current = null
  }, [liveUrl, refreshNonce, mode, suspended])

  /*
   * 标注态下 iframe 设置了 pointer-events: none，滚轮事件到不了页面内部。
   * 为了让滚轮像真实浏览器一样滚动页面，这里把 delta 通过 postMessage
   * 转发给 SDK；只有 Ctrl / Cmd + 滚轮才留给 usePanZoom 做缩放。
   *
   * 交互态 iframe 自己消费滚轮，无需也不应重复转发。
   */
  useEffect(() => {
    const node = stageRef.current
    if (!node) return undefined

    const onWheel = (event) => {
      if (event.ctrlKey || event.metaKey) return
      if (mode !== 'live' || liveInteractive) return
      const frame = liveFrameRef.current
      if (!frame?.contentWindow || !liveReadyRef.current) return

      const unit =
        event.deltaMode === 1
          ? 16
          : event.deltaMode === 2
            ? node.clientHeight
            : 1
      const dx = event.deltaX * unit
      const dy = event.deltaY * unit
      // 边界链式滚动：页面还能滚就滚页面；页面已到顶 / 到底，
      // 剩余滚轮交还外层画布容器（用于查看 1440×900 视口的其余部分）。
      const snapshot = liveScrollRef.current
      if (!snapshot) return
      const scrollX = Number(snapshot.scrollX) || 0
      const scrollY = Number(snapshot.scrollY) || 0
      const maxX = Number(snapshot.maxX) || 0
      const maxY = Number(snapshot.maxY) || 0
      const canScrollX =
        dx < 0 ? scrollX > 0.5 : dx > 0 ? scrollX < maxX - 0.5 : false
      const canScrollY =
        dy < 0 ? scrollY > 0.5 : dy > 0 ? scrollY < maxY - 0.5 : false
      if (!canScrollX && !canScrollY) return

      event.preventDefault()
      frame.contentWindow.postMessage(
        {
          channel: 'vibe-inspect',
          type: 'scroll-by',
          payload: {
            dx: canScrollX ? dx : 0,
            dy: canScrollY ? dy : 0,
          },
        },
        '*',
      )
    }

    node.addEventListener('wheel', onWheel, { passive: false })
    return () => node.removeEventListener('wheel', onWheel)
  }, [mode, liveInteractive])

  const flashLimitNotice = () => {
    setLimitNotice(true)
    if (limitTimerRef.current) window.clearTimeout(limitTimerRef.current)
    limitTimerRef.current = window.setTimeout(() => setLimitNotice(false), 2600)
  }

  const activateAnnotation = (annotationId) => {
    setActiveAnnotationId(annotationId)
  }

  const deactivateAnnotation = (annotationId) => {
    setActiveAnnotationId((current) =>
      current === annotationId ? null : current,
    )
  }

  /**
   * Screen -> World：先减去画布在屏幕上的左上角与当前 translate，再除以
   * 当前 scale，最后归一化到固定 1440×900 逻辑视口。这样无论怎么平移 / 缩放，
   * 圈记录的都是页面内容坐标，而不是屏幕坐标。
   */
  const toPoint = (event) => {
    const node = viewportRef.current
    if (!node || viewport.scale <= 0) return { x: 0, y: 0 }
    const box = node.getBoundingClientRect()
    const worldX = (event.clientX - box.left) / viewport.scale
    const worldY = (event.clientY - box.top) / viewport.scale
    return {
      x: clamp01(worldX / designWidth),
      y: clamp01(worldY / designHeight),
    }
  }

  const handlePointerDown = (event) => {
    // 中键 / 空格 + 左键优先进入平移，避免误触发画布圈选。
    if (viewport.startPan(event)) {
      return
    }
    if (canvasInspect) {
      if (event.button !== 0) return
      event.preventDefault()
      event.stopPropagation()
      const point = toLiveFramePoint(event)
      if (point) sendLiveInspect(message('pick', point))
      return
    }
    if (disabled || !interactive || event.button !== 0) {
      return
    }
    // 交互态下指针还能落到 stage，说明点的是 iframe 外的灰色空白区域。
    // 先收回标注态，再让这一次按下继续参与圈选（拖拽即可顺势画圈）。
    if (liveInteractive) {
      setInteractionMode('annotate')
    }
    // 点击标注标签 = 选中该圈；不开始新的圈选。
    const annotationNode = event.target.closest?.('[data-annotation-id]')
    if (annotationNode) {
      activateAnnotation(annotationNode.getAttribute('data-annotation-id'))
      return
    }
    setActiveAnnotationId(null)
    if (atLimit) {
      flashLimitNotice()
      return
    }
    event.preventDefault()
    const point = toPoint(event)
    startTargetRef.current = readTarget(event.target)
    livePressRef.current = Boolean(
      event.target?.closest?.('.canvas-live-page'),
    )
    event.currentTarget.setPointerCapture(event.pointerId)
    setDrawing(true)
    setDraft({ originX: point.x, originY: point.y, x: point.x, y: point.y })
  }

  const handlePointerMove = (event) => {
    if (viewport.movePan(event)) {
      return
    }
    if (canvasInspect) {
      const point = toLiveFramePoint(event)
      if (point) {
        sendLiveInspect(message('move', point))
      } else {
        sendLiveInspect(message('clear'))
        setInspectHover(null)
      }
      return
    }
    if (!drawing || !draft) {
      return
    }
    const point = toPoint(event)
    setDraft((current) => (current ? { ...current, x: point.x, y: point.y } : current))
  }

  const finishDrawing = (event) => {
    if (viewport.endPan(event)) {
      return
    }
    if (canvasInspect) {
      event.preventDefault()
      return
    }
    if (!drawing || !draft) {
      return
    }
    const point = toPoint(event)
    const rect = normalizeRect(draft.originX, draft.originY, point.x, point.y)
    setDrawing(false)
    setDraft(null)

    // live 画布的“单击进入交互态”：按下与松开几乎同一点，且落在真实
    // 页面区域内。第一次点击只负责放行指针，第二次点击就能直接操作
    // 页面里的按钮；拖拽仍然保持圈选行为。
    const isPlainClick = rect.width <= 0.008 && rect.height <= 0.008
    const clickedLivePage = livePressRef.current
    livePressRef.current = false
    if (
      mode === 'live' &&
      interactionMode === 'annotate' &&
      clickedLivePage &&
      isPlainClick
    ) {
      setActiveAnnotationId(null)
      setInteractionMode('interact')
      startTargetRef.current = null
      return
    }

    if (rect.width > 0.02 && rect.height > 0.02) {
      // 把归一化选区还原为屏幕坐标，直接测量 DOM 节点的真实覆盖面积。
      // 不再只看“中心点命中谁”，否则空白区域会被误判成修改父容器。
      // 直接使用视口元素的屏幕矩形：滚动偏移与 zoom transform 都已包含，
      // 因此无论容器滚到哪里、缩放多少，选区的屏幕坐标都准确。
      const box = viewportRef.current.getBoundingClientRect()
      const left = box.left + rect.x * designWidth * viewport.scale
      const top = box.top + rect.y * designHeight * viewport.scale
      const screenRect = {
        left,
        top,
        right: left + rect.width * designWidth * viewport.scale,
        bottom: top + rect.height * designHeight * viewport.scale,
        width: rect.width * designWidth * viewport.scale,
        height: rect.height * designHeight * viewport.scale,
      }
      // live 模式的页面在 iframe 里，画布无法跨文档读取 data-source。
      // 圈选仍然保留为“页面区域新增”，后续由 Inspect / 预览面板精确定位。
      const pageRoot =
        mode === 'mock'
          ? stageRef.current.querySelector('.mock-page')
          : null
      const analysis =
        mode === 'live'
          ? {
              action: CREATE_NEW,
              target: LIVE_PAGE_TARGET,
              addTarget: LIVE_PAGE_TARGET,
              relativeRect: rect,
              measured: { mode: 'canvas-live' },
            }
          : analyzeSelection(
              pageRoot,
              screenRect,
              {
                x: left + screenRect.width / 2,
                y: top + screenRect.height / 2,
              },
              viewport.scale,
            )
      onAnnotationAdd?.({ rect, ...analysis })
    }
    startTargetRef.current = null
  }

  const movingAnnotation = movingId
    ? annotations.find((item) => item.id === movingId) || null
    : null

  /** 移动预览开始：只记录起点，不修改任何宽高 / left/top。 */
  const beginMoveDrag = (event) => {
    if (!movingAnnotation || blocked || disabled) return
    if (event.button !== 0) return
    event.preventDefault()
    event.stopPropagation()
    event.currentTarget.setPointerCapture?.(event.pointerId)
    moveDragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      baseX: moveOffset.x,
      baseY: moveOffset.y,
    }
  }

  /** 拖拽中只更新 transform 位移；DOM 尺寸保持不变，按钮不会“瞬间变长”。 */
  const moveMoveDrag = (event) => {
    const drag = moveDragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    event.preventDefault()
    event.stopPropagation()
    const scale = viewport.scale > 0 ? viewport.scale : 1
    setMoveOffset({
      x: drag.baseX + (event.clientX - drag.startX) / scale,
      y: drag.baseY + (event.clientY - drag.startY) / scale,
    })
  }

  /** 松手：像素位移转为自然语言，由 Agent 的 move_component Skill 执行。 */
  const endMoveDrag = (event) => {
    const drag = moveDragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    event.preventDefault()
    event.stopPropagation()
    const scale = viewport.scale > 0 ? viewport.scale : 1
    const dx = drag.baseX + (event.clientX - drag.startX) / scale
    const dy = drag.baseY + (event.clientY - drag.startY) / scale
    moveDragRef.current = null
    setMovingId(null)
    setMoveOffset({ x: 0, y: 0 })
    if (Math.abs(dx) >= 4 || Math.abs(dy) >= 4) {
      onAnnotationMove?.(movingId, dx, dy)
    }
  }

  const draftRect = draft
    ? normalizeRect(draft.originX, draft.originY, draft.x, draft.y)
    : null

  const skipPopovers = drawing || popoversHidden

  return (
    <div className="canvas-frame">
      <div className="canvas-chrome">
        <span className="chrome-dots">
          <i className="dot-red" />
          <i className="dot-amber" />
          <i className="dot-green" />
        </span>
      </div>

      <div
        ref={stageRef}
        className={`canvas-stage ${disabled || blocked ? 'is-disabled' : ''} ${drawing ? 'is-drawing' : ''} ${liveInteractive ? 'is-interacting' : ''} ${viewport.spaceHeld ? 'is-pan-ready' : ''} ${viewport.panning ? 'is-panning' : ''}`}
        data-canvas-interaction={mode === 'live' ? interactionMode : undefined}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={finishDrawing}
        onPointerCancel={finishDrawing}
        onAuxClick={(event) => event.preventDefault()}
      >
        {/* 页面层与标注层共享同一个 transform：两者“长在一起”。 */}
        <div
          ref={viewportRef}
          className="canvas-viewport"
          style={{
            width: `${designWidth}px`,
            height: `${designHeight}px`,
            transform: `translate(${viewport.translateX}px, ${viewport.translateY}px) scale(${viewport.scale})`,
            transformOrigin: 'top left',
          }}
        >
          {mode === 'mock' && (
            <div
              className="mock-page"
              data-source="page"
              data-source-kind="container"
            >
              <section
                className="mock-card"
                data-source="card"
                data-source-kind="container"
                data-vibe-target="card"
              >
                <p
                  className="mock-kicker"
                  data-source="kicker"
                  data-vibe-target="kicker"
                >
                  Vibe Studio
                </p>
                <h1
                  className="mock-title"
                  data-source="title"
                  data-vibe-target="title"
                >
                  把想法画出来。
                  <br />
                  剩下的交给 Agent。
                </h1>
                <p
                  className="mock-copy"
                  data-source="copy"
                  data-vibe-target="copy"
                >
                  在画布上圈选任意组件，写下修改意图。Agent 会生成代码、运行构建，并自动修复错误。
                </p>
                <button
                  className="mock-cta"
                  data-source="cta-button"
                  data-vibe-target="cta-button"
                  type="button"
                >
                  开始体验
                </button>
                <span className="cta-hint">圈选任意组件，写下你的修改意图</span>
              </section>
            </div>
          )}

          {mode === 'live' && (
            <div className="canvas-live-page" data-canvas-live>
              {liveUrl && !suspended ? (
                <iframe
                  key={`${liveUrl}:${refreshNonce}`}
                  ref={liveFrameRef}
                  className="canvas-live-frame"
                  src={withRefresh(liveUrl, refreshNonce)}
                  title="实时画布预览"
                  // 标注态：指针交给覆盖层，圈选 / 平移 / 缩放不被 iframe 吞掉。
                  // 交互态：把指针放行给真实页面，用户可以直接点击按钮。
                  style={{ pointerEvents: liveInteractive ? 'auto' : 'none' }}
                />
              ) : (
                <div className="canvas-live-empty">
                  {liveUrl ? (
                    '标签页已暂停，切回后自动恢复预览'
                  ) : (
                    <>
                      <span className="mini-spinner" aria-hidden="true" />
                      正在连接实时页面…
                    </>
                  )}
                </div>
              )}
            </div>
          )}

          {(mode === 'blank' ||
            mode === 'bootstrapping' ||
            mode === 'failed') && (
            <div className={`canvas-skeleton is-${mode}`} aria-hidden="true">
              <div className="canvas-skeleton-bar is-title" />
              <div className="canvas-skeleton-bar" />
              <div className="canvas-skeleton-bar is-short" />
              <div className="canvas-skeleton-grid">
                <span />
                <span />
                <span />
              </div>
            </div>
          )}

          <div className="annotation-layer">
            {annotations.map((annotation, index) => {
              const active = annotation.id === activeAnnotationId
              return (
                <div
                  key={annotation.id}
                  className={`annotation-ellipse tone-${index % 3} ${active ? 'is-active' : ''}`}
                  data-annotation-id={annotation.id}
                  data-annotation-index={index}
                  style={{
                    left: `${annotation.rect.x * 100}%`,
                    top: `${annotation.rect.y * 100}%`,
                    width: `${annotation.rect.width * 100}%`,
                    height: `${annotation.rect.height * 100}%`,
                    zIndex: active ? 100 : 20 + index,
                  }}
                >
                  <span className="annotation-tag">标注 {index + 1}</span>
                </div>
              )
            })}

            {draftRect && (
              <div
                className="annotation-ellipse is-draft"
                style={{
                  left: `${draftRect.x * 100}%`,
                  top: `${draftRect.y * 100}%`,
                  width: `${draftRect.width * 100}%`,
                  height: `${draftRect.height * 100}%`,
                }}
              >
                <span className="annotation-tag">新标注</span>
              </div>
            )}

            {movingAnnotation && (
              <div
                className={`move-frame ${moveDragRef.current ? 'is-dragging' : ''}`}
                data-move-frame
                style={{
                  left: `${movingAnnotation.rect.x * 100}%`,
                  top: `${movingAnnotation.rect.y * 100}%`,
                  width: `${movingAnnotation.rect.width * 100}%`,
                  height: `${movingAnnotation.rect.height * 100}%`,
                  transform: `translate3d(${moveOffset.x}px, ${moveOffset.y}px, 0)`,
                  willChange: 'transform',
                }}
                onPointerDown={beginMoveDrag}
                onPointerMove={moveMoveDrag}
                onPointerUp={endMoveDrag}
                onPointerCancel={endMoveDrag}
              >
                <span className="move-frame-label">
                  拖拽预览 · 松手交给 Agent 执行
                </span>
              </div>
            )}

            {annotations.map((annotation, index) => (
                <AnnotationPopover
                  key={annotation.id}
                  annotation={annotation}
                  index={index}
                  hidden={skipPopovers}
                  submitting={submitting}
                  blocked={blocked}
                  pending={pendingAnnotationId === annotation.id}
                  pendingAction={pendingAnnotationId === annotation.id ? pendingAction : null}
                  active={annotation.id === activeAnnotationId}
                  panReady={viewport.spaceHeld}
                  inputRef={(node) => {
                    if (node) inputRefs.current.set(annotation.id, node)
                    else inputRefs.current.delete(annotation.id)
                  }}
                  onChange={onAnnotationIntentChange}
                  onActionChange={onAnnotationActionChange}
                  onRemove={onAnnotationRemove}
                  onUnmount={onAnnotationUnmount}
                  onSend={onAnnotationSend}
                  onClarificationAnswer={onClarificationAnswer}
                  clarificationPending={Boolean(clarification)}
                  clarification={
                    clarification &&
                    (clarification.annotationId === annotation.id ||
                      (clarification.threadId &&
                        clarification.threadId === annotation.threadId))
                      ? clarification
                      : null
                  }
                  onRollback={onAnnotationRollback}
                  onActivate={activateAnnotation}
                  onDeactivate={deactivateAnnotation}
                  viewportWidth={designWidth}
                  viewportHeight={designHeight}
                />
              ))}
          </div>

          {/* 画布模式下的 Inspect 高亮：位于 canvas-viewport 内部，直接使用
              iframe 逻辑坐标，父层 transform 会自动完成缩放/平移映射。 */}
          {canvasInspect && inspectHover?.rect && (
            <div
              className="canvas-inspect-highlight"
              data-canvas-inspect-highlight
              style={{
                left: `${inspectHover.rect.x}px`,
                top: `${inspectHover.rect.y}px`,
                width: `${inspectHover.rect.width}px`,
                height: `${inspectHover.rect.height}px`,
              }}
            >
              <span className="canvas-inspect-label">
                {inspectHover.source?.component || inspectHover.tag || '组件'}
                {inspectHover.source?.file ? (
                  <em>
                    {inspectHover.source.file}
                    {inspectHover.source.line
                      ? `:${inspectHover.source.line}`
                      : ''}
                  </em>
                ) : null}
              </span>
            </div>
          )}
        </div>

        {mode === 'blank' && (
          <div
            className="canvas-start-state"
            onPointerDown={(event) => event.stopPropagation()}
          >
            <span className="canvas-start-kicker">ARCHITECT FIRST</span>
            <h2>从一个想法开始</h2>
            <p>
              点击开始，Architect 会通过多轮对话确认用户群体、核心功能、
              技术栈与数据实体，然后生成可运行的前端骨架。
            </p>
            <button
              type="button"
              className="canvas-start-button"
              onClick={onStartCreate}
            >
              <span aria-hidden="true">✨</span>
              开始创建
            </button>
            <span className="canvas-start-hint">
              不需要提前画原型，说清楚你要做什么即可
            </span>
          </div>
        )}

        {mode === 'bootstrapping' && (
          <div
            className="canvas-start-state is-busy"
            onPointerDown={(event) => event.stopPropagation()}
          >
            <span className="canvas-start-kicker">FRONTEND CODER</span>
            <h2>
              <span className="mini-spinner" aria-hidden="true" /> 等待架构设计完成
            </h2>
            <p>
              架构契约已确认，正在根据页面布局与组件列表生成 React 骨架，
              并执行构建与沙盒重启。
            </p>
            <div className="canvas-bootstrap-steps" role="status">
              <span>生成页面骨架</span>
              <span>校验构建</span>
              <span>重启沙盒</span>
            </div>
          </div>
        )}

        {mode === 'failed' && (
          <div
            className="canvas-start-state is-error"
            onPointerDown={(event) => event.stopPropagation()}
          >
            <span className="canvas-start-kicker">BOOTSTRAP FAILED</span>
            <h2>初始前端页面生成失败</h2>
            <p>
              架构契约已确认，但代码生成或沙盒启动没有完成。可以直接重试，
              也可以回到架构面板调整契约。
            </p>
            <div className="canvas-start-actions">
              <button
                type="button"
                className="canvas-start-button"
                onClick={onRetryBootstrap}
              >
                重试初始化
              </button>
              <button
                type="button"
                className="ghost-button"
                onClick={onOpenArchitect}
              >
                打开架构面板
              </button>
            </div>
          </div>
        )}

        {(atLimit || limitNotice) && (
          <div
            className={`annotation-limit ${limitNotice ? 'is-flashing' : ''}`}
            role="status"
            data-annotation-limit
          >
            最多支持 3 个标注 · {annotations.length}/3
            {limitNotice
              ? ' · 请先删除或清除一个标注，再继续圈选'
              : ' · 已达上限'}
          </div>
        )}

        <div className="viewport-controls" onPointerDown={(event) => event.stopPropagation()}>
          <span className="viewport-zoom" data-viewport-zoom>
            {viewport.zoomPercent}%
          </span>
          <button
            className="viewport-reset"
            type="button"
            onClick={viewport.reset}
            title="恢复 100% 并回到初始位置"
          >
            重置视图
          </button>
          {activeAnnotationId && (
            <button
              className={`viewport-reset canvas-move-toggle ${movingId ? 'is-active' : ''}`}
              type="button"
              data-canvas-move-toggle
              aria-pressed={Boolean(movingId)}
              disabled={blocked || disabled}
              title={
                movingId
                  ? '退出移动预览'
                  : '移动当前标注（快捷键 D）：拖拽只做预览，松手后由 Agent 执行'
              }
              onClick={() => {
                setMovingId((current) =>
                  current === activeAnnotationId ? null : activeAnnotationId,
                )
                setMoveOffset({ x: 0, y: 0 })
                moveDragRef.current = null
              }}
            >
              {movingId ? '✕ 退出移动' : '⇢ 移动 (D)'}
            </button>
          )}
          {mode === 'live' && (
            <button
              className={`viewport-reset canvas-interaction-toggle ${liveInteractive ? 'is-active' : ''}`}
              type="button"
              data-canvas-interaction-toggle
              aria-pressed={liveInteractive}
              title={
                liveInteractive
                  ? '回到标注态：圈选页面、平移和缩放'
                  : '浏览页面：让指针穿透到 iframe，可点击页面内按钮'
              }
              onClick={() =>
                setInteractionMode((current) =>
                  current === 'interact' ? 'annotate' : 'interact',
                )
              }
            >
              {liveInteractive ? '✋ 标注' : '🖱 浏览'}
            </button>
          )}
          {annotations.length > 0 && (
            <button
              className="viewport-reset"
              type="button"
              aria-pressed={popoversHidden}
              title="临时隐藏标注输入框，方便继续圈选下一个区域"
              onClick={() => setPopoversHidden((value) => !value)}
            >
              {popoversHidden ? '显示标注输入' : '隐藏标注输入'}
            </button>
          )}
        </div>

        <div className="canvas-hint">
          {mode === 'blank'
            ? '先完成架构对话，画布会在契约确认后自动生成'
            : mode === 'bootstrapping'
              ? '正在生成初始前端页面…'
              : mode === 'failed'
                ? '可重试初始化，或打开架构面板调整契约'
                : drawing
                  ? '松开鼠标完成圈选'
                  : mode === 'live'
                    ? liveInteractive
                      ? '浏览中 · 点击灰色空白回到标注 · 中键平移'
                      : '标注中 · 单击页面进入浏览 · 拖拽圈选 · 中键平移'
                    : interactive
                      ? '圈选页面区域 · 滚轮滚动页面 · Ctrl+滚轮缩放 · 空格/中键平移'
                      : ''}
        </div>
      </div>
    </div>
  )
}

const message = (type, extra = {}) => ({ type, ...extra })

// 时间轴 / 其他项目的 Run 更新不会改变画布 props；memo 避免整块画布重渲染。
export default memo(CanvasStage)
