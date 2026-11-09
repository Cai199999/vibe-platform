# 2026-11-09 · 画布交互双态与顶部工具栏压缩

## 目标

解决 live iframe 内按钮无法点击的事件冲突，同时压缩顶部导航，让画布/预览占满可用高度。

## 画布双态状态机

- `annotate`（默认）：覆盖层 `pointer-events: auto`，iframe `pointer-events: none`，可正常圈选、快捷键操作。
- `interact`：单击 live 页面后覆盖层放行，iframe 接收指针事件，内部按钮可直接点击。
- 回到标注态：
  - 在 iframe 外灰色区域单击/拖拽；
  - 点击工具栏 `[data-canvas-interaction-toggle]`；
  - 按 `Esc`；
  - 离开 live 模式时自动重置。
- 状态暴露在 `.canvas-stage[data-canvas-interaction]`，便于 E2E 断言。

### 指针捕获注意点

`setPointerCapture` 会把 `pointerup` 的 target 重定向到 stage，不能在 `finishDrawing` 里用 `event.target.closest('.canvas-live-page')` 判断是否点在页面上。现在在 `pointerdown` 时记录 `livePressRef.current`，抬手时据此判断“单击页面 → 切交互态”。

## 中键平移桥接

- 标注态：父页面 stage 直接处理中键。
- 交互态：iframe 内 `inspect_sdk.js` 监听中键并 postMessage：
  - `pan-start` → `beginExternalPan()`
  - `pan-move` → `moveExternalPan(dx * scale, dy * scale)`
  - `pan-end` / `pan-cancel` → `endExternalPan()`
- 父页面只接受 `liveFrameRef.current.contentWindow` 发来的 `vibe-inspect` 消息。

## 工具栏压缩

- TopBar / workspace padding 大幅缩小，`--viewport-h: clamp(480px, calc(100vh - 190px), 1200px)`。
- “对比/展示”合并为单图标切换，`aria-label` 保留“对比模式/展示模式”以兼容自动化。
- 清空标注、保存快照、导出代码、Agent 目录折叠进 `[data-toolbar-more]` “⋯ 更多”菜单。
- 模型下拉保留在工具栏，选项文案由 `Mock（本地）` 改为 `本地演示`，避免界面出现开发者词汇。
- 保留 `data-snapshot-save`、`.export-button`、`[data-agent-catalog]` 等测试钩子。

## 验证

- `conversation_first_e2e_test.py`：双态切换 + 两种状态中键平移 PASS。
- `multi_annotation_e2e_test.py`：3 标注、More 菜单导出/清除 PASS。
- `custom_agent`、`drag_placement`、`snapshot_management`、`annotation_delete_rollback`、`inspect_mode`、`ui_copy_clean`、`approval_ui`、`fullstack_gate_ui`、`fullstack_generation_ui`、`ui_e2e_test` 全部 PASS。
- 前端生产构建 PASS（78 modules）。
