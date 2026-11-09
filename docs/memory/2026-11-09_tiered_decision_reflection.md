# 2026-11-09 · 分级决策机制（Tiered Decision）

## 目标

让 Agent 区分「核心业务拍板」与「实现细节」，减少无意义打断：

- `CORE_BUSINESS`：架构变更、API/数据表契约、破坏性操作、业务分支选择；
- `IMPLEMENTATION_DETAIL`：颜色/间距/圆角、变量命名、日志、JSX 结构、登录框 UI。

只有前者允许 `interrupt()`；后者由 Agent 按最佳实践自主生成，用户通过沙盒预览验收。

## 状态机

```text
summarize_history → research → planner → clarify_gate → coder → applier
→ verifier → evaluator → reflection → approval_gate / succeeded
```

- `planner` 输出 `decision_impact`；模型缺省时用确定性关键词兜底。
- `clarify_gate` 双保险：非 `CORE_BUSINESS` 即使 `plan_needs_clarification=true` 也直接放行。
- `reflection` 只做确定性偏好证据提取，不调用模型。
- 失败/拒绝/取消不进 reflection 落盘路径。

## API 契约

`clarification.required` / `Clarification` 新增：

- `decisionImpact`
- `decisionSummary`
- `clarificationOptions[]`：`optionId/label/description/recommended/questionId/value`

前端优先渲染一键决策按钮；缺失时回退 `questions[].options`。点击推荐项且其余必填项已满足时自动提交。

## Reflection 偏好账本

`ProjectMemory` 新增 `preferences.json`：

```json
{"primary_color":{"values":{"purple":{"count":2,"lastSeen":"...","label":"紫色"}}}}
```

- `AGENT_PREFERENCE_MIN_EVIDENCE` 默认 2；
- 同一 key 只选择达到阈值且票数最高（同票取最近）的值；
- 稳定偏好以只读块注入 Planner / Coder Prompt；
- 只在本轮 Run `SUCCEEDED`（含审批通过）后落盘。

## 验证

- `tests/test_tiered_decision.py`：16 项通过（分类/中断策略/Mock Planner/一键选项/偏好账本）。
- `tests/test_run_options.py`：成功路由改为先 reflection 再审批/成功。
- `scripts/tiered_decision_e2e_test.py`：详情需求不打断；核心需求出现 3 个一键选项，点击推荐项自动恢复并完成。
- `scripts/clarification_ui_e2e_test.py`：主输入栏保留、自定义输入、一键选项断言均通过。
- `scripts/ui_e2e_test.py`：全流程回归通过。
- 前端 `npm run build` 通过。
