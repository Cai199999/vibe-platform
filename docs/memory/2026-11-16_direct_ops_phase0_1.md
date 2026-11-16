# 2026-11-16 Direct Ops Phase 0/1：地基与 UPDATE_TEXT 闭环

## Phase 0：契约与写锁

- 新增 `ai_core/domain/direct_ops.py`：
  - `DirectActionPayload`（target / action / payload 必填）
  - `DirectActionRisk`：L0_DIRECT / L1_VERIFY_DIRECT / L2_HITL /
    L3_AI_REQUIRED
  - `DirectErrorCode`、`DirectActionResponse`、`ChangeLogEntry`
  - 非 dryRun 强制 `baseRevision`；UPDATE_TEXT 文案禁止 `< > { }` 与换行
- 新增 `ai_core/project_lock.py`：
  - 按 projectId 的进程内互斥锁，支持超时与观测 snapshot；
  - AI Run 的 promote + commit 持锁；
  - Fullstack 整段生成持锁；
  - Rollback 的 `git reset --hard` 持锁；
  - Direct Ops 执行 + commit 持锁。
- 新增 `direct.*` 事件命名空间：`direct.started / direct.succeeded /
  direct.failed`（`direct.decision.required` 预留给 HITL）。
- 前端 Inspect 悬浮卡新增 ➕ ❌ ✏️ 工具栏：
  - Phase 0 的 ➕ / ❌ 只发 dryRun；
  - ✏️ 在 Phase 1 直接执行 UPDATE_TEXT。

## Phase 1：UPDATE_TEXT 极简闭环

链路：`validate → resolve_targets → risk_gate → read → update_text →
apply_patch → git_commit → ChangeLog`。

- Babel 脚本 `apply_ast_edit.mjs` 新增 `update_text`：
  - 只允许唯一 `JSXText` 子节点；
  - 混合子元素 / 表达式 / 自闭合元素返回 `TEXT_TARGET_NOT_LEAF`；
  - 保留原缩进与换行，替换后重新 parse 校验。
- 新增 `ai_core/direct_ops/service.py`：
  - 服务端重新解析 target，不信任 selector；
  - 公共组件 / 多实例 → L2_HITL，不直接写；
  - baseRevision 不一致 → 409 `DIRECT_WRITE_CONFLICT`；
  - 项目写锁超时 → 409 `DIRECT_PROJECT_BUSY`；
  - 写后 commit 失败自动回滚原文件；
  - idempotencyKey 防重复写入；
  - 成功后写 ChangeLog（动作、目标、diff 摘要、前后哈希、commit）。
- 新增 API：`POST /api/v1/internal/direct-ops/actions`、
  `GET /api/v1/internal/direct-ops/revision`；BFF 新增
  `/api/v1/direct-ops/*` 代理。
- 前端新增 `workspace/directOps.js`，WorkspacePage 在每次写入前读取
  revision，成功后刷新预览。

## 验证

- 项目写锁：8 线程 × 20 次循环，最大同时持锁数 = 1，无异常；
  超时返回 `ProjectBusyError`。
- Direct Ops 单测 8 项；Babel update_text 5 项；全量 Python 572 项通过。
- 前端 16 项 Vitest 通过，生产构建通过；BFF 单测通过。
- 真实沙盒端到端（UPDATE_TEXT）：
  - dryRun：680ms（revision 260 / resolve 179 / AST 编辑 238）
  - 真实写入：1673ms（revision 274 / resolve 184 / AST 225 /
    write 608 / commit 378 / changelog 0.3）
  - 文件已变为 `<h1>我的任务</h1>`，commit
    `7952a69 direct: dact_e2e_real - UPDATE_TEXT`。
  - 旧 revision 重放返回 409 标准信封；BFF revision/actions 代理正常。

## 结论与后续

- 写锁与 revision 校验有效；AI Run / Direct Ops 主工作树写入不会并发。
- UPDATE_TEXT 目前是“亚秒到 1.7 秒级”，尚未达到严格毫秒级。主要耗时：
  多次 Docker exec 往返、`apply_patch` 的 200ms Vite settle sleep、
  Git commit 容器内进程启动。
- 若要进一步压缩：合并 resolve/edit/write/commit 为单次容器内脚本、
  Direct Ops 跳过 settle sleep、或引入容器内常驻执行器。
- ChangeLog 目前为进程内环形存储，Architect 注入与持久化留待后续阶段。
