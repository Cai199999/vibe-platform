# 2026-11-04 · M1 单 Agent LangGraph 闭环

## 目标

打通「标注 -> 计划 -> 生成代码 -> 写入沙盒 -> 构建验证 -> 失败自动修复 ->
等待审批」的后台闭环，暂不接前端画布。

## 已实现

- `ai_core/agent/state.py`：AgentState
  - `annotation_context`、`messages`、`current_code`、`generated_patch`、
    `verify_result`、`retry_count`（硬上限 3）。
- `ai_core/agent/graph.py`：
  - `planner -> coder -> applier -> verifier -> evaluator`
  - 失败回到 `coder`，预算耗尽进入 `failed`，成功进入 `awaiting_approval`。
  - coder / applier 异常同样进入 evaluator，复用同一重试预算。
- `ai_core/agent/llm.py`：
  - `MockAgentLLM`：fail_first / always_fail / none 三种确定性场景；
  - `DeepSeekAgentLLM`：OpenAI 兼容协议，要求模型输出严格 JSON（diff 或 files）。
- `POST /api/v1/internal/runs/execute`：
  - 接收 camelCase `AnnotationEnvelope`；
  - 创建沙盒、读取 current_code、同步执行 Graph；
  - 返回状态、补丁、构建结果、重试次数、节点消息。
- `SandboxProvider.read_file`：applier 应用后回读文件，保证 current_code 与沙盒一致。

## 关键设计决策

1. **Applier 直接复用 SandboxProvider**，不通过内部 HTTP 自调用 apply-patch，
   避免一次无意义的网络回环；两者是同一实现。
2. **files 与 diff 二选一应用**。files 模式下生成的展示 diff 只用于审计，
   不能再打一次补丁，否则必然 `patch does not apply`。
3. **重试语义**：`retry_count` 表示已执行的修复次数，只有失败且
   `retry_count < max_retries` 时才 +1 并回到 coder；3 次修复后第 4 次失败
   直接 FAILED。
4. **Mock fail_first**：首次输出全量错误文件，第二次输出 unified diff 修复，
   一次 E2E 同时覆盖 files 与 diff 两种补丁模式。
5. **M1 边界**：FULLSTACK 意图在 service 层直接拒绝
   `422 FULLSTACK_NOT_SUPPORTED`，不静默降级。
6. **同步执行**：M1 保持同步便于联调；M2 改为 checkpoint + 202 + 事件流。

## 实测结果

- fail_first：首次 esbuild 报错 `Unexpected ";"` -> 自动修复 1 次 ->
  `AWAITING_APPROVAL`，最终补丁为 unified diff，预览模块已包含红色样式。
- always_fail：`retryCount=3` -> `FAILED`，错误「连续 3 次修复后构建仍失败」。
- FULLSTACK：`422 FULLSTACK_NOT_SUPPORTED`。
- 沙盒 E2E 与 Java -> Python 健康检查回归通过。

## 下一步

- 接收前端画布真实 AnnotationEnvelope，接入 Run 状态与事件流。
- DeepSeek 实模联调（当前无密钥，Mock 路径已覆盖）。
- 审批接口：批准后合并快照 / 拒绝后回滚。
