import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'

const STORAGE_KEY = 'vibe.settings.v1'

/**
 * 用户级设置默认值。
 *
 * requireManualApproval 关闭时，Python AI Core 在构建验证通过后
 * 直接提交 Git checkpoint 并返回 SUCCEEDED；开启后才进入审批暂停。
 */
const DEFAULT_SETTINGS = {
  requireManualApproval: false,
}

function normalize(raw) {
  if (!raw || typeof raw !== 'object') return { ...DEFAULT_SETTINGS }
  return {
    ...DEFAULT_SETTINGS,
    requireManualApproval: Boolean(raw.requireManualApproval),
  }
}

function readSettings() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return { ...DEFAULT_SETTINGS }
    return normalize(JSON.parse(raw))
  } catch {
    return { ...DEFAULT_SETTINGS }
  }
}

const SettingsContext = createContext(null)

/**
 * 全局设置 Provider。
 *
 * 状态以 Context 为唯一来源，并同步写入 localStorage；同一浏览器窗口内
 * 所有页面（工作台、设置弹窗、Run 请求组装）读取到的是同一个值。
 */
export function SettingsProvider({ children }) {
  const [settings, setSettings] = useState(readSettings)

  // 设置变化后立即持久化；写入失败（隐私模式 / 配额满）不影响当前会话。
  useEffect(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(settings))
    } catch {
      // 浏览器禁用 localStorage 时仅保留内存状态。
    }
  }, [settings])

  const setRequireManualApproval = useCallback((value) => {
    setSettings((previous) => ({
      ...previous,
      requireManualApproval: Boolean(value),
    }))
  }, [])

  const value = useMemo(
    () => ({
      ...settings,
      setRequireManualApproval,
    }),
    [settings, setRequireManualApproval],
  )

  return <SettingsContext.Provider value={value}>{children}</SettingsContext.Provider>
}

export function useSettings() {
  const value = useContext(SettingsContext)
  if (!value) {
    throw new Error('useSettings 必须在 SettingsProvider 内使用')
  }
  return value
}
