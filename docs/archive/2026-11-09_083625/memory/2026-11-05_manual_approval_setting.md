# 人工审批设置开关（2026-11-05）

## 目标

把「代码生成后是否需要人工审批」从固定后端契约提升为用户可见设置：

- 右上角头像菜单提供「账户」与「设置」；
- 设置弹窗内的开关默认关闭；
- 开关值持久化到 `localStorage`，并通过 React Context 全局同步；
- 每次 `/runs/stream` 在 `options.requireManualApproval` 中显式携带当前值；
- 关闭时构建验证通过直接 `SUCCEEDED`；开启时才暂停在 Diff 审批。

## 前端实现

- `src/settings/SettingsContext.jsx`
  - `SettingsProvider` 持有唯一状态；
  - 存储键 `vibe.settings.v1`，默认 `requireManualApproval: false`；
  - 初始化容错：localStorage 缺失、JSON 损坏或隐私模式均回落默认值。
- `src/components/SettingsDialog.jsx`
  - 使用 `role="switch"` / `aria-checked` 的无障碍开关；
  - 文案明确开启 = 暂停看逐文件 Diff，关闭 = 验证通过自动完成。
- `src/components/AppHeader.jsx`
  - 「账户设置」更名为「账户」；
  - 下方新增「设置」菜单项。
- `src/pages/WorkspacePage.jsx`
  - 单标注与批量请求都携带 `options.requireManualApproval`；
  - 新协议不再发送旧字段 `approvalMode`。

## 后端实现

- `RunOptions`
  - 新增 `require_manual_approval: bool | None`，支持 JSON 驼峰
    `requireManualApproval`；
  - `manual_approval_required` 优先读取新字段，未传时回退旧字段
    `approval_mode`，保证既有调用方兼容。
- `service.py`
  - 初始 graph state 写入 `approval_required`；
  - `AWAITING_APPROVAL`（审批通过）与 `SUCCEEDED`（免审批）统一执行
    Git checkpoint commit，成功后发出 `run.succeeded`。
- `graph.py`
  - evaluator 构建成功时按 `approval_required` 分支；
  - 开启：`next_action=approve` -> `approval_gate` -> `interrupt()`；
  - 关闭：`next_action=succeed` -> 新增 `succeeded` 节点 -> `END`，
    不经过审批节点；
  - 失败重试 / 重试耗尽的 `FAILED` 分支不受开关影响。
- 注意：LangGraph 0.2.x 的条件边不能直接指向 `END`，因此使用显式
  `succeeded` 终态节点承接免审批路径。

## 状态流

```text
构建成功
  ├─ requireManualApproval=false -> SUCCEEDED -> Git commit -> run.succeeded
  └─ requireManualApproval=true  -> approval_gate(interrupt)
                                      ├─ approve -> AWAITING_APPROVAL -> SUCCEEDED
                                      └─ reject  -> REJECTED

构建失败
  ├─ retry_count < maxRetries -> REPAIRING -> coder
  └─ retry 耗尽 -> FAILED（与审批开关无关）
```

## 验证

- `services/orchestrator/tests/test_run_options.py`
  - 默认关闭、新字段 true/false、新字段覆盖旧字段；
  - 旧 `approvalMode` 兼容；
  - evaluator 审批 / 免审批分支；
  - 失败重试与耗尽失败不受开关影响；
  - 全量 25 项单测通过。
- `scripts/settings_manual_approval_e2e_test.py`
  - 菜单包含「账户」「设置」；
  - 默认关闭且 localStorage 为 false；
  - 关闭时请求携带 `requireManualApproval=false`、无 `approvalMode`，
    构建后直接「已完成」，无审批暂停；
  - 打开后 localStorage 同步为 true，请求携带 true，出现审批面板；
  - 批准后正常「已完成」。
- 回归：`approval_ui_e2e_test.py`、`approval_cancel_e2e_test.py`、
  `add_component_e2e_test.py`、`multi_annotation_e2e_test.py`、
  `multi_turn_chat_e2e_test.py`、`ui_e2e_test.py`、
  `agent_e2e_test.py`、`platform_e2e_test.py` 全部通过；
  `npm run build` 通过。

## 已知边界

1. 当前开关是浏览器本地设置，并随请求下传；生产环境若要强制合规，
   应在租户 / 项目策略层增加服务端兜底，不能只信任客户端。
2. 开关对「新发起的 Run」生效；已暂停在审批中的 Run 仍按暂停时状态处理。
3. 旧客户端仍可发送 `approvalMode=required`，服务端保持兼容。
