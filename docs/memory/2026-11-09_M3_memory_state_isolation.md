# 2026-11-09 · 多智能体记忆与状态隔离

## 目标

消除「不同 Agent / 不同项目共用记忆库」导致的上下文污染与 Token 爆炸，
把隔离从“约定”固化为可执行的物理键与 E2E 断言。

## 落地

- `domain/scope.py`：`memory_scope(tenant, project)` 生成 24 位 SHA-256
  作用域；`scoped_checkpoint_key(kind, runId, ...)` 统一 checkpoint 物理键。
- `ThreadMemoryStore`：所有读写增加 `scope`，同名 `threadId` 在不同项目 /
  租户下拥有独立的对话、沙盒、checkpoint 与回滚记录；回滚请求新增
  `tenantId/projectId`（BFF 同步透传）。
- LangGraph：UI 使用 `ui_run:<scope>:<runId>`，Architect 使用
  `architect_run:<scope>:<runId>`，共享 `MemorySaver` 也互不可读。
- Supervisor：新增 `architectHistory` 专属入口；Dispatcher 只投影
  `role/content`（≤30 条、单条 ≤8k）。旧 `uiHistory` 仅保留反序列化兼容，
  不再转发给任何子图。前端 Architect 历史改取自 Architect 面板状态。
- State 边界：`AgentState` 禁止契约/架构字段，`ArchitectState` 禁止 Coder
  产物字段，由 schema 断言锁定。

## 测试

`services/orchestrator/tests/test_memory_isolation.py`（10 项）：

- Scope 键稳定性 / 项目 / 租户 / Agent kind 区分；
- `ThreadMemoryStore` 跨项目不共享历史、沙盒、checkpoint、回滚；
- State 字段边界；
- Supervisor Context 投影；
- E2E：真实 UI Graph 改按钮 + 真实 Architect Graph 澄清，共享
  `MemorySaver`，断言双方状态与 checkpoint 完全隔离。

全量：148 tests passed。`black` 无改动。

## 全局 vs 独立

- 全局（容器，不承载业务隔离）：checkpoint Provider、ThreadMemoryStore、
  活跃 Run 注册表、事件缓冲区、门控注册表；均以 tenant/project/kind/run
  物理键区分，重启清空。
- 独立：每个 scope 的 ThreadRecord、每个 kind+scope 的 LangGraph
  checkpoint、每轮 Run 的 active 快照、每个标注 thread 的 chatHistory。
