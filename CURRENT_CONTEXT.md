# vibe-platform · 当前状态简报

> 2026-11-17 · 历史细节见 docs/memory/

## 架构
React+Vite → Java21 BFF → Python3.12 FastAPI/LangGraph → Docker 沙盒；中间件复用 ~/Workspace/infra；AI Core 六边形分层。

## 已闭环
- 画布 / Direct Ops：圈选、ADD/Inspect/拖拽、HITL、Diff、快照回滚；✏️/↕/➕/❌；NodeIdentity/ScopePolicy、错误信封、项目写锁。
- 生成链路：Architect 契约、多 Agent、语义路由、混合 RAG、Reflection、Token 成本、AST/ORM/DDL/契约审计与风险门禁。
- 离线可视化：VisualDocument + 离线画布 + 局部节点 Agent + 冻结 / 业务审查。
- 阶段三 CodeGen：Tailwind JSX + data-node-id / data-source，完整 Vite 工程；Babel AST 门禁；构建失败安全占位；工作台写回源码、v0 快照、ChangeLog。
- 真实沙盒 E2E：npm install / build / dev + Playwright PASS（0 错误）。
- Phase 1 存量只读投影：Babel 分析 → Python/BFF → 导入 .jsx/.tsx 只读树；hover 层级/className/位置；明确不转虚拟树、暂不可编辑。

## 最新验证
前端 47 项 Vitest、构建通过；Python 20 项相关全绿；全量 653 项仅 7 项环境失败。真实沙盒 CodeGen + Playwright、存量投影浏览器 E2E 均 PASS。

## 真假边界
DeepSeek 真实调用；鉴权 Mock；BFF 无持久化；沙盒无 gVisor；未云端部署。

## 下一步
存量 Phase 2 ASTCommandLayer（docs/design/12，源码直改，不转虚拟树）；阶段四 Direct Ops；custom LLM 兜底；二次冻结；多项目隔离；Redis / 云端。
