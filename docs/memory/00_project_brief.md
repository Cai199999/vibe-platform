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

## 2. 核心技术栈

| 层 | 选型 |
|---|---|
| 前端 | React 18 + JavaScript/JSX + Vite + 原生 CSS；画布壳 platform-web |
| BFF/业务 | Java 21 + Spring Boot 3.3 + MyBatis-Plus + Flyway；用户、项目、配额、审批、长连接 |
| AI 编排 | Python 3.12 + FastAPI + LangGraph + Redis checkpoint；Supervisor/多 Agent |
| 沙盒 | Python Sandbox Service + Docker（M1）/ gVisor（M2）；多服务 + edge 单入口 |
| 数据 | MySQL 8（业务）、Redis（事件/checkpoint/租约）、MinIO（源码/快照/产物） |
| 模型 | DeepSeek + Mock；ModelRouter 预留多 provider |
| 协议 | OpenAPI/JSON Schema 作为唯一事实源，生成 TS/Java/Python 类型 |
| 兼容 | `apps/legacy` 冻结为 Prompt/视觉兜底，不参与新平台主链路 |

## 3. 预期核心模块

- M01–M06：认证、租户、项目、源码快照、预览会话
- M07–M10：画布工作台、标注采集、Task Contract、React Source Bridge
- M11–M17：Run 生命周期、Planner、Context/RAG、Coder、Verifier、HITL、Memory
- M18–M22：沙盒生命周期、Workspace Daemon、Preview Proxy、快照、资源治理
- M23–M26：Model Router、Prompt/Context Packer、Policy/Guard、RAG（M2）
- M27–M31：配额、审计、管理后台、legacy 兼容
- M32–M37（M2 实现、M1 契约预留）：Supervisor、FrontendCoder、BackendCoder、DBA、Reviewer/IntegrationVerifier、多服务沙盒编排

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

## 5. 当前文档状态

- `docs/design/01.功能模块梳理.md`：v1.1，已加入 M32–M37、澄清状态、全栈流程与 M1 预留验收。
- `docs/design/02.整体架构设计.md`：v1.1，已加入多 Agent 拓扑、全栈多服务沙盒、澄清状态机、全栈数据表与演进路线。
- `docs/design/03.API接口设计.md`：v1.1，已加入 `appType`、`services[]`、澄清接口、全栈标注契约、迁移/验证/事件/错误码。
- 本阶段只做设计，不写业务代码；实现按 M1 前端闭环优先推进。

## 6. 下一步

1. 按 M1 设计基线拆分实现任务：协议包 → BFF → AI Core → Sandbox → platform-web。
2. 优先固化 `packages/protocol` 与契约测试，确保 M2 全栈扩展不产生破坏性变更。
3. M1 完成后以同一套契约启用 M2：多 Agent、`AWAITING_CLARIFICATION`、web/api/db/edge 多服务沙盒。
