/**
 * Vibe Inspect SDK
 * ------------------------------------------------------------------
 * 该脚本由 vite 插件注入到沙盒 index.html 中。平台预览 iframe 与沙盒
 * 跨域，父页面无法直接访问 DOM，因此双方通过 postMessage 通信：
 *
 *   parent -> iframe: { channel:'vibe-inspect',
 *                       type:'enable'|'move'|'pick'|'clear'|'measure'|
 *                            'scroll-by'|'scroll-to' }
 *   iframe -> parent: { channel:'vibe-inspect',
 *                       type:'ready'|'hover'|'picked'|'measured'|
 *                            'scroll'|'click' }
 *
 * 坐标全部使用 iframe 内部视口 CSS 像素（1440×900 设计视口），
 * 由父页面根据 iframe 的 transform scale 反算后传入。
 * 滚动位置使用 window.scrollX / scrollY（文档坐标），双屏据此同步。
 *
 * 安全边界：只读取 DOM 属性与 rect，不读取 cookie / storage，不执行
 * 父页面传入的任何代码。
 */
(function () {
  'use strict'

  var CHANNEL = 'vibe-inspect'
  var enabled = false
  var overlay = null
  // 预览侧重放点击时置位，避免把重放产生的新 click 再回传父页面。
  var replayingClick = false
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
      { channel: CHANNEL, type: type, payload: payload || {} },
      origin || '*',
    )
  }

  /** 统一向父页面发送消息（滚动 / 点击等主动事件）。 */
  function post(type, payload) {
    try {
      window.parent.postMessage(
        { channel: CHANNEL, type: type, payload: payload || {} },
        '*',
      )
    } catch (error) {
      // iframe 被移除或浏览器策略拦截时静默降级。
    }
  }

  /**
   * 读取消息体。
   *
   * 历史协议里存在两种写法：inspect 指令（move/pick）把参数平铺在消息
   * 顶层，而滚动指令（scroll-by/scroll-to）统一放在 payload 里。这里
   * 同时兼容 `data.payload.xxx` 与 `data.xxx`，避免父页面升级消息结构
   * 后 iframe 静默不滚动。
   */
  function bodyOf(data) {
    return data && data.payload && typeof data.payload === 'object'
      ? data.payload
      : {}
  }

  function readNumber(data, body, key) {
    var value = data[key] !== undefined && data[key] !== null ? data[key] : body[key]
    var number = Number(value)
    return isFinite(number) ? number : 0
  }

  /** 当前文档滚动快照；父页面用它驱动右侧预览同步。 */
  function scrollSnapshot() {
    var maxX = Math.max(
      0,
      (document.documentElement.scrollWidth || 0) - window.innerWidth,
    )
    var maxY = Math.max(
      0,
      (document.documentElement.scrollHeight || 0) - window.innerHeight,
    )
    return {
      scrollX: window.scrollX || 0,
      scrollY: window.scrollY || 0,
      maxX: maxX,
      maxY: maxY,
      width: window.innerWidth,
      height: window.innerHeight,
      scrollWidth: document.documentElement.scrollWidth || window.innerWidth,
      scrollHeight: document.documentElement.scrollHeight || window.innerHeight,
    }
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
    /*
     * 双屏滚动同步 + 标注态滚轮转发。
     *  - scroll-by：父页面把 wheel delta 转进来，等价于真实浏览器滚动；
     *  - scroll-to：右侧预览对齐画布的滚动位置。
     * 两者都限制在文档可滚动范围内，避免负值或越界。
     */
    /*
     * 预览侧点击重放。
     * ------------------------------------------------------------------
     * 画布与预览是两个独立的 iframe / React 实例，状态不共享。用户在
     * 画布里点击按钮后，父页面把同一视口坐标发给预览，这里在预览自己
     * 的 DOM 上重放一次 click：
     *   - 计数器 +1、弹窗打开、Tab 切换等 React 状态变化会真实发生；
     *   - 不需要序列化整棵 DOM，也不会破坏组件内部状态；
     *   - 依赖滚动已同步，保证 elementFromPoint 命中同一个元素。
     * 重放期间置位 replayingClick，阻止 click 监听器再次回传形成回环。
     */
    if (data.type === 'replay-click') {
      var clickBody = bodyOf(data)
      var clickX = readNumber(data, clickBody, 'x')
      var clickY = readNumber(data, clickBody, 'y')
      var target = document.elementFromPoint(clickX, clickY)
      if (
        !target ||
        target === document.documentElement ||
        target === document.body
      ) {
        return
      }
      replayingClick = true
      try {
        if (typeof target.click === 'function') {
          target.click()
        } else {
          target.dispatchEvent(
            new MouseEvent('click', {
              bubbles: true,
              cancelable: true,
              clientX: clickX,
              clientY: clickY,
              view: window,
            }),
          )
        }
      } catch (error) {
        // 重放失败不影响预览自身继续使用。
      } finally {
        replayingClick = false
      }
      return
    }
    if (data.type === 'scroll-by') {
      var scrollByBody = bodyOf(data)
      var dx = readNumber(data, scrollByBody, 'dx')
      var dy = readNumber(data, scrollByBody, 'dy')
      try {
        window.scrollBy({ left: dx, top: dy, behavior: 'auto' })
      } catch (error) {
        window.scrollBy(dx, dy)
      }
      return
    }
    if (data.type === 'scroll-to') {
      var scrollToBody = bodyOf(data)
      var snapshot = scrollSnapshot()
      var targetX = Math.max(
        0,
        Math.min(snapshot.maxX, readNumber(data, scrollToBody, 'x')),
      )
      var targetY = Math.max(
        0,
        Math.min(snapshot.maxY, readNumber(data, scrollToBody, 'y')),
      )
      var smooth =
        (data.behavior || scrollToBody.behavior) === 'smooth'
      try {
        window.scrollTo({
          left: targetX,
          top: targetY,
          behavior: smooth ? 'smooth' : 'auto',
        })
      } catch (error) {
        window.scrollTo(targetX, targetY)
      }
      return
    }
  })

  /*
   * 滚动事件：rAF 节流后把位置回传父页面。
   * 画布收到后驱动“展示模式”的预览 iframe 同步滚动；预览自身收到
   * scroll-to 也会触发本事件，但父页面只在画布侧消费，不会形成回环。
   */
  var scrollFrame = 0
  // 上一次广播的快照指纹：DOM 观察器会频繁触发，只有尺寸 / 位置真正
  // 变化时才通知父页面，避免无关的重渲染造成预览同步抖动。
  var lastScrollKey = ''

  function scrollKey(snapshot) {
    return [
      snapshot.scrollX,
      snapshot.scrollY,
      snapshot.maxX,
      snapshot.maxY,
      snapshot.width,
      snapshot.height,
      snapshot.scrollWidth,
      snapshot.scrollHeight,
    ].join(':')
  }

  function publishScroll() {
    var snapshot = scrollSnapshot()
    var key = scrollKey(snapshot)
    if (key === lastScrollKey) return
    lastScrollKey = key
    post('scroll', snapshot)
  }

  function flushScroll() {
    scrollFrame = 0
    var snapshot = scrollSnapshot()
    lastScrollKey = scrollKey(snapshot)
    post('scroll', snapshot)
  }
  window.addEventListener(
    'scroll',
    function () {
      if (scrollFrame) return
      scrollFrame = window.requestAnimationFrame
        ? window.requestAnimationFrame(flushScroll)
        : window.setTimeout(flushScroll, 50)
    },
    { passive: true },
  )

  /*
   * 页面内真实点击：无论是否处于 Inspect，都把点击位置与命中元素回传。
   * 父页面在预览区同位置绘制涟漪，形成画布 / 预览的实时视觉联动。
   * 注意：Inspect 模式下的点击由上面的捕获监听处理为 picked，这里只负责
   * 普通浏览态，避免一次点击产生两条反馈。
   */
  document.addEventListener(
    'click',
    function (event) {
      if (enabled || replayingClick) return
      var target = event.target
      if (!target || typeof target.getBoundingClientRect !== 'function') return
      var sourceNode = target.closest
        ? target.closest(
            '[data-source-file], [data-source], [data-vibe-target]',
          )
        : null
      var rect = rectOf(sourceNode || target)
      var source = sourceNode ? readSource(sourceNode) : null
      post('click', {
        x: round2(event.clientX),
        y: round2(event.clientY),
        rect: rect,
        source: source,
        label:
          (source && (source.component || source.tag)) ||
          String(target.tagName || '').toLowerCase(),
        tag: String(target.tagName || '').toLowerCase(),
        role: target.getAttribute ? target.getAttribute('role') || '' : '',
        scrollX: window.scrollX || 0,
        scrollY: window.scrollY || 0,
      })
    },
    true,
  )

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

  /*
   * 文档尺寸观察。
   * ------------------------------------------------------------------
   * SDK 在 <head> 里执行时 React 还没挂载，首次 ready 快照的
   * scrollHeight 只有 900；如果之后不补发，父页面会一直认为“页面不可
   * 滚动”，普通滚轮就被错误地交还给外层容器。
   *
   * ResizeObserver 只能看到元素自身的盒子尺寸变化，documentElement 的
   * 尺寸恒等于视口，所以再配合 MutationObserver 监听 React 渲染 / HMR
   * 造成的 DOM 变化，用 120ms 防抖 + 指纹去重补发快照。
   */
  var snapshotTimer = 0
  function scheduleScrollSnapshot() {
    if (snapshotTimer) return
    snapshotTimer = window.setTimeout(function () {
      snapshotTimer = 0
      publishScroll()
    }, 120)
  }

  if (typeof window.ResizeObserver === 'function') {
    var resizeObserver = new window.ResizeObserver(scheduleScrollSnapshot)
    resizeObserver.observe(document.documentElement)
    if (document.body) resizeObserver.observe(document.body)
  }
  if (typeof window.MutationObserver === 'function') {
    var mutationObserver = new window.MutationObserver(scheduleScrollSnapshot)
    mutationObserver.observe(document.documentElement, {
      childList: true,
      subtree: true,
    })
  }
  window.addEventListener('resize', scheduleScrollSnapshot)
  window.addEventListener('load', scheduleScrollSnapshot)

  // 通知父页面 SDK 已就绪；父页面收到后再下发 enable，避免首次点击丢失。
  post('ready', {
    url: window.location.href,
    scroll: scrollSnapshot(),
  })
  // 首次绘制完成后再补几次滚动快照，覆盖字体 / 图片 / 异步渲染撑开
  // 文档的情况。每次 publishScroll 内部会做指纹去重。
  ;[0, 60, 300, 1000, 2500].forEach(function (delay) {
    window.setTimeout(publishScroll, delay)
  })
})()
