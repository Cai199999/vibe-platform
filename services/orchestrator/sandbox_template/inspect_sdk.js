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
  // 直接拖拽布局：由父页面显式开启，避免普通浏览时点击按钮误触发位移。
  var dragArmed = false
  var dragState = null
  // token -> {node, startTranslate, selector}；成功提交后由父页面清理。
  // 记录“开始拖拽前的 inline translate”，失败回滚时原样还原。
  var appliedDrags = {}
  // 拖拽松手后浏览器仍会补发一次 click；这里吞掉它，避免误触按钮。
  var suppressClick = false
  var dragTokenSeed = 0
  var INLINE_TAGS = {
    span: true,
    p: true,
    strong: true,
    em: true,
    b: true,
    i: true,
    small: true,
    label: true,
    a: true,
    code: true,
    h1: true,
    h2: true,
    h3: true,
    h4: true,
    h5: true,
    h6: true,
  }
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

  // ------------------------------------------------------------
  // 实例定位（instance / callSite）
  // ------------------------------------------------------------
  // 可复用组件在页面中会渲染出多个 DOM 节点，它们共享同一份源码出处
  // （file:line:column）。仅凭源码行号无法区分“第几个实例”，模型就会
  // 去改基础组件，污染同一页面的所有同类组件。这里在运行时补充：
  //   - instance：当前 DOM 节点在相同源码出处中的序号（1 起）；
  //   - instanceCount：该源码出处在本页面的实例总数；
  //   - callSite：父级组件里真正写 <Button /> 的那个 JSX 调用位置。
  var instanceCache = null
  var instanceCacheAt = 0

  function normalizeSourceFile(file) {
    var value = String(file || '')
      .replace(/\\/g, '/')
      .split('?')[0]
    var srcIndex = value.lastIndexOf('/src/')
    if (srcIndex >= 0) return value.slice(srcIndex + 1)
    if (value.indexOf('/app/') === 0) return value.slice(5)
    return value
  }

  function sourceKeyOf(file, line, column) {
    return [file || '', Number(line) || 0, Number(column) || 0].join(':')
  }

  function buildInstanceIndex() {
    var map = new Map()
    var nodes = document.querySelectorAll('[data-source-file]')
    for (var i = 0; i < nodes.length; i += 1) {
      var node = nodes[i]
      var key = sourceKeyOf(
        node.getAttribute('data-source-file'),
        node.getAttribute('data-source-line'),
        node.getAttribute('data-source-column'),
      )
      var list = map.get(key)
      if (!list) {
        list = []
        map.set(key, list)
      }
      list.push(node)
    }
    instanceCache = map
    instanceCacheAt = Date.now()
    return map
  }

  function sourceInstanceIndex(file, line, column) {
    // 250ms 缓存足够覆盖 hover / pick / drag 的连续读取，又能在 HMR 后
    // 快速失效；避免每次 readSource 都全量扫描 DOM。
    if (!instanceCache || Date.now() - instanceCacheAt > 250) {
      buildInstanceIndex()
    }
    return instanceCache.get(sourceKeyOf(file, line, column)) || []
  }

  function fiberFromNode(node) {
    if (!node) return null
    var keys = Object.keys(node)
    for (var i = 0; i < keys.length; i += 1) {
      var key = keys[i]
      if (
        key.indexOf('__reactFiber$') === 0 ||
        key.indexOf('__reactInternalInstance$') === 0
      ) {
        return node[key]
      }
    }
    return null
  }

  function componentNameOfFiber(fiber) {
    if (!fiber) return ''
    var type = fiber.type
    if (typeof type === 'function') {
      return type.displayName || type.name || ''
    }
    if (type && typeof type === 'object') {
      return (
        type.displayName ||
        type.name ||
        (type.render &&
          (type.render.displayName || type.render.name)) ||
        ''
      )
    }
    return ''
  }

  function debugLocationOfFiber(fiber) {
    var source = fiber && fiber._debugSource
    if (!source) return null
    var file = normalizeSourceFile(source.fileName || '')
    var line = Number(source.lineNumber) || 0
    var column = Number(source.columnNumber) || 0
    if (!file || !line) return null
    return { file: file, line: line, column: column }
  }

  /** owner 之上是否还存在组件父级；没有则 owner 就是根组件（App）。 */
  function hasComponentAncestor(fiber) {
    var current = fiber && fiber.return
    while (current) {
      if (current.tag === 3) return false
      if (componentNameOfFiber(current)) return true
      current = current.return
    }
    return false
  }

  function callSiteFor(node) {
    var fiber = fiberFromNode(node)
    var current = fiber
    while (current) {
      var name = componentNameOfFiber(current)
      if (name) {
        // 根组件直接挂在 HostRoot 下，它的 _debugSource 指向 main.jsx，
        // 不是业务 JSX 调用点，绝不能回传成 callSite。
        if (!hasComponentAncestor(current)) return null
        var location = debugLocationOfFiber(current)
        if (!location) return null
        return {
          file: location.file,
          line: location.line,
          column: location.column,
          component: name,
        }
      }
      current = current.return
    }
    return null
  }

  /**
   * 计算节点在页面中的实例信息。
   *
   * 注意：instance 依赖当前 DOM 顺序，只在本次渲染内有效；跨 HMR 后
   * 必须重新计算，因此 instanceId 由“源码 key + 序号”组成，不含随机数。
   */
  function instanceInfoFor(node, file, line, column) {
    var result = {
      instance: 1,
      instanceCount: 1,
      instanceId: '',
      callSite: null,
    }
    if (!file || !line) return result
    var matches = sourceInstanceIndex(file, line, column)
    if (matches.length > 0) {
      result.instanceCount = matches.length
      var index = matches.indexOf(node)
      result.instance = index >= 0 ? index + 1 : 1
    }
    result.instanceId =
      sourceKeyOf(file, line, column) + '#' + result.instance
    result.callSite = callSiteFor(node)
    return result
  }

  // ------------------------------------------------------------
  // 视觉签名（reference_styles）
  // ------------------------------------------------------------
  // 大模型无法跨域读取真实 DOM，只靠“和登录按钮一样”会凭空猜颜色与圆角。
  // 这里在 SDK 内提取**绝对视觉事实**：真实 className 集合 + computed CSS。
  // 父页面把签名原样交给 Coder，模型只允许抄，不允许自由发挥。
  var STYLE_FINGERPRINT_KEYS = [
    'backgroundColor',
    'color',
    'borderColor',
    'borderWidth',
    'borderStyle',
    'borderRadius',
    'fontSize',
    'fontWeight',
    'lineHeight',
    'letterSpacing',
    'boxShadow',
    'textTransform',
    'display',
    'alignItems',
    'justifyContent',
    'gap',
  ]

  function classNamesOf(node) {
    var tokens = []
    var seen = {}
    function addFrom(element) {
      if (!element || typeof element.getAttribute !== 'function') return
      var raw = element.getAttribute('class') || ''
      var parts = String(raw).split(/\s+/)
      for (var i = 0; i < parts.length; i += 1) {
        var token = parts[i]
        if (!token || token.length > 80 || seen[token]) continue
        // 构建期注入的定位类不属于设计系统，不能回传给模型。
        if (token.indexOf('vibe-') === 0) continue
        seen[token] = true
        tokens.push(token)
      }
    }
    addFrom(node)
    var descendants = node.querySelectorAll ? node.querySelectorAll('*') : []
    var limit = Math.min(descendants.length, 24)
    for (var index = 0; index < limit; index += 1) {
      addFrom(descendants[index])
    }
    return tokens.slice(0, 60)
  }

  function sideShorthand(computed, prefix) {
    var top = String(computed[prefix + 'Top'] || '')
    var right = String(computed[prefix + 'Right'] || '')
    var bottom = String(computed[prefix + 'Bottom'] || '')
    var left = String(computed[prefix + 'Left'] || '')
    if (!top && !right && !bottom && !left) return ''
    var values = [top, right, bottom, left]
    var zero = values.every(function (value) {
      return value === '0px' || value === '0'
    })
    if (zero) return ''
    if (top === right && right === bottom && bottom === left) return top
    if (top === bottom && right === left) return top + ' ' + right
    return values.join(' ')
  }

  /**
   * 提取组件的视觉指纹。
   *
   * 返回扁平结构，键名与 reference_styles 契约一致；空值 / 默认值会被
   * 剔除，避免把整份 computedStyle 塞进 Prompt。
   */
  function styleSignature(node) {
    if (!node || typeof window.getComputedStyle !== 'function') return null
    var computed = window.getComputedStyle(node)
    if (!computed) return null
    var signature = {
      tailwindClasses: classNamesOf(node),
      tag: String(node.tagName || '').toLowerCase(),
    }
    for (var i = 0; i < STYLE_FINGERPRINT_KEYS.length; i += 1) {
      var key = STYLE_FINGERPRINT_KEYS[i]
      var value = String(computed[key] || '').trim()
      if (!value) continue
      if (
        value === 'none' ||
        value === 'normal' ||
        value === 'auto' ||
        value === '0px'
      ) {
        continue
      }
      if (value.length > 160) value = value.slice(0, 160)
      signature[key] = value
    }
    var padding = sideShorthand(computed, 'padding')
    if (padding) signature.padding = padding
    var margin = sideShorthand(computed, 'margin')
    if (margin) signature.margin = margin
    var fontFamily = String(computed.fontFamily || '')
      .split(',')[0]
      .replace(/["']/g, '')
      .trim()
    if (fontFamily) signature.fontFamily = fontFamily
    // 透明背景、零边框、无阴影都属于“默认值”，回传只会增加噪声。
    if (signature.backgroundColor === 'rgba(0, 0, 0, 0)') {
      delete signature.backgroundColor
    }
    if (signature.borderWidth === '0px') {
      delete signature.borderWidth
      delete signature.borderColor
      delete signature.borderStyle
    }
    if (signature.boxShadow === 'none') delete signature.boxShadow
    return signature
  }

  /**
   * 读取节点的源码出处。
   *
   * 优先级：
   *  1. data-source-file / data-source-line / data-source-column（插件注入）
   *  2. data-source 中形如 "src/App.jsx:12:5" 的生成标记
   *  3. 仅有业务 marker 时保留 marker，但 file/line 为空
   *
   * withStyles 只允许 pick / 圈选分析开启：hover 每帧都会调用它，读取
   * computedStyle + 扫描子树代价太高。
   */
  function readSource(node, withStyles) {
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
    // 实例定位：同一份源码可能渲染多个 DOM 节点，必须把“第几个”与
    // 父级 JSX 调用位置一并回传，否则 Coder 只能改基础组件造成全局污染。
    var instanceInfo = instanceInfoFor(node, file, line, column)
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
      instance: instanceInfo.instance,
      instanceCount: instanceInfo.instanceCount,
      instanceId: instanceInfo.instanceId,
      callSite: instanceInfo.callSite,
      // 绝对视觉事实：参考组件 / 目标节点当前真实使用的类名与样式。
      styleSignature: withStyles ? styleSignature(node) : null,
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
   * 描述一个 data-source 节点，供父页面构造 AnnotationEnvelope。
   *
   * 与父页面 CanvasStage.targetForNode 保持同一字段集合；区别是这里运行
   * 在 iframe 内部，能拿到构建期注入的 file / line / column。
   */
  function targetDescriptor(node, withStyles) {
    if (!node) return null
    var source = readSource(node, withStyles)
    if (!source) return null
    var label =
      source.component && source.component !== 'Component'
        ? source.component
        : source.tag
    return {
      marker: source.marker,
      selector: source.selector,
      label: label,
      role:
        node.getAttribute('role') ||
        (source.kind === 'container' ? 'container' : source.tag),
      component: source.component,
      tag: source.tag,
      kind: source.kind,
      file: source.file,
      line: source.line,
      column: source.column,
      sourceKind: source.sourceKind,
      instance: source.instance,
      instanceCount: source.instanceCount,
      instanceId: source.instanceId,
      callSite: source.callSite,
      styleSignature: source.styleSignature,
    }
  }

  /** 页面根目标：所有组件都没带 data-source 时的兜底插入容器。 */
  function pageTarget() {
    return {
      marker: 'page',
      selector: '',
      label: '页面',
      role: 'container',
      component: 'App',
      tag: 'div',
      kind: 'container',
      file: '',
      line: 0,
      column: 0,
      sourceKind: 'jsx',
    }
  }

  function intersectionArea(a, b) {
    var left = Math.max(a.left, b.left)
    var right = Math.min(a.right, b.right)
    var top = Math.max(a.top, b.top)
    var bottom = Math.min(a.bottom, b.bottom)
    if (right <= left || bottom <= top) return 0
    return (right - left) * (bottom - top)
  }

  /** 选择框（iframe 视口 CSS 像素）→ 文档坐标。 */
  function toDocumentRect(rect) {
    return {
      x: round2(rect.x + (window.scrollX || 0)),
      y: round2(rect.y + (window.scrollY || 0)),
      width: round2(rect.width),
      height: round2(rect.height),
    }
  }

  var MODIFY_MIN_COVERAGE = 0.35
  var ADD_AREA_RATIO = 4
  var MODIFY_MIN_SELECTION_SHARE = 0.12

  /**
   * 在 iframe 内部完成圈选几何判定。
   *
   * 平台不能跨域读取 live 页面 DOM，所以旧实现只能回传“屏幕坐标”并假定
   * 目标是一个空 selector 的页面根节点。这里把与 Mock 画布完全一致的
   * 覆盖面积算法下放到 SDK：
   *   - 命中叶子且覆盖充分 → MODIFY_EXISTING，target = 该节点；
   *   - 否则 → CREATE_NEW，addTarget = 最近的容器型 data-source 祖先；
   *   - 同时返回 relativeRect（相对父容器）与 documentRect（文档坐标）。
   */
  function analyzeSelection(rect) {
    var selectionRect = {
      left: rect.x,
      top: rect.y,
      right: rect.x + rect.width,
      bottom: rect.y + rect.height,
      width: rect.width,
      height: rect.height,
    }
    var selectionArea = Math.max(0, rect.width) * Math.max(0, rect.height)
    var nodes = []
    var seen = {}
    var candidates = document.querySelectorAll(
      '[data-source], [data-vibe-target]',
    )
    for (var i = 0; i < candidates.length; i += 1) {
      var node = candidates[i]
      if (seen[node] || !node.isConnected) continue
      seen[node] = true
      var nodeRect = node.getBoundingClientRect()
      var area = nodeRect.width * nodeRect.height
      if (area <= 0) continue
      var overlap = intersectionArea(nodeRect, selectionRect)
      if (overlap <= 0) continue
      var source = readSource(node)
      if (!source) continue
      nodes.push({
        node: node,
        rect: nodeRect,
        area: area,
        overlap: overlap,
        coverage: overlap / area,
        share: overlap / selectionArea,
        source: source,
        isContainer: source.kind === 'container',
      })
    }
    var leaves = nodes
      .filter(function (item) {
        return !item.isContainer && item.source.selector
      })
      .sort(function (a, b) {
        if (b.overlap !== a.overlap) return b.overlap - a.overlap
        return a.area - b.area
      })
    var best = leaves[0] || null
    var coversEnough =
      best &&
      best.coverage >= MODIFY_MIN_COVERAGE &&
      selectionArea <= best.area * ADD_AREA_RATIO &&
      best.share >= MODIFY_MIN_SELECTION_SHARE
    var action = coversEnough ? 'MODIFY_EXISTING' : 'CREATE_NEW'

    // ADD 父容器：中心点命中的最近容器 → 命中节点祖先 → 重叠最大的容器。
    var centerX = rect.x + rect.width / 2
    var centerY = rect.y + rect.height / 2
    var centerNode = document.elementFromPoint(centerX, centerY)
    var parentNode = centerNode ? findContainer(centerNode) : null
    if (!parentNode && best) {
      var ancestor = best.node.parentElement
      parentNode = ancestor ? findContainer(ancestor) : null
    }
    if (!parentNode) {
      parentNode = nodes
        .filter(function (item) {
          return item.isContainer
        })
        .sort(function (a, b) {
          return b.overlap - a.overlap || a.area - b.area
        })[0]?.node || null
    }
    var targetNode = coversEnough ? best.node : parentNode
    var relative = null
    if (targetNode) {
      var targetRect = rectOf(targetNode)
      relative = relativeRect(rect, targetRect)
    }
    var scroll = scrollSnapshot()
    return {
      ok: true,
      action: action,
      target: coversEnough ? targetDescriptor(best.node, true) : null,
      addTarget: targetDescriptor(parentNode) || pageTarget(),
      relativeRect: relative,
      documentRect: toDocumentRect(rect),
      measured: {
        mode: 'sdk-live',
        selectionArea: Math.round(selectionArea),
        hitArea: best ? Math.round(best.overlap) : 0,
        hitCoverage: best ? Number(best.coverage.toFixed(3)) : 0,
        candidateCount: leaves.length,
      },
      source: best
        ? readSource(best.node, true)
        : parentNode
          ? readSource(parentNode)
          : null,
      addSource: parentNode ? readSource(parentNode) : null,
      viewport: {
        width: window.innerWidth,
        height: window.innerHeight,
        scrollX: scroll.scrollX,
        scrollY: scroll.scrollY,
      },
      scroll: scroll,
    }
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

  // ------------------------------------------------------------
  // 直接拖拽布局
  // ------------------------------------------------------------
  /**
   * 解析当前节点上的 inline translate。
   *
   * 拖拽预览必须叠加在最终 CSS 规则之上：
   *   - 首次拖拽：inline translate 为空，直接显示手势位移；
   *   - 第二次拖拽：上一次成功同步后 HMR 已经把位移写成 CSS transform，
   *     inline 仍为空，此时手势位移会与 CSS transform 自然叠加；
   *   - 若上一次同步尚未 HMR，inline 里仍是上次的位移，则从该值继续。
   * 因此这里只读取 inline style，绝不把 computed transform 当成基准，
   * 否则 CSS 位移会被重复计算。
   */
  function readInlineTranslate(node) {
    if (!node || !node.style) return { x: 0, y: 0 }
    var value = String(node.style.translate || '').trim()
    if (!value || value === 'none') return { x: 0, y: 0 }
    var parts = value.split(/\s+/)
    return {
      x: parseFloat(parts[0]) || 0,
      y: parseFloat(parts[1]) || 0,
    }
  }

  function applyInlineTranslate(node, x, y) {
    if (!node || !node.style) return
    var px = round2(x)
    var py = round2(y)
    // 归零时移除 inline，让最终 CSS 规则（上一轮已同步的位置）接管。
    node.style.translate = px || py ? px + 'px ' + py + 'px' : ''
  }

  function nextDragToken() {
    dragTokenSeed += 1
    return 'drag_' + Date.now().toString(36) + '_' + dragTokenSeed
  }

  /**
   * 选择拖拽目标。
   *
   * 插件会给每个 JSX 节点注入 data-source-file，若直接使用命中节点，
   * 点击卡片标题时只会移动标题。这里对 span / p / h1 等行内文本节点
   * 向上提升到最近的容器型 data-source（卡片 / section / main），
   * 让“拖卡片”符合直觉；按钮等交互节点仍保持自身为目标。
   */
  function resolveDragTarget(node) {
    if (!node || typeof node.closest !== 'function') return null
    var current = node.closest(
      '[data-source-file], [data-source], [data-vibe-target]',
    )
    if (!current) return null
    var first = current
    while (current) {
      var source = readSource(current)
      if (!source) return first
      if (source.kind === 'container') return current
      if (!INLINE_TAGS[String(source.tag || '').toLowerCase()]) return current
      var parent = current.parentElement
      current = parent
        ? parent.closest(
            '[data-source-file], [data-source], [data-vibe-target]',
          )
        : null
    }
    return first
  }

  /** 内联 transform 会覆盖页面样式，拖拽期间禁用文字选择 / 原生拖图。 */
  function ensureDragStyle() {
    if (document.getElementById('vibe-drag-style')) return
    var style = document.createElement('style')
    style.id = 'vibe-drag-style'
    style.textContent = [
      'html.vibe-drag-enabled [data-source],html.vibe-drag-enabled [data-source-file],html.vibe-drag-enabled [data-vibe-target]{cursor:grab !important;}',
      'html.vibe-drag-enabled.vibe-drag-active,html.vibe-drag-enabled.vibe-drag-active *{cursor:grabbing !important;user-select:none !important;-webkit-user-select:none !important;}',
    ].join('')
    document.documentElement.appendChild(style)
  }

  // 父页面在拖拽过程中可能因为提交 / 门控状态变化补发 drag-mode=false。
  // 此时手势还没结束，不能立刻 cancelActiveDrag() 把组件弹回原位；
  // 先记下“待退出”，等 pointerup 完成本次拖拽后再真正解除。
  var pendingDragDisarm = false

  function setDragArmed(value) {
    var next = Boolean(value)
    if (!next && dragArmed && dragState && dragState.dragging) {
      pendingDragDisarm = true
      return
    }
    pendingDragDisarm = false
    dragArmed = next
    ensureDragStyle()
    document.documentElement.classList.toggle(
      'vibe-drag-enabled',
      dragArmed,
    )
    if (!dragArmed) {
      cancelActiveDrag()
      document.documentElement.classList.remove('vibe-drag-active')
    }
  }

  /** 退出拖拽模式 / iframe 重载前恢复未提交的位移，避免页面停在半途。 */
  function cancelActiveDrag() {
    if (!dragState) return
    var state = dragState
    dragState = null
    applyInlineTranslate(
      state.node,
      state.baseX,
      state.baseY,
    )
    if (state.dragging) {
      post('drag-cancel', {
        token: state.token,
        selector: state.selector,
        source: state.source,
      })
    }
  }

  /**
   * 防抖窗口内同一组件可能经历多次手势。第二个 token 记录到的
   * ``node.style.translate`` 已经包含第一段预览位移，绝不能把它当作
   * “原始基线”，否则成功提交后 CSS transform 与内联 translate 会叠加。
   * 因此新记录必须继承同一节点已有记录的 startTranslate。
   */
  function inheritedStartTranslate(node) {
    for (var key in appliedDrags) {
      if (!Object.prototype.hasOwnProperty.call(appliedDrags, key)) continue
      var record = appliedDrags[key]
      if (record && record.node === node) return record.startTranslate
    }
    return null
  }

  /** 提交 / 回滚后清掉同一节点的所有 token，避免旧记录残留。 */
  function clearNodeRecords(node, selector) {
    Object.keys(appliedDrags).forEach(function (key) {
      var record = appliedDrags[key]
      if (!record) {
        delete appliedDrags[key]
        return
      }
      if (node && record.node === node) {
        delete appliedDrags[key]
        return
      }
      if (!node && selector && record.selector === selector) {
        delete appliedDrags[key]
      }
    })
  }

  /** 预览侧应用主画布拖拽：同一 token 只记录一次起始 inline。 */
  function applyDragPreview(body) {
    var selector = String(body.selector || '')
    var token = String(body.token || '')
    var node = queryNode(selector)
    if (!node) return
    var record = appliedDrags[token]
    if (!record) {
      var inherited = inheritedStartTranslate(node)
      record = {
        node: node,
        selector: selector,
        startTranslate:
          inherited === null ? node.style.translate || '' : inherited,
      }
      appliedDrags[token] = record
    }
    if (!record.node || !record.node.isConnected) {
      record.node = node
    }
    applyInlineTranslate(
      record.node,
      Number(body.x) || 0,
      Number(body.y) || 0,
    )
  }

  /** 回滚：优先用 token 记录的原始节点，找不到时按 selector 重新查询。 */
  function rollbackDrags(body) {
    var items = Array.isArray(body.items)
      ? body.items
      : [
          {
            token: body.token,
            selector: body.selector,
            startTranslate: body.startTranslate,
          },
        ]
    items.forEach(function (item) {
      if (!item) return
      var token = String(item.token || '')
      var record = token ? appliedDrags[token] : null
      if (record) {
        var target = record.node && record.node.isConnected
          ? record.node
          : queryNode(record.selector || item.selector)
        if (target) target.style.translate = record.startTranslate || ''
        // 同一节点在防抖窗口内可能积累多个 token，按 selector + 节点双重清理。
        clearNodeRecords(null, record.selector || item.selector)
        if (target) clearNodeRecords(target)
        return
      }
      var node = queryNode(item.selector)
      if (node) node.style.translate = item.startTranslate || ''
      clearNodeRecords(null, item.selector)
      if (node) clearNodeRecords(node)
    })
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

  function inspectAt(x, y, withStyles) {
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

    var source = readSource(target, withStyles)
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
      // 参考组件取色：pick 时返回真实视觉签名，hover 时为 null 以节省开销。
      styles: withStyles ? styleSignature(target) : null,
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
    /*
     * 直接拖拽协议（画布 ↔ iframe）：
     *   parent -> iframe: drag-mode / drag-apply / drag-rollback / drag-commit
     *   iframe -> parent: drag-start / drag-move / drag-end / drag-cancel
     *
     * drag-mode 是显式模式：普通浏览态点击按钮不会变成拖拽。
     * drag-apply 只用于右侧预览，保持双屏视觉一致（最终仍以 HMR 为准）。
     */
    if (data.type === 'drag-mode') {
      setDragArmed(bodyOf(data).enabled ?? data.enabled)
      return
    }
    if (data.type === 'drag-apply') {
      applyDragPreview(bodyOf(data))
      return
    }
    if (data.type === 'drag-rollback') {
      rollbackDrags(bodyOf(data))
      return
    }
    if (data.type === 'drag-commit') {
      /*
       * 同步成功：后端已经写入累计 transform。预览阶段的内联 translate
       * 必须归零，否则“CSS transform + 内联 translate”会叠成双倍位移。
       * 第二次拖拽从归零后的基线开始，父页面负责累加历史 dx/dy。
       */
      var commitBody = bodyOf(data)
      var commitItems = Array.isArray(commitBody.items)
        ? commitBody.items
        : [commitBody]
      commitItems.forEach(function (item) {
        var commitToken = String((item && item.token) || '')
        var record = commitToken ? appliedDrags[commitToken] : null
        if (record) {
          var commitNode =
            record.node && record.node.isConnected
              ? record.node
              : queryNode(record.selector || item.selector)
          if (commitNode && commitNode.style) {
            commitNode.style.translate = record.startTranslate || ''
          }
          // 同一节点的旧 token（防抖窗口内的前几次手势）一并清理。
          clearNodeRecords(null, record.selector || item.selector)
          if (commitNode) clearNodeRecords(commitNode)
          return
        }
        var fallback = queryNode(item && item.selector)
        if (fallback && fallback.style) fallback.style.translate = ''
        clearNodeRecords(null, item && item.selector)
      })
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
      // pick = 用户最终选中，必须带上真实视觉签名（reference_styles）。
      var payload = inspectAt(Number(data.x), Number(data.y), true)
      reply(event.source, event.origin, 'picked', payload)
    }
    if (data.type === 'measure') {
      // 测量不依赖 inspect 模式，HMR 刷新后平台可随时请求。
      var measured = measureElement(data.selector, data.parentSelector)
      measured.requestId = data.requestId || ''
      reply(event.source, event.origin, 'measured', measured)
    }
    if (data.type === 'analyze-selection') {
      // 圈选分析：父页面把归一化选区换算成 iframe 视口 CSS 像素后发进来，
      // SDK 在真实 DOM 上完成覆盖面积判定，返回目标 / 父容器 / 局部坐标。
      var analyzeBody = bodyOf(data)
      var analyzeRect = analyzeBody.rect || {
        x: readNumber(data, analyzeBody, 'x'),
        y: readNumber(data, analyzeBody, 'y'),
        width: readNumber(data, analyzeBody, 'width'),
        height: readNumber(data, analyzeBody, 'height'),
      }
      var analyzed = analyzeSelection({
        x: Number(analyzeRect.x) || 0,
        y: Number(analyzeRect.y) || 0,
        width: Number(analyzeRect.width) || 0,
        height: Number(analyzeRect.height) || 0,
      })
      analyzed.requestId = String(
        analyzeBody.requestId || data.requestId || '',
      )
      reply(event.source, event.origin, 'selection-analyzed', analyzed)
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
      // 拖拽松手后浏览器补发的 click：吞掉一次，避免误触按钮。
      if (suppressClick) {
        suppressClick = false
        event.preventDefault()
        event.stopPropagation()
        return
      }
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
   * 直接拖拽布局。
   * ------------------------------------------------------------------
   * 拖拽模式下左键按下即锁定一个 data-source 节点：
   *   - 位移超过 4px 才真正开始，避免把普通点击误判成拖拽；
   *   - 拖拽中只改 inline `translate`，不碰 width / height / layout，
   *     页面不会回流，手感与设计工具一致；
   *   - 松手后保留 inline 位移等待后端 HMR；失败时父页面发
   *     drag-rollback，按 token 还原到拖拽前的位置。
   * 中键平移仍由上面的 pan-* 协议负责，两者互不干扰。
   */
  document.addEventListener(
    'pointerdown',
    function (event) {
      if (!dragArmed || enabled || event.button !== 0 || dragState) return
      var target = resolveDragTarget(event.target)
      if (!target) return
      var source = readSource(target)
      if (!source) return
      var selector = source.selector
      if (!selector && source.file) {
        selector =
          '[data-source="' +
          source.file +
          ':' +
          source.line +
          ':' +
          source.column +
          '"]'
      }
      if (!selector) return
      var start = readInlineTranslate(target)
      dragState = {
        token: nextDragToken(),
        node: target,
        selector: selector,
        source: source,
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        baseX: start.x,
        baseY: start.y,
        dx: 0,
        dy: 0,
        dragging: false,
        startTranslate: target.style.translate || '',
        frame: 0,
        lastRect: rectOf(target),
      }
      // 阻止浏览器把图片 / 链接解释成 HTML5 drag，但先不 preventDefault：
      // 用户只是单击时，按钮仍应收到正常 click。
    },
    true,
  )

  document.addEventListener(
    'pointermove',
    function (event) {
      var state = dragState
      if (!state || state.pointerId !== event.pointerId) return
      // 不再用 event.buttons 判断是否取消：部分浏览器 / 触控笔在拖动中
      // 会把 buttons 报成 0，导致“刚按下就被取消”。手势结束统一由
      // pointerup / pointercancel 收口。
      var dx = event.clientX - state.startX
      var dy = event.clientY - state.startY
      if (!state.dragging) {
        if (Math.abs(dx) < 4 && Math.abs(dy) < 4) return
        state.dragging = true
        document.documentElement.classList.add('vibe-drag-active')
        try {
          window.getSelection().removeAllRanges()
        } catch (error) {
          // 某些浏览器允许 selection 为空，忽略即可。
        }
        post('drag-start', {
          token: state.token,
          selector: state.selector,
          source: state.source,
          rect: rectOf(state.node),
          label:
            (state.source && (state.source.component || state.source.tag)) ||
            '',
          dx: 0,
          dy: 0,
        })
      }
      event.preventDefault()
      event.stopPropagation()
      state.dx = dx
      state.dy = dy
      if (!state.node || !state.node.isConnected) {
        state.node = queryNode(state.selector)
        if (!state.node) return
      }
      applyInlineTranslate(
        state.node,
        state.baseX + dx,
        state.baseY + dy,
      )
      // 位移消息按帧合并：拖拽期间父页面只需要最新位置。
      if (state.frame) return
      state.frame = window.requestAnimationFrame
        ? window.requestAnimationFrame(function () {
            state.frame = 0
            if (!state.node) return
            state.lastRect = rectOf(state.node)
            post('drag-move', {
              token: state.token,
              selector: state.selector,
              source: state.source,
              dx: state.dx,
              dy: state.dy,
              inlineX: state.baseX + state.dx,
              inlineY: state.baseY + state.dy,
              rect: state.lastRect,
            })
          })
        : window.setTimeout(function () {
            state.frame = 0
            post('drag-move', {
              token: state.token,
              selector: state.selector,
              source: state.source,
              dx: state.dx,
              dy: state.dy,
              inlineX: state.baseX + state.dx,
              inlineY: state.baseY + state.dy,
              rect: rectOf(state.node),
            })
          }, 16)
    },
    true,
  )

  function settlePendingDragDisarm() {
    if (!pendingDragDisarm) return
    pendingDragDisarm = false
    dragArmed = false
    document.documentElement.classList.remove(
      'vibe-drag-enabled',
      'vibe-drag-active',
    )
  }

  function finishDrag(event) {
    var state = dragState
    if (!state || state.pointerId !== event.pointerId) return
    dragState = null
    if (state.frame) {
      if (window.cancelAnimationFrame) window.cancelAnimationFrame(state.frame)
      else window.clearTimeout(state.frame)
      state.frame = 0
    }
    document.documentElement.classList.remove('vibe-drag-active')
    if (!state.dragging) return
    // 拖拽完成后吞掉浏览器补发的 click，避免误触按钮 / 链接。
    suppressClick = true
    window.setTimeout(function () {
      suppressClick = false
    }, 400)
    var node = state.node && state.node.isConnected
      ? state.node
      : queryNode(state.selector)
    if (!node) {
      post('drag-cancel', {
        token: state.token,
        selector: state.selector,
        source: state.source,
      })
      settlePendingDragDisarm()
      return
    }
    applyInlineTranslate(node, state.baseX + state.dx, state.baseY + state.dy)
    // 保留起始 inline：同步失败时父页面按 token 精确还原。
    // 防抖窗口内同一节点已有 token 时，沿用最早的基线，避免把上一段
    // 未提交的预览位移误当成“原始位置”。
    var inherited = inheritedStartTranslate(node)
    appliedDrags[state.token] = {
      node: node,
      selector: state.selector,
      startTranslate:
        inherited === null ? state.startTranslate : inherited,
    }
    post('drag-end', {
      token: state.token,
      selector: state.selector,
      source: state.source,
      label:
        (state.source && (state.source.component || state.source.tag)) || '',
      // dx / dy 是本次手势位移；父页面叠加“已提交位移”后得到累计值。
      dx: round2(state.dx),
      dy: round2(state.dy),
      inlineX: round2(state.baseX + state.dx),
      inlineY: round2(state.baseY + state.dy),
      startTranslate: state.startTranslate,
      rect: rectOf(node),
    })
    settlePendingDragDisarm()
  }

  document.addEventListener('pointerup', finishDrag, true)
  document.addEventListener(
    'pointercancel',
    function (event) {
      var state = dragState
      if (!state || state.pointerId !== event.pointerId) return
      cancelActiveDrag()
      document.documentElement.classList.remove('vibe-drag-active')
      settlePendingDragDisarm()
    },
    true,
  )

  // HTML5 原生拖拽（图片 / 链接）在拖拽模式下必须禁用，否则会抢走手势。
  document.addEventListener(
    'dragstart',
    function (event) {
      if (dragArmed) event.preventDefault()
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

  /*
   * 路由 / 页面切换通知。
   * ------------------------------------------------------------------
   * 标注圈记录的是“某个页面上的某个元素”。一旦 iframe 内部换了路由，
   * 原来的圈就画在空气上，甚至会把改动提交到错误的页面。SDK 因此监听：
   *   - history.pushState / replaceState（React Router 等 SPA 跳转）；
   *   - popstate / hashchange（浏览器前进后退、锚点跳转）；
   *   - 整页 reload（PerformanceNavigationTiming.type === 'reload'）。
   * 父页面收到 route-change 后主动清空标注与草稿，不做代码回滚。
   */
  var lastRouteKey = ''
  var routeTimer = 0

  function routeKey() {
    return (
      String(window.location.pathname || '/') +
      String(window.location.search || '') +
      String(window.location.hash || '')
    )
  }

  function announceRoute(reason, force) {
    var key = routeKey()
    if (!force && key === lastRouteKey) return
    lastRouteKey = key
    post('route-change', {
      reason: reason,
      path: key,
      url: window.location.href,
      scroll: scrollSnapshot(),
    })
  }

  function scheduleRoute(reason) {
    if (routeTimer) return
    // 放到宏任务：Framework 往往在 pushState 后同一事件循环内同步渲染，
    // 等 DOM 提交完再通知父页面，避免父页面清空后又被旧渲染覆盖。
    routeTimer = window.setTimeout(function () {
      routeTimer = 0
      announceRoute(reason)
    }, 0)
  }

  lastRouteKey = routeKey()

  ;['pushState', 'replaceState'].forEach(function (method) {
    var original = window.history && window.history[method]
    if (typeof original !== 'function') return
    window.history[method] = function () {
      var result = original.apply(this, arguments)
      scheduleRoute(method === 'pushState' ? 'push' : 'replace')
      return result
    }
  })
  window.addEventListener('popstate', function () {
    scheduleRoute('popstate')
  })
  window.addEventListener('hashchange', function () {
    scheduleRoute('hash')
  })

  // 整页 reload：新文档无法知道父页面还存着旧标注，必须主动告知。
  var navigationType = ''
  try {
    if (typeof performance.getEntriesByType === 'function') {
      var navEntry = performance.getEntriesByType('navigation')[0]
      navigationType = navEntry ? navEntry.type : ''
    }
    if (
      !navigationType &&
      performance.navigation &&
      typeof performance.navigation.type === 'number'
    ) {
      navigationType = performance.navigation.type === 1 ? 'reload' : ''
    }
  } catch (error) {
    navigationType = ''
  }

  // 通知父页面 SDK 已就绪；父页面收到后再下发 enable，避免首次点击丢失。
  post('ready', {
    url: window.location.href,
    route: routeKey(),
    navigationType: navigationType,
    scroll: scrollSnapshot(),
  })
  if (navigationType === 'reload') {
    announceRoute('reload', true)
  }
  // 首次绘制完成后再补几次滚动快照，覆盖字体 / 图片 / 异步渲染撑开
  // 文档的情况。每次 publishScroll 内部会做指纹去重。
  ;[0, 60, 300, 1000, 2500].forEach(function (delay) {
    window.setTimeout(publishScroll, delay)
  })
})()
