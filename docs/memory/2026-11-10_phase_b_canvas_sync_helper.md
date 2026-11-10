# Phase B：画布滚动、双屏同步与影子 LLM

日期：2026-11-10

## 目标

1. 画布 / 预览放弃 `transform: scale` 强行适配，固定 1440×900 逻辑视口 + 容器原生滚动；
2. 画布 iframe 的滚动 / 点击通过 `postMessage` 驱动预览同步；
3. Architect 引入只读影子助手（Helper Agent）与关键问题 Banner / 生成门禁。

## 实现

### 画布滚动（2026-11-10 焦点模型修正）

- `useFillViewport` 恒返回 1440×900、scale=1、offset=0；`canvas-stage` / `runtime-preview-screen` 使用 `overflow:auto`。
- **焦点决定滚轮语义**：
  - 画布焦点（`interactionMode=annotate`）：普通滚轮直接缩放画布（`usePanZoom.zoomOnWheel=true`）；
  - 网页焦点（`interactionMode=interact`）：iframe 接管指针，滚轮使用浏览器原生滚动（`zoomOnWheel=false`，画布不再转发 wheel）；
  - 中键 / 空格+左键：两种焦点下都平移（网页焦点由 SDK 转发 `pan-start/move/end`）。
- 焦点切换：单击 iframe 页面进入网页焦点；单击 `.canvas-live-page` 之外的画布空白回到画布焦点。
- 指针落在标注浮层聊天记录 / 输入框等可滚动控件上时，`usePanZoom` 主动让出滚轮，避免缩放抢走浮层滚动。
- 模板 `html { overflow-y:auto; scrollbar-gutter:stable }` 让文档根节点承担滚动条；body / #root 不再各自开内层滚动，保证 `window.scrollY` 真实可用。
- `.canvas-live-page` 用内阴影描边区分网页视口；网页焦点时切换为蓝色描边。

### postMessage 协议（channel = `vibe-inspect`）

- 父 → iframe：`enable | move | pick | clear | measure | scroll-by | scroll-to`。
- iframe → 父：`ready | hover | picked | measured | scroll | click`。
- **关键坑**：父页面把 `scroll-by/scroll-to` 参数放在 `payload` 内，SDK 旧代码却读顶层 `data.dx`，导致指令静默失效；`inspect_sdk.js` 现同时兼容 `data.payload.x` 与顶层字段。
- **关键坑**：SDK 在 `<head>` 执行，首次 `ready` 的 `scrollHeight` 恒为 900；渲染完成后父页面仍认为不可滚动。现用 `ResizeObserver + MutationObserver`（120ms 防抖 + 指纹去重）补发真实快照。

### 双屏同步

- `CanvasStage` 的 `scroll` 事件统一写入 `canvasScrollRef`；即使单屏模式也记录，切入双屏时立即补一次 `scrollSync`。
- `ScaledPreview` 收到 `scroll-to` 后滚动 iframe；点击 `click` 事件带 `clientX/clientY`，预览按点击点绘制 44px（世界坐标）涟漪，不再用元素 rect（点击 `<main>` 会导致覆盖整页的巨型光斑）。
- **点击状态同步**：父页面把点击坐标发给预览 SDK，SDK 在自己的 DOM 上 `elementFromPoint(x,y).click()` 重放一次点击。计数器 +1、弹窗打开、Tab 切换等 React 状态在预览中真实发生；重放期间 `replayingClick` 阻止 click 回传死循环。Inspect 的“选中元素”点击标记 `synthetic`，只画涟漪不重放。

### 影子 LLM

- 后端：`POST /api/v1/internal/architect/runs/{runId}/helper`，只读快照 + 可选 RAG，返回 `readOnly=true`；BFF 透传同路径。
- 前端：Architect 面板双 Tab（架构确认必须回答 / 随意提问只读）；助手消息独立存储，不写入主 `messages`、不推进状态机。
- `countPendingKeyQuestions` 统计 required 澄清问题；顶部不可关闭 Banner 显示剩余数量；`handleFullstackGenerate` / 手动 bootstrap 均先经过 `blockGenerateUntilAnswered`。

## 验证

- 焦点模型 E2E（`/tmp/focus_viewport_e2e.py`）：
  - 画布焦点滚轮缩放 `scale 1 → 1.568`，iframe `scrollY` 保持 0；
  - 单击页面进入网页焦点，滚轮 `scrollY=600`，画布 transform 不变；
  - 连续滚到底 `scrollY=3579 = max`；
  - 点击画布右侧空白回到画布焦点，滚轮重新缩放；
  - 中键平移在两种焦点下均生效（网页焦点由 SDK 转发）；
  - 双屏 `canvas=3179 / preview=3179`；
  - 画布点击计数器 `0→1`，预览重放后同为 `点击计数：1`，涟漪 49×49；
  - Banner 关闭面板后仍在；影子助手回答只读，主状态仍 `AWAITING_CLARIFICATION`。
- 早期滚动/双屏 E2E（`/tmp/scroll_sync_helper_e2e.py`）已验证 `scroll-by` 协议与边界接力。
- `vite build` 通过；`black --check` 146 文件通过。
- 沙盒镜像 `vibe-platform/sandbox-react:dev` 已重建（改 `inspect_sdk.js` 必须重建，插件有 `sdkCache`）。

## 注意

- 修改 `sandbox_template/inspect_sdk.js` 后必须重建镜像并重新创建沙盒，旧容器不会自动更新。
- 影子助手绝不写主 `architect.messages`，避免污染澄清轮次与契约生成。
