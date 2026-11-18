# vibe-platform · 当前状态简报

> 2026-11-18 · 历史细节见 docs/memory/

## 架构
React+Vite → Java21 BFF → Python3.12 FastAPI/LangGraph → Docker 沙盒；中间件复用 ~/Workspace/infra；六边形分层。

## 已闭环
- 画布/Direct Ops：圈选、拖拽、HITL、Diff、回滚；NodeIdentity/ScopePolicy、错误信封。
- 生成链路：Architect 契约、多 Agent、语义路由、RAG、审计门禁。
- 离线可视化：多页面 VisualDocument + 页面 Tab + 高保真/模拟预览 + 冻结/审查。
- 阶段三 CodeGen：Tailwind JSX + node-id/source；Babel 门禁；自带 inspect SDK 与 /api 代理；写回源码、v0 快照、ChangeLog。
- 存量只读投影：Babel 分析 → 导入 .jsx/.tsx 只读树；不转虚拟树。
- 双轨制：前后端分流；新建强制选从零/导入；空工作区可直达导入；Resolver 以 files/sandbox 事实判定（仅声明存量不再生效）；Fullstack Run 带 projectMode，存量禁止 frontend/fullstack CodeGen，仅后端增量；Architect 首屏切换为存量文案。
- 源码持久化：导入先 PUT BFF，MySQL 存元数据/文件树，MinIO 存正文；重开/清缓存从后端回灌，ReadOnly 投影可直接补拉。
- 两阶段：V0 只出虚拟树 → 画布 delta → BACKEND_BINDING；最终契约带冻结树。
- 审查门禁：FROZEN repeat 未绑定仅 warning；CodeGen 用示例数据；二次 Architect 全量审查交互/repeat+delta。
- 方案文档 01–05；质量分级一次；禁物理外键、分层。
- 源码级可视化：前端 Agent 语义工具复用 Direct Ops（文本/移动/增删/撤回），不 fork Utopia。

## 最新验证
前端 68 项 Vitest；Python 701 项全通过；BFF 23 项 Maven 测试；源码 PUT/GET 经 Vite 代理与真实 MySQL/MinIO 往返 PASS。

## 真假边界
DeepSeek 真实调用；鉴权 Mock；BFF 仅源码快照持久化（Run/指标仍无持久化）；沙盒无 gVisor；未云端部署。

## 下一步
P0：构建期 data-oid / vibe-source-map、node-id→AST 索引、实例路径；P1：class/style/props、wrap、预览/构建验证；存量 Phase 2；Redis/云端。
