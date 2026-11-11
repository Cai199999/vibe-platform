# 2026-11-11 · 主从表契约与关联生成

## 目标

把全栈生成从「单表 CRUD」推进到「主从表业务系统」：Users 1:N Resumes
这类真实结构必须由契约驱动，而不是让模型在代码里自由发挥。

## 契约层

- `ArchitectContract.relations` 支持显式声明：
  `parentTable / childTable / foreignKey / type(one_to_many)`。
- 未显式声明时，`normalize_table_relations` 从 `<parent>_id` 列推导，
  保持旧契约向后兼容。
- 关系审计：父表、子表、外键列必须同时存在于 DDL；缺一即报错，
  且明确指向 `childTable.foreignKey`。

## 生成层

- DBA：
  - 生成 `RELATED_MODELS`（子表 + 全部列）与 `RELATIONS` 常量；
  - SQLite 预览 schema 覆盖全部业务表；
  - 关联系统表（如 users）中的敏感列在下发给前端/子查询前剥离。
- Backend：
  - Sequelize 生成 `User.hasMany(Resume)` / `Resume.belongsTo(User)`；
  - 列表/详情按 `include` + `separate: true` 预加载，避免笛卡尔膨胀；
  - 自动补嵌套路由 `GET/POST /api/v1/users/:id/resumes`；
  - 导出清单 `express.relation` 记录关联，供验收与前端消费。
- 关联表的 `payloadKey` 使用自身展示列，绝不继承核心表 POST 字段。

## 门禁

- `schema_ast_check.mjs` 用 Babel 静态读取 `RELATED_MODELS` / `RELATIONS`，
  校验：
  - 表/列必须与 DDL 对齐；
  - 运行期 `push` 等变更直接失败；
  - 关系两端必须是已建模的表，外键必须存在于子表列；
  - 有 `RELATED_MODELS` 必须有 `RELATIONS`，禁止半截产物。
- `schema_ast.py` 暴露 `JsRelatedModel` / `JsRelation` / `table_columns`。
- 生成期、ConsistencyVerifier、`scripts/acceptance_test.py` 共用同一套断言。

## 验证

- 新增测试覆盖：关系推导、未知父表、未知列、运行期变更、缺
  `RELATIONS`、外键不在子表、敏感列剥离、嵌套路由生成。
- 全量 Python 单测 417 项通过（排除历史 `test_eval_suite`）。

## 边界

- 不做物理外键；关系只在 ORM 与路由层表达。
- 暂只支持一级 `one_to_many`，多对多与自关联留给后续版本。
