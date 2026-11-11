# 2026-11-11 · ORM / DDL 一致性门禁（Unknown column 永久止血）

> 事故：真实 Docker 部署后，`GET/POST /api/v1/resumes` 返回
> `500 Unknown column 'name'`。健康检查却是 200，因为 ORM 指向
> 第一张 `users` 表；生产迁移创建的却是 `resumes`。

## 1. 根因

1. `extract_table_name` 只取第一条 `CREATE TABLE`，且用贪婪正则
   `\((.*)\)$` 收集字段，多表 DDL 会把 users 与 resumes 的字段混在一起；
2. 无 DDL / 解析失败时回退到硬编码的 `users` + `name/status`；
3. BackendCoder 不知道 DBACoder 最终选了哪张表，只按契约路由猜；
4. ConsistencyVerifier 只比对路由，不比对 ORM 字段与迁移列；
5. 健康检查的 `tableReady` 对 users 也成立，掩盖了业务表错位。

## 2. 三层门禁

### 生成期（DBACoder / BackendCoder）

- `parse_ddl_tables` 按括号配对逐表解析，字段/约束完全隔离；
- `core_table_for_contract` 按 API 集合资源选核心业务表：
  `/api/v1/resumes` → `resumes`，并排除 users / sessions / migrations
  等系统表；没有 DDL 时兜底 `app_records` + `id/title/created_at`，
  绝不使用 `users` 或 `name`；
- DBACoder 写出 `server/schema.core.json`（表、字段、列元数据、
  displayKey、payloadKey），作为机器可读的 DBA→Backend 契约；
- BackendCoder 必须读取该文件（回退迁移 SQL），表名不一致直接抛错；
- `_assert_generated_schema` 在写盘前再次断言 ORM / 迁移 / 契约同表同字段；
- 模板根据 NOT NULL / DEFAULT / UNIQUE 生成默认值，避免修好 `name`
  后又撞上 `Column 'content' cannot be null`。

### 构建期（ConsistencyVerifier）

- 新增 `_orm_schema_issues`：解析 `server/db.js` 的 `const TABLE`、
  `COLUMN_DEFS`，与 `server/migrations/001_init.sql` 中核心表字段对比；
- 发现问题产出 `kind=orm_schema` / `fix_action=REGENERATE_ORM`；
- 图新增 `"regenerate": "DBACoder"` 边，最多重写 2 次，之后才允许
  install / build / restart；修复事件标注 `reason=ORM_SCHEMA_MISMATCH`。

### 验收期（acceptance_test.py）

- 阶段 5 增加 `check_orm_ddl_consistency`：静态对比 db.js / index.js
  与迁移 DDL，禁止 `Unknown column` 产物进入构建；
- `verify_crud` 校验响应 `table` 等于核心业务表；
- 阶段 8 对比 ORM 字段与真实 MySQL 列，并明确断言：
  “核心业务表 … 的 GET/POST 未因字段缺失返回 500”。

## 3. 回归与验收

- `tests.test_fullstack` + `tests.test_consistency`：50 项全绿；
- 新增事故复现用例：多表 DDL（users + resumes + jds）、缺失 DDL 兜底、
  单表隔离、Backend 读取 DBA schema、拒绝漂移 schema、
  ORM 字段错位阻断、图自动回退 DBACoder；
- 全量 389 项仅剩既有的 `eval_suite` 模块缺失（容器历史问题）；
- E2E：`scripts/acceptance_test.py` 8/8 通过，SQLite 与真实 MySQL
  均完成迁移、CRUD、直连查行与幂等迁移，无 500。

关键验收输出：

```
✓ ORM 模型与迁移 DDL 字段一致（核心表 todo），不存在 Unknown column 风险
✓ 核心业务表 todo 的 GET/POST 未因字段缺失返回 500
✓ 生产迁移重复执行成功（exit=0），checksum 未变化
🎉 生产级产物验收通过，可直接交付云端部署
```

## 4. 边界

- ~~静态门禁识别模板化 Sequelize（`COLUMN_DEFS` / `attributes`）；
  若未来改为自由手写 ORM，需要升级为 AST 解析；~~
  **已升级**：正则已全部替换为 Babel AST，见 [[2026-11-11_orm_ast_gate]]；
- 本次 E2E 的 mock 契约是单表 todo；多表 users → resumes 事故由
  上述回归单测覆盖；
- 仍未验证真实云端部署、Redis / MinIO 与多服务编排。
