# 2026-11-04 · 工作台重构 Phase 1：认证与画布浮层

## 目标

把单次演示页升级为带账户体系与工作台路由的产品形态，同时把「圈选 → 输入意图 →
发送」收敛到画布内，移除页面下方固定的输入区。

## 已实现

### 路由与 Mock 认证

- 引入 `react-router-dom@6.28.0`（`BrowserRouter`）。
- 路由：
  - `/login`：账号密码或一键体验；
  - `/workspace`：受保护工作台，未登录重定向 `/login` 并记录来源路径；
  - `/`、`*`：重定向工作台。
- `AuthProvider`：
  - 会话写入 `localStorage` 的 `vibe.auth.session`；
  - 保存 `token`、`user`、`issuedAt`、`mode: mock`；
  - 刷新页面后自动恢复。
- 顶栏头像菜单：账户设置弹窗（用户 / 租户 / 角色 / Token 掩码）、退出登录。

### 画布浮层

- 移除 `composer` 固定输入框与提交按钮。
- 圈选完成后在选区旁出现 `.canvas-popover`：
  - 右侧空间足够时放右侧，否则左侧；两侧都不足时横跨选区下方；
  - 基于 `ResizeObserver` 的画布像素尺寸定位，带边界收拢；
  - 自动聚焦输入框，Enter / 发送按钮提交；
  - 提交中禁用输入并显示 spinner，Agent 结果仍由右侧 RunPanel 展示。
- 浮层交互阻止冒泡，不会误触发新的圈选。

### 请求鉴权头

- Run 请求携带 `Authorization: Bearer <mock token>`。
- 注意：不能直接把 localStorage 的 JSON 塞进 Header，中文字符会触发
  `Failed to read 'headers' ... non ISO-8859-1 code point`；必须取
  `session.token`。

## 验证

`python3 scripts/ui_e2e_test.py`（Playwright）：

- 未登录访问 `/workspace` 跳转 `/login`；
- 一键登录后 localStorage 存在会话；
- 圈选按钮后浮层出现，输入「改颜色」并 Enter；
- Loading → `AWAITING_APPROVAL` / 自动修复 1 次；
- iframe 内按钮 `rgb(255, 0, 0)`；
- 账户设置弹窗展示 `demo@vibe.local`；
- 清理沙盒，无浏览器控制台错误。

截图：`docs/screenshots/phase1-login.png`、`phase1-canvas-popover.png`、
`phase1-loading.png`、`phase1-result.png`、`phase1-account.png`。

## 后续阶段评估

- Phase 2（多 Tab 工作区）：约 3–4 人日。需要项目数据模型、Tab 状态机、
  `@dnd-kit` 拖拽、每个 Tab 独立的画布 / 沙盒预览，以及 Run 状态的并发管理。
  当前同步 Run 架构在多 Tab 下需要改成「提交后按 runId 查询 / 轮询」。
- Phase 3（导入 HTML/URL）：约 5–8 人日。需要 zip 解析与安全校验、Python
  创建静态沙盒、URL 加载与静态资源代理（SSRF / 相对路径 / CORS）、AST 标记
  注入与视觉模式降级。
