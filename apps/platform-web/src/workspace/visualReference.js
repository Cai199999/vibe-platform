/**
 * 可视化选型（Visual Reference）共享工具。
 *
 * 大模型无法跨域读取沙盒 DOM，用户回答“和登录按钮一样”时，只能靠模型
 * 脑补颜色和圆角。这里负责识别这类回答，并把 SDK 点选到的真实类名 /
 * computed CSS 封装成统一契约，供画布、预览与提交链路复用。
 */

/** “和…一样 / 参考… / 同款”等指代式回答。 */
const REFERENCE_INTENT_RE =
  /(?:和|与|跟).{0,24}(?:一样|一致|相同|同款)|像.{0,24}(?:一样|那样|这种)|参考.{0,32}|照着.{0,32}|仿照.{0,32}|同款|风格一致|same\s+as|like\s+the|look\s+like|match(?:es)?\s+the/i

export function isVisualReferenceText(text) {
  return REFERENCE_INTENT_RE.test(String(text || ''))
}

/**
 * 样式澄清里的“和现有主按钮一致”等 quick option 必须取真实 reference。
 *
 * 普通业务澄清的 quick option 追求一键提交；但样式选项的文案本身就是
 * 指代式回答（“和…一致”），如果直接提交文本，Coder 拿不到主按钮的
 * class / computed style，只能复刻颜色。这里只对 `style.*` 问题返回 true，
 * 其余问题保持原有“一键提交”行为。
 */
export function shouldPickReference(questionId, value) {
  return (
    String(questionId || '').startsWith('style.') &&
    isVisualReferenceText(value)
  )
}

/** 样式 quick option → Architect 默认样式 key。 */
export function styleDefaultKey(questionId, value) {
  if (!String(questionId || '').startsWith('style.')) return ''
  const text = String(value || '')
  if (/次要|secondary|描边|幽灵|ghost|outline/i.test(text)) {
    return 'secondary_button'
  }
  if (/登录|login|sign\s*in/i.test(text)) return 'login_button'
  if (/主按钮|主色|primary|主要/i.test(text)) return 'primary_button'
  if (/按钮|button/i.test(text)) return 'button'
  return ''
}

/**
 * Architect 默认样式 → 后端 VisualReference 契约。
 *
 * 字段形状与 buildVisualReference 保持一致；只是来源从“用户点选的 DOM”
 * 变成 Architecture 阶段写入 VisualDocument 的真实 Tailwind 类。
 */
export function referenceFromComponentStyle(style, fallbackLabel = '默认组件样式') {
  if (!style || typeof style !== 'object') return null
  const classes = Array.isArray(style.classes)
    ? style.classes.map((item) => String(item || '').trim()).filter(Boolean)
    : []
  if (!classes.length && !style.styles) return null
  return {
    kind: 'visual_reference',
    label: style.label || fallbackLabel,
    tag: style.tag || '',
    classes: classes.slice(0, 60),
    styles: { ...(style.styles || {}) },
    source: style.source || null,
    capturedAt: new Date().toISOString(),
  }
}

/** SDK picked 载荷 → 后端 VisualReference 契约。 */
export function buildVisualReference(payload) {
  if (!payload?.source?.file) return null
  const styles = payload.styles || payload.source?.styleSignature || {}
  const source = payload.source || {}
  return {
    kind: 'visual_reference',
    label: payload.label || source.component || source.tag || '参考组件',
    tag: payload.tag || source.tag || '',
    classes: Array.isArray(styles.tailwindClasses)
      ? styles.tailwindClasses.slice(0, 60)
      : [],
    styles: {
      ...styles,
      // tailwindClasses 已在 classes 中单独表达，避免重复占 Token。
      tailwindClasses: undefined,
    },
    source: {
      file: source.file,
      line: Number(source.line) || 1,
      column: Number(source.column) || 1,
      component: source.component || '',
      instance: Number(source.instance) || undefined,
      instanceCount: Number(source.instanceCount) || undefined,
      instanceId: source.instanceId || undefined,
    },
    capturedAt: new Date().toISOString(),
  }
}

/** 参考样式摘要：进入聊天记录 / 幽灵预览，用户可核对提取结果。 */
export function formatReferenceComment(reference) {
  if (!reference) return ''
  const classes = (reference.classes || []).slice(0, 12).join(' ')
  const styles = reference.styles || {}
  const facts = [
    styles.backgroundColor && `背景 ${styles.backgroundColor}`,
    styles.color && `文字 ${styles.color}`,
    styles.borderRadius && `圆角 ${styles.borderRadius}`,
    styles.fontSize && `字号 ${styles.fontSize}`,
    styles.padding && `内边距 ${styles.padding}`,
  ].filter(Boolean)
  const head = `参考「${reference.label || reference.tag || '组件'}」真实样式`
  return [
    head,
    classes ? `类名 ${classes}` : '',
    facts.length ? `（${facts.join('，')}）` : '',
  ]
    .filter(Boolean)
    .join('；')
}

/**
 * 从本轮澄清答案中提取所有可读文本，用于判断是否出现指代式回答。
 * 数组答案（多选）逐项展开；对象只取 value，避免把 questionId 也当文本。
 */
export function collectReferenceText(...sources) {
  const parts = []
  const visit = (value) => {
    if (value === undefined || value === null) return
    if (Array.isArray(value)) {
      value.forEach(visit)
      return
    }
    if (typeof value === 'object') {
      if ('value' in value) visit(value.value)
      return
    }
    const text = String(value).trim()
    if (text) parts.push(text)
  }
  sources.forEach(visit)
  return parts.join(' ')
}
