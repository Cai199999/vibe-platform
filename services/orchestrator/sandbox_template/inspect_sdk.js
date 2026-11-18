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
  // 构建期源码地图：oid -> { file, start, end, line, column, component,
  // isContainer, parentOid, ... }。DOM 只认 data-oid，不再靠 file:line 猜。
  var sourceMap = { nodes: {} }
  var sourceMapLoading = null
  var lastSourceMapRefresh = 0

  function loadSourceMap() {
    if (sourceMapLoading) return sourceMapLoading
    sourceMapLoading = fetch('/vibe-source-map.json', { cache: 'no-store' })
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status)
        return response.json()
      })
      .then(function (data) {
        if (data && data.nodes && typeof data.nodes === 'object') {
          sourceMap = data
        }
        return sourceMap
      })
      .catch(function () {
        return sourceMap
      })
    return sourceMapLoading
  }

  function refreshSourceMap() {
    sourceMapLoading = null
    return loadSourceMap()
  }

  function sourceMapNodeForOid(oid) {
    if (!oid) return null
    var mapped = sourceMap.nodes && sourceMap.nodes[oid]
    // HMR 后插件会重写地图；遇到未知 oid 时限频刷新一次。
    if (!mapped && Date.now() - lastSourceMapRefresh > 2000) {
      lastSourceMapRefresh = Date.now()
      refreshSourceMap()
    }
    return mapped || null
  }

  /** 同一 oid 模板在当前父容器中的运行时序号。 */
  function runtimeOidIndex(node) {
    var oid = node && node.getAttribute && node.getAttribute('data-oid')
    if (!oid || !node.parentElement) return { index: 0, count: 1 }
    var matches = []
    var children = node.parentElement.children || []
    for (var i = 0; i < children.length; i += 1) {
      var child = children[i]
      if (child.getAttribute && child.getAttribute('data-oid') === oid) {
        matches.push(child)
      }
    }
    var index = matches.indexOf(node)
    return {
      index: index >= 0 ? index : 0,
      count: Math.max(1, matches.length),
    }
  }

  /**
   * 运行时实例路径：从文档根到当前节点的 oid 链 + 同模板序号。
   * 这是 .map / 重复渲染消歧的唯一运行时事实，不靠源码行号猜。
   */
  function computeInstancePath(node) {
    var path = []
    var current = node
    while (current && current !== document.documentElement) {
      var oid =
        current.getAttribute && current.getAttribute('data-oid')
      if (oid) {
        var slot = runtimeOidIndex(current)
        path.unshift({
          oid: oid,
          index: slot.index,
          count: slot.count,
          tag: String(current.tagName || '').toLowerCase(),
        })
      }
      current = current.parentElement
    }
    return path
  }

  function instanceKeyOf(path) {
    return (path || [])
      .map(function (item) {
        return item.oid + '[' + item.index + ']'
      })
      .join('/')
  }
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
  // 原生可交互标签：命中其内部文字 / 图标时，应把标注提升到这些节点。
  var INTERACTIVE_TAGS = {
    button: true,
    input: true,
    select: true,
    textarea: true,
    summary: true,
  }
  // 只从“纯展示型”节点向上提升，避免把已经是交互节点的目标改掉。
  var PROMOTE_TAGS = {
    span: true,
    p: true,
    strong: true,
    em: true,
    b: true,
    i: true,
    small: true,
    code: true,
    h1: true,
    h2: true,
    h3: true,
    h4: true,
    h5: true,
    h6: true,
    svg: true,
    path: true,
    g: true,
    use: true,
    img: true,
    picture: true,
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

  // 实例身份：同一个组件可能被 map 渲染多次，仅仅知道“第几个”还不足以
  // 让 Coder 写出稳定的局部条件。这里从最近的组件 fiber 上提取 React key
  // 与安全的基本类型 props（如 item.id / item.title），模型可以直接写成
  // `item.id === 'xxx'` 这样的守卫，而不是靠 index 猜。
  var IDENTITY_PROP_KEYS = [
    'id',
    'key',
    'slug',
    'name',
    'title',
    'label',
    'code',
    'type',
    'variant',
    'status',
  ]
  var IDENTITY_VALUE_MAX = 80
  var IDENTITY_PROP_LIMIT = 12

  function identityValue(value) {
    if (typeof value === 'string') {
      var text = value.trim()
      if (!text || text.length > IDENTITY_VALUE_MAX) return undefined
      return text
    }
    if (typeof value === 'number' || typeof value === 'boolean') return value
    return undefined
  }

  function instanceIdentityOfFiber(fiber) {
    var identity = { instanceKey: '', instanceProps: {} }
    if (!fiber) return identity
    // React 内部 key（以 . 开头）对业务代码没有意义，直接忽略。
    if (
      fiber.key != null &&
      String(fiber.key) &&
      String(fiber.key).charAt(0) !== '.'
    ) {
      identity.instanceKey = String(fiber.key).slice(0, IDENTITY_VALUE_MAX)
    }
    var props = fiber.memoizedProps
    if (!props || typeof props !== 'object') return identity
    var names = Object.keys(props)
    var count = 0
    for (var i = 0; i < names.length && count < IDENTITY_PROP_LIMIT; i++) {
      var name = names[i]
      if (name === 'children' || name === 'style' || name === 'ref') continue
      if (name === 'className' || name.indexOf('on') === 0) continue
      var value = props[name]
      var direct = identityValue(value)
      if (direct !== undefined) {
        identity.instanceProps[name] = direct
        count++
        continue
      }
      if (!value || typeof value !== 'object' || Array.isArray(value)) continue
      // 只下钻一层：item.id / user.name 这类列表常见结构。
      for (var j = 0; j < IDENTITY_PROP_KEYS.length; j++) {
        var nestedName = IDENTITY_PROP_KEYS[j]
        var nested = identityValue(value[nestedName])
        if (nested === undefined) continue
        identity.instanceProps[name + '.' + nestedName] = nested
        count++
        if (count >= IDENTITY_PROP_LIMIT) break
      }
    }
    return identity
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
        var identity = instanceIdentityOfFiber(current)
        return {
          file: location.file,
          line: location.line,
          column: location.column,
          component: name,
          instanceKey: identity.instanceKey,
          instanceProps: identity.instanceProps,
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
      instanceKey: '',
      instanceProps: {},
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
    if (result.callSite) {
      result.instanceKey = result.callSite.instanceKey || ''
      result.instanceProps = result.callSite.instanceProps || {}
    }
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
    var oid = node.getAttribute('data-oid') || ''
    var mapped = sourceMapNodeForOid(oid)
    var file = (mapped && mapped.file) || node.getAttribute('data-source-file') || ''
    var line = Number(
      (mapped && mapped.line) || node.getAttribute('data-source-line') || 0,
    )
    var column = Number(
      (mapped && mapped.column) || node.getAttribute('data-source-column') || 0,
    )
    var component =
      (mapped && mapped.component) || node.getAttribute('data-source-component') || ''
    var dataSource =
      node.getAttribute('data-source') ||
      node.getAttribute('data-vibe-target') ||
      ''
    // CodeGen 会同时写 data-node-id 与 data-source；AI 新增节点可能只有
    // data-node-id，因此选择器必须支持两种主键。
    var nodeId = node.getAttribute('data-node-id') || ''
    var marker = oid || dataSource || nodeId
    var selector = oid
      ? '[data-oid="' + oid + '"]'
      : dataSource
        ? '[data-source="' + dataSource + '"]'
        : nodeId
          ? '[data-node-id="' + nodeId + '"]'
          : ''
    if (!file && dataSource) {
      var generated = /^(.+?):(\d+):(\d+)$/.exec(dataSource)
      if (generated) {
        file = generated[1]
        line = Number(generated[2])
        column = Number(generated[3])
      }
    }
    var kind =
      (mapped && mapped.isContainer ? 'container' : '') ||
      node.getAttribute('data-source-kind') ||
      (CONTAINER_TAGS[String(node.tagName || '').toLowerCase()]
        ? 'container'
        : 'element')
    var sourceRange =
      mapped && Number.isFinite(Number(mapped.start))
        ? {
            startOffset: Number(mapped.start),
            endOffset: Number(mapped.end),
          }
        : null
    var containerScope =
      (mapped && mapped.containerScope) ||
      (mapped && mapped.isContainer ? 'local' : 'element')
    // 组件定义根节点若只有一个调用点，直接把 callSite 带给父页面；
    // 多个调用点时不猜，交给后端 HITL 选择范围。
    // 实例定位：同一份源码可能渲染多个 DOM 节点，必须把“第几个”与
    // 父级 JSX 调用位置一并回传，否则 Coder 只能改基础组件造成全局污染。
    var instanceInfo = instanceInfoFor(node, file, line, column)
    var callSites =
      mapped && Array.isArray(mapped.callSites) ? mapped.callSites : []
    var callSite = null
    var callSiteConfidence = 'none'
    var callSiteAmbiguous = false
    if (callSites.length === 1) {
      callSite = callSites[0]
      callSiteConfidence = 'exact'
    } else if (callSites.length > 1) {
      // 静态顺序 + 运行时实例序号：只在调用点全部非循环、非条件渲染，
      // 且静态调用点数量与运行时实例数一致时才自动消歧。
      var ordered = callSites.every(function (item) {
        return item.safeForOrder !== false && !item.inMap && !item.conditional
      })
      var instanceIndex = Number(instanceInfo.instance) || 1
      if (
        ordered &&
        callSites.length === instanceInfo.instanceCount &&
        instanceIndex >= 1 &&
        instanceIndex <= callSites.length
      ) {
        callSite = callSites[instanceIndex - 1]
        callSiteConfidence = 'ordered'
      } else {
        callSiteAmbiguous = true
      }
    }
    var callSiteRange =
      callSite && Number.isFinite(Number(callSite.start))
        ? {
            startOffset: Number(callSite.start),
            endOffset: Number(callSite.end),
          }
        : null
    var instancePath = computeInstancePath(node)
    var identity = {
      oid: oid,
      sourceRange: sourceRange,
      parentOid: (mapped && mapped.parentOid) || '',
      callSiteOid: (callSite && callSite.callSiteOid) || '',
      callSiteFile: (callSite && callSite.file) || '',
      callSiteRange: callSiteRange,
      callSiteConfidence: callSiteConfidence,
      callSiteAmbiguous: callSiteAmbiguous,
      instancePath: instancePath,
      instanceKey: instanceKeyOf(instancePath),
      containerScope: containerScope,
      file: file,
      line: line,
      column: column,
      component: component || 'Component',
      selector: selector,
      instance: instanceInfo.instance,
      instanceCount: instanceInfo.instanceCount,
      callSiteFile: (instanceInfo.callSite && instanceInfo.callSite.file) || '',
      sourceKind: sourceKindFor(file),
      domKind: kind,
    }
    return {
      oid: oid,
      sourceRange: sourceRange,
      parentOid: (mapped && mapped.parentOid) || '',
      callSiteOid: (callSite && callSite.callSiteOid) || '',
      callSiteFile: (callSite && callSite.file) || '',
      callSiteRange: callSiteRange,
      callSiteConfidence: callSiteConfidence,
      callSiteAmbiguous: callSiteAmbiguous,
      instancePath: instancePath,
      instanceKey: instanceKeyOf(instancePath),
      containerScope: containerScope,
      file: file,
      line: line,
      column: column,
      component: component || 'Component',
      identity: identity,
      marker: marker,
      sourceKind: sourceKindFor(file),
      kind: kind,
      tag: String(node.tagName || '').toLowerCase(),
      selector: selector,
      instance: instanceInfo.instance,
      instanceCount: instanceInfo.instanceCount,
      instanceId: instanceInfo.instanceId,
      instanceKey: instanceInfo.instanceKey,
      instanceProps: instanceInfo.instanceProps,
      callSite: instanceInfo.callSite,
      // 绝对视觉事实：参考组件 / 目标节点当前真实使用的类名与样式。
      styleSignature: withStyles ? styleSignature(node) : null,
    }
  }

  /** 判断节点本身是否承载交互语义。 */
  function isInteractiveNode(node) {
    if (!node || typeof node.getAttribute !== 'function') return false
    var tag = String(node.tagName || '').toLowerCase()
    if (INTERACTIVE_TAGS[tag]) return true
    if (tag === 'a' && node.hasAttribute('href')) return true
    if (node.getAttribute('data-interactive') === 'true') return true
    var role = String(node.getAttribute('role') || '').toLowerCase()
    return (
      role === 'button' ||
      role === 'link' ||
      role === 'checkbox' ||
      role === 'radio' ||
      role === 'switch' ||
      role === 'tab' ||
      role === 'menuitem'
    )
  }

  /**
   * 层级提升：点击按钮内的 span / 图标时，最近的带源码的可交互祖先
   * 才是用户真正想操作的目标。
   *
   * 插件会逐元素注入 data-source-file，因此 elementFromPoint 往往会先
   * 命中 <span> 或 <svg>。如果不提升，Agent 会收到“改文字”指令却只拿到
   * 内联节点的源码位置，导致改错组件或只改到子节点。
   *
   * 约束：只有祖先能解析出源码出处时才提升，否则保留原节点，避免把
   * file:line 信息丢掉。
   */
  function promoteInteractiveNode(node) {
    if (!node || isInteractiveNode(node)) return node
    var tag = String(node.tagName || '').toLowerCase()
    if (!PROMOTE_TAGS[tag]) return node
    var current = node.parentElement
    var hops = 0
    while (current && current !== document.body && hops < 8) {
      if (isInteractiveNode(current)) {
        return readSource(current) ? current : node
      }
      current = current.parentElement
      hops += 1
    }
    return node
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
      oid: source.oid || '',
      sourceRange: source.sourceRange || null,
      parentOid: source.parentOid || '',
      callSiteOid: source.callSiteOid || '',
      callSiteFile: source.callSiteFile || '',
      callSiteRange: source.callSiteRange || null,
      callSiteConfidence: source.callSiteConfidence || 'none',
      callSiteAmbiguous: Boolean(source.callSiteAmbiguous),
      instancePath: source.instancePath || [],
      instanceKey: source.instanceKey || '',
      containerScope: source.containerScope || 'local',
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
      instanceKey: source.instanceKey,
      instanceProps: source.instanceProps,
      callSite: source.callSite,
      styleSignature: source.styleSignature,
    }
  }

  /** 页面根目标：所有组件都没带 data-source 时的兜底插入容器。 */
  function pageTarget() {
    return {
      oid: '',
      sourceRange: null,
      parentOid: '',
      callSiteOid: '',
      callSiteFile: '',
      callSiteRange: null,
      callSiteConfidence: 'none',
      callSiteAmbiguous: false,
      instancePath: [],
      instanceKey: '',
      containerScope: 'local',
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
    // 必须用 Set：以 DOM 节点作为普通对象 key 会被统一转成
    // "[object HTMLButtonElement]" 之类的字符串，导致同标签只剩第一个
    // 节点参与命中（列表里的多个按钮会被静默丢掉）。
    var seen = new Set()
    var candidates = document.querySelectorAll(
      '[data-source], [data-vibe-target], [data-node-id]',
    )
    for (var i = 0; i < candidates.length; i += 1) {
      // 内层文字 / 图标提升到可交互祖先后再参与覆盖面积计算。
      var node = promoteInteractiveNode(candidates[i])
      if (!node || seen.has(node) || !node.isConnected) continue
      seen.add(node)
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

    // 圈选精度评测：回传框内所有叶子组件（按源码出处去重），
    // 不只回传“最佳命中”。多组件圈选（如整个表单）需要这份集合
    // 才能验证召回率；业务侧仍只消费 target 字段，不受影响。
    var elementSeen = {}
    var elements = []
    for (var j = 0; j < leaves.length; j += 1) {
      var item = leaves[j]
      var key =
        (item.source.file || '') +
        ':' +
        (item.source.line || 0) +
        ':' +
        (item.source.marker || '')
      if (elementSeen[key]) continue
      elementSeen[key] = true
      elements.push({
        source: item.source,
        rect: rectOf(item.node),
        coverage: Number(item.coverage.toFixed(3)),
      })
    }
    elements.sort(function (a, b) {
      return a.rect.y - b.rect.y || a.rect.x - b.rect.x
    })

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
        // 框内叶子组件集合（source + rect），供精度评测计算召回率。
        elements: elements,
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

  // ------------------------------------------------------------
  // 暂存组件拖拽：落点父容器命中
  // ------------------------------------------------------------
  /** readSource 永远返回对象；只有 marker / file 才代表“可定位源码”。 */
  function hasSourceAnchor(source) {
    return Boolean(source && (source.oid || source.marker || source.file))
  }

  /** 从节点向上找最近的 oid 容器；这是父容器解析的第一优先级。 */
  function findOidContainer(node) {
    var current = node
    while (current && current !== document.documentElement) {
      var oid = current.getAttribute && current.getAttribute('data-oid')
      var mapped = oid ? sourceMapNodeForOid(oid) : null
      if (mapped && mapped.isContainer) return current
      current = current.parentElement
    }
    return null
  }

  /**
   * 指针命中节点的最近源码父容器。
   *
   * 优先级：
   *  1. data-source-kind=container 的最近祖先（置信度 exact）；
   *  2. 最近的带源码祖先（容器标记可能缺失，置信度 container_only）；
   *  3. 页面最大容器 / 页面根（置信度 page）。
   *
   * 绝不返回 body / html：它们的源码位置没有意义，会让 Resolver 迷路。
   */
  function stageParentOf(node) {
    // 1. 构建期 oid 地图命中容器：确定性查表，不做标签 / class 猜测。
    var oidContainer = node ? findOidContainer(node) : null
    if (oidContainer) return { node: oidContainer, confidence: 'oid' }
    // 2. 旧项目兜底：已有 data-source-kind 容器标记。
    var container = node ? findContainer(node) : null
    if (container) return { node: container, confidence: 'exact' }
    var current = node
    var fallback = null
    while (current && current !== document.documentElement) {
      var source = readSource(current)
      if (hasSourceAnchor(source)) {
        fallback = current
        break
      }
      current = current.parentElement
    }
    if (fallback) return { node: fallback, confidence: 'container_only' }
    var candidates = document.querySelectorAll('[data-source-kind="container"]')
    var root = null
    var rootArea = 0
    for (var i = 0; i < candidates.length; i += 1) {
      var rect = candidates[i].getBoundingClientRect()
      var area = Math.max(0, rect.width) * Math.max(0, rect.height)
      if (area > rootArea) {
        rootArea = area
        root = candidates[i]
      }
    }
    return { node: root, confidence: root ? 'page' : 'none' }
  }

  /**
   * 父容器的直接 JSX 子节点。
   *
   * 只认“自身带 data-source / data-source-file”的直接子元素：
   * 如果子元素只是无标记的包装层，向下找标记会把插入锚点错认成
   * 孙子节点，最终把组件插进错误组件内部。宁可退化成 inside_end。
   */
  function stageSourceChildren(parentNode) {
    var result = []
    if (!parentNode || !parentNode.children) return result
    var children = parentNode.children
    for (var i = 0; i < children.length; i += 1) {
      var child = children[i]
      if (!child || !child.getBoundingClientRect) continue
      var source = readSource(child)
      if (!hasSourceAnchor(source)) continue
      var rect = child.getBoundingClientRect()
      result.push({ node: child, source: source, rect: rect })
    }
    return result
  }

  /** 主轴方向：flex-row / grid 横向排列时按 x 判定，其余按 y。 */
  function stageAxisIsRow(parentNode) {
    if (!parentNode || typeof window.getComputedStyle !== 'function') return false
    var style = window.getComputedStyle(parentNode)
    var display = String(style.display || '')
    if (display.indexOf('flex') < 0) return false
    var direction = String(style.flexDirection || 'row')
    return direction.indexOf('row') === 0
  }

  /**
   * 根据指针位置计算“插到哪个兄弟之前 / 之后”。
   *
   * 返回的 insertPosition 是给 apply_ast_edit 的结构化指令：
   *  - before / after：锚点是确定的兄弟节点；
   *  - inside_start / inside_end：容器没有可定位子节点时的兜底。
   *
   * siblingIndex 只是给用户和模型参考的序号，真正写入以锚点为准。
   */
  function stageSlotFor(parentNode, children, x, y) {
    var parentRect = parentNode.getBoundingClientRect()
    if (!children.length) {
      var atTop = y < parentRect.top + parentRect.height / 2
      return {
        anchor: null,
        insertPosition: atTop ? 'inside_start' : 'inside_end',
        siblingIndex: 0,
        siblingCount: 0,
        childIndex: 0,
        insertRect: {
          x: round2(parentRect.left),
          y: round2(atTop ? parentRect.top : parentRect.bottom),
          width: round2(parentRect.width),
          height: 0,
        },
      }
    }
    var isRow = stageAxisIsRow(parentNode)
    var point = isRow ? x : y
    for (var i = 0; i < children.length; i += 1) {
      var rect = children[i].rect
      if (rect.width <= 0 && rect.height <= 0) continue
      var middle = isRow
        ? rect.left + rect.width / 2
        : rect.top + rect.height / 2
      if (point < middle) {
        return {
          anchor: children[i],
          insertPosition: 'before',
          siblingIndex: i,
          siblingCount: children.length,
          childIndex: i,
          insertRect: {
            x: round2(rect.left),
            y: round2(rect.top),
            width: round2(rect.width),
            height: 0,
          },
        }
      }
    }
    var last = children[children.length - 1]
    return {
      anchor: last,
      insertPosition: 'after',
      siblingIndex: children.length,
      siblingCount: children.length,
      childIndex: children.length,
      insertRect: {
        x: round2(last.rect.left),
        y: round2(last.rect.bottom),
        width: round2(last.rect.width),
        height: 0,
      },
    }
  }

  /**
   * 暂存组件拖拽时的落点命中（只读，不修改 DOM）。
   *
   * 父页面已把指针换算成 iframe 视口 CSS 像素，因此这里可以直接用
   * elementFromPoint。每次 pointermove（rAF 合并后）调用一次，
   * readSource 的实例索引有 250ms 缓存，滚动 / HMR 后会自然失效。
   */
  /** 页面最顶层 JSX 源码节点；用于 root-level 重排兜底。 */
  function topLevelSourceNode() {
    var nodes = document.querySelectorAll('[data-source], [data-source-file], [data-node-id]')
    var root = null
    for (var i = 0; i < nodes.length; i += 1) {
      var current = nodes[i]
      var parent = current.parentElement
      var nested = parent
        ? parent.closest?.('[data-source], [data-source-file], [data-node-id]')
        : null
      if (!nested) root = current
    }
    return root
  }

  function stageHitAt(x, y) {
    var node = document.elementFromPoint(x, y)
    if (!node || node === document.documentElement) {
      return { ok: false, reason: 'NO_ELEMENT' }
    }
    var parentHit = stageParentOf(node)
    var parentNode = parentHit.node
    if (!parentNode || !hasSourceAnchor(readSource(parentNode))) {
      return { ok: false, reason: 'NO_SOURCE_PARENT' }
    }
    var children = stageSourceChildren(parentNode)
    var slot = stageSlotFor(parentNode, children, x, y)
    var parentRect = rectOf(parentNode)
    var scroll = scrollSnapshot()
    return {
      ok: true,
      parent: targetDescriptor(parentNode),
      rect: parentRect,
      documentRect: toDocumentRect(parentRect),
      insertAnchor: slot.anchor
        ? targetDescriptor(slot.anchor.node)
        : null,
      insertPosition: slot.insertPosition,
      insertRect: slot.insertRect,
      siblingIndex: slot.siblingIndex,
      siblingCount: slot.siblingCount,
      childIndex: slot.childIndex,
      confidence: parentHit.confidence,
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

  /**
   * 按源码 selector + 实例序号定位真实 DOM。
   * map 循环中的多个卡片共享同一 selector，不能再默认取第一个。
   */
  function resolveSourceInstance(source) {
    if (!source || !source.selector) return null
    var matches
    try {
      matches = document.querySelectorAll(source.selector)
    } catch (error) {
      return null
    }
    if (!matches.length) return null
    var index = Math.max(0, Number(source.instance || 1) - 1)
    return matches[index] || matches[0]
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
      '[data-source-file], [data-source], [data-vibe-target], [data-node-id]',
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
            '[data-source-file], [data-source], [data-vibe-target], [data-node-id]',
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
      'html.vibe-drag-enabled [data-source],html.vibe-drag-enabled [data-source-file],html.vibe-drag-enabled [data-vibe-target],html.vibe-drag-enabled [data-node-id]{cursor:grab !important;}',
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
          '[data-source-file], [data-source], [data-vibe-target], [data-node-id]',
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
    // 内层 span / svg 自带 data-source 时，closest 会停在子节点；
    // 这里再提升一次到最近的可交互祖先（button / a / [role=button]）。
    target = promoteInteractiveNode(target)

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
    if (data.type === 'stage-hit') {
      // 暂存组件拖拽落点：高频、只读。父页面用 rAF 合并请求，
      // 这里只做一次 elementFromPoint + 直接子节点遍历。
      var stageBody = bodyOf(data)
      var stageHit = stageHitAt(
        readNumber(data, stageBody, 'x'),
        readNumber(data, stageBody, 'y'),
      )
      stageHit.requestId = String(
        stageBody.requestId || data.requestId || '',
      )
      reply(event.source, event.origin, 'stage-hit-result', stageHit)
      return
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
            '[data-source-file], [data-source], [data-vibe-target], [data-node-id]',
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
    // Direct Ops：松手位置换算为确定性父容器 + 子节点序号。
    // 父页面据此直接调用 MOVE_NODE，不经过 Agent / LLM。
    var placement = null
    try {
      var hitNode = document.elementFromPoint(event.clientX, event.clientY)
      var parentHit = stageParentOf(hitNode)
      var parentNode = parentHit.node
      // 目标可以是 source 的祖先（同父级 / 根级重排），只要不是 source
      // 自身或 source 的子树即可。旧逻辑错误地排除了“父容器包含 source”，
      // 导致容器只能在另一个容器内部移动，无法回到页面根级。
      if (!parentNode || parentNode === node) {
        parentNode = node.parentElement
          ? stageParentOf(node.parentElement).node
          : null
      }
      if (parentNode && parentNode !== node && !node.contains(parentNode)) {
        var parentSource = readSource(parentNode)
        if (
          !parentSource ||
          !parentSource.selector ||
          !parentSource.file
        ) {
          var rootNode = topLevelSourceNode()
          if (rootNode && rootNode !== node && !node.contains(rootNode)) {
            parentNode = rootNode
            parentSource = readSource(parentNode)
          }
        }
        // Babel 的 move_node 以“全部 JSX 子节点”计数；这里不能用
        // stageSourceChildren（只含带 data-source 的节点），否则自定义
        // 组件 / 无标记节点会造成 siblingIndex 越界。
        var allChildren = Array.prototype.slice
          .call(parentNode.children || [])
          .filter(function (child) {
            return child && child.getBoundingClientRect
          })
        var slotChildren = allChildren.map(function (child) {
          return {
            node: child,
            source: readSource(child),
            rect: child.getBoundingClientRect(),
          }
        })
        var slot = stageSlotFor(
          parentNode,
          slotChildren,
          event.clientX,
          event.clientY,
        )
        var index = slot.siblingIndex
        if (slot.insertPosition === 'after') index += 1
        if (slot.insertPosition === 'inside_start') index = 0
        if (slot.insertPosition === 'inside_end') index = allChildren.length
        var sourceIndex = allChildren.indexOf(node)
        if (sourceIndex >= 0 && index > sourceIndex) index -= 1
        if (
          parentSource &&
          parentSource.selector &&
          parentSource.file
        ) {
          placement = {
            parentSelector: parentSource.selector,
            parentFile: parentSource.file,
            parentLine: parentSource.line,
            parentColumn: parentSource.column,
            parentComponent: parentSource.component,
            parentInstance: parentSource.instance,
            parentInstanceCount: parentSource.instanceCount,
            insertPosition: 'inside_index',
            siblingIndex: Math.max(0, index),
          }
        }
      }
    } catch (error) {
      placement = null
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
      placement: placement,
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

  // ------------------------------------------------------------
  // Direct Ops：内联 ➕ / 悬浮 ❌ / 双击文本编辑
  // ------------------------------------------------------------
  // 该层不修改 React 源码 DOM，只在 documentElement 上挂 fixed 浮层，
  // 因此不会触发 HMR，也不会影响组件自身布局。
  var directEnabled = false
  var directProjectId = ''
  var directBound = false
  var directDeleteBtn = null
  var directPlusBtn = null
  var directOutline = null
  var directUndoBtn = null
  var directMenu = null
  var directConfirm = null
  var directToast = null
  var directFormatBar = null
  var directTarget = null
  var directTargetSource = null
  var directContainer = null
  var directDeleteSource = null
  var directDeleteNode = null
  var directDeleteAutoConfirm = false
  var directDeletePending = false
  var directEdit = null
  var directLastTextEdit = null

  function directEl(tag, styles, text) {
    var node = document.createElement(tag)
    if (styles) node.style.cssText = styles
    if (text) node.textContent = text
    node.setAttribute('data-vibe-direct-ui', 'true')
    document.documentElement.appendChild(node)
    return node
  }

  function ensureDirectUi() {
    if (!directOutline) {
      directOutline = directEl(
        'div',
        'position:fixed;display:none;z-index:2147483644;pointer-events:none;' +
          'border:1.5px dashed rgba(96,165,250,.9);border-radius:3px;' +
          'background:rgba(96,165,250,.06);box-sizing:border-box;',
      )
    }
    if (!directUndoBtn) {
      directUndoBtn = directEl(
        'button',
        'position:fixed;display:none;left:12px;bottom:12px;z-index:2147483645;' +
          'height:28px;padding:0 10px;border-radius:14px;' +
          'border:1px solid rgba(148,163,184,.4);background:rgba(15,23,42,.95);' +
          'color:#cbd5e1;font-size:12px;cursor:pointer;pointer-events:auto;',
        '↶ 撤回',
      )
      directUndoBtn.title = '撤回最近一次直接操作'
      directUndoBtn.addEventListener('click', function (event) {
        event.preventDefault()
        event.stopPropagation()
        post('direct-undo', { projectId: directProjectId })
      })
    }
    if (!directDeleteBtn) {
      directDeleteBtn = directEl(
        'button',
        'position:fixed;display:none;z-index:2147483645;width:22px;height:22px;' +
          'border-radius:11px;border:1px solid rgba(248,113,113,.65);' +
          'background:rgba(15,23,42,.92);color:#fca5a5;font-size:12px;' +
          'line-height:20px;padding:0;cursor:pointer;pointer-events:auto;',
        '×',
      )
      directDeleteBtn.title = '删除组件'
      directDeleteBtn.addEventListener('click', function (event) {
        event.preventDefault()
        event.stopPropagation()
        if (!directTarget || directDeletePending) return
        if (!directTarget.isConnected && directTargetSource?.selector) {
          directTarget = queryNode(directTargetSource.selector)
        }
        if (!directTarget || !directTarget.isConnected) {
          showDirectToast('目标已失效，请重新选择组件')
          return
        }
        directDeleteSource = readSource(directTarget)
        if (!directDeleteSource) {
          showDirectToast('目标已失效，请重新选择组件')
          return
        }
        // 循环项共享同一 selector，必须记住被点击的 DOM 实例。
        directDeleteNode = directTarget
        directDeleteAutoConfirm =
          directTarget.getAttribute('data-vibe-created') === 'direct' ||
          Boolean(directTarget.querySelector?.('[data-vibe-created]'))
        directDeletePending = true
        window.setTimeout(function () {
          directDeletePending = false
        }, 3000)
        post('direct-delete-request', {
          projectId: directProjectId,
          source: directDeleteSource,
          autoConfirm: directDeleteAutoConfirm,
        })
      })
    }
    if (!directPlusBtn) {
      directPlusBtn = directEl(
        'button',
        'position:fixed;display:none;z-index:2147483645;width:24px;height:24px;' +
          'border-radius:12px;border:1px solid rgba(96,165,250,.7);' +
          'background:rgba(15,23,42,.94);color:#bfdbfe;font-size:15px;' +
          'line-height:22px;padding:0;cursor:pointer;pointer-events:auto;',
        '+',
      )
      directPlusBtn.title = '新增基础组件'
      directPlusBtn.addEventListener('click', function (event) {
        event.preventDefault()
        event.stopPropagation()
        if (!directContainer) return
        openDirectMenu()
      })
    }
  }

  function directStyleContext(node) {
    var sibling = node && node.previousElementSibling
    while (sibling) {
      if (sibling.tagName === node.tagName) break
      sibling = sibling.previousElementSibling
    }
    if (!sibling) sibling = node && node.previousElementSibling
    if (!sibling) return {}
    return {
      className: String(sibling.className || '').slice(0, 240),
      style: String(sibling.getAttribute('style') || '').slice(0, 300),
      tag: String(sibling.tagName || '').toLowerCase(),
    }
  }

  function hideDirectUi() {
    if (directOutline) directOutline.style.display = 'none'
    if (directDeleteBtn) directDeleteBtn.style.display = 'none'
    if (directPlusBtn) directPlusBtn.style.display = 'none'
    if (directMenu) directMenu.style.display = 'none'
    if (directConfirm) directConfirm.style.display = 'none'
  }

  function placeAt(node, yOffset) {
    var rect = node.getBoundingClientRect()
    return {
      rect: rect,
      left: Math.max(2, rect.right - 26),
      top: Math.max(2, rect.top + (yOffset || 2)),
    }
  }

  // hover 事件每帧最多处理一次，避免 mouseover 在嵌套 DOM 中抖动。
  var directHoverFrame = 0
  var directHoverTarget = null

  function onDirectOver(event) {
    if (!directEnabled || enabled || dragState || directEdit) return
    directHoverTarget = event.target
    if (directHoverFrame) return
    directHoverFrame = window.requestAnimationFrame(function () {
      directHoverFrame = 0
      applyDirectOver(directHoverTarget)
    })
  }

  function applyDirectOver(node) {
    if (!directEnabled || enabled || dragState || directEdit) return
    if (!node || node.closest?.('[data-vibe-direct-ui]')) return
    var sourceNode = node.closest?.('[data-source], [data-source-file], [data-node-id]')
    if (!sourceNode || node === document.documentElement) {
      hideDirectUi()
      return
    }
    var source = readSource(sourceNode)
    if (!source || !hasSourceAnchor(source)) {
      hideDirectUi()
      return
    }
    directTarget = sourceNode
    directTargetSource = source
    ensureDirectUi()
    var outlineRect = sourceNode.getBoundingClientRect()
    directOutline.style.display = 'block'
    directOutline.style.left = outlineRect.left + 'px'
    directOutline.style.top = outlineRect.top + 'px'
    directOutline.style.width = outlineRect.width + 'px'
    directOutline.style.height = outlineRect.height + 'px'
    var placed = placeAt(sourceNode, 2)
    directDeleteBtn.style.left = placed.left + 'px'
    directDeleteBtn.style.top = placed.top + 'px'
    directDeleteBtn.style.display = 'block'

    var isContainer =
      String(sourceNode.getAttribute('data-source-kind') || '') === 'container' ||
      CONTAINER_TAGS[String(sourceNode.tagName || '').toLowerCase()] === true
    if (isContainer) {
      directContainer = sourceNode
      var rect = sourceNode.getBoundingClientRect()
      directPlusBtn.style.left = Math.max(2, rect.right - 26) + 'px'
      directPlusBtn.style.top = Math.max(2, rect.bottom - 26) + 'px'
      directPlusBtn.style.display = 'block'
    } else {
      directContainer = null
      directPlusBtn.style.display = 'none'
    }
  }

  function closeDirectMenu() {
    if (directMenu) directMenu.style.display = 'none'
  }

  function openDirectMenu() {
    if (!directContainer) return
    if (!directMenu) {
      directMenu = directEl(
        'div',
        'position:fixed;display:none;z-index:2147483647;padding:6px;' +
          'border-radius:10px;border:1px solid rgba(148,163,184,.35);' +
          'background:rgba(15,23,42,.98);box-shadow:0 10px 30px rgba(0,0,0,.4);' +
          'pointer-events:auto;',
      )
      ;['Button', 'Input', 'Text', 'Card', 'Container'].forEach(function (type) {
        var item = document.createElement('button')
        item.type = 'button'
        item.textContent = type
        item.style.cssText =
          'display:block;width:100%;margin:2px 0;padding:5px 10px;border:0;' +
          'border-radius:6px;background:transparent;color:#e2e8f0;' +
          'font-size:12px;text-align:left;cursor:pointer;'
        item.addEventListener('click', function (event) {
          event.preventDefault()
          event.stopPropagation()
          closeDirectMenu()
          if (!directContainer) return
          post('direct-add', {
            projectId: directProjectId,
            source: readSource(directContainer),
            componentType: type,
            styleContext: directStyleContext(directContainer),
          })
        })
        directMenu.appendChild(item)
      })
    }
    var rect = directContainer.getBoundingClientRect()
    directMenu.style.left = Math.max(2, rect.right - 100) + 'px'
    directMenu.style.top = Math.max(2, rect.top + 26) + 'px'
    directMenu.style.display = directMenu.style.display === 'block' ? 'none' : 'block'
  }

  function ensureDirectConfirm() {
    if (directConfirm) return directConfirm
    directConfirm = directEl(
      'div',
      'position:fixed;display:none;z-index:2147483647;padding:10px 12px;' +
        'border-radius:10px;border:1px solid rgba(148,163,184,.4);' +
        'background:rgba(15,23,42,.98);color:#e2e8f0;font-size:12px;' +
        'pointer-events:auto;box-shadow:0 10px 30px rgba(0,0,0,.45);',
    )
    var text = document.createElement('div')
    text.textContent = '删除这个组件？'
    text.style.marginBottom = '8px'
    directConfirm.appendChild(text)
    var row = document.createElement('div')
    row.style.cssText = 'display:flex;gap:8px;justify-content:flex-end;'
    var ok = document.createElement('button')
    ok.type = 'button'
    ok.textContent = '确认删除'
    ok.style.cssText =
      'padding:4px 8px;border-radius:6px;border:1px solid rgba(248,113,113,.6);' +
      'background:rgba(248,113,113,.15);color:#fecaca;cursor:pointer;'
    ok.addEventListener('click', function (event) {
      event.preventDefault()
      event.stopPropagation()
      directConfirm.style.display = 'none'
      post('direct-delete-confirmed', {
        projectId: directProjectId,
        source: directDeleteSource,
      })
    })
    var cancel = document.createElement('button')
    cancel.type = 'button'
    cancel.textContent = '取消'
    cancel.style.cssText =
      'padding:4px 8px;border-radius:6px;border:1px solid rgba(148,163,184,.3);' +
      'background:transparent;color:#cbd5e1;cursor:pointer;'
    cancel.addEventListener('click', function () {
      directConfirm.style.display = 'none'
    })
    row.appendChild(ok)
    row.appendChild(cancel)
    directConfirm.appendChild(row)
    return directConfirm
  }

  function showDirectToast(message) {
    if (!directToast) {
      directToast = directEl(
        'div',
        'position:fixed;left:50%;bottom:28px;transform:translateX(-50%);' +
          'z-index:2147483647;padding:8px 14px;border-radius:999px;' +
          'background:rgba(15,23,42,.96);border:1px solid rgba(148,163,184,.35);' +
          'color:#e2e8f0;font-size:12px;pointer-events:none;transition:opacity .2s;',
      )
    }
    directToast.textContent = message
    directToast.style.opacity = '1'
    window.clearTimeout(directToast._timer)
    directToast._timer = window.setTimeout(function () {
      directToast.style.opacity = '0'
    }, 2200)
  }

  function textEditable(node) {
    if (!node || node.children.length > 0) return false
    return Boolean(String(node.textContent || '').trim())
  }

  function startDirectTextEdit(node) {
    if (!directEnabled || enabled || !textEditable(node)) {
      if (node && !textEditable(node)) {
        showDirectToast('该组件不是纯文本节点，建议使用 AI 修改')
      }
      return
    }
    directEdit = {
      node: node,
      source: readSource(node),
      original: node.textContent,
    }
    node.setAttribute('contenteditable', 'true')
    node.style.outline = '2px solid rgba(96,165,250,.8)'
    node.style.outlineOffset = '2px'
    node.focus()
    try {
      var range = document.createRange()
      range.selectNodeContents(node)
      var selection = window.getSelection()
      selection.removeAllRanges()
      selection.addRange(range)
    } catch (error) {
      // selection 不可用时仍允许直接编辑。
    }
    if (!directFormatBar) {
      directFormatBar = directEl(
        'div',
        'position:fixed;display:none;z-index:2147483647;padding:4px 6px;' +
          'border-radius:8px;border:1px solid rgba(148,163,184,.3);' +
          'background:rgba(15,23,42,.98);gap:4px;pointer-events:auto;',
      )
      ;['B', 'A-', 'A+'].forEach(function (label) {
        var button = document.createElement('button')
        button.type = 'button'
        button.textContent = label
        button.title = '字体设置即将支持'
        button.disabled = true
        button.style.cssText =
          'min-width:24px;padding:2px 6px;border-radius:4px;border:0;' +
          'background:rgba(148,163,184,.12);color:#94a3b8;font-size:11px;'
        directFormatBar.appendChild(button)
      })
    }
    var rect = node.getBoundingClientRect()
    directFormatBar.style.left = Math.max(2, rect.left) + 'px'
    directFormatBar.style.top = Math.max(2, rect.top - 30) + 'px'
    directFormatBar.style.display = 'flex'
    hideDirectUi()
  }

  function finishDirectTextEdit(cancel) {
    var edit = directEdit
    if (!edit) return
    directEdit = null
    if (directFormatBar) directFormatBar.style.display = 'none'
    var node = edit.node
    if (!node || !node.isConnected) return
    node.removeAttribute('contenteditable')
    node.style.outline = ''
    node.style.outlineOffset = ''
    if (cancel) {
      node.textContent = edit.original
      return
    }
    var text = String(node.textContent || '').trim()
    if (!text || text === String(edit.original || '').trim()) return
    directLastTextEdit = {
      node: node,
      source: edit.source,
      originalText: edit.original,
    }
    post('direct-text', {
      projectId: directProjectId,
      source: edit.source,
      text: text,
      originalText: edit.original,
    })
  }

  function bindDirectEvents() {
    if (directBound) return
    directBound = true
    document.addEventListener('mouseover', onDirectOver, true)
    document.addEventListener('scroll', hideDirectUi, true)
    document.addEventListener('click', function (event) {
      if (!event.target.closest?.('[data-vibe-direct-ui]')) {
        closeDirectMenu()
        if (directConfirm) directConfirm.style.display = 'none'
      }
    }, true)
    document.addEventListener('dblclick', function (event) {
      if (!directEnabled || enabled) return
      var node = event.target?.closest?.('[data-source], [data-source-file], [data-node-id]')
      if (!node) return
      event.preventDefault()
      event.stopPropagation()
      startDirectTextEdit(node)
    }, true)
    document.addEventListener('keydown', function (event) {
      if (!directEdit) return
      if (event.key === 'Escape') {
        event.preventDefault()
        finishDirectTextEdit(true)
      } else if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault()
        finishDirectTextEdit(false)
      }
    }, true)
    document.addEventListener('focusout', function (event) {
      if (directEdit && event.target === directEdit.node) {
        finishDirectTextEdit(false)
      }
    }, true)
  }

  function setDirectMode(value) {
    directEnabled = Boolean(value)
    if (directEnabled) {
      ensureDirectUi()
      bindDirectEvents()
      if (directUndoBtn) directUndoBtn.style.display = 'block'
    } else {
      hideDirectUi()
      if (directUndoBtn) directUndoBtn.style.display = 'none'
      finishDirectTextEdit(true)
    }
  }

  window.addEventListener('message', function (event) {
    var data = event.data
    if (!data || data.channel !== CHANNEL) return
    if (data.type === 'direct-mode') {
      var directValue = data.enabled
      if (directValue === undefined && data.payload) {
        directValue = data.payload.enabled
      }
      directProjectId =
        data.projectId ||
        (data.payload && data.payload.projectId) ||
        directProjectId
      setDirectMode(directValue !== false)
      return
    }
    if (data.type === 'direct-delete-result') {
      directDeletePending = false
      var result = data.payload || {}
      if (result.handled) {
        // 父页面已弹出 HITL 决策面板，SDK 不再重复提示。
        return
      }
      if (result.safe && directDeleteAutoConfirm) {
        directDeleteAutoConfirm = false
        post('direct-delete-confirmed', {
          projectId: directProjectId,
          source: directDeleteSource,
        })
        return
      }
      if (result.safe) {
        var panel = ensureDirectConfirm()
        var node =
          directDeleteNode && directDeleteNode.isConnected
            ? directDeleteNode
            : resolveSourceInstance(directDeleteSource)
        if (node) {
          var rect = node.getBoundingClientRect()
          panel.style.left = Math.max(2, rect.right - 190) + 'px'
          panel.style.top = Math.max(2, rect.top + 26) + 'px'
        } else {
          panel.style.left = '50%'
          panel.style.top = '30%'
        }
        panel.style.display = 'block'
      } else {
        showDirectToast(
          '已拦截：' + ((result.reasons || []).join('；') || '该组件不可直接删除'),
        )
      }
      return
    }
    if (data.type === 'direct-visual-hide') {
      // 仅视觉隐藏：不改 JSX、不 commit；刷新页面后恢复。
      var hidePayload = data.payload || {}
      var hideSource = hidePayload.source || directDeleteSource
      var hideNode =
        directDeleteNode && directDeleteNode.isConnected
          ? directDeleteNode
          : hideSource && hideSource.selector
            ? resolveSourceInstance(hideSource)
            : null
      if (!hideNode || !hideNode.isConnected) {
        showDirectToast('目标已失效，请重新选择组件')
        return
      }
      hideNode.style.display = 'none'
      hideNode.setAttribute('data-vibe-visual-hidden', 'true')
      directDeleteSource = null
      directDeleteNode = null
      directTarget = null
      hideDirectUi()
      showDirectToast('已隐藏当前项（仅视觉，未修改代码）')
      return
    }
    if (data.type === 'direct-result') {
      showDirectToast(String((data.payload || {}).message || ''))
      return
    }
    if (data.type === 'direct-text-rollback') {
      var payload = data.payload || {}
      var target =
        (directEdit && directEdit.node) ||
        (directLastTextEdit && directLastTextEdit.node)
      if (target && payload.originalText !== undefined) {
        target.textContent = payload.originalText
      }
      directLastTextEdit = null
      return
    }
  }, true)

  // 通知父页面 SDK 已就绪；先等构建期 source map 就绪，避免第一次
  // 选择 / 删除拿到只有 data-oid、没有源码范围的半成品目标。
  var readyAnnounced = false
  function announceReady() {
    if (readyAnnounced) return
    readyAnnounced = true
    post('ready', {
      url: window.location.href,
      route: routeKey(),
      navigationType: navigationType,
      scroll: scrollSnapshot(),
    })
    if (navigationType === 'reload') {
      announceRoute('reload', true)
    }
  }
  loadSourceMap().then(function (map) {
    announceReady()
    // 首次加载时 App 模块可能还没被 Vite transform，地图暂时为空；
    // 稍后重拉一次，避免第一轮 hover / 删除拿不到源码范围。
    if (!map || !map.nodes || Object.keys(map.nodes).length === 0) {
      window.setTimeout(refreshSourceMap, 400)
    }
  })
  // 地图接口异常 / 慢请求时不能让画布一直转圈。
  window.setTimeout(announceReady, 1200)
  // 首次绘制完成后再补几次滚动快照，覆盖字体 / 图片 / 异步渲染撑开
  // 文档的情况。每次 publishScroll 内部会做指纹去重。
  ;[0, 60, 300, 1000, 2500].forEach(function (delay) {
    window.setTimeout(publishScroll, delay)
  })
})()
