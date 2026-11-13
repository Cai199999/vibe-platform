import { describe, expect, it } from 'vitest'
import {
  PREVIEW_STATE,
  previewLinkHint,
  resolveEffectiveLinked,
  resolvePreviewState,
} from '../previewState.js'

describe('预览状态 / 视口联动降级', () => {
  it('预览从 LIVE 掉线时禁用联动，但不覆盖 linkedPreference', () => {
    let linkedPreference = true
    const project = {
      previewUrl: 'http://127.0.0.1:3100/',
      sandboxId: 'sbx_123',
      hasSnapshot: true,
    }

    const live = resolvePreviewState({ ...project, connection: 'online' })
    const liveEffective = resolveEffectiveLinked(linkedPreference, live)

    const offline = resolvePreviewState({ ...project, connection: 'offline' })
    const degradedEffective = resolveEffectiveLinked(
      linkedPreference,
      offline,
    )

    // 用户偏好没有被任何一次状态迁移改写。
    const preferenceAfterDegrade = linkedPreference
    const recovered = resolvePreviewState({ ...project, connection: 'online' })
    const recoveredEffective = resolveEffectiveLinked(
      preferenceAfterDegrade,
      recovered,
    )

    console.log('[预览降级] LIVE -> effectiveLinked =', liveEffective)
    console.log(
      '[预览降级] offline -> previewState =',
      offline,
      'effectiveLinked =',
      degradedEffective,
    )
    console.log(
      '[预览降级] linkedPreference 保持 =',
      preferenceAfterDegrade,
    )
    console.log(
      '[预览降级] 恢复 LIVE -> effectiveLinked =',
      recoveredEffective,
    )

    expect(live).toBe(PREVIEW_STATE.LIVE)
    expect(liveEffective).toBe(true)
    expect(offline).toBe(PREVIEW_STATE.SNAPSHOT)
    expect(degradedEffective).toBe(false)
    expect(preferenceAfterDegrade).toBe(true)
    expect(recoveredEffective).toBe(true)
  })

  it('BOOTING / DEGRADED / SNAPSHOT / OFFLINE_EMPTY 均不可联动', () => {
    const states = [
      resolvePreviewState({
        previewUrl: 'http://x',
        sandboxId: 'sbx',
        connection: 'checking',
      }),
      resolvePreviewState({
        previewUrl: 'http://x',
        sandboxId: 'sbx',
        connection: 'offline',
        hasSnapshot: false,
      }),
      resolvePreviewState({
        previewUrl: 'http://x',
        sandboxId: 'sbx',
        connection: 'offline',
        hasSnapshot: true,
      }),
      resolvePreviewState({
        previewUrl: '',
        sandboxId: null,
        connection: 'idle',
      }),
    ]

    states.forEach((state) => {
      console.log(
        '[降级矩阵]',
        state,
        'effectiveLinked =',
        resolveEffectiveLinked(true, state),
        'hint =',
        previewLinkHint(state),
      )
      expect(resolveEffectiveLinked(true, state)).toBe(false)
      expect(previewLinkHint(state)).not.toBe('')
    })
    expect(states).toEqual([
      PREVIEW_STATE.BOOTING,
      PREVIEW_STATE.DEGRADED,
      PREVIEW_STATE.SNAPSHOT,
      PREVIEW_STATE.OFFLINE_EMPTY,
    ])
  })
})
