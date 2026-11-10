# 2026-11-10 · 全栈语义路由与一致性自愈

## 背景

用户的全栈生成用例中，前端生成了抓取 JD、AI 改写、投递、对话四个独立业务请求，
但 BackendCoder 只生成了一个 `/api/v1/resumes` CRUD；ConsistencyVerifier 又把
缺失路径建议成“前端改请求简历接口”，会直接摧毁业务语义。

根因有三层：

1. BackendCoder 是确定性模板，只注册 collection/item 通用 CRUD，完全忽略契约
   `apiRoutes` 中的业务动作；
2. `server/api-routes.json` 只写 collection/item，一致性检查也只读这两个字段，
   即使后端注册了业务路由也看不见；
3. 一致性失败直接阻断，没有把缺失路由回传 Coder 自愈。

## 修复内容

### 1. BackendCoder：每个业务动作独立路由

- `generate_backend_bundle(contract, extra_routes=...)` 合并契约路由与自愈补齐路由。
- 通用 CRUD 已覆盖的 `(Method, Path)` 不重复注册；其余每个动作生成独立
  `app.get/post/put/patch/delete(...)`，POST/PUT/PATCH 返回
  `{success, route, description, data, message}` 的确定性 Mock 响应。
- 集合路径选择排除 `/scrape` `/rewrite` `/deliver` `/messages` `/chat` 等
  动作后缀，避免把 `/api/v1/jobs/scrape` 误当成数据表根路径。
- `api-routes.json.routes` 记录**实际注册的全部路由**（含 source），作为一致性
  唯一事实来源；`contractRoutes` 继续保留契约声明，兼容旧读取方。

### 2. ConsistencyVerifier：FixAction 只指向 BackendCoder

- `ConsistencyIssue` 新增 `fixAction`：
  - `ADD_BACKEND_ROUTE`：路径缺失 / Method 缺失；
  - `EXTEND_BACKEND_FIELD`：Payload 字段不在 DDL 可写列。
- 修复建议统一为“由 BackendCoder 补齐 Method + Path / 扩展字段，前端请求保持不变”，
  删除 `_closest_route`，不再推荐任何无关的现有 CRUD 路径。
- `ConsistencyReport.missing_routes()` 输出去重后的 `{method, path, description}`；
  `repairable` 只对 path/method 为真，Payload 冲突不会空转重试。
- `extract_backend_routes` 优先解析 manifest `routes`，旧清单才回退
  collection / item / contractRoutes。

### 3. LangGraph 自愈闭环

- 拓扑新增条件边：
  `BackendCoder -[正常]-> FrontendCoder`
  `BackendCoder -[自愈]-> ConsistencyVerifier`
  `ConsistencyVerifier -[通过]-> BuildVerifier`
  `ConsistencyVerifier -[缺失路由]-> BackendCoder`
- 缺失路由时发出 `fullstack.consistency.repair` 事件（attempt/maxAttempts/
  missingRoutes/report），不抛错、不阻断用户；BackendCoder 重新生成
  `server/index.js` 与 `api-routes.json` 后自动重检。
- `MAX_CONSISTENCY_REPAIRS = 2`：初始检查 + 最多 2 次补齐；仍失败才抛
  `FullstackConsistencyError`，且不会进入 BuildVerifier / SandboxRestart。
- 自愈只重跑 BackendCoder，不覆盖前端 App.jsx；前端业务请求是事实来源。

## 验证

- 新增/更新测试：
  - `test_every_business_action_gets_a_matching_backend_route`
  - `test_resume_assistant_business_actions_are_all_registered`
  - `test_consistency_failure_self_heals_by_backend_routes`
  - `test_consistency_retries_at_most_twice_then_fails`
- 全量后端测试：**280 tests OK**（一次性容器 + 仓库只读挂载）。
- 真实 Node 冒烟：用生成代码启动 Express + SQLite，`/api/health` 正常，
  `POST /api/v1/jobs/scrape`、`/resumes/1/rewrite`、`/applications/deliver`、
  `/chat/messages` 均返回 HTTP 201 且 `success=true`；`node --check` 通过。
- Python AI 镜像已重建并重启。

## 注意

- BackendCoder 当前没有 LLM System Prompt（确定性模板）；契约规则已固化在
  `generate_backend_bundle` docstring、模板注释和 `_business_route_code` 中。
  若未来切换到 LLM Coder，需把同样的规则写入其 System Prompt。
- 业务路由默认返回 Mock 数据；真正持久化仍走 collection/item 的 Sequelize 仓储。
