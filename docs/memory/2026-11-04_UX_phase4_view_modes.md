# 2026-11-04 · 工作台重构 Phase 4：对比/展示模式与无缝预览

## 目标

1. 每个项目窗口支持「对比模式」（单屏切换画布/预览）与「展示模式」
   （左右 50% 同显），切换时组件零卸载。
2. 预览与画布容器严格等高同宽，外层无原生滚动条，iframe 等比缩放居中，
   iframe body 不出现滚动条。
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
  `previewVisible` 同理；两个面板始终挂载，只用 `.is-hidden`
  （`display:none`）切换。
- 展示模式使用 CSS Grid 左右各 `minmax(0, 1fr)`，两个面板宽高完全一致。
- 每个 Tab 的 `viewMode/mode` 是组件本地状态；所有 Tab 常驻，因此切换 Tab
  会保留各自最后的布局选择。

### 无缝等比预览（`ScaledPreview.jsx` / `RuntimePanel.jsx`）

- iframe 固定以 1024×640 渲染，`ResizeObserver` 监听容器尺寸，计算
  `scale = min(containerW / 1024, containerH / 640)`，通过
  `translate(-50%, -50%) scale(...)` 居中。
- 只用 transform 缩放，不修改 `src`、不重建 iframe；切换布局只改变 scale，
  HMR WebSocket 与页面状态保持不变。
- `.project-viewport`、`.viewport-pane`、`.runtime-stage`、
  `.runtime-preview-screen`、`.canvas-stage` 全部 `overflow:hidden`。
- 沙盒模板与初始 App 都设置 `html/body { overflow:hidden }`；iframe 同时
  使用 `scrolling="no"`，双重保证不出现原生滚动条。
- 沙盒页面背景改为纯 `#020617`，与 `.runtime-stage` 同色，contain 缩放
  产生的留白不会形成可见接缝。
- RuntimePanel 改为「42px chrome + 全出血 stage」：
  - chrome 与 CanvasStage 的浏览器栏等高，双屏左右严格对齐；
  - 状态 pill、修复次数、详情按钮以底部浮层覆盖；
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
3. iframe 应用 transform scale，缩放后不超出容器且水平/垂直居中。
4. 在 DOM 上标记 canvas / iframe 节点，往返对比/展示与画布/预览后仍是
   同一节点，证明零重挂载。
5. iframe 内按钮仍为 `rgb(255, 0, 0)`，body `overflow:hidden`。
6. 详情抽屉展示 Diff 与 sandboxId，关闭后恢复预览。
7. 刷新后恢复画布模式、CTA 与 sandboxId；关闭 Tab 后 Docker 容器销毁。
8. 5 Tab 上限、按存活数量递增命名且无重复、账户体系均通过。

截图：`phase4-canvas-mode.png`、`phase4-preview-mode.png`、
`phase4-dual-mode.png`、`phase4-tab-limit.png`、`phase4-account.png`。

## 踩坑记录

- React/Playwright 的 `get_by_role(name=re.compile(...))` 在包含 `/` 的
  文本上会生成非法属性选择器；按钮名含斜杠时直接使用精确字符串。
- 外层 `overflow:hidden` 不代表 `scrollWidth == clientWidth`：绝对定位的
  scaler 仍会扩大滚动范围。测试只对真正会滚动的容器断言 scroll 尺寸，
  对 `runtime-preview-screen` 断言 computed overflow。
- 固定设计分辨率 + contain 缩放会出现左右/上下留白；把沙盒页面与 stage
  背景统一为 `#020617` 后视觉无缝。
- 不要为了让预览“更大”改成 cover，会裁切长页面；当前产品形态要求完整
  页面可见，坚持 contain。

## 遗留

- 直接关闭浏览器标签页不会触发前端 cleanup，仍需服务端 TTL / 孤儿容器回收。
- 预览设计分辨率固定 1024×640，后续可增加设备尺寸选择（桌面/平板/手机）。
- 导入项目仍是占位；任意页面若要完整适配，可考虑读取文档尺寸后动态缩放。
