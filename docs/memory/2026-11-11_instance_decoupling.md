# 重复容器实例解耦（2026-11-11）

## 问题

用户在一个卡片 / 容器里新增按钮后，所有同类容器都出现了同一个按钮。
根因是共享 `map` 返回体：Coder 只在 JSX 里无条件插入节点，而没有写出
“只命中第 N 个实例”的守卫。

## 方案

1. **SDK 提取实例身份**：`inspect_sdk.js` 从最近的组件 fiber 读取
   `fiber.key` 与基本类型 props（如 `item.id` / `item.title`），随
   `source.instanceKey` / `source.instanceProps` 回传；无 callSite 时
   仍有 `instance` / `instanceCount` 兜底。
2. **全链路透传**：`ProjectWorkspace`、`WorkspacePage` 的 Inspect 草稿、
   批量布局、最终 Envelope，以及后端 `AnnotationSource` /
   `compact_annotations` 均保留这两个字段。
3. **Prompt 强约束**：Planner / Coder 被要求：多实例场景必须给出实例
   身份守卫，优先用 `instanceKey` / `instanceProps` 字面量做严格比较，
   没有身份信息时才允许 `index === instance-1`；禁止无条件写入 map。
4. **Applier 确定性守卫**：`_instance_scope_violation` 新增
   `INSTANCE_GUARD_MISSING`。当 `instanceCount > 1` 且补丁触及
   callSite（或根组件源码行）时，从 files 或 unified diff 的 `+` 行中
   检查是否出现带比较运算符的真实身份字面量 / index 守卫；缺失直接
   打回 Coder，不写沙盒。
5. **防误判**：守卫字面量必须出现在 `===` / `&&` / `?` / `includes`
   等条件表达式中；仅把 `item.title` 渲染成文案不算守卫。

## 关键文件

- `services/orchestrator/sandbox_template/inspect_sdk.js`
- `apps/platform-web/src/components/ProjectWorkspace.jsx`
- `apps/platform-web/src/pages/WorkspacePage.jsx`
- `services/orchestrator/ai_core/domain/agent.py`
- `services/orchestrator/ai_core/agent/ui_context.py`
- `services/orchestrator/ai_core/agent/llm.py`
- `services/orchestrator/ai_core/agent/graph.py`
- `services/orchestrator/tests/test_instance_scope.py`

## 验证

- `test_instance_scope` 14 项通过；相关后端套件 61 项通过；全量 382 项
  仅保留容器缺少 `eval_suite` 的历史导入错误。
- `npm run build` 通过；`node --check inspect_sdk.js` 通过。
- 沙盒镜像已重建，运行中的两个沙盒已注入新 SDK 并重启。
