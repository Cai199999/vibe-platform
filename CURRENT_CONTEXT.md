# vibe-platform · 当前状态简报

> 唯一上下文入口 · 2026-11-09 · 详细历史见 `docs/design`、`docs/archive`（默认不读）

## 架构底座

React 18 + Vite → Java 21 + Spring Boot 3.3.5 BFF（WebClient）→ Python 3.12 + FastAPI + LangGraph → Docker 沙盒（Node 20 + React/Vite，单服务）。中间件复用 `~/Workspace/infra`：MySQL / Redis / MinIO。

## 已闭环能力

- 画布圈选 / 空白 ADD、多标注（≤3）、多轮对话、世界坐标无漂移
- SSE `seq`/`afterSeq` 重放、HITL 澄清、Diff 审批、取消、Git 回滚
- Supervisor 路由：ui / architect / both / custom / rejected
- Architect 形态判别 + 动态澄清 + BM25 契约
- 多 Tab、zip/HTML/图片导入、预览、`/admin` 看板
- Token 降本：模型分级、历史摘要、错误截断、同错终止
- Eval 双引擎 10/10；单测 108 passed

## 真 / 假边界

| 能力 | 真实状态 |
|---|---|
| DeepSeek | 已接 OpenAI 兼容接口，`.env` 有 Key；Eval 仍用 Mock |
| 鉴权 | 前端 Mock Token + `X-User-Role`，无 JWT |
| 持久化 | BFF 无 DB 层；状态进程内，重启丢失 |
| MySQL / Redis / MinIO | 仅连接配置，未读写 |
| 数据中台 | Knowledge / Prompt / Feedback 仅契约 |
| 沙盒 | 单 React/Vite，无多服务、无 gVisor |
| Agent | BackendCoder / ScriptAgent 仅 Manifest |
| Admin Eval | 契约冻结，未接路由 |

## 下一步

- **P0 真实 DeepSeek**：Eval 接 `deepseek-chat`，建质量 / 成本基线
- **P0 M3.2 知识中台**：摄取、租户隔离检索
- **P1 前端精度**：高 DPI / 极端缩放下的圈选与浮层坐标
- **P1 运行时**：Redis checkpoint + Stream + 分布式门控
- **P2 契约驱动**：Spring Boot / DDL + 多服务沙盒
- **P2 平台化**：真实认证 / RBAC、Prompt Registry、Feedback
