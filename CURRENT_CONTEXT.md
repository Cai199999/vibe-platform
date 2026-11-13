# vibe-platform · 当前状态简报

> 2026-11-13 · 历史细节见 `docs/memory/`

## 架构

React + Vite → Java 21 BFF → Python 3.12 FastAPI/LangGraph → Docker 沙盒；中间件复用 `~/Workspace/infra`；AI Core 六边形分层。

## 已闭环能力

- 画布圈选 / ADD / Inspect / 批量拖拽、多标注、双屏同步、HITL 澄清、Diff 审批、取消、Git 快照回滚。
- 全栈生成：Architect 契约、多 Agent、语义路由自愈、SQLite/MySQL 验收、产物导出导入。
- 混合 RAG、分级决策、Reflection、Token 成本、骨架防脑补、Agent 管理与知识库上传。
- AST/ORM/DDL 门禁、Applier 责任隔离、契约审计、代码/契约缺陷归因。
- 风险门禁 L1/L2/L3：按契约质量开关 AST、ORM/DDL、SQLite 与 MySQL 验收。
- **圈选精度**：固定夹具 + 6 条用例 + Playwright 点击/圈选，产出 Hit Rate 与 IoU。
- **实例污染 P0**：AST 差分拦截公共组件 / 无守卫 JSX / 数据源篡改；结构化删除阈值与 export 保护。
- **草稿隔离 P0**：git worktree + 5174 草稿预览；审批前主树零变化，批准后 ff-only 合并，拒绝/失败/取消自动丢弃。

## 最新验证

Python 469 项测试通过；Vite build 通过；草稿隔离 E2E 19/19（暂停主树 SHA 不变、草稿可预览、批准合并、拒绝零变化）。

## 真假边界

DeepSeek 真实调用；鉴权 Mock；BFF 无持久化；沙盒无 gVisor；未真实部署云端。

## 下一步

Phase 1 Tool 协议（Resolver / apply_ast_edit）；Phase 2 多轮决策分流；Redis checkpoint；云端部署。
