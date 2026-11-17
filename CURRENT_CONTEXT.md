# vibe-platform · 当前状态简报

> 2026-11-17 · 历史细节见 docs/memory/

## 架构
React+Vite → Java21 BFF → Python3.12 FastAPI/LangGraph → Docker 沙盒；中间件复用 ~/Workspace/infra；六边形分层。

## 已闭环
- 画布 / Direct Ops：圈选、ADD/Inspect/拖拽、HITL、Diff、快照回滚；NodeIdentity/ScopePolicy、错误信封。
- 生成链路：Architect 契约、多 Agent、语义路由、RAG、Token 成本、AST/ORM/DDL/契约审计与风险门禁。
- 离线可视化：VisualDocument（多页面/navigate）+ 离线画布 + 页面 Tab + 高保真预览 + 局部 Agent + 冻结/审查。
- 阶段三 CodeGen：Tailwind JSX + data-node-id/data-source，多页面导航；Babel AST 门禁；写回源码、v0 快照、ChangeLog。
- Phase 1 存量只读投影：Babel 分析 → Python/BFF → 导入 .jsx/.tsx 只读树；不转虚拟树、暂不可编辑。
- 双轨制分流：前后端判定；新建强制选从零/导入；存量只读投影；禁止 VisualDocument/前端 CodeGen；只允许 backend 增量。
- 两阶段产出：新建首次只出 V0 虚拟树（后端字段自动剥离）；画布编辑后算 delta；第二次只做 BACKEND_BINDING；最终契约带冻结树供 CodeGen。
- Architect 稳定性：流式断连重试 + token reset + 可恢复错误。

## 最新验证
前端 59 项 Vitest、构建通过；Python 全量 675 项仅 7 项环境失败。CodeGen/投影/分流/V0→delta/多页面预览 E2E PASS。

## 真假边界
DeepSeek 真实调用；鉴权 Mock；BFF 无持久化；沙盒无 gVisor；未云端部署。

## 下一步
存量 Phase 2 AST 直改；V0 视觉编辑体验打磨；AST build 门禁；Redis / 云端。
