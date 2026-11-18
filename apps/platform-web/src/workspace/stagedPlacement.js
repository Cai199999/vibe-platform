/**
 * 暂存组件拖拽 / 落点排版的纯函数层。
 *
 * 这一层刻意不依赖 React、DOM 或 iframe，只负责三件事：
 *  1. 把指针坐标限制在 iframe 视口内，避免把 ghost 拖到网页外的黑边；
 *  2. 把 Drop 结果整理成结构化 placement（父容器 + 插入锚点 + 顺序）；
 *  3. 生成人类可读的校验文案，供 AnnotationEnvelope 的 acceptance 使用。
 *
 * 注意：placement 里的 relativeX / relativeY 只是布局提示，源码写入必须
 * 依赖 targetParentId + insertAnchorId + insertPosition，禁止生成绝对定位。
 */

export const DEFAULT_STAGE_VIEWPORT = Object.freeze({
  width: 1440,
  height: 900,
})

const round2 = (value) => Math.round((Number(value) || 0) * 100) / 100
const clamp01 = (value) => Math.min(1, Math.max(0, Number(value) || 0))

/** 拖拽 ghost 的归一化矩形必须完整落在 [0,1] 视口内。 */
export function clampGhostRect(rect) {
  const width = Math.min(1, Math.max(0, Number(rect?.width) || 0))
  const height = Math.min(1, Math.max(0, Number(rect?.height) || 0))
  const x = Math.min(Math.max(0, Number(rect?.x) || 0), 1 - width)
  const y = Math.min(Math.max(0, Number(rect?.y) || 0), 1 - height)
  return { x, y, width, height }
}

/**
 * 父页面 client 坐标 → iframe 视口 CSS 像素。
 *
 * viewportNode 已经包含画布的 translate / scale，因此只需除以 scale；
 * 不再手动叠加 stage.scrollTop，避免两套偏移量重复计算。
 */
export function clientToFramePoint({
  clientX,
  clientY,
  box,
  scale,
  width = DEFAULT_STAGE_VIEWPORT.width,
  height = DEFAULT_STAGE_VIEWPORT.height,
}) {
  if (!box || !Number.isFinite(scale) || scale <= 0) return null
  const x = (Number(clientX) - box.left) / scale
  const y = (Number(clientY) - box.top) / scale
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null
  return {
    x,
    y,
    inside: x >= 0 && y >= 0 && x <= width && y <= height,
  }
}

/** 把视口坐标夹到 iframe 边界，供“指针在画布外但仍需高亮边缘容器”时使用。 */
export function clampFramePoint(point, viewport = DEFAULT_STAGE_VIEWPORT) {
  const width = Number(viewport?.width) || DEFAULT_STAGE_VIEWPORT.width
  const height = Number(viewport?.height) || DEFAULT_STAGE_VIEWPORT.height
  return {
    x: Math.min(Math.max(0, Number(point?.x) || 0), width),
    y: Math.min(Math.max(0, Number(point?.y) || 0), height),
  }
}

/** 归一化 ghost 矩形 → iframe 文档坐标（滚动后 ghost 仍吸附在页面上）。 */
export function ghostRectToDocumentRect(rect, scroll = {}) {
  const width = Number(scroll.width) || DEFAULT_STAGE_VIEWPORT.width
  const height = Number(scroll.height) || DEFAULT_STAGE_VIEWPORT.height
  const scrollX = Number(scroll.scrollX) || 0
  const scrollY = Number(scroll.scrollY) || 0
  return {
    x: round2(rect.x * width + scrollX),
    y: round2(rect.y * height + scrollY),
    width: round2(rect.width * width),
    height: round2(rect.height * height),
  }
}

/**
 * 父容器稳定身份。
 *
 * data-source 的值是 file:line:column，同一组件渲染多次时完全相同；
 * instanceId 额外带 #instance，才能区分“第几个实例”。
 */
export function parentAnchorId(parent) {
  if (!parent) return 'page'
  if (parent.instanceId) return String(parent.instanceId)
  if (parent.marker) {
    const instance = Number(parent.instance) || 1
    return `${parent.marker}#${instance}`
  }
  return 'page'
}

/** 只保留 Resolver 需要的字段，避免把 computed style 等大对象写进草稿。 */
export function pickParentDescriptor(parent) {
  if (!parent) return null
  const fields = [
    'marker',
    'selector',
    'label',
    'role',
    'component',
    'tag',
    'kind',
    'file',
    'line',
    'column',
    'sourceKind',
    'instance',
    'instanceCount',
    'instanceId',
    'instanceKey',
    'instanceProps',
    'callSite',
    'containerScope',
    'callSiteOid',
    'callSiteFile',
    'callSiteRange',
  ]
  const picked = {}
  fields.forEach((field) => {
    const value = parent[field]
    if (value === undefined || value === null || value === '') return
    picked[field] = value
  })
  return Object.keys(picked).length ? picked : null
}

/**
 * Drop 结果 → 可直接写回 pending addition 的 patch。
 *
 * hit 由 iframe SDK 的 stage-hit 返回：
 *   parent / documentRect / insertAnchor / insertPosition / siblingIndex ...
 */
export function buildPlacementPatch({
  ghostRect,
  hit,
  scroll,
  now = Date.now(),
}) {
  const safeGhost = clampGhostRect(ghostRect)
  const viewport = {
    width: Number(scroll?.width) || DEFAULT_STAGE_VIEWPORT.width,
    height: Number(scroll?.height) || DEFAULT_STAGE_VIEWPORT.height,
    scrollX: Number(scroll?.scrollX) || 0,
    scrollY: Number(scroll?.scrollY) || 0,
  }
  const documentRect = ghostRectToDocumentRect(safeGhost, viewport)
  const parentDocumentRect = hit?.documentRect || null
  const parentWidth = Number(hit?.rect?.width) || Number(hit?.parentWidth) || 0
  const parentHeight = Number(hit?.rect?.height) || Number(hit?.parentHeight) || 0
  const relativeX =
    parentDocumentRect && Number.isFinite(Number(parentDocumentRect.x))
      ? round2(documentRect.x - Number(parentDocumentRect.x))
      : null
  const relativeY =
    parentDocumentRect && Number.isFinite(Number(parentDocumentRect.y))
      ? round2(documentRect.y - Number(parentDocumentRect.y))
      : null
  const relativeRect =
    relativeX !== null &&
    relativeY !== null &&
    parentWidth > 0 &&
    parentHeight > 0
      ? {
          x: round2(clamp01(relativeX / parentWidth)),
          y: round2(clamp01(relativeY / parentHeight)),
          width: round2(clamp01(documentRect.width / parentWidth)),
          height: round2(clamp01(documentRect.height / parentHeight)),
          relativeX,
          relativeY,
          widthPx: documentRect.width,
          heightPx: documentRect.height,
          parentWidth: round2(parentWidth),
          parentHeight: round2(parentHeight),
          coordinateSpace: 'parent-relative',
        }
      : null

  const parent = pickParentDescriptor(hit?.parent)
  // 兄弟锚点必须携带完整 file / line / column：Resolver 只拿到一个
  // instanceId 字符串无法在源码里定位到具体 JSX 兄弟节点。
  const insertAnchor = pickParentDescriptor(hit?.insertAnchor)
  const placement = {
    version: 1,
    targetParentId: parentAnchorId(parent),
    parent,
    parentCallSiteOid: parent?.callSiteOid || '',
    parentContainerScope: parent?.containerScope || 'local',
    parentCallSiteRange: parent?.callSiteRange || null,
    insertAnchor,
    insertAnchorId:
      insertAnchor?.instanceId || insertAnchor?.marker || '',
    insertPosition: hit?.insertPosition || 'inside_end',
    siblingIndex: Number.isFinite(Number(hit?.siblingIndex))
      ? Number(hit.siblingIndex)
      : null,
    siblingCount: Number.isFinite(Number(hit?.siblingCount))
      ? Number(hit.siblingCount)
      : null,
    childIndex: Number.isFinite(Number(hit?.childIndex))
      ? Number(hit.childIndex)
      : null,
    relativeX,
    relativeY,
    widthPx: documentRect.width,
    heightPx: documentRect.height,
    parentWidth: round2(parentWidth),
    parentHeight: round2(parentHeight),
    confidence: hit?.confidence || 'exact',
    measuredAt: now,
  }

  return {
    rect: safeGhost,
    documentRect,
    frameScroll: {
      scrollX: viewport.scrollX,
      scrollY: viewport.scrollY,
      width: viewport.width,
      height: viewport.height,
    },
    // CREATE_NEW 的目标必须是父容器；source 同步写父容器，方便 Resolver
    // 拿到 file / line / column / instance，而不是把新增 JSX 塞进叶子节点。
    addTarget: parent,
    source: parent,
    relativeRect,
    placement,
  }
}

/** 高亮 Tooltip：让用户确认“对，我就是要塞进这个盒子里”。 */
export function stageTargetLabel(hit) {
  const parent = hit?.parent || {}
  const isPage = !parent.selector || parent.marker === 'page'
  const raw = String(parent.label || parent.component || parent.tag || '页面')
  const suffix = isPage ? '' : parent.kind === 'container' ? ' 容器' : ' 组件'
  let text = `即将放入：${raw}${suffix}`
  if (hit?.confidence && hit.confidence !== 'exact') text += '（低置信）'
  if (Number(parent.instanceCount) > 1) text += ' · 多处复用'
  return text
}

/**
 * 写进 AnnotationEnvelope.acceptance 的确定性插入约束。
 *
 * 这段文案会随 Prompt 进入 Coder，明确禁止用绝对定位模拟位置。
 */
export function formatPlacementAcceptance(placement, parentLabel = '父容器') {
  if (!placement) return ''
  const position = placement.insertPosition || 'inside_end'
  // before / after 是 SDK 相对兄弟节点的原始语义；文字里必须说明它
  // 仍发生在父容器内部，避免模型把新增节点插到父容器外面。
  const positionText =
    position === 'before'
      ? '父容器内部，位于兄弟锚点之前'
      : position === 'after'
        ? '父容器内部，位于兄弟锚点之后'
        : position
  const anchor = placement.insertAnchorId || '无'
  const index = Number.isFinite(Number(placement.siblingIndex))
    ? Number(placement.siblingIndex)
    : '未知'
  return (
    `插入到「${parentLabel}」内部（位置：${positionText}；` +
    `兄弟锚点：${anchor}；顺序索引：${index}），` +
    '按父容器自然文档流插入，禁止写死 left / top 或 position:absolute'
  )
}
