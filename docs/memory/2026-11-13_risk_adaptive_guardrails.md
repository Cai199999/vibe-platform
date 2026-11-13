# 2026-11-13 · 风险自适应门禁（Risk-Adaptive Guardrails）

## 目标

低要求项目不再承受全量门禁成本：用户在契约确认前必须选择质量分级，
后端按分级确定性地开关 AST、ORM/DDL 比对、SQLite 验收、真实 MySQL
迁移验收与阻塞策略；前端在结果面板展示对应风险徽标。

## 分级语义

| 分级 | AST 门禁 | ORM/DDL 强比对 | SQLite 验收 | 真实 MySQL 验收 | 失败阻塞 | 构建重试 |
| --- | --- | --- | --- | --- | --- | --- |
| L1_PROTOTYPE | ✗ | ✗ | ✗ | ✗ | 否（降级告警） | 1 次 |
| L2_STANDARD | ✓ | ✓ | ✓ | ✗ | 是 | 0 |
| L3_PRODUCTION | ✓ | ✓ | ✓ | ✓（临时库 + information_schema） | 是 | 0 |

## 实现要点

- 契约新增 `quality_tier`（默认 L2，兼容旧调用）；`contract_gate` 的
  interrupt 载荷新增 `qualityTierRequired / qualityTierPrompt /
  qualityTierOptions`，前端单选前确认按钮禁用。
- `fullstack/quality.py` 提供只读 `QualityPolicy`，各节点不再散落
  `if tier == ...`；`warning_badge()` 统一 L1/L3 文案。
- `Applier` 在 L1 跳过 Babel AST；`ConsistencyVerifier` 在 L1 跳过
  ORM 比对与 SQLite MCP，并把契约审计/一致性问题降级为 warnings。
- `BuildVerifier` 按 `build_retry_limit` 重试；L1 最终失败只告警，
  `SandboxRestart` 失败同样不阻塞。
- L3 新增 `fullstack/mysql_schema.py`：创建 `vibe_l3_verify_*` 临时库，
  执行 `server/migrations/001_init.sql`，查询 information_schema 比对
  字段，最后在 `finally` 删除临时库；连接/驱动错误标记为环境错误，
  Schema 缺陷路由回 DBACoder。
- 事件与快照新增 `qualityTier / qualityLabel / qualityBadge /
  warnings / mysqlCheck`；前端展示黄色 L1 徽标、绿色 L3 徽标与
  MySQL 验收结果。空 `mysqlCheck` 回传 `null`，避免 `{}` 被误判失败。

## 环境与配置

- `MYSQL_HOST`（容器内默认 `host.docker.internal`）、`MYSQL_PORT`、
  `MYSQL_DATABASE`、`MYSQL_USER`、`MYSQL_PASSWORD`。
- `QUALITY_MYSQL_VERIFY_ENABLED`（默认 true）、
  `QUALITY_MYSQL_TIMEOUT_SECONDS`（默认 8）。
- 新增依赖 `PyMySQL==1.1.1`。

## 验证

- Python：447 项通过（排除历史 `test_eval_suite` 路径导入问题）。
- 前端：`npm run build` 通过。
- 浏览器 E2E `scripts/quality_tier_ui_e2e_test.py`：
  - 未选分级时确认按钮禁用；
  - L1 生成成功 + 黄色徽标 + 无 MySQL 验收；
  - L2 生成成功 + 无徽标 + 无 MySQL 验收；
  - L3 生成成功 + 绿色徽标 + `data-quality-mysql="passed"`。
- 截图：`docs/screenshots/quality-tier-l{1,2,3}-*.png`。
