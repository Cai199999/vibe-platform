# 2026-11-04 · 工作台重构 Phase 4：对比/展示模式与无缝预览

## 目标

1. 每个项目窗口支持「对比模式」（单屏切换画布/预览）与「展示模式」
   （左右 50% 同显），切换时组件零卸载。
2. 预览与画布容器严格等高同宽，外层无原生滚动条，iframe 等比缩放后严格
   铺满预览屏幕，iframe body 不出现滚动条。
3. 保持 5 Tab 上限、关闭即销毁沙盒、非激活 Tab 休眠，并改为按存活数量
   递增命名。

## 已实现

### 双视图布局（`ProjectWorkspace.jsx`）

- 新增两个独立状态：
  - `viewMode: 'compare' | 'dual'` 控制布局；
  - `mode: 'canvas' | 'preview'` 只在对比模式下生效。
- 顶部工具栏包含两组 Segmented Control：
  - `对比模式 / 展示模式`；
  - 对比模式下显示 `画布 / 预览`，展示模式下隐藏。
- `canvasVisible = viewMode === 'dual' || mode === 'canvas'`，
  `previewVisible` 同理；两个面板始终挂载。对比模式通过
  `visibility:hidden + pointer-events:none + z-index:0` 隐藏非当前面板，
  透明 iframe 不会截获另一侧的画布鼠标事件；Tab 级休眠才使用 `display:none`。
- 展示模式使用 CSS Grid 左右各 `minmax(0, 1fr)`，两个面板宽高完全一致。
- 每个 Tab 的 `viewMode/mode` 是组件本地状态；所有 Tab 常驻，因此切换 Tab
  会保留各自最后的布局选择。

### 无缝等比预览（`ScaledPreview.jsx` / `RuntimePanel.jsx`）

- `useFillViewport` 使用固定 **1280×720** 设计视口（沙盒默认桌面尺寸），
  `scale = max(containerWidth / 1280, containerHeight / 720)`（cover），
  再以 `translate(offsetX, offsetY) scale(scale)` 居中；多出的方向由外层
  `overflow:hidden` 裁切。contain 会留黑边，固定 16:9 + 非等比拉伸会变形，
  cover 是「固定视口 + 铺满容器 + 不变形」三者同时成立的唯一方案。
- iframe 的 `width/height` 固定为 1280×720，沙盒页面按 1280×720 排版；
  画布 Mock 的 `.canvas-viewport` 使用完全相同的尺寸、scale 与 offset。
- `CanvasStage` 的 Mock 页面与 iframe 内页面使用同一套卡片样式（720px 卡片、
  `border-box`、零 `p` 外边距、相同字号/行高），E2E 直接比较两者 boundingBox
  以及相对屏幕的左上角偏移和垂直中心。
- 只用 transform 缩放，不修改 `src`、不重建 iframe；切换布局只改变 scale，
  HMR WebSocket 与页面状态保持不变。
- `.project-viewport`、`.viewport-pane`、`.runtime-stage`、
  `.runtime-preview-screen`、`.canvas-stage` 全部 `overflow:hidden`。
- 沙盒模板与初始 App 都设置 `html/body { overflow:hidden }`；iframe 同时
  使用 `scrolling="no"`，双重保证不出现原生滚动条。
- `.canvas-frame` 与 `.runtime-panel` 都相对父级 `.viewport-pane` 绝对定位
  `inset:0`，高度完全由 pane 控制，内部内容不能把框架撑高。
- 画布、iframe、chrome 与舞台统一使用 `#0f172a`（`--screen-bg`），不再使用
  纯黑，边缘完全融合。
- 预览 URL 通过 `fetch(..., { mode: 'no-cors' })` 探活；容器被外部销毁后显示
  `PREVIEW_OFFLINE` 覆盖层，并提供「重新执行 Agent」。`lastInstruction` 随
  Run 快照写入 sessionStorage，重试时复用原标注，不需要重新圈选。
- 内置模板增加 `templateVersion`；旧 localStorage 项目加载时自动替换为新版
  `INITIAL_APP`。此前旧文件缺少 `box-sizing: border-box` 和 `p { margin: 0 }`，
  会导致画布与预览卡片宽高、垂直位置不一致，这是对比模式“框不一样”的根因。
- RuntimePanel 改为「42px chrome + 全出血 stage」：
  - chrome 与 CanvasStage 的浏览器栏等高，双屏左右严格对齐；
  - 状态 pill、修复次数、详情按钮改为 `position:absolute; bottom:0;
    left:0; right:0; z-index:10` 的全宽 HUD，半透明深色 + backdrop blur，
    悬浮在 iframe 上方，不占用任何布局高度；
  - Diff / 运行元信息放入 `.runtime-drawer` 覆盖层，不挤压 iframe；
  - Loading / Idle / Error 都是 stage 内的居中覆盖层，不产生滚动。

### 生命周期

- 保留 5 Tab 上限与关闭即销毁：关闭 Tab 删除项目、运行状态，并调用 BFF
  `DELETE /api/v1/sandboxes/{sandboxId}` 销毁全部关联容器。
- `nextProjectName(projects, openTabCount)` 以 `openTabCount + 1` 为起点，
  跳过已占用编号；例如存活 4 个且 5、6 已占用时，新项目为 7，保证递增且
  不重名。
- 未激活 Tab 继续使用 `display:none` + `content-visibility:hidden`。

## 状态隔离说明

- `useWorkspace` 是唯一的工作区 store，持有 `projects/openTabs/activeTabId`；
  `WorkspacePage` 用 `runStates[projectId]` 保存每个项目的标注、意图、
  运行结果与 `sandboxIds`。
- `ProjectWorkspace` 的 `viewMode/mode`、`CanvasStage` 的圈选草稿、
  `RuntimePanel` 的详情抽屉开关都是每个 Tab 实例自己的本地状态。
- 项目窗口以 `project.id` 为 key 常驻渲染，隐藏时不卸载，因此每个 Tab 的
  DOM、iframe 与本地 state 都彼此独立。
- 异步 Run 始终带 `projectId` 回写；关闭项目后 `closedProjectsRef` 阻止
  迟到结果污染状态，并立即销毁结果中的新沙盒。
- 认证态由 `AuthContext` 管理；工作区不依赖全局 Context 共享运行态，避免
  多个 Tab 互相覆盖。

## 验证

`/tmp/vibe-browser/bin/python scripts/ui_e2e_test.py` 通过：

1. 对比模式默认画布，圈选并完成 Agent；「查看效果」切到全屏预览。
2. 切到展示模式：两个面板同时可见，`.canvas-frame` 与 `.runtime-panel`
   宽高一致；外层容器 overflow hidden 且无滚动距离。
3. iframe 以 1280×720 为设计视口、`top left` 原点 cover 缩放：宽高都不小于
   预览屏幕、至少一个方向严丝合缝、水平/垂直居中。画布 stage 与预览 screen
   同宽同高，两侧卡片相对屏幕的左上角偏移、宽高与垂直中心偏差均不超过 2px。
   实测对比模式 891.0×320.7、offset `(346.5,143.1)` vs `(346.5,143.1)`；
   展示模式 581.0×209.1、offset `(95.5,185.9)` vs `(95.5,185.9)`。
4. 在 DOM 上标记 canvas / iframe 节点，往返对比/展示与画布/预览后仍是
   同一节点，证明零重挂载。
5. iframe 内按钮仍为 `rgb(255, 0, 0)`，body `overflow:hidden`。
6. 详情抽屉展示 Diff 与 sandboxId，关闭后恢复预览。
7. 刷新后恢复画布模式、CTA 与 sandboxId；关闭 Tab 后 Docker 容器销毁。
8. 外部 `docker rm -f` 当前沙盒后切回预览，显示 PREVIEW_OFFLINE；点击
   「重新执行 Agent」会创建新沙盒并恢复红色按钮预览。
9. 5 Tab 上限、按存活数量递增命名且无重复、账户体系均通过。

截图：`phase4-canvas-mode.png`、`phase4-preview-mode.png`、
`phase4-dual-mode.png`、`phase4-preview-offline.png`、
`phase4-tab-limit.png`、`phase4-account.png`。

## 踩坑记录

- React/Playwright 的 `get_by_role(name=re.compile(...))` 在包含 `/` 的
  文本上会生成非法属性选择器；按钮名含斜杠时直接使用精确字符串。
- 外层 `overflow:hidden` 不代表 `scrollWidth == clientWidth`：绝对定位的
  transform 仍会扩大滚动范围。测试只对真正会滚动的容器断言 scroll 尺寸，
  对 `runtime-preview-screen` 断言 computed overflow。
- 沙盒初始 App 默认 `content-box`，卡片会出现 `720 + padding + border` 的
  实际宽度；必须显式 `box-sizing: border-box`，否则和画布卡片宽度对不上。
- 沙盒 `<p>` 默认外边距会撑高卡片，初始 App 的 kicker/copy 必须 `margin:0`，
  否则两侧卡片高度和垂直中心不一致。
- 固定 1280×720 + contain 在宽高比不一致时必然留黑边；固定宽高 + 非等比拉伸
  会变形。cover（取较大比例 + 居中 + 裁切）才能同时满足「固定设计视口」
  与「铺满容器」；当前页面核心卡片居中，裁切只发生在页面边缘。
- 画布 Mock 的响应式规则（如手机断点单独改 `padding`）必须以 1280×720
  设计视口为准，不能跟随真实浏览器视口，否则会与 iframe 内页面产生差异。
- cover 会让 `.canvas-stage` 的 `scrollWidth > clientWidth`，这是被裁剪的
  预期结果；只要 computed `overflow:hidden`，用户无法滚动。E2E 只对真正
  需要无滚动的外层容器断言 scroll 尺寸。

## 遗留

- 直接关闭浏览器标签页不会触发前端 cleanup，仍需服务端 TTL / 孤儿容器回收。
- 预览设计视口固定 1280×720，后续可增加设备尺寸选择（桌面/平板/手机）。
- 导入项目仍是占位；任意页面若要完整适配，可考虑读取文档尺寸后动态缩放。
