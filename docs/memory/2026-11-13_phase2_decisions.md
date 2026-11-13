# Phase 2：多轮决策分流（Human-in-the-loop Decisions）

日期：2026-11-13

## 目标

把 Resolver 的“硬拒绝”（目标歧义 / 公共组件保护）转成用户在**安全路径**中做选择，
既不让系统直接失败，也绝不允许用户豁免写入门禁。

## 状态机

```
style_check -> resolver -> decision_gate -> resolver -> coder -> applier ...
```

- `resolver`：每次只产出最多一个 `pending_decision`；
- `decision_gate`：单节点单次 `interrupt()`，避免一次执行多次中断导致的恢复歧义；
- 用户选择写回 state 后回到 `resolver` 重新生成 `TargetResolution`；
- 最多 3 轮（`MAX_DECISION_ROUNDS`）；超限 `FAILED / DECISION_LIMIT`，
  文案“无法确定安全路径，操作已取消”。

## 领域契约

- 状态：`AWAITING_DECISION`；
- SSE：`decision.required`（挂起）/ `decision.answered`（恢复）；
- `PendingDecision`：decisionId / kind / round / maxRounds / target / options / expiresAt；
- 安全动作白名单：`select_target`、`modify_call_site`、`cancel`；
- 黑名单（服务端拒绝）：`force_modify_shared`、`override_protection`、
  `modify_component_definition`、`force`、`bypass`。

## 两类决策

1. **target_ambiguous**：候选节点列表 + 取消；用户选中的 range 由服务端
   从候选中复制到 `decision_selections`，Resover 用 `selectedRange` 精确命中。
2. **protected_component**：`resolve_target.mjs` 预先计算 `callSiteAnchor`；
   用户选择“只在当前实例修改”后写 `decision_overrides[anchorId]`，第二轮
   解析用 callSite 文件/范围覆盖共享组件定义，`protected=false`，
   `allowedFiles=[callSiteFile]`，AST 工具门禁自然通过。

## 接口

- Python：`POST /api/v1/internal/runs/{runId}/decisions/{decisionId}`（202）、
  `GET /api/v1/internal/runs/{runId}/decisions`；
- BFF：`POST/GET /api/v1/runs/{runId}/decisions...`；
- 恢复值只携带 `{decisionId, optionId}`，`decision_gate` 按 optionId 找回
  服务端生成的选项，前端不能自造动作 / 区间 / 文件。

## 取消与清理

`cancel` 选项不恢复 Graph：Service 直接 `_complete_cancelled()` 回滚沙盒、
丢弃草稿并发送 `run.cancelled`。普通 Run 取消也覆盖 `AWAITING_DECISION`。

## 验证

- `tests/test_phase2_decisions.py` 17 项：纯函数、节点、Service、API、
  真实 LangGraph `invoke` / `Command(resume)` E2E；
- 全量 520 项通过；`black` 完成；BFF `mvn package`、Vite build 通过。

## 已知边界

- “抽取新组件”暂未作为选项：需要多文件代码生成与新建组件流程，当前只提供
  可执行的 callSite 实例级修改与取消；
- 决策 TTL 默认 24h，过期后 Run 标记 `EXPIRED`，需重新发起修改。
