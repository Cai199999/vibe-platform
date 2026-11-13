# vibe-platform · 当前状态简报

> 2026-11-13 · 历史细节见 docs/memory/

## 架构
React+Vite → Java21 BFF → Python3.12 FastAPI/LangGraph → Docker 沙盒；中间件复用 ~/Workspace/infra；AI Core 六边形分层。

## 已闭环
- 画布圈选/ADD/Inspect/批量拖拽、双屏同步、HITL 澄清、Diff 审批、取消、Git 快照回滚。
- 全栈生成：Architect 契约、多 Agent、语义路由自愈、SQLite/MySQL 验收、导入导出。
- 混合 RAG、分级决策、Reflection、Token 成本、骨架防脑补、Agent/知识库管理。
- AST/ORM/DDL 门禁、Applier 隔离、契约审计、缺陷归因、风险门禁 L1/L2/L3。
- Phase1：确定性 Resolver + apply_ast_edit 硬门禁；Phase2：AWAITING_DECISION + decision.required + 3 轮上限。
- 画布生命周期：生成后自动 createSnapshot；Zustand+IndexedDB 草稿按 tenant:user:project 隔离；revision barrier 保留保存期间新操作；PreviewState 驱动 effectiveLinked；关闭/退出/刷新守卫+同步墓碑。
- 暂存组件自由拖拽：视口 clamp、SDK stage-hit 父容器高亮、Drop 写 placement（instanceId+兄弟锚点+siblingIndex）；保存时 Resolver 逐项解析，apply_ast_edit 支持 inside_* 插入。

## 最新验证
前端 13 项 Vitest、Vite build 通过；Python phase1/2 共 55 项通过。

## 真假边界
DeepSeek 真实调用；鉴权 Mock；BFF 无持久化；沙盒无 gVisor；未云端部署。

## 下一步
孤儿 Draft 7 天回收；多新增组件同父容器连续插入的 offset 重算；真实沙盒端到端验收；Redis checkpoint；云端部署。
