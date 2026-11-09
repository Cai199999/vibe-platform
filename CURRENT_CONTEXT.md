# vibe-platform · 当前状态简报

> 2026-11-09 · 历史见 `docs/design`、`docs/archive`

## 架构底座

React+Vite → Java BFF → Python FastAPI/LangGraph → Docker 沙盒（Node 22+Vite+Express）；中间件复用 `~/Workspace/infra`；AI Core 六边形分层。

## 已闭环

- 画布圈选/ADD/Inspect、多标注多 Tab、导入预览
- SSE 重放、HITL 澄清、Diff 审批、取消、Git 回滚/快照
- Supervisor 路由、Architect 动态澄清、8 Agent Manifest、Tool/Skill/MCP
- 记忆隔离、上下文压缩
- 生产级全栈：六节点 FullstackGraph（DBA→Backend→Frontend→Infra→Verifier→Restart）；Sequelize+`DATABASE_URL` 连 MySQL/PostgreSQL，沙盒回退 node:sqlite；契约 `databaseEngine` 三选一
- 生成 IaC：Dockerfile/.dockerignore/render.yaml/.env.example/DEPLOY.md
- 预览【🚀 部署上线】Mock 指引（Render/Vercel/Railway）
- 验证：Python 203、四套 E2E、前端构建、MySQL/SQLite 冒烟 PASS

## 真假边界

| 能力 | 状态 |
|---|---|
| DeepSeek | 接口已接，Eval 仍 Mock |
| 鉴权 | Mock Token |
| 持久化 | BFF 无 DB；生产路径需自备云库 |
| 中间件 | 仅配置未读写 |
| 沙盒 | 双服务，无 gVisor |
| 部署按钮 | 仅指引 |
| Agent | 3 available + 5 reserved |
| 外网 | 不可达，search_web 失败 |

## 下一步

- P0 DeepSeek 基线
- P0 M3.2 知识中台
- P1 圈选精度、Redis checkpoint
- P2 部署按钮接真实 API
