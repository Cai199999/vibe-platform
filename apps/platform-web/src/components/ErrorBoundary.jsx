import { Component } from 'react'

/**
 * 区域级错误边界。
 *
 * 画布与预览是两棵互相独立的渲染树，任何一棵抛错都不应该让整个工作区白屏。
 * 这里用 class 组件实现 React 官方错误边界协议，并把 fallback 限制在
 * 当前面板内部；父级再包一层，覆盖路由 / 顶栏等公共区域。
 */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
    this.handleReset = this.handleReset.bind(this)
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    // 保留完整堆栈，便于线上问题定位；不向上抛出，避免整页崩溃。
    console.error('[Vibe ErrorBoundary]', this.props.label ?? 'unknown', error, info)
  }

  componentDidUpdate(previousProps) {
    // Tab 切换 / 项目重建时自动恢复，不需要用户手动刷新页面。
    if (
      this.state.error &&
      previousProps.resetKey !== this.props.resetKey
    ) {
      this.setState({ error: null })
    }
  }

  handleReset() {
    this.setState({ error: null })
  }

  render() {
    if (!this.state.error) return this.props.children
    const { label = '当前区域', compact = false } = this.props
    return (
      <div
        className={`error-boundary ${compact ? 'is-compact' : ''}`}
        role="alert"
        data-error-boundary={label}
      >
        <span className="error-boundary-code">UI_RENDER_FAILED</span>
        <h3>{label}暂时不可用</h3>
        <p>
          页面其他区域仍可正常使用。可以尝试重试；如果问题持续出现，请刷新页面。
        </p>
        <div className="error-boundary-actions">
          <button type="button" className="ghost-button" onClick={this.handleReset}>
            重试当前区域
          </button>
          <button
            type="button"
            className="ghost-button"
            onClick={() => window.location.reload()}
          >
            刷新页面
          </button>
        </div>
      </div>
    )
  }
}
