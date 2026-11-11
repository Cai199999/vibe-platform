# vibe-platform · 当前状态简报

> 2026-11-11 · 历史见 `docs/design`、`docs/archive`

## 架构底座

React+Vite → Java BFF → Python FastAPI/LangGraph → Docker 沙盒；中间件复用 `~/Workspace/infra`；AI Core 六边形分层。

## 已闭环

- 画布圈选/ADD/Inspect、多标注、导入预览；SSE 重放、HITL 澄清、Diff 审批、取消、Git 回滚/快照。
- 对话前置建项 + Architect 多轮契约 + 自动前端接力；8 Agent Manifest、Tool/Skill/MCP、记忆隔离。
- 画布双态（annotate/interact）、紧凑工具栏、固定 1440×900 逻辑视口 + 容器原生滚动。
- 分级决策（CRITICAL 打断 / MAJOR 缺信息才问 / MINOR 静默）+ Reflection 偏好。
- **前端卡顿修复**：SSE 100ms 合并提交、组件 memo、非激活 Tab 卸载 iframe、runStates/Architect 800ms 防抖、移除页面级计时器。
- **真实模型基线**：Eval 默认 DeepSeek，10 例 7 过=70%，57s，26,223 tokens/¥0.050088；001/005 为断言只认 CSS（实际 React hex），004 使用未装 Tailwind 类=真实失败。
- **输出自修复**：Planner/Coder 校验失败回传原始输出+错误重试（`AGENT_JSON_RETRY_LIMIT=2`），Planner 耗尽降级文本计划。
- **Token 成本**：每节点采集 usage、reducer 累加，随 `node.finished`/响应/终态事件下发；`LLM_PRICING` 可配。
- **防脑补骨架**：抽象目标（如“做个工作台”）在 `skeleton_gate` 被确定性拦截，完全不调用 LLM；契约标记 `skeletonMode=true`，组件仅 `isPlaceholder=true`；FrontendCoder 只渲染 Header + Sidebar + Main、响应式 Grid、Welcome 与唯一占位。E2E 已跑通。
- **体验断层修复（六项）**：契约确认后对话栏常驻、可追加需求并走同一契约 REVISE；Inspect 在 live 画布可用；全栈前后端共用 collectionPath（不再猜 `/stream`）；侧边栏按路由真实切换内容；拖拽痕仅在画布、展示区渲染最终代码；移动改为 `move_component` Tool，只写 `transform: translate3d`，拖拽仅转自然语言指令。
- **企业级升级（五问题）**：画布完整 flex 链 + 自适应缩放，Tab 激活强制重算并重载 iframe；`管理 Agent` 支持内置锁定、AgentBuilder 新建/删除、知识库 `.md/.txt` 上传；Architect 强制交互澄清，前端按契约生成真实 onClick + Loading/Error/Success；新增 `ConsistencyVerifier`，构建前比对前端 Method/Path/Payload 与后端实际路由，缺失路由回传 BackendCoder 自愈（最多 2 次），耗尽才阻断。
- **Phase B 交互闭环**：画布/预览固定 1440×900 + 文档原生滚动；焦点模型——点击页面进网页焦点（滚轮滚页面），点击画布空白回画布焦点（滚轮缩放），中键/空格始终平移；SDK 补发真实滚动快照，双屏同步 scroll-to，并在预览重放 click 使 React 状态一致；Architect 双 Tab（架构确认/影子助手），影子 LLM 只读问答 + 顶部不可关闭待答 Banner + 生成门禁。
- **直接拖拽静默同步**：SDK 接管 pointer 手势，拖拽期间只写内联 `translate`；宿主 500ms 防抖合并后以 `action=MOVE`（携带 dx/dy/selector）静默派发 `ui_graph`，后端确定性调用 `move_component` 只写 `transform: translate3d` 并经 Vite HMR 生效；成功后清内联位移与记录，失败/超时回滚到拖前位置并 Toast；同步期间保持画布焦点与拖拽模式。E2E 覆盖防抖累计、二次叠加与失败回滚。
- **全栈语义路由自愈**：BackendCoder 为契约每个业务动作（抓取/改写/投递/对话等）注册独立 Express 路由并生成 Mock 处理，不再折叠为通用 CRUD；`api-routes.json.routes` 记录全部实际路由；一致性 FixAction 只让后端补路由，绝不改前端到无关接口；LangGraph `ConsistencyVerifier -> BackendCoder -> ConsistencyVerifier` 自愈最多 2 次。280 tests 通过，真实 Node 服务四端点均 201。
- **Architect 面板收尾**：移除架构确认/随意提问双 Tab，主状态机与影子助手问答合并为统一时间轴，正式提问高亮；正式推进只保留正文“追加修改需求…”，底部仅保留只读助手输入（标题已删），提问绝不触发决策；全栈 SUCCEEDED 且一致性通过后成功卡一键导出 tar.gz。E2E 覆盖路由隔离与真实下载。
- **全栈产物验收**：新增 `scripts/acceptance_test.py`，生成→导出→安全解压→校验三件套→前后端路由比对→纯净容器 npm build→无 DB env 跑通 SQLite CRUD；生成 Dockerfile 已改为保留 `server/` 层级，修复 `/data` EACCES。
- **业务 Eval**：`business_cases.yaml` 4 例（纯前端/CRUD/上传），真实 DeepSeek 契约 + 产物语义断言 4/4；上传契约自动注入 multer，构建前安装 `server/` 依赖。
- **P0 真实业务验收（2026-11-11）**：验收脚本新增真实 `multipart/form-data` 上传→静态回读→字节比对，以及宿主机 MySQL 临时库建表/CRUD/直连查行；业务 Eval `semantic.runtime` 复用同一链路，`biz_003` 100% 通过（101.9s / ¥0.0289）。实测修复：Dockerfile 二次 COPY 后 uploads/data `EACCES`、`resume_file_*` 字段漏映射、上传响应缺标准 `upload` 元数据、常量路由解析、表名语义匹配。
- **生产级武装（2026-11-11）**：产物强制 env 隔离（DATABASE_URL/CORS/PORT）、显式 SQL 迁移（`server/migrations/001_init.sql` + `_schema_migrations` + `migrate.mjs`，移除 `sync()`/`DB_AUTO_MIGRATE`）、真实 DB 健康检查（未迁移 503/`tableReady`）、前端全局 fetch 拦截器与错误 Banner、多阶段非 root Dockerfile；新增 `production_ready` Skill 挂载 Backend/Infra Agent，SQLite MCP 在 ConsistencyVerifier 真实执行 DDL 验表。验收第 8 步改为云端发布模拟：`.env` 注入→未迁移 503→独立 migrate→直连校验表/checksum→200+CRUD→重复迁移幂等。69 单测 + 9 脚本单测 + 端到端全绿，出口 `🎉 生产级产物验收通过，可直接交付云端部署`。

- **通用 AI 全栈平台五项升级（2026-11-11）**：`DecisionImpact`（CRITICAL/MAJOR/MINOR，旧值兼容）；Architect 澄清 10 轮 + 用户“一键采用建议”（AI 无权跳过）；`web_best_practices` Skill 强制挂载 BackendCoder 并生成 Bcrypt 安全模块；契约 `includeAiCapability` 驱动 OpenAI 真实调用 + AI 对话/上传 UI + `OPENAI_API_KEY`；导入支持平台全栈 tar.gz，自动双启 Express(3000)/Vite(5173)、补 `/api` 代理、重注入 `data-source`。306 单测全绿；真实导出→导入与 Playwright 圈选标记验证通过。
- **混合 RAG（2026-11-11）**：本地 `chromadb` PersistentClient + ONNX MiniLM（镜像预热、离线可用）；`HybridRAGProvider` 并行跑 BM25 与向量、RRF(k=60) 融合、单路故障降级；Architect/Planner 默认切到 `hybrid`，Planner 结果写入 `rag_context/rag_citations`。新增《企业内部安全开发规范与 JWT 鉴权指南》；演示两问 Top-3 精准命中 OAuth 2.1 / 15 分钟；316 单测全绿。

## 真假边界

DeepSeek=真实调用+Eval；鉴权=Mock Token；持久化=BFF 无 DB；中间件仅配置；沙盒无 gVisor；部署按钮仅指引；外网不可达。产物已按生产契约生成并本地模拟云端发布，但尚未实际部署 Render/阿里云，也没有回滚（down）迁移与云端密钥托管。

## 下一步

P0 剩余：生成产物多服务编排（web/api/db），迁移回滚脚本，导入包不再携带本地 SQLite；P1 业务 Eval 扩至 15–20 例、圈选精度、Redis checkpoint、RAG 中文 Embedding 评估；P2 真实云端部署按钮；P3 M3.2 知识中台。
