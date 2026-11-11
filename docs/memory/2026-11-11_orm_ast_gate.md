# 2026-11-11 · ORM 静态门禁升级为 Babel AST

> 前序：[[2026-11-11_orm_ddl_guard]] 用正则读 `server/db.js` 的
> `const TABLE` / `COLUMN_DEFS`。正则只看声明文本，属于“看起来通过、
> 运行期 500”的结构性漏判，本次彻底替换。

## 1. 正则的四个盲区

1. `COLUMN_DEFS.push(['name', 'TEXT'])`：声明合法，运行期追加 DDL
   不存在的字段，正则完全看不到；
2. `JSON.parse(process.env.COLUMNS || '[]')`：字段集合来自环境变量，
   静态不可知，正则要么漏判要么误判；
3. 字符串拼接 / 模板字符串 / 常量引用 / `Object.freeze`：同一份
   字段集合有多种等价写法，正则必须不断打补丁；
4. 语法错误：正则匹配不到时会静默返回空集合，校验“通过”。

## 2. 实现

新增 `ai_core/fullstack/schema_ast_check.mjs`：

- 使用 `@babel/parser`（复用 `sandbox_template/node_modules`，
  不额外引入 npm 依赖）解析 `server/db.js` / `server/index.js`；
- 静态求值支持：字面量、无插值模板字符串、数组（含 spread）、对象、
  一元正负号、`const` 引用、括号 / TS 包装、`+` 字符串拼接、
  `Object.freeze(...)`、`JSON.parse('静态字符串')`；
- 识别运行期变更：对 `TABLE` / `COLUMN_DEFS` 的赋值、自增、元素写入、
  `delete`，以及 `push/pop/shift/unshift/splice/sort/reverse/fill`
  等原地方法，一律标记为“静态字段不可信”；
- 解析 Sequelize `define(..., { tableName })` 的 `tableName` 与
  `attributes` 键集合，交叉校验模型表名 / 字段；
- 报告重复列、`COLUMN_DEFS` 结构错误、非静态 `COLUMN_META`、
  多余 meta 列、多个或错位的 `define` 表名；
- 输入 JSON（stdin）、输出 JSON（stdout）；退出码 0 表示分析完成，
  2 表示校验器自身不可用。

新增 `ai_core/fullstack/schema_ast.py`（仅标准库）：

- `analyze_js_files(files, paths=...) -> {path: JsFileSchema}`；
- Node / Babel 缺失、脚本异常、输出非法时抛 `SchemaAstUnavailable`，
  **禁止回退正则**；
- 错误格式化为“第 N 行第 M 列：…”，直接进入修复提示。

接入点：

- `coders.extract_orm_schema` / `check_orm_schema` / BackendCoder
  生成期表名断言全部改走 AST；
- `consistency._orm_schema_issues` 捕获 `SchemaAstUnavailable`，
  产出 `kind=ast_unavailable` / `fix_action=FIX_ENVIRONMENT`，
  不触发 DBACoder 自愈（环境问题重跑模型没有意义）；
- `scripts/acceptance_test.py` 的静态 ORM/DDL 比对与 MySQL 运行时
  字段比对同样改走同一 AST 脚本，主机直接用仓库内
  `sandbox_template` 的 Babel。

镜像：`services/orchestrator/Dockerfile` 增加 `nodejs`（Debian 包，
容器内 `node v20.19.2`），并保留 `sandbox_template/node_modules`。

## 3. 回归

- `tests/test_schema_ast.py`：静态提取、拼接 / `JSON.parse`、运行期
  `push`、动态字段、语法错误、`define`/`attributes`、表名与未知字段；
- `tests/test_consistency.py::test_runtime_column_mutation_blocks_build`：
  基线与真实生成产物通过，追加 `COLUMN_DEFS.push` 后必须阻断并
  进入 `REGENERATE_ORM`；
- `scripts/test_acceptance_runtime.py::OrmAstConsistencyTest`：验收侧
  同一组静态 / 运行期 / 动态用例；
- 容器内 `test_schema_ast + test_fullstack + test_consistency` 59 项全绿；
  全量 398 项仅剩既有 `eval_suite` 模块缺失；
- E2E `scripts/acceptance_test.py` 8/8 通过（`ACCEPTANCE_KEEP=1`）：
  阶段 5 输出“ORM 模型与迁移 DDL 字段一致（核心表 todo）”，
  阶段 7/8 SQLite 与真实 MySQL 的迁移、CRUD、直连查行、幂等均正常。

## 4. 边界

- 只处理模板化 / 可静态求值的 JS；若引入 `require` 外部模块或
  从数据库读取字段集合，会按“非静态”阻断，需要人工确认；
- `server/index.js` 仅要求可解析 + 表名一致，路由仍由
  `api-routes.json` 与既有路由提取负责；
- Node 是硬依赖：镜像缺少 `nodejs` 时生成会被 `ast_unavailable`
  阻断，这是刻意设计，避免静默降级。
