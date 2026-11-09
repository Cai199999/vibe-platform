/**
 * 画布标注动作语义（前端唯一入口）。
 *
 * 后端 Planner 已区分：
 *  - CREATE_NEW     新增组件：只生成组件主体，位置由用户拖拽后回写；
 *  - MODIFY_EXISTING 修改已有元素（含移动 / 重排）。
 *
 * 早期版本使用 ADD / MODIFY，localStorage 与旧会话里仍可能残留；
 * 这里集中做归一化，避免各组件各自维护一份字符串比较。
 */
export const CREATE_NEW = 'CREATE_NEW'
export const MODIFY_EXISTING = 'MODIFY_EXISTING'

const CREATE_ACTIONS = new Set(['ADD', CREATE_NEW])
const MODIFY_ACTIONS = new Set(['MODIFY', MODIFY_EXISTING])

/** 兼容旧值：ADD / CREATE_NEW 一律返回 CREATE_NEW，其余返回 MODIFY_EXISTING。 */
export function normalizeAction(action) {
  const value = String(action || '').trim().toUpperCase()
  if (CREATE_ACTIONS.has(value)) return CREATE_NEW
  if (MODIFY_ACTIONS.has(value)) return MODIFY_EXISTING
  return MODIFY_EXISTING
}

export function isCreateAction(action) {
  return normalizeAction(action) === CREATE_NEW
}

export function isModifyAction(action) {
  return normalizeAction(action) === MODIFY_EXISTING
}
