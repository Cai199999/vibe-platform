# vibe-platform · 当前状态简报

> 2026-11-17 · 历史细节见 docs/memory/

## 架构
React+Vite → Java21 BFF → Python3.12 FastAPI/LangGraph → Docker 沙盒；中间件复用 ~/Workspace/infra；AI Core 六边形分层。

## 已闭环
- 画布 / Direct Ops：圈选、ADD/Inspect/拖拽、HITL、Diff、取消、快照回滚；✏️/↕/➕/❌ 全链路；NodeIdentity/ScopePolicy；错误信封与项目写锁。
- 生成链路：Architect 契约、多 Agent、语义路由自愈、混合 RAG、Reflection、Token 成本、AST/ORM/DDL/契约审计与风险门禁。
- 离线优先可视化：VisualDocument + 离线画布 + 局部节点 Agent + 冻结 / 业务审查。
- 阶段三 CodeGen：确定性 Compiler 注入 data-node-id，输出 App.jsx + visual.css；Babel AST 门禁（import 白名单、nodeId 唯一 / 完整、禁绝对定位 / 原始 HTML）；构建失败降级安全占位页；工作台“生成应用”写回源码、建 v0 快照并切 live。
- 真实 Docker 沙盒 E2E 覆盖编译 → build → Vite 重启。

## 最新验证
前端 41 项 Vitest、生产构建通过；Python 新增 15 项 CodeGen 全绿；全量 648 项仅 7 项环境相关失败（MCP demo / 写权限 / eval_suite 依赖）。真实沙盒 E2E PASS（6 节点）。

## 真假边界
DeepSeek 真实调用；鉴权 Mock；BFF 无持久化；沙盒无 gVisor；未云端部署。

## 下一步
阶段四 Direct Ops 基于 data-node-id 微调；custom 节点 LLM 兜底；审查后二次冻结；VisualDocument 多项目隔离；Redis checkpoint / 云端部署。
