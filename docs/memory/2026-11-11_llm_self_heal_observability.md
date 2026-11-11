# 2026-11-11 · LLM 自愈与监控闭环

## 背景

确定性模板在同一输入下必然产出同一错误，导致 `CODE_DEFECT` 重试没有真实收益。
本轮把“拦截诊断”变成 LLM 的修复输入，同时把 Applier / 契约审计指标暴露到
Admin 看板，并强制 Architect 显式声明子资源契约。

## 实现

### 1. 带 Feedback 的 LLM BackendCoder

- 新增 `ai_core/fullstack/llm_backend.py`。
- 默认仍走 `generate_backend_bundle`；只有 Graph 状态存在 `applier_feedback`
  时才调用 LLM。反馈来源：
  - Applier AST 门禁拦截且 `audit_contract` 判为 `CODE_DEFECT`；
  - ConsistencyVerifier 报 `REPAIR_BACKEND_JOIN`。
- Prompt 包含：核心表、契约路由、显式 relations、databaseDdl、完整
  `schema.core.json`、Applier 诊断、当前完整 `server/index.js`。
- LLM 只能输出 `server/index.js`；越权路径、空内容、超过 512KB、非 Express
  文件一律拒收；合并后仍由 Applier 重新 AST 门禁。
- LLM 超时 / 关闭 / 输出非法时回退模板，不把环境问题伪装成修复成功。
- 开关：`FULLSTACK_LLM_REPAIR_ENABLED`（默认 true，关闭后退化为纯模板）。
- 事件：`fullstack.applier.llm_repair`（SUCCEEDED / SKIPPED / FAILED）。
- 状态新增 `applier_feedback_attempt` / `llm_repair_count`。

### 2. 监控闭环（指标前端化）

- Python：`GET /api/v1/internal/fullstack/metrics`（已有）。
- Java BFF：新增 `AdminFullstackMetricsService` + `AdminFullstackMetricsController`，
  `GET /api/v1/admin/fullstack/metrics` 透传并转 camelCase。
- 上游失败抛 `FullstackMetricsUnavailableException` → `AdminExceptionHandler`
  返回 502 `AI_CORE_METRICS_UNAVAILABLE`，禁止用全零数据冒充健康。
- 前端：`fetchAdminFullstackMetrics` 与主看板并行拉取；失败只降级健康卡片。
- Admin 新增 “系统健康与缺陷拦截” 卡片：
  - Applier 拦截率进度条；
  - 契约缺陷误重试计数（目标 0）；
  - 多表 AST 通过率；
  - JOIN / 其他 ORM 拦截明细。

### 3. 子资源契约强制声明

Architect `draft_contract` 与 `revise_contract` system prompt 同时加入：
嵌套子资源路由必须显式创建子表并在 relations 声明父表、子表、外键；
没有独立子表就改为父资源详情查询，禁止隐式推断。

## 验证

- Python：440 项通过，仅排除历史 `tests.test_eval_suite`。
- 新增：
  - `tests/test_llm_backend.py` 6 项（Prompt、权限、空文件、开关）；
  - Graph E2E：模板两次错误 → LLM 修复 → Applier 复检通过；
  - Graph 降级：LLM 失败 → 模板回退 → 门禁连续拦截并报错；
  - Consistency JOIN → `applier_feedback` → LLM；
  - Architect 起草 / 修订 Prompt 子资源断言。
- Java BFF 9 项测试通过；新增 3 项健康指标控制器测试。
- 前端 `vite build` 通过。
- live：重建 python-ai / java-bff 后，
  `GET /api/v1/admin/fullstack/metrics` 管理员 200，匿名 403。
