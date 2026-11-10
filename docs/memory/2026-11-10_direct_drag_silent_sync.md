# 2026-11-10 · 画布直接拖拽 + 静默同步闭环

## 目标

把「组件移动」从自然语言对话改为业界标准交互：用户直接拖动组件，后台静默派发给 Coder，自动写回代码。

## 交互链路

1. **SDK（iframe 内）**：`inspect_sdk.js` 在 drag 模式下监听 `pointerdown/move/up`，命中 `[data-source]` 后接管手势。
   - 拖动过程只改元素内联 `style.translate`，走合成层，不触发布局回流。
   - 松手后 `postMessage({type:'drag-end', selector, source, dx, dy, token})`。
   - 位移累计采用「记录已提交起点」策略：同节点新 drag 继承上一条记录的 `startTranslate`，避免二次拖拽重复累加。
2. **宿主状态机**：`WorkspacePage` 收到 `drag-end` 后立即 Toast「位置已更新，Agent 正在同步代码...」，并按 500ms 防抖合并连续拖拽，仅提交最后一次 `dx/dy`。
3. **标准信封**：构造 `AnnotationEnvelope`，`action: "MOVE"`，`target: {selector, source}`，`payload: {dx, dy, move:{...}}`；直接派发 `ui_graph`，`requireManualApproval: false`。
4. **后端确定性路径**：`_is_explicit_move_batch` 识别 MOVE 批次并绕过 Planner LLM；`_try_move_tool` 读取 payload 中的 `dx/dy` 调 `move_component`，只生成/更新 `transform: translate3d(...)`，禁止改宽高；经 sandbox patch + Vite HMR 生效。
5. **成功清理**：前端收到成功终态后发 `drag-commit`，SDK 把内联 `translate` 归零并清除该节点记录，避免与后端 CSS transform 叠加成双倍位移。
6. **失败回滚**：调度失败或 120s 超时，`drag-rollback` 把内联位移还原到手势前快照，Toast「代码同步失败，位置已还原」。

## 关键坑与修复

- **焦点被抢**：静默同步走的是同一条 Supervisor SSE 链路，`attachUiDispatch` 原本无条件 `focusPreview`，会把用户从画布切到预览面板。新增 `switchToPreview` 选项，静默 MOVE 传 `false`。
- **拖拽模式被退出**：`CanvasStage` 的 disarm effect 把 `disabled=submitting` 当作交互结束。新增 `dragSyncActive`，同步期间保持 drag 模式；`releaseDragSync` 轮询 `runStates.submitting` 归零后再解除。
- **双倍位移**：`drag-commit` 只删 token 记录但未清内联 translate，HMR 后与 CSS transform 叠加。改为提交时重置内联 translate 并按 selector/node 清空全部记录。
- **防抖二次计数**：第二次 drag 的 `startTranslate` 已包含第一次未提交的内联位移。用 `inheritedStartTranslate()` 继承已提交起点，保证 delta 是本次手势量。
- **Sandbox 镜像**：`inspect_sdk.js` 打进 `vibe-platform/sandbox-react:dev` 镜像，改动后必须重建镜像，旧容器不会热更新。

## 验证

- `scripts/direct_drag_sync_e2e_test.py`（Playwright）覆盖：连续两次拖拽仅一次 dispatch（累计 `dx=120,dy=60`）、HMR 后 computed transform 正确、内联 translate 清零、拖拽模式与画布焦点保持、第二次拖拽累计到 `(150,80)`、dispatch 中断后回滚到 `(150,80)` 并提示失败。
- 依赖：本机需 `playwright`（可用 `/tmp/e2e-venv`），5173/8080/8100 已启动，首次运行前重建 sandbox 镜像并重启 `vibe-python-ai`。
- 前端 `npm run build` 通过。

## 未覆盖

- 双屏预览（ScaledPreview）的 `drag-apply/drag-commit` 同步尚未单独 E2E 验证。
- 拖拽仅支持单组件平移，未做网格吸附/多选/键盘微调。
