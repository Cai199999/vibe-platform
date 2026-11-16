# 2026-11-16 FailureRouter 与 HITL 安全路径扩展

## FailureRouter

- 新增 `ai_core/direct_ops/failure_router.py`：
  - Button / Input / Text / Card → `DIRECT_TEMPLATE`，严禁进入 LLM；
  - 其它组件 → `LLM_CODER`。
- Direct Ops ADD 入口先过 Router：
  - 白名单继续 AST 模板插入；
  - 目录外组件返回 `DIRECT_REQUIRES_LLM`（422），不再落到通用校验错误。
- 删除了 Domain 层对 componentType 的白名单硬拒绝，改为 Router 决策，
  避免“入口先失败、根本没机会分流”。

## HITL 扩展

- DecisionAction 新增：
  - `retry`
  - `visual_placeholder`
- `_build_pending_decision_payload` 保证选项永不为空：
  - 始终包含重试（最多 2 次）与取消；
  - 新增组件 / 公共组件修改额外提供“降级为视觉占位”。
- `decision_gate` 新增处理：
  - RETRY：`decision_retry_count + 1`，回到 PLANNING 只重跑确定性
    Resolver，不调用 LLM；达到 2 次后不再下发重试。
  - VISUAL_ONLY：返回 `status=REJECTED`、`visual_only=True`、
    `error_code=VISUAL_ONLY`，Service 丢弃草稿，不 commit。
- `ExecuteRunResponse` 新增 `visual_only` / `committed`。
- `run.rejected` 事件新增 `visualOnly` / `committed`。
- ChangeLogEntry 新增 `applied` / `visual_only`；视觉占位写入
  `applied=false, visual_only=true, actor=AI_RUN` 的记录。
- 视觉占位助手文案明确“代码未写入、不会 commit”。

## 测试

- 新增 `tests/test_failure_router.py`：
  - 四个白名单组件全部 DIRECT；
  - 未知组件 LLM；
  - HITL 选项非空且包含 retry/cancel；
  - 新增组件包含 visual；
  - retry 达到上限后消失；
  - visual-only ChangeLog 为 applied=false；
  - response committed=false。
- 更新既有 decision 测试接受 retry 选项。
- 全量 Python 606 项通过。
- 真实服务验证：`ADD_COMPONENT LoginForm` 在沙盒不存在时仍优先返回
  HTTP 422 `DIRECT_REQUIRES_LLM`，证明路由发生在沙盒 / 锚点访问之前。

## 未完成

- 前端 visualDrafts 持久化与幽灵节点恢复（任务三）尚未接入。
- RETRY 目前只重跑 Resolver / 确定性步骤；完整的 Coder 失败重试上下文
  复用留给 RecoveryOrchestrator 下一阶段。
