# 2026-11-16 Direct Ops 最终闭环

## DELETE_NODE 真实执行

- 前置：`analyze-delete` 静态判定，`safe=false` 时前端只展示原因，
  不提供确认按钮。
- 执行：
  - `baseRevision` 再次校验；
  - Babel `delete` 做 AST 同构校验；
  - 新增 `nodeCountBefore / nodeCountAfter / targetNodeCount`，
    要求 `before - after == targetNodeCount` 且 ≥1；
  - 写盘 → Git commit → ChangeLog。
- 前端 ❌：分析 safe 后出现“确认删除 / 取消”，确认才发真实动作。
- ChangeLog DELETE 摘要使用节点源码预览，避免把“组件 App”误写成
  被删除对象：`删除 App 内的节点 <p data-safe="note">说明</p>`。

## 终极 E2E 冒烟（真实沙盒，全程无 LLM）

| 操作 | HTTP 总耗时 | 内部 latencyMs | 结果 |
|---|---:|---:|---|
| ✏️ UPDATE_TEXT | 1612ms | 1604ms | 成功 |
| ➕ ADD_COMPONENT(Button) | 1526ms | 1523ms | 成功 |
| ❌ DELETE_NODE(<p>) | 1606ms | 1603ms | 成功 |
| ❌ analyze-delete | — | 190ms | safe=true |

- 三次操作分别生成 commit：
  `dact_text` / `dact_add` / `dact_delete`；
- 最终页面：标题“我的任务”，Button 已插入，说明段落已删除；
- ChangeLog 三条全部落盘 `data/changelog.jsonl`；
- Architect 快照 `directChangelog` 返回 3 条；
- 真实 DeepSeek Helper 准确复述三步操作及当前页面状态。

## 删除拦截矩阵（静态分析）

| 节点类型 | 判定 | 结果 |
|---|---|---|
| 普通独立节点 | safe=true | 允许删除 |
| 页面顶层根节点 | isRouteRoot | 拦截 |
| map 回调内节点 | inMap | 拦截 |
| 公共 / 多实例组件 | isShared | 拦截 |

真实 E2E 样本：safe 叶子 1 个允许，根节点 1 个拦截；
风险矩阵单测中 3 类危险节点全部拦截。

## 验证

- Python 594 项 unittest 全绿；
- 前端 16 项 Vitest 全绿，构建通过；
- BFF 单测通过；
- 真实沙盒三类写操作均低于约定阈值：
  UPDATE_TEXT < 2s、ADD < 2s、DELETE < 2.5s。

## 收尾

Direct Ops 四项动作（✏️ / ↕ / ➕ / ❌）全部闭环，且全部绕过 LLM、
带项目写锁、Revision 校验、节点守恒校验、Git commit 与 ChangeLog。
