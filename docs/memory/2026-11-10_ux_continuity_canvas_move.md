# 2026-11-10 · 六项体验断层修复与移动 Tool 化

## 背景

演示测试暴露六个阻断体验：确认契约后架构师对话栏关闭、Inspect 只能在预览用、
全栈前端请求 `/api/v1/tasks/stream` 命中 404、侧边栏点击不换内容、拖拽辅助线
污染展示区且拖拽时按钮被拉长、移动仍是前端隐式拖拽而非 Agent Skill。

## 修复

### 1. 架构师持续在线

- 前端：`ArchitectPanel` 在 `CONFIRMED` 状态保留面板，底部新增“追加修改需求…”
  输入框；提交时复用契约决策接口发 `REVISE`。
- 后端：`ArchitectService.decide_contract` 允许终态 `CONFIRMED` 再次 `REVISE`，
  把追加文本写入 `ui_history` 后从 `revise_contract` 继续，不重新澄清。
- Graph：`summarize_history` 条件路由在 `contract_decision=REVISE` 时直达
  `revise_contract`。
- 修复两处事件时序：`contract.confirmed` 的 dedupe key 加入 revision，避免
  二次确认被事件缓冲区丢弃；confirmed 事件发出前预热内存状态，避免用户
  立即追加需求命中 `CONFIRMING_CONTRACT` 409。

### 2. Inspect 下放画布

- `CanvasStage` 在 live 画布开启 inspect 时接管指针，通过
  `vibe-inspect` postMessage 向 iframe SDK 发送 `enable / hover / pick`。
- 高亮框由 `canvas-viewport` 内部按 iframe 逻辑坐标绘制，缩放/平移自动跟随。
- `ProjectWorkspace.toggleInspect` 仅对 mock 画布保留“自动切预览”的兜底。

### 3. 全栈路由 404

- `FullstackCoder` 使用共享 `collection_path()` 选择静态集合路由；参数化路径、
  `/stream`、`/events` 等后缀不再被误判为集合。
- 前端 `fullstack-api.jsx` 与后端 Express 共用同一个 `COLLECTION_PATH`；
  生成时执行 `_assert_route_pair()` 并输出 `server/api-routes.json`。
- Prompt 增加铁律：前端请求路径必须与 Express 路由逐字符一致。

### 4. 侧边栏真实路由

- 确定性 FrontendCoder 模板新增 `routeComponents` 分组与 `activeRoute` 状态；
  点击 sidebar 更新 `activePath`，只渲染当前路由的组件与空态。
- Prompt 要求生成物包含真实切换逻辑（`react-router-dom` 或 `useState`）。

### 5. 画布痕迹与展示隔离

- 删除“新建后自动进入 placement”旧链路；预览区不再接收 placement 辅助线。
- 画布拖拽仅渲染 `.move-frame` 覆盖层，位置使用 `translate3d`，不改
  `width / height / left / top`。
- 展示区只加载最终生成代码；拖拽预览不会进入预览 DOM。

### 6. 移动 Tool 化

- 新增 `move_component(element_id, x, y)` 标准 Tool，输出
  `transform:translate3d(dx,dy,0) !important`，拒绝宽高类修改与超范围偏移。
- `AgentGraph._coder` 对“纯移动指令”确定性短路：真正 `ToolRegistry.get(...)`
  并 `invoke`，随后把规则追加到 JSX；混合指令（如移动+改色）回退 LLM。
- 追加 CSS 时兼容 `<style>{GLOBAL_CSS}</style>`：新增合法兄弟 `<style>`，
  避免把裸 CSS 插到 `</style>` 前造成 JSX 语法错误。
- 前端 D 键 / 工具栏只提供画布拖拽预览，松手后转成自然语言，经 Supervisor
  `ui_move` 交给 Agent 执行。

## 验证

- Python：250 tests OK。
- 前端：`npm run build` 78 modules，2.22s。
- 契约追加 E2E：CONFIRM → REVISE（CSV 需求）→ contract.ready → 再次 CONFIRM，
  同一 contractId，第二次 confirmed 事件正常送达。
- 全栈 E2E：17 文件、Express+SQLite CRUD 通过；前端源码含正确
  `/api/v1/app_records`，不含 `/stream` 猜测；GET 集合路由 200。
- 前端模式 E2E：生成 App 含 `activePath / routeComponents / data-route-path`。
- 移动 Agent E2E：真实 Tool 调用、BuildVerifier 通过、preview 模块含
  `translate3d(48.00px,0.00px,0)`，补丁无 width/height。
- 未跑浏览器自动化：当前环境缺少 Playwright；Inspect UI 由构建与代码路径验证。
