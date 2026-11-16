# 2026-11-16 画布性能急救（阶段一）

## 1. 拖拽错误可视化

- `handleComponentDragEnd` 的 catch 不再吞错：
  - Toast 显示后端真实 `code · message`；
  - `console.warn` 输出 source / placement / 完整 error；
  - 成功前 `console.info` 输出 move 请求上下文。
- Direct Ops 错误 Toast 已按错误码映射（REQUIRES_LLM / WRITE_CONFLICT /
  PROJECT_BUSY / TARGET_AMBIGUOUS）。

## 2. rAF 调度器

- CanvasStage Inspect hover：
  - pointermove 只写 `inspectMovePointRef`；
  - 每帧最多发送一次 `vibe-inspect move`；
  - 卸载时取消未执行帧。
- ScaledPreview Inspect hover：同样 rAF 合并 move 消息。
- SDK Direct hover：
  - `mouseover` 只记录 target；
  - `requestAnimationFrame` 合并后执行 `applyDirectOver`；
  - 嵌套 DOM 的 mouseover 抖动不再触发连续布局读取。

## 3. 高亮脱 React

- CanvasStage：
  - 移除 `inspectHover` rect state；
  - 高亮 div 使用 `ref` 直接写 left/top/width/height；
  - React 只维护 `inspectHoverKey`（file:line:column:instance），目标变化才 setState。
- ScaledPreview：
  - 同样改为 ref DOM 更新；
  - 保存 `lastInspectHoverRef`，缩放 / 平移时只重绘高亮，不重渲染 hover 状态。
- SDK Direct hover 新增 `directOutline` fixed 虚线框：
  - 与删除 / ➕ 共用一次 `getBoundingClientRect`；
  - scroll / drag / disable 时隐藏。

## 验证

- SDK `node --check` 通过；
- 前端 16 项 Vitest 全绿，生产构建通过；
- 新沙盒预览已确认 `/__vibe_inspect_sdk.js` 含 `directHoverFrame` 与 `directOutline`；
- 后端 607 项 unittest 保持通过（本轮未改后端逻辑）。

## 尚未做

- 浏览器帧率实测（需 Playwright/DevTools Performance）；
- `iterationGroup` 组件分类；
- CLONE_SIBLING；
- Zustand / 独立交互 store 抽取。
