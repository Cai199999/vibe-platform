# M3-A：Diff 审批与取消 Run（2026-11-05）

## 目标

在 M2 的 SSE + HITL 基础上补上「写代码之前最后一次人工闸门」：

- 构建通过后不立刻提交 Git checkpoint，而是暂停并返回**逐文件 unified diff**；
- 用户批准才 `git commit` 并发送 `run.succeeded`；
- 用户拒绝或取消则 `git reset --hard` 回本轮 Run 开始前的 HEAD；
- 执行中的 Run 支持协作式取消，在节点边界安全收敛。

## 状态机

```
... -> VERIFYING -> evaluator
                        ├─ approvalMode=off      -> AWAITING_APPROVAL（旧行为）
                        └─ approvalMode=required -> approval_gate(interrupt)
                                                      ├─ approve -> AWAITING_APPROVAL -> SUCCEEDED
                                                      └─ reject  -> REJECTED
暂停/执行中 --cancel--> CANCELLED
```

`AWAITING_APPROVAL` 是**暂停态而非终态**：不提交、不销毁沙盒、不写 thread 成功记忆。
真正的终态为 `SUCCEEDED / REJECTED / CANCELLED / FAILED / EXPIRED`。

## 核心实现

### Python AI Core

- `RunOptions.approval_mode`：`required | off`，默认 `off`，保证旧同步 / 回归脚本
  行为不变；产品前端显式传 `required`。
- `graph.py`
  - evaluator `approve` 分支按 `approval_required` 路由到 `approval_gate`；
  - `approval_gate` 先发 `node.finished(paused)`，再发 `approval.required`，
    最后 `interrupt(payload)`；恢复时返回 `approval_decision / approval_id`；
  - `_build_approval_payload()` 以 `initial_files`（本轮 Run 开始前的文件）为基线
    生成逐文件 diff，计算增删行、风险级别（LOW/MEDIUM/HIGH）、构建证据与
    `expiresAt`；单文件 diff 超过 20k 字符截断并标记 `truncated`；
  - 新增 `RunCancelledError`；`AgentGraphRunner` 接受 `cancel_check`，
    在每个节点执行前后检查，命中即发 `node.finished(cancelled)` 并抛出。
- `service.py`
  - `PreparedRun.base_commit`：`_prepare` 时读取 HEAD，作为拒绝 / 取消的回滚锚点；
  - `_complete_finished`：`AWAITING_APPROVAL` 才 commit；`REJECTED` 先回滚工作区，
    不生成 commit / thread 成功记忆；
  - `_complete_approval_pause`：只挂起，不 commit / 不销毁沙盒；
  - `_complete_cancelled`：回滚到 `base_commit`，发送 `run.cancelled`；
  - `decide_approval`：幂等重复决策；不同决策 409；过期 410；返回 202 后
    后台线程 `Command(resume=...)` 恢复；
  - `cancel_run`：暂停态立即回滚 + `run.cancelled`；执行中置 `CANCELLING` 并
    发送 `run.cancel_requested`，由节点安全点收敛；终态幂等返回；
  - 快照新增 `approval` 与 `cancelRequested`。
- `api/runs.py` 新增内部接口：
  - `POST /runs/{runId}/approvals/{approvalId}/decision` → 202
  - `GET  /runs/{runId}/approvals`
  - `POST /runs/{runId}/cancel`
- 修复：恢复 M2 提交中误删的 `RunExecutionService.rollback()`，
  `/api/v1/runs/rollback` 与审批回滚共用 Git 恢复能力。

### Java BFF

- `RunController` / `RunService` 增加三个代理：
  `POST /runs/{runId}/approvals/{approvalId}/decision`、
  `GET /runs/{runId}/approvals`、`POST /runs/{runId}/cancel`；
  原样保留 AI Core 的 4xx 状态与 JSON，不重新解释错误语义。

### 前端 platform-web

- `runStream.js`：`approval.required` 加入终态事件集合（暂停后关闭连接，
  决策后用 `afterSeq` 重放）。
- `WorkspacePage`
  - 新增 `targetAnnotationId`：审批 / 取消后的恢复事件流仍写回正确标注；
  - `approval.required` → `approval`；`approval.answered` → 清空；
  - `run.cancel_requested` → `cancelRequested`；
  - `run.succeeded / run.rejected / run.cancelled / run.failed` 统一终态落地；
  - `handleApprovalDecision`：POST 决策 → 从 `lastSeq` 重放；
  - `handleCancelRun`：POST 取消 → 从 `lastSeq` 重放；
  - 审批未决时禁止发送新指令 / 批量提交 / 删除标注；
  - 关闭项目时 `run.cancelled / run.rejected` 同样销毁沙盒。
- `RuntimePanel`
  - 新增 `ApprovalPanel`：风险级别、+/- 统计、构建状态、逐文件可折叠 diff、
    审批备注、`批准并应用` / `拒绝并撤销` / `取消执行`；
  - 执行中与澄清暂停均提供取消入口；
  - 状态标签支持 `APPROVAL · 待审批`、`CANCELLED`、`REJECTED`；
    成功判定兼容旧快照的 `AWAITING_APPROVAL`。
- `CanvasStage` 新增 `blocked`：审批未决时冻结画布标注编辑与发送，
  聊天记录仍可查看。
- `WorkspaceTabs` 新增 `pending` 状态（琥珀色圆点）。

## 验证

- `scripts/approval_cancel_e2e_test.py`（新增）
  - 审批暂停：`approval.required` 带逐文件 Diff；暂停期间无 commit；
  - 批准：HEAD 前进、工作区干净、`approval.answered` + `run.succeeded`；
  - 拒绝：HEAD 保持不变、`run.rejected`；
  - 审批中取消：立即回滚、`run.cancelled`；
  - BFF 审批 / 取消代理与审批列表全部通过。
- `scripts/approval_ui_e2e_test.py`（新增，Playwright）
  - 批准后预览变蓝；
  - 审批暂停时画布输入框禁用；
  - 取消 / 拒绝后状态分别为「已取消」「已拒绝」，预览保持上一轮已批准结果。
- 回归：`scripts/agent_e2e_test.py`、`scripts/platform_e2e_test.py` 通过
  （默认 `approvalMode=off`，成功状态由 `AWAITING_APPROVAL` 变为 `SUCCEEDED`，
  旧脚本已改为两者均接受）。
- `services/orchestrator` 全量 `black .`；`python -m compileall`、`mvn compile`、
  `vite build` 通过。

## 已知限制与后续

1. 审批记录仍在进程内 `ACTIVE_RUNS`；服务重启后待审批 Run 丢失，
   下一步接 Redis checkpointer + 审批表持久化。
2. 过期审批目前只在决策 / 快照访问时惰性判定，缺少后台清理任务；
   需要定时扫描并发送 `approval.expired`。
3. 先支持整单批准 / 拒绝；逐文件部分批准需要引入 apply 计划。
4. 取消为协作式：模型调用与 `npm run build` 不可抢占，需等待当前节点返回。
