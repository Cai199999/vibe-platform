# vibe-platform · 当前状态简报

> 唯一上下文入口 · 2026-11-09 · 详细历史见 `docs/design`、`docs/archive`（默认不读）

## 架构底座

React 18 + Vite → Java 21 + Spring Boot 3.3.5 BFF（WebClient）→ Python 3.12 + FastAPI + LangGraph → Docker 沙盒（Node 20 + React/Vite，单服务）。中间件复用 `~/Workspace/infra`：MySQL / Redis / MinIO。AI Core 已按六边形架构分层：`domain`（纯模型）/ `ports`（抽象）/ `adapters`（实现）/ `agent·architect·supervisor`（应用编排），旧 import 保留兼容 shim。

## 已闭环能力

- 画布圈选 / 空白 ADD / 精准点击 Inspect（React+Vue 源码文件:行:列）
- 多标注（≤3）、多轮对话、世界坐标无漂移
- SSE `seq`/`afterSeq` 重放、HITL 澄清、Diff 审批、取消、Git 回滚
- Supervisor 路由：ui / architect / both / custom / rejected
- Architect 形态判别 + 动态澄清 + BM25 契约
- 多 Tab、zip/HTML/图片导入、预览、`/admin`
- Token 降本：模型分级、历史摘要、错误截断、同错终止
- 团队 8 Agent Manifest（3+5）
- Tool：`search_web`（research 节点）+ Supervisor 工具网关
- Skill：Markdown 挂载 `ui.accessibility`，动态注入 Coder
- MCP：标准 stdio 子集 + `get_current_time` 示例
- 六边形重构：`AnnotationEnvelope/ArchitectContract/State` 入 domain；
  `ModelClient/Sandbox/RAG/Checkpoint` 入 ports；DeepSeek/Docker/Markdown/Chroma 入 adapters
- Eval 10/10；单测 138 passed

## 真 / 假边界

| 能力 | 真实状态 |
|---|---|
| DeepSeek | 已接 OpenAI 兼容接口，`.env` 有 Key；Eval 仍用 Mock |
| 鉴权 | Mock Token + Header，无 JWT |
| 持久化 | BFF 无 DB；状态进程内，重启丢失 |
| MySQL / Redis / MinIO | 仅连接配置，未读写 |
| 数据中台 | Knowledge/Prompt/Feedback 仅契约 |
| 沙盒 | React/Vue Vite 单服务，无多服务/gVisor |
| Agent | 3 available + 5 reserved；reserved 仅 Manifest |
| 外网 | 当前环境不可达，`search_web` 返回可读失败；解析器已单测 |
| Admin Eval | 契约冻结，未接路由 |

## 下一步

- **P0 真实 DeepSeek**：Eval 接 `deepseek-chat`，建质量/成本基线
- **P0 M3.2 知识中台**：摄取、租户隔离检索
- **P1 前端精度**：高 DPI / 极端缩放的圈选与高亮坐标
- **P1 运行时**：Redis checkpoint + Stream + 分布式门控
- **P2 契约驱动**：Spring Boot/DDL + 多服务沙盒
- **P2 平台化**：真实认证/RBAC、Prompt Registry、Feedback
