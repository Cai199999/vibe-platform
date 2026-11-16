# 2026-11-16 契约审计 kebab/snake 命名归一化

## 任务四确认

两次失败 Run：

- `arc_9ae85e1bf6e0`：`tailored-resumes` 子表
- `arc_f5e2b78a01fa`：`job-descriptions` 子表

检查结果：

- 两个 Run 的 `sourceHash` 都是
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`
  （空内容的 SHA-256），说明 Architect 启动时没有源码 / 沙盒；
- 当前宿主机没有任何 `vibe-sandbox-*` 容器；
- 因此不存在可查询的 `server/migrations/001_init.sql`。

DDL 只存在于失败 Run 的契约草稿中，而该草稿未在快照中暴露。无法从
沙盒文件确认表是否存在；但错误形态明确是路由资源与 DDL 表名比对失败，
且两个资源都使用 kebab-case、SQL 惯例为 snake_case，符合命名归一化
缺陷的典型特征。

## 修复

1. `contract_audit._singular()`：
   - 单数化之前先移除所有非字母数字字符，等价于把 `-` 归一化为 `_`；
   - `tailored-resumes` 与 `tailored_resumes` 都归一为 `tailoredresumes`，
     再单数化为 `tailoredresume`。
2. 嵌套路由 relation 推导：
   - `users` 等系统表默认不参与推导；
   - 当它作为嵌套路由父资源时，额外用 `derive_table_relations(
     core_table=parent)` 推导一次，避免修完表名后又误报缺 JOIN。
3. Prompt：
   - 契约起草 / 修订均新增“URL kebab-case ↔ DDL snake_case”规范；
   - 声明嵌套路由必须在 DDL 创建对应 snake_case 子表。
4. Repair Feedback：
   - `_repair_feedback()` 把审计 hint 与命名规范一起回灌模型；
   - 明确示例：`/tailored-resumes` ↔ `tailored_resumes`。
5. 审计 hint：
   - `nested_table_missing` 提示直接给出 snake_case 表名，而不是原样
     kebab-case 资源名。

## 验证

- 新增审计用例：
  - kebab 路由 + snake DDL → 通过；
  - DDL 真缺表 → 仍拦截，且 hint 包含 `job_descriptions`。
- 新增 Graph 用例：复现 kebab/snake 契约，修复后直接到达
  `contract_confirmation`，不再进入失败。
- `_repair_feedback` 单元测试确认包含 kebab-case / snake_case 提示。
- 全量 Python 598 项通过。
