# vibe-platform · 当前状态简报

> 2026-11-13 · 历史细节见 `docs/memory/`

## 架构

React + Vite → Java 21 BFF → Python 3.12 FastAPI/LangGraph → Docker 沙盒；中间件复用 `~/Workspace/infra`；AI Core 六边形分层。

## 已闭环能力

- 画布圈选 / ADD / Inspect / 批量拖拽、多标注、双屏同步、HITL 澄清、Diff 审批、取消、Git 快照回滚。
- 全栈生成：Architect 契约、多 Agent、语义路由自愈、SQLite/MySQL 验收、产物导出导入。
- 混合 RAG、分级决策、Reflection、Token 成本、骨架防脑补、Agent 管理与知识库上传。
- AST/ORM/DDL 门禁、Applier 责任隔离、契约审计、代码/契约缺陷归因。
- 风险门禁 L1/L2/L3；圈选精度夹具；实例污染 AST 拦截；草稿隔离 worktree + 5174 预览。
- **Phase 1 工具协议**：确定性 Resolver 输出锚点 / 白名单 / 公共组件保护；Coder 仅能调 `apply_ast_edit`（Babel 精确切片 + 删除同构校验 + 硬门禁）。
- **Phase 2 决策分流**：歧义 / 公共组件保护转 `AWAITING_DECISION` + `decision.required`；安全选项仅 select_target / modify_call_site / cancel；单 interrupt 自环重解析，3 轮上限。

## 最新验证

Python 520 项测试通过（含 Phase 1 25 项、Phase 2 17 项，含真实 LangGraph interrupt/resume）；真实沙盒 Resolver/Edit 冒烟通过；Vite build / BFF package 通过。

## 真假边界

DeepSeek 真实调用；鉴权 Mock；BFF 无持久化；沙盒无 gVisor；未真实部署云端。

## 下一步

Phase 2 前端联调与真实沙盒验收；抽取新组件选项；Redis checkpoint；云端部署。
