/**
 * 预览状态机（纯函数，便于单测）。
 *
 *   BOOTING / DEGRADED / SNAPSHOT / OFFLINE_EMPTY 都不具备“实时双向操作”
 *   能力；只有 LIVE 才允许视口联动与预览侧交互。
 *
 * 预览断开时绝不能把用户的 linkedPreference 改掉：降级只是“暂时不可用”，
 * 恢复 LIVE 后应自动回到用户原来的选择。
 */
export const PREVIEW_STATE = Object.freeze({
  LIVE: 'LIVE',
  BOOTING: 'BOOTING',
  DEGRADED: 'DEGRADED',
  SNAPSHOT: 'SNAPSHOT',
  OFFLINE_EMPTY: 'OFFLINE_EMPTY',
})

export function resolvePreviewState({
  previewUrl,
  sandboxId,
  connection,
  hasSnapshot = false,
}) {
  if (!previewUrl || !sandboxId) return PREVIEW_STATE.OFFLINE_EMPTY
  if (connection === 'online') return PREVIEW_STATE.LIVE
  if (connection === 'checking') return PREVIEW_STATE.BOOTING
  if (connection === 'offline') {
    return hasSnapshot ? PREVIEW_STATE.SNAPSHOT : PREVIEW_STATE.DEGRADED
  }
  // idle：当前 Tab 未激活 / 尚未开始探测。保守按不可联动处理。
  return PREVIEW_STATE.DEGRADED
}

export function isPreviewLive(state) {
  return state === PREVIEW_STATE.LIVE
}

/** 视口联动是否真正生效：用户偏好打开 + 预览处于 LIVE。 */
export function resolveEffectiveLinked(linkedPreference, previewState) {
  return Boolean(linkedPreference) && isPreviewLive(previewState)
}

/** 供 UI 展示的弱提示；LIVE 时不展示。 */
export function previewLinkHint(previewState) {
  switch (previewState) {
    case PREVIEW_STATE.LIVE:
      return ''
    case PREVIEW_STATE.BOOTING:
      return '预览启动中，视口联动稍后可用'
    case PREVIEW_STATE.SNAPSHOT:
      return '当前为快照预览，视口联动已暂停'
    case PREVIEW_STATE.OFFLINE_EMPTY:
      return '暂无实时预览，视口联动不可用'
    default:
      return '预览连接中断，视口联动已暂停'
  }
}
