/**
 * Vibe Inspect SDK
 * ------------------------------------------------------------------
 * 该脚本由 vite 插件注入到沙盒 index.html 中。平台预览 iframe 与沙盒
 * 跨域，父页面无法直接访问 DOM，因此双方通过 postMessage 通信：
 *
 *   parent -> iframe: { channel:'vibe-inspect', type:'enable'|'move'|'pick'|'clear'|'measure' }
 *   iframe -> parent: { channel:'vibe-inspect', type:'ready'|'hover'|'picked'|'measured' }
 *
 * 坐标全部使用 iframe 内部视口 CSS 像素（1280×720 设计视口），
 * 由父页面根据 iframe 的 transform scale 反算后传入。
 *
 * 安全边界：只读取 DOM 属性与 rect，不读取 cookie / storage，不执行
 * 父页面传入的任何代码。
 */
(function () {
  'use strict'

  var CHANNEL = 'vibe-inspect'
  var enabled = false
  var overlay = null
  var CONTAINER_TAGS = {
    div: true,
    section: true,
    main: true,
    article: true,
    header: true,
    footer: true,
    nav: true,
    aside: true,
    ul: true,
    ol: true,
    form: true,
    table: true,
  }

  function reply(source, origin, type, payload) {
    if (!source || typeof source.postMessage !== 'function') return
    source.postMessage(
      { channel: CHANNEL, type: type, payload: payload },
      origin || '*',
    )
  }

  function ensureOverlay() {
    if (overlay && overlay.isConnected) return overlay
    overlay = document.createElement('div')
    overlay.setAttribute('data-vibe-inspect-overlay', 'true')
    overlay.style.cssText = [
      'position:fixed',
      'pointer-events:none',
      'z-index:2147483646',
      'border:2px solid #38bdf8',
      'background:rgba(56,189,248,.10)',
      'box-shadow:0 0 0 1px rgba(2,6,23,.55),0 0 18px rgba(56,189,248,.45)',
      'border-radius:2px',
      'box-sizing:border-box',
      'transition:none',
    ].join(';')
    document.documentElement.appendChild(overlay)
    return overlay
  }

  function clearOverlay() {
    if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay)
    overlay = null
  }

  function sourceKindFor(file) {
    var lower = String(file || '').toLowerCase()
    if (lower.endsWith('.vue')) return 'vue'
    if (lower.endsWith('.html')) return 'html'
    if (lower.endsWith('.tsx') || lower.endsWith('.jsx')) return 'jsx'
    if (lower.endsWith('.ts')) return 'ts'
    return 'jsx'
  }

  /**
   * 读取节点的源码出处。
   *
   * 优先级：
   *  1. data-source-file / data-source-line / data-source-column（插件注入）
   *  2. data-source 中形如 "src/App.jsx:12:5" 的生成标记
   *  3. 仅有业务 marker 时保留 marker，但 file/line 为空
   */
  function readSource(node) {
    if (!node || typeof node.getAttribute !== 'function') return null
    var file = node.getAttribute('data-source-file') || ''
    var line = Number(node.getAttribute('data-source-line') || 0)
    var column = Number(node.getAttribute('data-source-column') || 0)
    var component = node.getAttribute('data-source-component') || ''
    var marker =
      node.getAttribute('data-source') ||
      node.getAttribute('data-vibe-target') ||
      ''
    if (!file && marker) {
      var generated = /^(.+?):(\d+):(\d+)$/.exec(marker)
      if (generated) {
        file = generated[1]
        line = Number(generated[2])
        column = Number(generated[3])
      }
    }
    var kind =
      node.getAttribute('data-source-kind') ||
      (CONTAINER_TAGS[String(node.tagName || '').toLowerCase()]
        ? 'container'
        : 'element')
    return {
      file: file,
      line: line,
      column: column,
      component: component || 'Component',
      marker: marker,
      sourceKind: sourceKindFor(file),
      kind: kind,
      tag: String(node.tagName || '').toLowerCase(),
      selector: marker ? '[data-source="' + marker + '"]' : '',
    }
  }

  function rectOf(node) {
    var rect = node.getBoundingClientRect()
    return {
      x: Math.round(rect.left * 100) / 100,
      y: Math.round(rect.top * 100) / 100,
      width: Math.round(rect.width * 100) / 100,
      height: Math.round(rect.height * 100) / 100,
    }
  }

  function round2(value) {
    return Math.round(value * 100) / 100
  }

  /**
   * 相对父容器的几何信息。
   *
   * SDK 运行在预览 iframe 内，rect / containerRect 已经是 1280×720 设计
   * 视口下的 CSS 像素，因此这里不需要再除 scale。归一化字段用于兼容旧
   * 契约；relativeX/Y 等像素字段供模型做精确落位（absolute / margin /
   * grid 偏移）。
   */
  function relativeRect(rect, containerRect) {
    if (!containerRect || containerRect.width <= 0 || containerRect.height <= 0) {
      return null
    }
    return {
      x: (rect.x - containerRect.x) / containerRect.width,
      y: (rect.y - containerRect.y) / containerRect.height,
      width: rect.width / containerRect.width,
      height: rect.height / containerRect.height,
      relativeX: round2(rect.x - containerRect.x),
      relativeY: round2(rect.y - containerRect.y),
      widthPx: round2(rect.width),
      heightPx: round2(rect.height),
      parentWidth: round2(containerRect.width),
      parentHeight: round2(containerRect.height),
    }
  }

  /** 从命中节点向上查找最近的容器型 data-source，供 ADD 插入使用。 */
  function findContainer(node) {
    var current = node
    while (current && current !== document.documentElement) {
      if (
        current.getAttribute &&
        current.getAttribute('data-source-kind') === 'container'
      ) {
        return current
      }
      current = current.parentElement
    }
    return null
  }

  /**
   * 按 CSS selector 安全查找节点。
   *
   * selector 由平台根据 annotationId 生成（[data-vibe-created="..."]），
   * 但仍要用 try/catch 防御非法选择器，不能让一条坏消息打断 inspect。
   */
  function queryNode(selector) {
    if (!selector) return null
    try {
      return document.querySelector(selector)
    } catch (error) {
      return null
    }
  }

  /**
   * 测量新增组件与父容器的位置。
   *
   * CREATE_NEW 的组件由 Coder 生成后带有 data-vibe-created 标记；平台
   * 通过 measure 拿到它在 iframe 内的真实 rect，再叠加用户拖拽位移，
   * 最终把“相对父容器左上角”的 x/y 回写给 Coder。
   */
  function measureElement(selector, parentSelector) {
    var node = queryNode(selector)
    if (!node) {
      return {
        ok: false,
        reason: 'NOT_FOUND',
        selector: selector || '',
      }
    }
    var rect = rectOf(node)
    var parentNode = parentSelector ? queryNode(parentSelector) : null
    if (!parentNode) {
      parentNode = findContainer(node) || node.parentElement
    }
    var parentRect = parentNode ? rectOf(parentNode) : null
    return {
      ok: true,
      selector: selector,
      parentSelector: parentSelector || '',
      rect: rect,
      parentRect: parentRect,
      relativeRect: relativeRect(rect, parentRect),
      source: readSource(node),
      parentSource: parentNode ? readSource(parentNode) : null,
      viewport: {
        width: window.innerWidth,
        height: window.innerHeight,
        scrollX: window.scrollX || 0,
        scrollY: window.scrollY || 0,
      },
    }
  }

  /** 向上收集 data-source 祖先链，帮助模型理解 DOM 层级。 */
  function readChain(node, limit) {
    var chain = []
    var current = node
    while (current && current !== document.documentElement) {
      if (
        current.getAttribute &&
        (current.getAttribute('data-source') ||
          current.getAttribute('data-source-file') ||
          current.getAttribute('data-vibe-target'))
      ) {
        chain.push(readSource(current))
        if (chain.length >= limit) break
      }
      current = current.parentElement
    }
    return chain
  }

  function inspectAt(x, y) {
    var element = document.elementFromPoint(x, y)
    if (!element) {
      clearOverlay()
      return { ok: false, reason: 'NO_ELEMENT' }
    }
    // shadow DOM 场景下 elementFromPoint 可能返回 host，这里先向上找
    // 最近的 data-source，再回退到容器。
    var target = element.closest
      ? element.closest(
          '[data-source-file], [data-source], [data-vibe-target]',
        )
      : null
    if (!target) {
      clearOverlay()
      return {
        ok: false,
        reason: 'NO_SOURCE',
        tag: String(element.tagName || '').toLowerCase(),
      }
    }

    var source = readSource(target)
    var rect = rectOf(target)
    var containerNode = findContainer(target)
    var containerSource = containerNode ? readSource(containerNode) : null
    var containerRect = containerNode ? rectOf(containerNode) : null
    var box = ensureOverlay()
    box.style.left = rect.x + 'px'
    box.style.top = rect.y + 'px'
    box.style.width = rect.width + 'px'
    box.style.height = rect.height + 'px'

    var label =
      source.component && source.component !== 'Component'
        ? source.component
        : source.tag
    return {
      ok: true,
      rect: rect,
      source: source,
      label: label,
      role:
        target.getAttribute('role') ||
        (source.tag === 'button'
          ? 'button'
          : source.kind === 'container'
            ? 'container'
            : source.tag),
      tag: source.tag,
      outerHtml: String(target.outerHTML || '').slice(0, 600),
      container: containerSource
        ? {
            source: containerSource,
            rect: containerRect,
            relativeRect: relativeRect(rect, containerRect),
            label: containerSource.component || containerSource.tag,
          }
        : null,
      chain: readChain(target, 6),
      viewport: {
        width: window.innerWidth,
        height: window.innerHeight,
        scrollX: window.scrollX || 0,
        scrollY: window.scrollY || 0,
      },
    }
  }

  function disable() {
    enabled = false
    clearOverlay()
    document.documentElement.classList.remove('vibe-inspect-enabled')
  }

  function enable() {
    enabled = true
    document.documentElement.classList.add('vibe-inspect-enabled')
  }

  function ensureStyle() {
    if (document.getElementById('vibe-inspect-style')) return
    var style = document.createElement('style')
    style.id = 'vibe-inspect-style'
    style.textContent =
      'html.vibe-inspect-enabled,html.vibe-inspect-enabled *{cursor:crosshair !important;}'
    document.documentElement.appendChild(style)
  }

  window.addEventListener('message', function (event) {
    var data = event.data
    if (!data || data.channel !== CHANNEL) return
    ensureStyle()
    if (data.type === 'enable') {
      if (data.enabled) enable()
      else disable()
      return
    }
    if (data.type === 'clear') {
      clearOverlay()
      return
    }
    if (data.type === 'move') {
      if (!enabled) return
      reply(
        event.source,
        event.origin,
        'hover',
        inspectAt(Number(data.x), Number(data.y)),
      )
      return
    }
    if (data.type === 'pick') {
      if (!enabled) return
      var payload = inspectAt(Number(data.x), Number(data.y))
      reply(event.source, event.origin, 'picked', payload)
    }
    if (data.type === 'measure') {
      // 测量不依赖 inspect 模式，HMR 刷新后平台可随时请求。
      var measured = measureElement(data.selector, data.parentSelector)
      measured.requestId = data.requestId || ''
      reply(event.source, event.origin, 'measured', measured)
    }
  })

  // 兜底：如果父页面的透明层没有拦截到点击，这里阻止默认行为并回传。
  document.addEventListener(
    'click',
    function (event) {
      if (!enabled) return
      event.preventDefault()
      event.stopPropagation()
      reply(
        window.parent,
        '*',
        'picked',
        inspectAt(event.clientX, event.clientY),
      )
    },
    true,
  )

  /*
   * 中键平移桥。
   * ------------------------------------------------------------------
   * 交互态下 iframe 接管了指针事件，父页面完全收不到中键 pointerdown。
   * 这里只转发「中键」手势：按下、位移（以 iframe 内 CSS 像素为单位）
   * 与松手。父页面乘上画布 scale 后平移画布，因此浏览页面时中键依然
   * 是平移，不会触发浏览器自动滚动。
   */
  var panning = false
  var panX = 0
  var panY = 0

  function sendPan(type, payload) {
    try {
      window.parent.postMessage(
        { channel: CHANNEL, type: type, payload: payload || {} },
        '*',
      )
    } catch (error) {
      // 父页面不存在或被浏览器策略拦截时静默降级，不影响页面自身交互。
    }
  }

  document.addEventListener(
    'pointerdown',
    function (event) {
      if (event.button !== 1) return
      event.preventDefault()
      panning = true
      panX = event.clientX
      panY = event.clientY
      sendPan('pan-start', { x: panX, y: panY })
    },
    true,
  )

  document.addEventListener(
    'pointermove',
    function (event) {
      if (!panning) return
      event.preventDefault()
      var dx = event.clientX - panX
      var dy = event.clientY - panY
      if (!dx && !dy) return
      panX = event.clientX
      panY = event.clientY
      sendPan('pan-move', { dx: dx, dy: dy })
    },
    true,
  )

  function endPointerPan(event) {
    if (!panning || (event && event.button !== 1 && event.type === 'pointerup')) {
      return
    }
    panning = false
    sendPan('pan-end', {})
  }

  document.addEventListener('pointerup', endPointerPan, true)
  document.addEventListener('pointercancel', endPointerPan, true)
  // 中键在 Windows 上可能触发自动滚动，auxclick 一并拦截。
  document.addEventListener(
    'auxclick',
    function (event) {
      if (event.button === 1) event.preventDefault()
    },
    true,
  )

  // 通知父页面 SDK 已就绪；父页面收到后再下发 enable，避免首次点击丢失。
  try {
    window.parent.postMessage(
      {
        channel: CHANNEL,
        type: 'ready',
        payload: { url: window.location.href },
      },
      '*',
    )
  } catch (error) {
    // 跨域 / 沙盒限制下 postMessage 仍可用，这里只防御极端环境。
  }
})()
