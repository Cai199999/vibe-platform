# 2026-11-04 · 工作台重构 Phase 3：单视口切换与资源生命周期

## 目标

1. 将每个项目 Tab 的上下/左右分屏改为「单视口 + 顶部分段切换」，
   切换时不改变外层尺寸，不丢失画布标注与 iframe 状态。
2. 增加项目生命周期管理：最多 5 个 Tab、关闭即销毁沙盒、名称按最小空闲
   编号复用、非激活 Tab 休眠。

## 已实现

### 单视口（`ProjectWorkspace.jsx` / `styles.css`）

- 顶部工具栏新增 Segmented Control：`画布编辑` / `实时预览`，右侧带运行状态点。
- 两个面板 **始终挂载**，仅用 `.is-visible` / `.is-hidden`（`display:none`）
  控制可见性。因此：
  - `CanvasStage` 的选区、缩放、内部 state 不重置；
  - iframe 不重新加载，预览页滚动位置与 HMR 连接保持。
- `.tab-window` 定义 `--viewport-h: clamp(480px, 62vh, 760px)`，两个面板
  绝对定位填满 `.project-viewport`，切换前后外框尺寸完全一致。
- Agent 完成且当前在画布模式时，右下角显示 `查看效果 →`，点击切到预览；
  刷新后只要 `result.previewUrl` 存在，CTA 仍会出现。
- 执行中的进度面板位于隐藏的预览面板，画布模式通过顶部 `seg-status-running`
  状态点与全局 `Agent 执行中` chip 反馈。

### 5 Tab 上限（`useWorkspace.js`）

- `MAX_OPEN_TABS = 5`；`createNewProject` / `openProject` 在上限时直接返回
  原状态，UI 层同时禁用入口。
- `normalizeState` 会把历史数据中超过 5 个的 `openTabs` 截断，避免旧
  localStorage 绕过限制。
- 下拉栏与 Tab 栏显示 `5 / 5`，并提示「最多只能同时打开 5 个项目，
  请关闭其他项目后再新建」。

### 关闭 = 彻底销毁

- `closeTab` 不再只移出 `openTabs`，而是从 `projects` 中删除项目，
  并按剩余 Tab 重新计算 `activeTabId`。
- 项目名复用：`nextProjectName` 找最小的未占用 `未命名项目 N`（N >= 2）。
  关闭项目 2 后新建仍为项目 2。
- 前端 `WorkspacePage.handleCloseTab`：
  1. 标记 `closedProjectsRef`，阻止在途 Run 回写；
  2. 删除项目 / Tab 与 `runStates[projectId]`；
  3. 遍历 `runStates[projectId].sandboxIds`，调用 BFF
     `DELETE /api/v1/sandboxes/{sandboxId}`。
- 同一项目多次执行会生成多个沙盒，`sandboxIds` 累积记录，关闭时全部销毁。
- 关闭时 Run 仍在途：结果返回后发现项目已关闭，立即销毁结果中的
  `sandboxId`，不写回状态。
- `destroySandbox` 使用 `keepalive: true`，页面跳转/关闭时仍可发出请求；
  404 视为已销毁（幂等），其他错误只 `console.warn`，不阻塞 UI。

### 刷新恢复

- 运行状态仍为会话态，但已完成结果写入 `sessionStorage['vibe.runs.v1']`：
  `{ annotation, result, error, sandboxIds }`。
- 刷新后按仍存在的项目恢复，默认回到画布模式，CTA 与 iframe 可用；
  `sandboxIds` 保留，用户随后关闭 Tab 仍能正确销毁容器。
- 不使用 `localStorage`，避免跨会话持有已失效的沙盒 ID。

### BFF 代理

- 新增 `SandboxController` / `SandboxService`：
  `DELETE /api/v1/sandboxes/{sandboxId}` → Python
  `DELETE /api/v1/internal/sandbox/{sandboxId}`。
- 校验 `[A-Za-z0-9_-]{1,80}`，非法 ID 返回 400 `INVALID_SANDBOX_ID`。
- Python 404 `SANDBOX_NOT_FOUND` 原样透传；AI Core 不可达返回 503
  `AI_CORE_UNAVAILABLE`。浏览器不直接访问 internal API。

### 休眠机制

- 非激活 Tab：`.tab-window.is-hidden { display:none; content-visibility:hidden; }`。
- 非活动视图面板同样 `display:none`。
- iframe 为保留状态而不卸载，因此仍有少量常驻内存；关闭 Tab 才销毁容器。

## 验证

`/tmp/vibe-browser/bin/python scripts/ui_e2e_test.py` 全流程通过：

1. 未登录跳转 → 一键登录。
2. 新建项目、拖拽排序、刷新后 localStorage 持久化。
3. 画布圈选发送「改颜色」，执行中显示状态点，完成后仍在画布模式。
4. 点击「查看效果」切到全屏预览，iframe 按钮 `rgb(255, 0, 0)`；
   画布 ↔ 预览往返切换，`project-viewport` 宽高误差 < 1px，标注与 iframe
   状态均保留。
5. 刷新页面：恢复画布模式、CTA、沙盒 ID 与红色预览。
6. 关闭 Tab：项目从下拉栏消失，Docker 容器
   `vibe-sandbox-{id}` 被销毁（`docker inspect` 轮询确认）。
7. 打开 5 个 Tab：新建按钮禁用、菜单项禁用、上限提示展示。
8. 关闭项目 2 后新建，名称复用「未命名项目 2」。
9. 账户设置 / 退出 / 账号密码登录正常，无控制台错误。

截图：`phase3-workspace.png`、`phase3-canvas-mode.png`、`phase3-loading.png`、
`phase3-preview-mode.png`、`phase3-tab-limit.png`、`phase3-account.png`。

## 5 项目 Docker 资源实测

并发执行 5 个 Run（Mock，`mockScenario=none`），每个约 13.5s 创建完成；
稳定后 `docker stats --no-stream`：

| 项目 | 单容器常驻内存 | CPU | PIDs |
|---|---|---|---|
| 5 个沙盒范围 | 78–95 MiB | 0.05%–0.19% | 29–32 |
| **5 个合计** | **425.6 MiB** | **0.56%** | **148** |

- 配置限额：每容器 768 MiB / 1 CPU / 256 PIDs；理论上限合计
  3.75 GiB / 5 CPU / 1280 PIDs。
- 每个容器映射 1 个随机宿主端口，仅绑定 `127.0.0.1`。
- 平台自身常驻：`java-bff` 230 MiB + `python-ai` 68 MiB +
  `platform-web` 79 MiB ≈ 377 MiB。
- 5 个项目全部打开时，平台 + 沙盒常驻约 800 MiB；构建/热更新时 CPU 短时
  冲高，但每个沙盒被 1 CPU 限额约束。
- 沙盒镜像 `vibe-platform/sandbox-react:dev` 约 492 MB，镜像层共享，
  不随容器数线性增长。
- 测量完成后通过 internal DELETE 接口销毁全部 5 个沙盒，`docker ps` 确认无残留。

## 踩坑记录

- E2E 不能等待「Agent 正在执行」标题可见：该标题在隐藏的预览面板中。
  应断言 `.seg-status-running` / `.chip-live`，完成后再点 CTA。
- `@dnd-kit` 会注入 `role="button"`；需把 `{...attributes}` 放在
  `role="tab"` 之前，并加 `aria-label`，否则 role 查询歧义。
- 截图应在展开 Diff 之前拍预览，否则 iframe 被压缩；菜单截图需等
  约 250ms 动画结束。
- 不要用 `sleep` 等待后台任务；用 Playwright 的 `expect` 轮询或 Docker
  状态轮询。

## 遗留 / 后续

- `sandboxIds` 仅在 sessionStorage；若用户直接关闭浏览器标签页，容器不会
  自动回收。M2 需要增加服务端 TTL / 空闲回收与启动时孤儿容器清理。
- 真实多用户场景不能依赖前端关闭 Tab，需要服务端项目生命周期与配额。
- 休眠目前只停渲染，未暂停 iframe 内 JS；后续可对长时间非激活 Tab 卸载
  iframe 并保留快照。
