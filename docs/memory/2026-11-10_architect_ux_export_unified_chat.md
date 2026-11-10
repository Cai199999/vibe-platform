# 2026-11-10 · Architect 面板收尾：统一聊天流与一键导出

## 目标

Architect 面板（全栈生成向导）最后两项体验收尾：

1. 全栈生成成功且一致性检查通过后，在面板内直接导出完整代码包；
2. 移除“架构确认 / 随意提问”双 Tab，改为统一聊天流 + 双输入框，
   并保证随意提问绝不会推进架构状态机。

## 实现

### 1. 成功卡片一键导出

- `FullstackGeneration` 在 `status === 'SUCCEEDED'` 且
  `consistency.ok !== false` 时渲染 `architect-export-card`。
- 按钮“📥 导出完整代码”直接调用 WorkspacePage 的导出逻辑，
  目标沙盒取 `fullstack.sandboxId || result.sandboxId`；无需回到顶部工具栏。
- 新增 `exportProject(projectId, target)`：
  - Architect 显式传 sandboxId 时直接走
    `GET /api/v1/sandboxes/{sandboxId}/export`，不依赖 BFF 内存中的
    runId → sandboxId 映射；
  - 顶部工具栏原有 `handleExport` 保持 `/runs/{runId}/export?sandboxId=...`
    的兼容路径。
- 下载仍用 Blob + `<a download>`，文件名 `project-export.tar.gz`。

### 2. 统一聊天流 + 双输入

- 删除 `tab` 状态与 Tab 按钮；主状态机 `messages` 与 `helper.messages`
  在展示层按 `createdAt` 合并为一条时间轴（`flowItems`），不写回主状态。
- 架构师正式提问（assistant 消息）加 `is-formal`：
  左侧蓝色描边、Bot 图标、提示“请在下方的「架构补充栏」回答”。
- 底部常驻 `ArchitectInputDock`，两个输入框各自独立 state：
  - 大框：只调用 `onAskHelper` → `/architect/runs/{runId}/helper`，
    placeholder “有什么不懂的随时问我...”，只读 RAG；
  - 小框：只调用 `submitArchitect`，按状态路由：
    - `AWAITING_CLARIFICATION` → `onAnswer([], text)`（comment-only）；
    - `AWAITING_CONTRACT_CONFIRMATION` / `CONFIRMED` → `onDecide('REVISE', text)`；
    - `IDLE` / `FAILED` / `CANCELLED` → `onStart(text)`；
    - 其它运行态禁用。
- 因此“随意提问”路径与外层 `answer/decide/start` 完全隔离，不会出现
  问答导致澄清被回答、契约被修订或状态乱跳。

## 验证

- `vite build` 通过（78 modules）。
- 新增 `scripts/architect_ux_e2e_test.py`，Playwright 注入工作区与
  Architect 快照：
  1. 无 Tab；统一流中 architect/helper 消息同屏，正式提问
     `data-formal-question=true`；
  2. 大框提问只命中 helper 接口，answers 调用数为 0，状态仍为“等待回答”；
  3. 小框提交命中 answers 接口，body 为 `answers: []` + comment；
  4. 成功快照下导出按钮可见，点击触发 `/sandboxes/{id}/export` 并下载
     `project-export.tar.gz`。
- E2E 结果：`ARCHITECT_UX_E2E_OK`。
- 脚本已通过 `black`。

## 注意

- `docs` 中的 Tab CSS 暂留作历史兼容，但面板不再渲染 Tab。
- 若未来接入正式鉴权，下载请求仍需带 `Authorization` 头，已在
  `exportProject` 内保留。
