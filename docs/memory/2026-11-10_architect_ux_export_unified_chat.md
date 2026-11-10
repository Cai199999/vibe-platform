# 2026-11-10 · Architect 面板收尾：统一聊天流与一键导出

## 目标

Architect 面板（全栈生成向导）最后两项体验收尾：

1. 全栈生成成功且一致性检查通过后，在面板内直接导出完整代码包；
2. 移除“架构确认 / 随意提问”双 Tab，改为统一聊天流；
   正式推进保留正文里的“追加修改需求…”表单，底部只保留只读助手输入。

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

### 2. 统一聊天流 + 正式/只读分离

- 删除 `tab` 状态与 Tab 按钮；主状态机 `messages` 与 `helper.messages`
  在展示层按 `createdAt` 合并为一条时间轴（`flowItems`），不写回主状态。
- 架构师正式提问（assistant 消息）加 `is-formal`：
  左侧蓝色描边、Bot 图标，并提示使用正文里的澄清表单 / 契约卡片。
- 正式推进入口只保留正文里的“追加修改需求…”（`ContractAppend`）：
  提交后调用 `onDecide('REVISE', text)`，重新生成契约。
- 曾有“架构补充栏”小输入框，因与追加需求重复且造成三个输入区，已彻底移除；
  同时删除“◇ 影子助手 · 只读问答”标题行。
- 底部 `ArchitectInputDock` 只保留唯一的影子助手输入：
  只调用 `onAskHelper` → `/architect/runs/{runId}/helper`，
  placeholder “有什么不懂的随时问我...”，只读 RAG。
- 因此“随意提问”与外层 `answer/decide/start` 完全隔离：只增加问答气泡，
  不会回答澄清、修订契约或改变架构状态。

## 验证

- `vite build` 通过（78 modules）。
- `scripts/architect_ux_e2e_test.py`，Playwright 注入工作区与
  Architect 快照：
  1. 无 Tab；统一流中 architect/helper 消息同屏，正式提问
     `data-formal-question=true`；
  2. AWAITING_CLARIFICATION 下不存在 `data-architect-input`，也不存在
     “架构补充栏”“影子助手 · 只读问答”标题；
  3. CONFIRMED 下同时存在正文“追加修改需求…”与底部助手输入；
     助手提问只命中 helper，决策调用数为 0；
     追加需求命中 decision（REVISE + 用户原文）；
  4. 成功快照下导出按钮可见，点击触发 `/sandboxes/{id}/export` 并下载
     `project-export.tar.gz`。
- E2E 结果：`ARCHITECT_UX_E2E_OK`。
- 脚本已通过 `black`。

## 注意

- 旧 Tab CSS 暂留作历史兼容，但面板不再渲染 Tab。
- 若未来接入正式鉴权，下载请求仍需带 `Authorization` 头，已在
  `exportProject` 内保留。
