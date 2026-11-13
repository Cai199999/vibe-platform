import { useEffect, useState } from 'react'

/**
 * 预览 URL 存活探测。
 *
 * 沙盒容器可能在页面刷新、服务重启或清理后被销毁，但 sessionStorage 里仍
 * 保留旧的 previewUrl。此时 iframe 只会显示浏览器自己的“连接失败”页面。
 * 用 no-cors 请求探活：能连上就返回 opaque response，连接拒绝会 reject。
 *
 * 这里只返回 idle / checking / online / offline 四个底层探测状态；
 * 业务侧的 LIVE / DEGRADED / SNAPSHOT 由 resolvePreviewState 统一映射。
 */
export function usePreviewConnection(previewUrl, active) {
  const [status, setStatus] = useState('idle')

  useEffect(() => {
    if (!previewUrl || !active) {
      setStatus('idle')
      return undefined
    }

    let cancelled = false
    const controller = new AbortController()
    // 端口被防火墙丢包时 fetch 可能长时间挂起，4s 后强制判定失联。
    const timeout = window.setTimeout(() => controller.abort(), 4000)
    setStatus('checking')

    const probe = async () => {
      for (let attempt = 0; attempt < 2; attempt += 1) {
        try {
          await fetch(previewUrl, {
            mode: 'no-cors',
            cache: 'no-store',
            signal: controller.signal,
          })
          window.clearTimeout(timeout)
          if (!cancelled) setStatus('online')
          return
        } catch {
          if (cancelled) return
          if (attempt === 0) {
            // Vite 刚启动时可能有一瞬间不可达，重试一次再判定失败。
            await new Promise((resolve) => setTimeout(resolve, 400))
          }
        }
      }
      window.clearTimeout(timeout)
      if (!cancelled) setStatus('offline')
    }

    void probe()
    return () => {
      cancelled = true
      window.clearTimeout(timeout)
      controller.abort()
    }
  }, [previewUrl, active])

  return status
}
