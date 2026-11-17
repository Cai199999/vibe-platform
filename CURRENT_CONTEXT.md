# vibe-platform · 当前状态简报

> 2026-11-17 · 历史细节见 docs/memory/

## 架构
React+Vite → Java21 BFF → Python3.12 FastAPI/LangGraph → Docker 沙盒；中间件复用 ~/Workspace/infra；六边形分层。

## 已闭环
- 画布 / Direct Ops：圈选、拖拽、HITL、Diff、快照回滚；NodeIdentity/ScopePolicy、错误信封。
- 生成链路：Architect 契约、多 Agent、语义路由、RAG、AST/ORM/DDL/契约审计与风险门禁。
- 离线可视化：多页面 VisualDocument + 页面 Tab + 高保真/模拟网页预览 + 冻结/审查。
- 阶段三 CodeGen：Tailwind JSX + data-node-id/data-source，多页面导航；Babel 门禁；写回源码、v0 快照、ChangeLog。
- 存量只读投影：Babel 分析 → 导入 .jsx/.tsx 只读树；不转虚拟树。
- 双轨制：前后端分流；新建强制选从零/导入；存量禁止 VisualDocument/前端 CodeGen，只 backend 增量。
- 两阶段：V0 只出虚拟树（后端字段剥离）→ 画布 delta → BACKEND_BINDING；最终契约带冻结树。
- 审查门禁：FROZEN repeat 未绑定仅 warning，CodeGen 才阻断；第二次 Architect 全量审查交互/repeat + delta。

## 最新验证
前端 62 项 Vitest、构建通过；Python 全量 676 项仅 7 项环境失败。CodeGen/投影/分流/V0→delta/多页面模拟预览 E2E PASS。

## 真假边界
DeepSeek 真实调用；鉴权 Mock；BFF 无持久化；沙盒无 gVisor；未云端部署。

## 下一步
存量 Phase 2 AST 直改；V0 视觉编辑体验打磨；AST build 门禁；Redis / 云端。
