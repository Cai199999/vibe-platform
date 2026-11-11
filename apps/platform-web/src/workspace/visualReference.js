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
