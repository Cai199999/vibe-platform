import { describe, expect, it } from 'vitest'
import {
  isVisualReferenceText,
  referenceFromComponentStyle,
  shouldPickReference,
  styleDefaultKey,
} from '../visualReference.js'

describe('样式澄清的 reference 判定', () => {
  it('识别指代式样式文案', () => {
    expect(isVisualReferenceText('和现有主按钮一致')).toBe(true)
    expect(isVisualReferenceText('和登录按钮一样')).toBe(true)
    expect(isVisualReferenceText('添加按钮')).toBe(false)
  })

  it('只有 style.* 的指代式选项才进入取色流程', () => {
    expect(
      shouldPickReference('style.appearance', '和现有主按钮一致'),
    ).toBe(true)
    // 业务澄清的 quick option 仍然一键提交。
    expect(
      shouldPickReference('project.scope', '和现有主按钮一致'),
    ).toBe(false)
    // 样式问题但如果选项不是指代式（例如具体色值），不需要取色。
    expect(shouldPickReference('style.appearance', '#2563eb')).toBe(false)
  })

  it('样式选项映射到 Architect 默认样式 key', () => {
    expect(styleDefaultKey('style.appearance', '和现有主按钮一致')).toBe(
      'primary_button',
    )
    expect(styleDefaultKey('style.appearance', '和现有次要按钮一致')).toBe(
      'secondary_button',
    )
    expect(styleDefaultKey('style.appearance', '和登录按钮一样')).toBe(
      'login_button',
    )
    expect(styleDefaultKey('project.scope', '和现有主按钮一致')).toBe('')
  })

  it('Architect 默认样式可生成结构化 reference', () => {
    const reference = referenceFromComponentStyle(
      {
        label: '主按钮',
        tag: 'button',
        classes: ['px-4', 'py-2', 'bg-[var(--color-primary)]', 'rounded'],
        styles: { borderRadius: 'var(--radius-sm)' },
      },
      '主按钮',
    )
    expect(reference.kind).toBe('visual_reference')
    expect(reference.label).toBe('主按钮')
    expect(reference.classes).toContain('bg-[var(--color-primary)]')
    expect(reference.styles.borderRadius).toBe('var(--radius-sm)')
    expect(referenceFromComponentStyle(null)).toBeNull()
  })
})
