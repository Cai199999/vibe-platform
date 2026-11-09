# vibe-platform · 当前状态简报

> 2026-11-09 · 历史见 `docs/design`、`docs/archive`

## 架构底座

React+Vite → Java BFF → Python FastAPI/LangGraph → Docker 沙盒（Node 22+Vite+Express）；中间件复用 `~/Workspace/infra`；AI Core 六边形分层。

## 已闭环

- 画布圈选/ADD/Inspect、多标注多 Tab、导入预览
- SSE 重放、HITL 澄清、Diff 审批、取消、Git 回滚/快照
- Supervisor 路由、Architect 动态澄清、8 Agent Manifest、Tool/Skill/MCP
- 记忆隔离、上下文压缩
- **对话前置建项（新）**：新建项目=空白画布+【✨ 开始创建】；纯自然语言驱动 Architect 多轮敲定契约（前后端路由/布局/组件/API/实体）；形态由 `infer_app_shape` 识别，已移除“全栈/轻量/脚本”三选一
- **自动前端接力（新）**：契约 CONFIRMED → 自动跑 FrontendCoder→BuildVerifier→SandboxRestart（`mode=frontend`）；`frontendEntry` 回写项目源码，画布从 blank→live 实时预览；失败可重试/重开架构
- 项目态：pending → architecting → confirmed → bootstrapping → ready/failed
- 生产级全栈：六节点 FullstackGraph；Sequelize+`DATABASE_URL`，沙盒回退 node:sqlite；IaC（Dockerfile/render.yaml/DEPLOY.md）
- 验证：Python 207、对话前置/架构面板/全栈门控/全栈生成/混合/双侧 E2E、前端构建 PASS

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
