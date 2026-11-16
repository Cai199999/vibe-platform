import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createRoot } from 'react-dom/client'
import DirectOpsDecisionDialog from '../DirectOpsDecisionDialog.jsx'

const DECISION = {
  decisionId: 'ddec_test',
  reasons: ['列表项由数据源生成，不能只删除一个 JSX 节点'],
  scope: 'data_source',
  strategy: 'hitl',
  anchor: { file: 'src/App.jsx', component: 'App' },
  source: { line: 8 },
  options: [
    {
      optionId: 'remove_data_item',
      label: '删除对应的数据项（AI 定位数据源）',
      action: 'AI_DATA_EDIT',
    },
    {
      optionId: 'visual_hide',
      label: '仅隐藏视觉上的当前项',
      action: 'VISUAL_ONLY',
    },
    { optionId: 'cancel', label: '取消', action: 'CANCEL' },
  ],
}

describe('DirectOpsDecisionDialog', () => {
  let container
  let root

  beforeEach(() => {
    globalThis.IS_REACT_ACT_ENVIRONMENT = true
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => root.unmount())
    container.remove()
    globalThis.IS_REACT_ACT_ENVIRONMENT = false
  })

  it('渲染 ScopePolicy 选项并只回传 optionId', () => {
    const onChoose = vi.fn()
    act(() => {
      root.render(
        <DirectOpsDecisionDialog
          decision={DECISION}
          onChoose={onChoose}
          onClose={() => {}}
        />,
      )
    })

    expect(container.textContent).toContain('需要你选择安全路径')
    expect(container.textContent).toContain('src/App.jsx')
    const visualButton = [...container.querySelectorAll('button')].find(
      (button) => button.textContent.includes('仅隐藏视觉'),
    )
    expect(visualButton).toBeTruthy()

    act(() => {
      visualButton.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    expect(onChoose).toHaveBeenCalledWith('visual_hide')
  })

  it('右上角关闭等价于取消决策', () => {
    const onChoose = vi.fn()
    act(() => {
      root.render(
        <DirectOpsDecisionDialog decision={DECISION} onChoose={onChoose} />,
      )
    })

    const closeButton = container.querySelector('button[aria-label="关闭"]')
    act(() => {
      closeButton.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    expect(onChoose).toHaveBeenCalledWith('cancel')
  })
})
