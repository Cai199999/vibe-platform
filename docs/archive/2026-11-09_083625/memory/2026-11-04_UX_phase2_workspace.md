# 2026-11-04 · 工作台重构 Phase 2：多 Tab 工作区

## 目标

把单一画布演示页升级为多项目工作台：项目下拉栏、多 Tab 窗口、拖拽排序、
每个项目独立的标注画布与沙盒实时预览，并移除右侧固定预览栏。

## 已实现

### 工作区状态（前端本地持久化）

- `src/workspace/useWorkspace.js`
  - 状态：`projects` / `openTabs` / `activeTabId`。
  - 持久化键：`localStorage['vibe.workspace.v1']`，带 `version: 1`。
  - 项目结构：`{ id, name, template: 'vibe-react', files: { 'src/App.jsx': INITIAL_APP }, createdAt }`。
  - 关闭 Tab 只移出 `openTabs`，不删除项目；可从下拉栏重新打开。
  - `normalizeState` 会过滤损坏项目、修复不存在的 Tab 与 activeTabId。
  - 运行状态、预览 URL 属于会话态，不持久化。

### 项目下拉栏

- `src/components/ProjectSwitcher.jsx`
  - 展示当前项目，菜单列出全部项目及「已打开 / 当前」状态。
  - 「新建项目」直接创建并打开；「导入项目」为 Phase 3 占位，禁用并标注。
  - 点击外部 / Escape 关闭。

### 多 Tab 与拖拽

- `src/components/WorkspaceTabs.jsx`
  - `@dnd-kit/core` + `@dnd-kit/sortable` + `@dnd-kit/utilities`。
  - `PointerSensor` 激活距离 6px，`KeyboardSensor` + `sortableKeyboardCoordinates`。
  - `horizontalListSortingStrategy`，`arrayMove` 等价逻辑由 `reorderTabs` 完成。
  - Tab 展示运行状态点：idle / running / success / error。
  - 关闭按钮阻止拖拽与选中冒泡；`＋` 快捷新建。
- 注意：`useSortable` 默认注入 `role="button"`。若产品语义是 Tab，需要把
  `{...attributes}` 放在 `role="tab"` 之前覆盖，并加 `aria-label={project.name}`，
  否则可访问名称会混入「关闭 xxx」，Playwright 的 role 查询也会产生歧义。

### 每个 Tab 独立的画布与预览

- `src/components/ProjectWorkspace.jsx`：Tab 窗口 = 左侧画布 + 右侧 RuntimePanel。
- `src/components/RuntimePanel.jsx`：空闲占位 / Loading 时间轴 / 结果三态；
  结果包含 iframe 预览、Run 元信息、可折叠 Diff、构建日志、Agent 轨迹。
- 所有 Tab 常驻挂载，非激活时仅 `display:none`：
  - 保留各 Tab 的标注、输入焦点与 iframe 预览，切换不闪烁；
  - 副作用是 DOM 中存在多个 `.runtime-placeholder` 等元素，测试定位必须
    限定在 `.tab-window.is-active` 内。
- `RuntimePanel` 高度 = 画布列高度（CSS 变量 `--canvas-h`），内部滚动，
  避免展开 Diff 后把整个 Tab 撑高。

### Run 状态与并发

- `WorkspacePage` 使用 `runStates: { [projectId]: RunState }`：
  `{ annotation, intent, submitting, elapsed, startedAt, result, error }`。
- 所有 Tab 共用一个 `setInterval`，按各自 `startedAt` 计算 elapsed。
- 每个 Tab 独立发起 `POST /api/v1/runs`；BFF 同步代理，多个并发请求互不影响，
  因此 Phase 2.1 不需要改成轮询。
- `inflightRef` 按 projectId 防止重复提交。
- 标注 / 清除标注时重置该项目的意图草稿；`CanvasStage` 的 `intent` 改为受控，
  切换 Tab 不丢草稿。

## 验证

`python3 scripts/ui_e2e_test.py`（Playwright，1680×1050）：

1. 未登录跳转 `/login`，一键登录写入会话与工作区。
2. 项目菜单展示导入项禁用；新建项目后 Tab 数 = 2。
3. 真实鼠标拖拽 Tab 排序，顺序变为 `未命名项目 2 → 演示项目`。
4. 刷新后 Tab 数量与顺序保持。
5. 切换 Tab，在激活窗口圈选并发送「改颜色」。
6. Loading 在激活 Tab 内；结果 `AWAITING_APPROVAL`，自动修复 1 次。
7. Tab 内 iframe 按钮背景 `rgb(255, 0, 0)`；展开 Diff 含 `backgroundColor` / `red`。
8. 关闭 Tab 后可从项目下拉栏重新打开，会话内预览保留。
9. 账户设置、退出登录、账号密码登录正常，无浏览器控制台错误。

截图：`phase2-login.png`、`phase2-project-menu.png`、`phase2-workspace.png`、
`phase2-canvas-popover.png`、`phase2-loading.png`、`phase2-result.png`、
`phase2-account.png`。

## 遗留 / 后续

- 项目与 Run 状态仍在前端 localStorage，后端 `projects` / `runs` 表尚未接入；
  多设备同步、项目重命名 / 删除、真实项目持久化属于 Phase 2.2。
- Phase 3：导入 HTML/CSS/JS zip 或公网 URL，Python 创建沙盒，Tab 内预览；
  AST 标记注入失败时降级为视觉模式。
