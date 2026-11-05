# 00 项目简报：vibe-platform

## 1. 项目名称与目标

- **项目名称**：vibe-platform
- **产品定位**：以前端可视化为抓手、以多轮对话为意图输入、以 LangGraph 多 Agent 为编排内核、以云端全栈沙盒为可信执行的下一代 Vibe Coding 平台。
- **终极目标**：生成完整可运行的全栈应用——React 前端 + Spring Boot 后端 + MySQL + API 契约 + 迁移脚本，并在同一沙盒内启动、联调、验证、预览。
- **M1 边界**：只实现 React/Vite 纯前端闭环（单 Agent、单服务沙盒）；但数据库表、Run 状态机、Task Contract、Sandbox spec、事件与 API 契约已为全栈生成完全预留。
- **M2 目标**：启用 Supervisor + Planner/FrontendCoder/BackendCoder/DBA/Reviewer 多 Agent、`AWAITING_CLARIFICATION` 多轮澄清、多服务沙盒与数据库迁移验证。
- **M3-A 已落地（2026-11-05）**：SSE 时间轴、HITL 澄清、Diff 审批闸门与协作式取消 Run；
  细节见 `docs/memory/2026-11-04_M2_SSE_HITL.md` 与 `docs/memory/2026-11-05_M3A_diff_approval_cancel.md`。
- **M3-B 已落地（2026-11-05）**：`CheckpointerProvider`、`CodeEditor` / `AnchorCodeEditor` /
  `FileCreator` / `TreeSitterEditor` 占位与 `EditorRegistry`；Mock 与 Coder files 模式已接入。
  细节见 `docs/memory/2026-11-05_M3B_code_editor_abstraction.md`。
- **M3-C 已落地（2026-11-05）**：右上角「设置」弹窗提供「代码生成后需人工审批」开关，
  默认关闭并持久化到 localStorage；请求携带 `options.requireManualApproval`，
  关闭时验证通过直接成功，开启时才进入 Diff 审批。
  细节见 `docs/memory/2026-11-05_manual_approval_setting.md`。
- **M2.5 已落地（2026-11-05）**：独立 Architect Graph，先判别 `lightweight_web` /
  `script_automation` / `full_stack_system` 应用形态，再按形态动态追问与分级生成
  《全栈实施契约》；RAG 使用 Markdown + BM25；支持 `CONFIRM / REVISE / CANCEL`。
  细节见 `docs/memory/2026-11-05_M2_5_app_shape_contract.md`。
- **M2.5 Supervisor 已落地（2026-11-05）**：新增只做意图路由的 Supervisor Graph，
  通过 AgentRegistry 把请求分发到 UI Graph / Architect Graph / both；预留
  BackendCoder、ScriptAgent 与企业 CustomAgent Manifest 扩展槽。
  细节见 `docs/memory/2026-11-05_M2_5_supervisor_multi_agent.md`。
- **M2.5 Supervisor 前端接入 + both 强门控已落地（2026-11-05）**：画布“发送 /
  提交”和工具栏“下一步”统一走 `route → dispatch`，按 `handoff` 动态切换 UI；
  `handoff_both` 先只启动 Architect，契约确认后 `release` 才启动 UI，项目锁、
  幂等释放与乱序拒绝全部落到代码；浏览器级混合 E2E 已通过。
  细节见 `docs/memory/2026-11-05_M2_5_supervisor_frontend_gate.md`。
- **M2.5 声明式 Manifest + Skill 预留已落地（2026-11-05）**：内置 Agent 改由
  `ai_core/*/manifest.json` 启动扫描注册，`AgentRegistry` 不再硬编码；新增
  `SkillRegistry` 抽象与 Agent / Tool / Skill 三层文档；明确当前仅线程级短期记忆、
  长期记忆与 MCP 留待 M3，架构图以虚线框预留 `MCP Client Adapter`。
  细节见 `docs/memory/2026-11-05_M2_5_agent_manifest_skill.md`。
- **M2.5 终局体验打磨已落地（2026-11-05）**：前端新增 Supervisor 决策可视化
  （分析 → 判定 → both 双阶段时间轴）与「我的专属 Agent」目录；Demo CustomAgent
  `excel_report_agent` 通过 Manifest 关键词秒级接入；README 增加 Mermaid 架构图，
  并新增《3 分钟演示脚本》。
  细节见 `docs/memory/2026-11-05_M2_5_demo_visualization_custom_agent.md`。
- **M3 数据中台 / 管理中台契约（2026-11-05，设计完成）**：新增独立
  `Knowledge Service`（摄取 / 切分 / Embedding / 租户隔离检索）、Admin API 与
  Prompt Registry（版本 / 灰度 / 热更新 / 回滚）、Feedback Collector 与基于
  Architect `REVISE` 的 Prompt 自优化闭环；**明确不做大模型微调**。
  本轮只交付架构、数据模型与接口契约，底层实现按 M3.1–M3.4 推进。
  细节见 `docs/design/06.Agent_OS_Data_Management.md` 与
  `docs/memory/2026-11-05_Agent_OS_data_management_architecture.md`。
- **M3.1 管理中台入口与指标看板已落地（2026-11-05）**：BFF 新增 `@IsAdmin`
  Mock RBAC 切面（`X-User-Role`）与 `GET /api/v1/admin/metrics`，内存聚合活跃
  沙盒、今日 Run、成功率、平均耗时、Agent 占比与最近 10 次执行；前端新增
  `/admin` 路由、管理员菜单入口、KPI 卡 + Agent 占比图 + 执行表，并由路由守卫
  对非管理员整页 403。指标尚未持久化，`ADMIN_METRICS_DEMO_SEED=true` 可注入
  演示数据。细节见 `docs/memory/2026-11-05_M3_1_admin_console_metrics.md`。
- **Eval Suite v0.1 已落地（2026-11-05）**：`config/eval_suite/core_cases.yaml`
  提供 10 条 YAML 用例；`services/orchestrator/eval_suite/` 用真实 Supervisor
  路由 + `MockAgentLLM` 做离线断言，输出 console / JSON / Markdown / JUnit；
  `./scripts/eval_suite.sh run` 复用 python-ai 镜像一次性容器执行。
  细节见 `docs/design/07.Eval_Suite.md` 与
  `docs/memory/2026-11-05_eval_suite.md`。
- **Eval Suite v0.2 已落地（2026-11-05）**：新增 `scripts/run_eval.py`，
  逐条调用真实 Supervisor 路由；代码用例通过 UI 子图启动 Docker 沙盒，
  构建预览后从沙盒导出包读取 `src/App.jsx` 做正则断言；终端与
  `docs/eval_report.md` 输出 10/10 PASS、总耗时与沙盒清理统计。
  每条用例使用独立 `projectId + threadId` 防历史快照污染。细节见
  `docs/memory/2026-11-05_eval_real_sandbox.md`。
- **Admin Eval 契约预留（2026-11-05，设计）**：`POST /api/v1/admin/eval/run`
  异步一键评测、评测详情与历史得分曲线；明确 Eval 与 Knowledge 解耦：
  评测集是“考题”，知识库是“教材”。细节见
  `docs/design/03.API接口设计.md` §14.4.2、`docs/design/06` §5.5、
  `docs/design/07` §1.3 / §9。

## 2. 核心技术栈

| 层 | 选型 |
|---|---|
| 前端 | React 18 + JavaScript/JSX + Vite + 原生 CSS；画布壳 platform-web |
| BFF/业务 | Java 21 + Spring Boot 3.3 + MyBatis-Plus + Flyway；用户、项目、配额、审批、长连接 |
| AI 编排 | Python 3.12 + FastAPI + LangGraph + Redis checkpoint；Supervisor/多 Agent |
| 沙盒 | Python Sandbox Service + Docker（M1）/ gVisor（M2）；多服务 + edge 单入口 |
| 数据 | MySQL 8（业务/知识元数据/Prompt/反馈）、Redis（事件/checkpoint/租约/缓存）、MinIO（源码/快照/产物/知识文档）、Qdrant（M3 向量） |
| 数据中台 | Knowledge Service（Python/FastAPI）；bge-m3 + bge-reranker；按租户 collection |
| 管理中台 | Java BFF：Admin API、Agent 指标、Prompt Registry、Feedback / 审批、审计 |
| 模型 | DeepSeek + Mock；ModelRouter 预留多 provider；**不做微调** |
| 协议 | OpenAPI/JSON Schema 作为唯一事实源，生成 TS/Java/Python 类型 |
| 兼容 | `apps/legacy` 冻结为 Prompt/视觉兜底，不参与新平台主链路 |

## 3. 预期核心模块

- M01–M06：认证、租户、项目、源码快照、预览会话
- M07–M10：画布工作台、标注采集、Task Contract、React Source Bridge
- M11–M17：Run 生命周期、Planner、Context/RAG、Coder、Verifier、HITL、Memory
- M18–M22：沙盒生命周期、Workspace Daemon、Preview Proxy、快照、资源治理
- M23–M26：Model Router、Prompt/Context Packer、Policy/Guard、RAG（M2）
- M27–M31：配额、审计、管理后台、legacy 兼容
- M32–M37：Supervisor 路由 / AgentRegistry / CustomAgent Manifest 已落地（M2.5）；
  FrontendCoder、BackendCoder、DBA、Reviewer/IntegrationVerifier、多服务沙盒编排仍按 M2 推进
- M38–M41（M3）：Knowledge Service（数据中台）、Feedback Collector、
  Prompt Registry / 自优化、Admin Console；M3.1 已完成 Admin Console 入口与
  指标看板（含 Mock RBAC），其余仍为契约
- M42（M3 质量基建）：Eval Suite 已完成 v0.2（离线引擎 + 真实沙盒引擎、
  10 条核心用例、路由 / 代码断言、终端与 Markdown 报告）；DeepSeek 评测与
  Admin 一键评测 / 历史曲线仍待扩展

## 4. 关键架构约束

1. 禁止物理外键；所有业务表带 `tenant_id`，关联一致性由业务代码、唯一索引与对账保证。
2. Java 是业务系统 of record；Python 是 Agent 执行状态 of record；通过事件、`seq` 与幂等最终一致。
3. 沙盒是唯一写入口；代码写入默认在构建验证通过后自动提交 checkpoint，
   用户可在「设置」中开启人工 Diff 审批，开启后必须批准才提交。
4. 自动修复最多 3 次；同一错误指纹重复/无进展/预算耗尽即停止。
5. 全栈沙盒数据库为临时实例：随机口令、无宿主端口、不连接生产库；迁移 up/down 成对验证。
6. 多 Agent 只有 Supervisor 写共享 State；契约先行（ApiContract + DataModel），子 Agent 返回结构化 `AgentResult`。
7. 局部 Run 澄清最多 3 轮；M2.5 Architect 最多 5 轮，耗尽后返回 `CONFIDENCE_EXHAUSTED`；不得臆造业务模型。
8. M2.5 以应用形态为第一判别项：轻量工具不追问后端 / 数据库，脚本不追问 UI，全栈才要求 API、DDL 与数据实体。
9. 基础设施复用 `~/Workspace/infra` 的 MySQL/Redis/MinIO；平台 Compose 不重复部署中间件。
10. 前端不得硬编码子图启动接口：所有“发送 / 提交 / 下一步”先走
    `POST /api/v1/supervisor/route` + `/dispatch`，再按 `decision.handoff`
    attach 到预启动 Run；`rejected` 必须提示先圈选组件。
11. `handoff_both` 必须先契约后 UI：契约未 `CONFIRMED` 时任何路径都不得启动
    UI Run；项目级 gate 保证单项目单门控、release 幂等、乱序 409。
12. 用户上传的 CustomAgent 未来必须在独立受限沙盒 / 临时 venv 中执行，
    其 Prompt、代码生成与脚本不得进入主进程或共享依赖环境。
13. Agent 能力以 `manifest.json` 为唯一声明源；新增 Agent 只加目录与 Manifest，
    不改 Supervisor / Registry 代码；`reserved` 能力不可路由。
14. 当前只有 LangGraph 线程级短期记忆；不引入跨会话长期记忆与用户画像，
    防止一次性意图污染后续上下文（长期记忆规划在 M3）。
15. MCP 仅以虚线框预留 `MCP Client Adapter`，M2.5 不引入依赖、不建立连接；
    Skill 当前只声明不执行，M3 再实现 Prompt + RAG + Tool 子集动态拼装。
16. 知识检索必须带 `tenantId`；内部 API 缺 `X-Tenant-Id` 直接失败，跨租户资源返回 404。
17. 生产 RAG 走 `KnowledgeRAGProvider`；本地 Markdown 仅 `APP_ENV=local` 兜底，
    检索内容视为不可信引用，不得覆盖系统指令。
18. Prompt 是不可变版本 + 可变指针；发布 / 灰度 / 回滚 / 热更新必须审计，
    Run 创建时 pin `promptVersionId`。
19. Agent 自进化只允许改 Prompt / 检索参数 / 工具策略；不做 SFT / LoRA / RLHF；
    LLM 只提议，人工审批后灰度，默认关闭。

## 5. 当前文档状态

- `docs/design/01.功能模块梳理.md`：v1.1，已加入 M32–M41、澄清状态、全栈流程、中台流程与验收。
- `docs/design/02.整体架构设计.md`：v1.2，已加入多 Agent 拓扑、全栈多服务沙盒、知识中台、管理中台、全栈数据表与演进路线。
- `docs/design/03.API接口设计.md`：v1.2，已加入 `appType`、`services[]`、澄清接口、全栈标注契约、迁移/验证/事件/错误码，以及 §14 中台端点索引。
- `docs/design/06.Agent_OS_Data_Management.md`：v1.0，Knowledge Service、Admin、Prompt Registry、Feedback 自优化契约（M3 设计稿）。
- `docs/design/04.M2_Agent_ToolCalling_SSE_HITL.md`：M2 执行层。
- `docs/design/05.Supervisor_Multi_Agent.md`：v1.2，Supervisor 总调度、声明式 Manifest 注册、
  Agent / Tool / Skill 三层、SkillRegistry、Memory / MCP 边界与 CustomAgent。
- 本阶段只做设计，不写业务代码；实现按 M1 前端闭环优先推进。

## 6. 下一步

1. 按 M1 设计基线拆分实现任务：协议包 → BFF → AI Core → Sandbox → platform-web。
2. 优先固化 `packages/protocol` 与契约测试，确保 M2 全栈扩展不产生破坏性变更。
3. M1 完成后以同一套契约启用 M2：多 Agent、`AWAITING_CLARIFICATION`、web/api/db/edge 多服务沙盒。
4. M3 中台按 M3.1 管理中台骨架 → M3.2 知识中台 → M3.3 Prompt Registry →
   M3.4 反馈与自优化推进；先做契约测试与租户隔离用例，再接真实向量库。
