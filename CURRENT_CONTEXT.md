# vibe-platform · 当前状态简报

> 2026-11-13 · 历史细节见 `docs/memory/`

## 架构

React + Vite → Java 21 BFF → Python 3.12 FastAPI/LangGraph → Docker 沙盒；中间件复用 `~/Workspace/infra`；AI Core 六边形分层。

## 已闭环能力

- 画布圈选 / ADD / Inspect / 批量拖拽、多标注、双屏同步、HITL 澄清、Diff 审批、取消、Git 快照回滚。
- 全栈生成：Architect 契约、Frontend/Backend/DBA/Infra 多 Agent、语义路由自愈、SQLite/MySQL 验收、产物导出导入。
- 混合 RAG、分级决策、Reflection、Token 成本、骨架防脑补、Agent 管理与知识库上传。
- AST/ORM/DDL 门禁、Applier 责任隔离、契约审计、契约缺陷 vs 代码缺陷归因。
- **风险自适应门禁**：契约确认前必选 L1/L2/L3；L1 跳过 AST/ORM/MySQL 且失败不阻塞；L2 保留 AST+ORM+SQLite；L3 追加真实 MySQL 迁移验收与缺陷重试。

## 最新验证

Python 447 项测试通过（排除历史 `test_eval_suite`）；Vite build 通过；三轮浏览器 E2E 通过：L1 黄色告警徽标、L2 无徽标、L3 绿色徽标 + information_schema 验收通过。

## 真假边界

DeepSeek 真实调用；鉴权 Mock；BFF 无持久化；沙盒无 gVisor；未真实部署云端。

## 下一步

圈选精度评测、Redis checkpoint、迁移回滚脚本、多服务编排、真实云端部署。
