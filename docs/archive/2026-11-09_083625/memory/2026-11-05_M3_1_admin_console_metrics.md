# 2026-11-05 · M3.1 管理中台入口与指标看板

## 本轮交付

1. **BFF 管理鉴权（Mock RBAC）**
   - 新增 `@IsAdmin` 注解 + `AdminAuthorizationAspect` 切面，
     从 `X-User-Role` 读取角色；允许 `admin`、`platform_admin`、`tenant_admin`、
     `tenant_operator`，其余一律 403。
   - 新增 `AdminExceptionHandler` 统一错误体：`{code: ADMIN_ROLE_REQUIRED,
     message, requestId}`，不暴露内部角色列表。
   - 这是 M3.1 的临时方案；M3.2 接入真实登录态与用户-租户-角色表后，
     切面改为从 `SecurityContext` 取角色，Header 仅作为本地开发回落。

2. **`GET /api/v1/admin/metrics`**
   - `AdminMetricsService` 在内存中聚合：活跃沙盒数、今日累计 Run 数、
     历史成功率、成功 Run 平均耗时、Agent 调用占比、最近 10 次 Run。
   - 数据来源三条：Run SSE 事件（`RunStreamService` / `ArchitectStreamService`
     的 `doOnNext`）、Supervisor `dispatch` 响应中的 `dispatches[]`（用于 Agent
     归因）、M1 同步 Run（`RunService` 直接登记）。
   - 有界 LRU：最多保留 5000 个 Run，优先淘汰终态记录。
   - 状态口径：`run.started → RUNNING`、`run.succeeded → SUCCEEDED`、
     `run.failed → FAILED`、`run.cancelled → CANCELLED`、`run.rejected → REJECTED`、
     `approval.required → AWAITING_APPROVAL`、
     `clarification.required → AWAITING_CLARIFICATION`、
     `architect.contract.ready → AWAITING_CONTRACT`。
   - 成功率分母只算 SUCCEEDED + FAILED；平均耗时同理，避免进行中 Run 拉低均值。
   - 活跃沙盒来自 `RunSandboxRegistry.activeCount()`，沙盒销毁时 `remove()`。

3. **演示数据**
   - `ADMIN_METRICS_DEMO_SEED=true` 时，仅在真实 Run 为空时注入 18 条样例；
     响应带 `demo: true`，前端顶部有“演示数据”标识，避免误读为生产指标。

4. **前端管理中台**
   - `AuthContext` 增加 `isAdmin`；Mock 登录勾选“以管理员身份登录”，或账号
     以 `admin@` 开头，即获得 `platform_admin` 角色。
   - 右上角账户菜单按 `isAdmin` 条件渲染“管理中台（Admin Console）”。
   - `/admin` 由 `AdminRoute` 守卫：未登录 → `/login`，非管理员 → 整页 403
     （`AdminForbidden`），且不发起任何管理 API 请求；BFF 侧再兜一层 403。
   - 看板：4 个 KPI 卡、Agent 调用占比横向条形图（直接标注，纯 CSS/SVG，
     无图表库依赖）、最近 10 次执行表；30 秒自动刷新，支持 AbortController 取消。
   - 配色经 dataviz 校验：`#3987e5`（UI）/ `#d95926`（Architect）在 `#04060b`
     深色底上通过 lightness / chroma / CVD / 对比度检查。

5. **文档**
   - `docs/design/03.API接口设计.md` 新增 §14.4.1，定义请求头、200 响应体、
     字段语义、状态枚举与 403 错误契约。

## 验证

- `mvn -pl apps/platform-bff test`：`AdminMetricsControllerTest` 4 项全过
  （缺角色 / member → 403；admin + demo → 200 且口径正确；platform_admin 与
  tenant_operator 放行）。
- `npm run build`：前端构建通过。
- `scripts/admin_console_e2e_test.py`（Playwright）：管理员入口、看板渲染、
  非管理员无入口 + 直连 403 + 零管理请求、未登录重定向，全部 PASS。
- 截图：`docs/screenshots/m3-1-admin-console.png`、
  `docs/screenshots/m3-1-admin-forbidden.png`。

## 已知边界

- 指标在 JVM 内存中，BFF 重启即清零；多实例不共享。
- 真实持久化（Run 指标表 / Redis 聚合）与 Prompt Registry、Feedback Collector
  留给 M3.2–M3.4。
- 角色仍由 Header 模拟，尚未接入真实鉴权。
