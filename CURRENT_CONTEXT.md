# vibe-platform · 当前状态简报

> 2026-11-13 · 历史细节见 `docs/memory/`

## 架构

React + Vite → Java 21 BFF → Python 3.12 FastAPI/LangGraph → Docker 沙盒；中间件复用 `~/Workspace/infra`；AI Core 六边形分层。

## 已闭环能力

- 画布圈选 / ADD / Inspect / 批量拖拽、多标注、双屏同步、HITL 澄清、Diff 审批、取消、Git 快照回滚。
- 全栈生成：Architect 契约、Frontend/Backend/DBA/Infra 多 Agent、语义路由自愈、SQLite/MySQL 验收、产物导出导入。
- 混合 RAG、分级决策、Reflection、Token 成本、骨架防脑补、Agent 管理与知识库上传。
- AST/ORM/DDL 门禁、Applier 责任隔离、契约审计、契约缺陷 vs 代码缺陷归因。
- 风险自适应门禁 L1/L2/L3：按契约质量分级开关 AST、ORM/DDL、SQLite 与真实 MySQL 验收。
- **圈选精度评测**：固定夹具 + 6 条 YAML 用例 + Playwright 真实点击/圈选，产出 Hit Rate 与 IoU；修复 SDK 节点去重键冲突与可交互父级提升。

## 最新验证

Python 447 项测试通过；Vite build 通过；风险分级三轮 E2E 通过。圈选评测基线 44.45% / IoU 0.643，修复后 100% / IoU 1.000；Inspect E2E 回归通过。

## 真假边界

DeepSeek 真实调用；鉴权 Mock；BFF 无持久化；沙盒无 gVisor；未真实部署云端。

## 下一步

Redis checkpoint、迁移回滚脚本、多服务编排、真实云端部署。
